"""사용자 관리: 원본 자동 선택(R4-1), 휴지통·격리 이동(R4-3), 안전장치(R4-4~6, R5-H)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass

import blake3

from .config import Limits, is_under
from .hashing import CHUNK, Cancelled, IdleTimeout, hash_file, run_watched
from .models import DupGroup, FileEntry
from .store import IgnoreList
from .winutil import can_recycle, long_path, recycle

TRASH, QUARANTINE = "휴지통", "격리"


class ActionError(Exception):
    pass


@dataclass
class ActionResult:
    path: str
    ok: bool
    action: str
    detail: str
    dest: str = ""


def auto_select(groups: list[DupGroup], preferred_dirs: list[str], mode: str = "all") -> set[str]:
    """처리 대상(남기지 않을 파일) 경로 집합. 묶음마다 원본 1개(비교 모드는 기준 파일 전부)를 남긴다."""
    selected: set[str] = set()
    for g in groups:
        if mode == "compare":
            selected.update(e.path for e in g.files if not e.is_base)
            continue
        keeper = min(g.files, key=lambda e: (
            not any(is_under(e.path, d) for d in preferred_dirs),  # ① 우선 폴더
            e.mtime_ns,                                            # ② 가장 오래된 파일
            len(e.path), e.path.lower()))
        selected.update(e.path for e in g.files if e is not keeper)
    return selected


def plan(groups: list[DupGroup], selected: set[str]):
    """(묶음, 처리할 파일, 남길 파일) 목록과 문제 목록."""
    items, problems = [], []
    for g in groups:
        remove = [e for e in g.files if e.path in selected and not e.is_base]
        keep = [e for e in g.files if e not in remove]
        if not remove:
            continue
        if not keep:  # R4-4
            problems.append(f"묶음의 모든 파일이 선택되어 건너뜀: {g.files[0].path}")
            continue
        items.append((g, remove, keep))
    return items, problems


def summarize(items) -> tuple[int, int]:
    n = sum(len(r) for _, r, _ in items)
    b = sum(e.size for _, r, _ in items for e in r)
    return n, b


def _same_volume(a: str, b: str) -> bool:
    try:
        return os.stat(long_path(a)).st_dev == os.stat(long_path(b)).st_dev
    except OSError:
        return False


def quarantine_dest(qroot: str, session: str, src: str) -> str:
    drive, rest = os.path.splitdrive(os.path.abspath(src))
    if drive.startswith("\\\\"):
        dpart = "UNC_" + drive.strip("\\").replace("\\", "_")
    else:
        dpart = drive.rstrip(":") or "_"
    dest = os.path.join(qroot, session, dpart, rest.lstrip("\\/"))
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.lexists(long_path(dest)) or os.path.lexists(long_path(dest + ".dfpartial")):
        dest = f"{base} ({i}){ext}"
        i += 1
    return dest


def cleanup_partials(qroot: str) -> int:
    """R5-G5: 중간에 남은 격리 복사본(.dfpartial)을 지운다."""
    n = 0
    if not os.path.isdir(qroot):
        return 0
    for dp, _dn, fn in os.walk(qroot):
        for f in fn:
            if f.endswith(".dfpartial"):
                try:
                    os.remove(long_path(os.path.join(dp, f)))
                    n += 1
                except OSError:
                    pass
    return n


class ActionRunner:
    def __init__(self, app_dir: str, limits: Limits | None = None, on_progress=None):
        self.app_dir = app_dir
        self.L = limits or Limits()
        self.on_progress = on_progress or (lambda done, total, path: None)
        self.cancel_event = threading.Event()
        self.session = time.strftime("%Y%m%d-%H%M%S")
        self.log_path = os.path.join(app_dir, "logs", f"actions-{time.strftime('%Y%m%d')}.jsonl")

    def cancel(self):
        self.cancel_event.set()

    # ---- 공개 동작

    def ignore(self, groups: list[DupGroup]) -> list[ActionResult]:
        IgnoreList(self.app_dir).add(g.hash_hex for g in groups)
        out = [ActionResult(g.files[0].path, True, "무시", f"묶음 {len(g.files)}개 파일을 무시 목록에 추가")
               for g in groups]
        for r in out:
            self._write_log(r)
        return out

    def preflight(self, items, action: str, qroot: str) -> None:
        """R5-H5: 격리 폴더 공간이 모자라면 시작하지 않는다."""
        if action != QUARANTINE:
            return
        os.makedirs(long_path(qroot), exist_ok=True)
        need = sum(e.size for _, r, _ in items for e in r if not _same_volume(e.path, qroot))
        free = shutil.disk_usage(qroot).free
        if need and free - need < self.L.min_free_bytes:
            raise ActionError(f"격리 폴더 드라이브 공간이 부족합니다(필요 {need / 1024**3:.2f}GB + 여유 1GB,"
                              f" 남은 공간 {free / 1024**3:.2f}GB)")

    def run(self, items, action: str, qroot: str = "") -> list[ActionResult]:
        self.preflight(items, action, qroot)
        results: list[ActionResult] = []
        total = sum(len(r) for _, r, _ in items)
        done = 0
        for g, remove, keep in items:
            if self.cancel_event.is_set():
                break
            keeper = self._valid_keeper(g, keep)  # R5-H1
            if keeper is None:
                for e in remove:
                    results.append(self._log(ActionResult(e.path, False, action, "남길 파일이 바뀌었거나 없어 묶음을 건너뜀")))
                done += len(remove)
                continue
            for e in remove:
                if self.cancel_event.is_set():
                    break
                self.on_progress(done, total, e.path)
                results.append(self._log(self._one(g, e, keeper, action, qroot)))
                done += 1
        self.on_progress(done, total, "")
        return results

    # ---- 내부

    def _matches(self, g: DupGroup, e: FileEntry) -> str | None:
        """검사 당시와 같으면 None, 다르면 사유."""
        try:
            _fid, digest = run_watched(hash_file, self.L.read_idle_timeout, self.cancel_event,
                                       e.path, e.size, e.mtime_ns, None, self.cancel_event)
        except Cancelled:
            return "취소됨"
        except IdleTimeout:
            return "응답 없음"
        except FileNotFoundError:
            return "파일이 없음"
        except OSError as ex:
            if getattr(ex, "winerror", None) in (32, 33):
                return "다른 프로그램이 사용 중"
            return f"확인 실패: {ex.strerror or ex}"
        except Exception:  # Changed 포함
            return "검사 이후 변경됨"
        return None if digest.hex() == g.hash_hex else "검사 이후 내용이 바뀜"

    def _valid_keeper(self, g, keep) -> FileEntry | None:
        for k in keep:
            if self._matches(g, k) is None:
                return k
        return None

    def _keeper_still_there(self, k: FileEntry) -> bool:
        """R5-H4: 실행 중에도 남길 파일이 그대로 있는지 다시 확인."""
        try:
            st = os.stat(long_path(k.path))
            return st.st_size == k.size and st.st_mtime_ns == k.mtime_ns
        except OSError:
            return False

    def _one(self, g, e: FileEntry, keeper: FileEntry, action, qroot) -> ActionResult:
        if e.is_base:
            return ActionResult(e.path, False, action, "기준 폴더 파일은 처리하지 않음")
        why = self._matches(g, e)
        if why:
            return ActionResult(e.path, False, action, f"건너뜀: {why}")
        if not self._keeper_still_there(keeper):
            return ActionResult(e.path, False, action, "남길 파일이 사라지거나 바뀌어 건너뜀")
        try:
            if action == TRASH:
                recycle(e.path)
                return ActionResult(e.path, True, action, f"남긴 파일: {keeper.path}")
            dest = quarantine_dest(qroot, self.session, e.path)
            self._move(e, dest, g.hash_hex)
            return ActionResult(e.path, True, action, f"남긴 파일: {keeper.path}", dest)
        except Cancelled:
            return ActionResult(e.path, False, action, "취소됨(원래 위치 유지)")
        except Exception as ex:  # noqa: BLE001
            return ActionResult(e.path, False, action, f"실패(원래 위치 유지): {ex}")

    def _move(self, e: FileEntry, dest: str, hash_hex: str) -> None:
        """R5-H3: 옮기다 만 파일을 남기지 않는다."""
        os.makedirs(long_path(os.path.dirname(dest)), exist_ok=True)
        if _same_volume(e.path, os.path.dirname(dest)):
            os.rename(long_path(e.path), long_path(dest))  # 같은 드라이브: 한 번에 이동
            return
        partial = dest + ".dfpartial"
        try:  # 다른 드라이브: 복사 → 검증 → 원본 삭제
            run_watched(self._copy_verify, self.L.read_idle_timeout, self.cancel_event, e.path, partial, hash_hex)
            os.replace(long_path(partial), long_path(dest))
        except BaseException:
            _silent_remove(partial)
            raise
        try:
            os.remove(long_path(e.path))
        except BaseException:
            _silent_remove(dest)  # 원본을 못 지우면 사본을 지워 원래 상태로
            raise

    def _copy_verify(self, src, dst, hash_hex, touch):
        h = blake3.blake3()
        with open(long_path(src), "rb", buffering=0) as fi, open(long_path(dst), "wb") as fo:
            while True:
                if self.cancel_event.is_set():
                    raise Cancelled()
                b = fi.read(CHUNK)
                if not b:
                    break
                h.update(b)
                fo.write(b)
                touch(len(b))
            fo.flush()
            os.fsync(fo.fileno())
        if h.hexdigest() != hash_hex:
            raise OSError("복사본 검증 실패(내용 불일치)")
        shutil.copystat(long_path(src), long_path(dst))

    def _log(self, r: ActionResult) -> ActionResult:
        self._write_log(r)
        return r

    def _write_log(self, r: ActionResult) -> None:  # R4-6
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "session": self.session, "action": r.action,
               "path": r.path, "dest": r.dest, "ok": r.ok, "detail": r.detail}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _silent_remove(p: str) -> None:
    try:
        os.remove(long_path(p))
    except OSError:
        pass
