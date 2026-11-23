"""검사 엔진: 파일 찾기 → 크기 비교 → 앞부분 해시 → 전체 해시 → 묶음 작성.

막힐 수 있는 작업(폴더 목록 읽기, 파일 읽기)은 모두 작업자 스레드에서 돌리고,
조정 루프는 절대 막히지 않게 해서 취소·한도·정지 조건을 항상 확인한다.
"""
from __future__ import annotations

import heapq
import itertools
import os
import queue
import re
import shutil
import stat
import threading
import time
import traceback
from collections import defaultdict, deque

from .config import ScanConfig, is_under, norm
from .hashing import PARTIAL_BYTES, Cancelled, Changed, hash_file
from .models import DupGroup, FileEntry, Issue, Reason, ScanResult, State
from .store import HashCache, IgnoreList, RunMarker
from .winutil import (
    CLOUD_NOT_LOCAL, DRIVE_REMOTE, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, LINK_TAGS,
    drive_root, drive_type, incurs_seek_penalty, is_unc, long_path, memory_usage, prevent_sleep,
)

PHASE_WALK, PHASE_SIZE, PHASE_PARTIAL, PHASE_FULL, PHASE_GROUP = (
    "파일 찾기", "크기 비교", "앞부분 비교", "전체 비교", "목록 작성")


class StopScan(Exception):
    def __init__(self, reason: Reason, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


class AskRequest:
    """사용자에게 묻기(Ⓔ). 정해진 시간 안에 답이 없으면 기본 동작."""

    def __init__(self, question: str, options: list[str], default: str, timeout: float):
        self.question, self.options, self.default, self.timeout = question, options, default, timeout
        self.choice: str | None = None
        self._ev = threading.Event()

    def reply(self, choice: str) -> None:
        if not self._ev.is_set():
            self.choice = choice
            self._ev.set()


# ---------------------------------------------------------------- 작업자

class Task:
    __slots__ = ("kind", "fn", "args", "ctx", "root", "started", "last", "finished", "abandoned",
                 "result", "exc", "worker", "timeouts", "locks", "_eng")

    def __init__(self, eng, kind, fn, args, ctx, root):
        self._eng, self.kind, self.fn, self.args, self.ctx, self.root = eng, kind, fn, args, ctx, root
        self.started = self.last = None
        self.finished = self.abandoned = False
        self.result = self.exc = self.worker = None
        self.timeouts = self.locks = 0

    def touch(self, n: int = 0) -> None:
        self.last = time.monotonic()
        if n:
            self._eng._add_bytes(n)

    def clone(self) -> "Task":
        t = Task(self._eng, self.kind, self.fn, self.args, self.ctx, self.root)
        t.timeouts, t.locks = self.timeouts, self.locks
        return t

    @property
    def path(self) -> str:
        return self.ctx[0] if self.kind == "dir" else self.ctx.path


class _Worker(threading.Thread):
    def __init__(self, pool):
        super().__init__(daemon=True)
        self.pool, self.retired = pool, False

    def run(self):
        while not self.retired:
            t = self.pool.q.get()
            if t is None:
                return
            if t.abandoned:
                continue
            t.worker = self
            t.started = t.last = time.monotonic()
            try:
                t.result = t.fn(*t.args, touch=t.touch)
            except BaseException as e:  # noqa: BLE001
                t.exc = e
            t.finished = True
            self.pool.done.put(t)


class WorkerPool:
    def __init__(self, n: int):
        self.q: queue.Queue = queue.Queue()
        self.done: queue.Queue = queue.Queue()
        self.workers: list[_Worker] = []
        for _ in range(n):
            self._spawn()

    def _spawn(self):
        w = _Worker(self)
        self.workers.append(w)
        w.start()

    def submit(self, t: Task):
        self.q.put(t)

    def replace(self, w: _Worker):
        """멈춘 작업자를 버리고 새 작업자를 세운다."""
        w.retired = True
        if w in self.workers:
            self.workers.remove(w)
            self._spawn()

    def shutdown(self):
        for w in self.workers:
            w.retired = True
        for _ in self.workers:
            self.q.put(None)


def list_dir(path, root_dev, visited, vlock, cancel, max_path_len, touch):
    """폴더 하나를 읽는다(작업자 스레드에서 실행)."""
    dst = os.stat(long_path(path))
    key = (dst.st_dev, dst.st_ino) if dst.st_ino else ("p", norm(path))
    with vlock:  # R5-B1: 실제 위치 기준으로 한 번만 방문
        if key in visited:
            return ("visited",)
        visited.add(key)
    if root_dev is not None and dst.st_dev != root_dev:  # R5-B6
        return ("crossdev",)
    dirs, files, issues = [], [], []
    with os.scandir(long_path(path)) as it:
        for i, e in enumerate(it):
            if (i & 127) == 0:
                touch()
                if cancel.is_set():
                    raise Cancelled()
            full = os.path.join(path, e.name)
            if len(full) > max_path_len:  # R5-B4
                issues.append(Issue(full, "파일", PHASE_WALK, f"경로가 {max_path_len}자를 넘음"))
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as ex:
                issues.append(Issue(full, "파일", PHASE_WALK, f"정보 읽기 실패: {ex.strerror or ex}", True))
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            tag = getattr(st, "st_reparse_tag", 0)
            is_link = (stat.S_ISLNK(st.st_mode) or (attrs & FILE_ATTRIBUTE_REPARSE_POINT and tag in LINK_TAGS)
                       or (hasattr(e, "is_junction") and e.is_junction()))
            if is_link:  # R1-5, R5-B1
                issues.append(Issue(full, "링크", PHASE_WALK, "바로가기(심볼릭 링크·정션)는 따라가지 않음"))
            elif attrs & FILE_ATTRIBUTE_DIRECTORY or stat.S_ISDIR(st.st_mode):
                dirs.append(full)
            elif stat.S_ISREG(st.st_mode):
                files.append((full, st.st_size, st.st_mtime_ns, getattr(st, "st_birthtime", st.st_ctime), attrs))
            else:  # R5-B7
                issues.append(Issue(full, "파일", PHASE_WALK, "일반 파일이 아님(장치·파이프 등)"))
    return ("ok", dst.st_dev, dirs, files, issues)


_DRIVE_SYSTEM_DIRS = {"$recycle.bin", "system volume information"}
_FOUND_DIR = re.compile(r"^found\.\d{3}$", re.IGNORECASE)  # chkdsk 복구 폴더


def is_drive_system_dir(d: str) -> bool:
    """모든 드라이브 최상위의 시스템 폴더(휴지통, 시스템 볼륨 정보, found.000 등)."""
    parent = os.path.dirname(os.path.abspath(d))
    if os.path.normcase(parent.rstrip("\\")) != os.path.normcase(drive_root(d).rstrip("\\")):
        return False
    name = os.path.basename(d)
    return name.lower() in _DRIVE_SYSTEM_DIRS or bool(_FOUND_DIR.match(name))


def classify_error(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, Cancelled):
        return "cancel", ""
    if isinstance(exc, Changed):
        return "skip", "검사 중 변경됨"
    if isinstance(exc, OSError):
        we = getattr(exc, "winerror", None)
        if we in (32, 33):
            return "lock", "다른 프로그램이 사용 중"
        if isinstance(exc, FileNotFoundError) or we in (2, 3):
            return "skip", "검사 중 삭제·이동됨"
        if we == 5 or isinstance(exc, PermissionError):
            return "fail", "접근 거부"
        if we in (21, 53, 59, 64, 67, 1167, 1231):
            return "fail", "장치·네트워크 오류"
        return "fail", f"읽기 오류: {exc.strerror or exc}"
    return "fail", f"처리 오류: {exc!r}"


# ---------------------------------------------------------------- 엔진

class Engine:
    def __init__(self, cfg: ScanConfig, on_event=None, interactive: bool = False):
        cfg.validate()
        self.cfg, self.L = cfg, cfg.limits
        self.on_event = on_event or (lambda kind, data: None)
        self.interactive = interactive
        self.cancel_event = threading.Event()
        self.hash_fn = hash_file   # 테스트에서 바꿔 끼울 수 있게
        self.list_fn = list_dir
        self.log: list[str] = []

    def cancel(self) -> None:
        self.cancel_event.set()

    # ---- 진입점

    def run(self) -> ScanResult:
        self._init_state()
        marker = RunMarker(self.cfg.app_dir)
        marker.start({"started": time.strftime("%Y-%m-%d %H:%M:%S"), "roots": [r for r, _ in self.roots]})
        prevent_sleep(True)
        reason, detail = Reason.COMPLETED, "모든 검사를 마쳤습니다"
        try:
            self.cache = HashCache(self.cfg.app_dir) if self.cfg.use_cache else None
            self._phase_walk()
            self._phase_size()
            self._phase_partial()
            self._phase_full()
        except StopScan as s:
            reason, detail = s.reason, s.detail
        except Exception as e:  # noqa: BLE001
            reason, detail = Reason.ERROR, f"내부 오류: {e!r}"
            self._log(traceback.format_exc())
        finally:
            self.pool.shutdown()
            prevent_sleep(False)
            if self.cache:
                if reason is Reason.COMPLETED:  # 끝까지 본 폴더만 정리(중간에 멈추면 못 본 파일이 있음)
                    try:
                        roots = [r for i, (r, _) in enumerate(self.roots) if i not in self.root_dead]
                        n = self.cache.prune(roots, (e.path for e in self.entries))
                        if n:
                            self._log(f"사라진 파일의 캐시 기록 {n:,}건 정리")
                    except Exception as e:  # noqa: BLE001
                        self._log(f"캐시 정리 실패: {e!r}")
                try:
                    self.cache.close()
                except Exception as e:  # noqa: BLE001  캐시 저장 실패는 검사 결과에 영향 없음
                    self._log(f"캐시 저장 실패: {e!r}")
            marker.finish()
        result = self._finalize(reason, detail)
        self.on_event("done", result)
        return result

    # ---- 상태

    def _init_state(self):
        cfg = self.cfg
        self.t0 = time.monotonic()
        self.start_wall = time.time()
        self.roots = cfg.all_roots()
        self.root_net = [is_unc(r) or drive_type(r) == DRIVE_REMOTE for r, _ in self.roots]
        self.root_dev: dict[int, int] = {}
        self.root_dead: dict[int, str] = {}
        self.root_fail: dict[int, int] = defaultdict(int)
        self.entries: list[FileEntry] = []
        self.issues: list[Issue] = []
        self.hardlinks: list[tuple[str, str]] = []
        self.pending: deque[Task] = deque()
        self.delayed: list = []
        self.inflight: set[Task] = set()
        self._seq = itertools.count()
        self.task_done = self.task_err = self.consec_err = self.abandoned = 0
        self.phase_total = self.phase_done = 0
        self._bytes = self.bytes_total = 0
        self._block = threading.Lock()
        self.last_progress = time.monotonic()
        self.phase = PHASE_WALK
        self.dirs_done = 0
        self.created_after = 0
        self.asked_max_files = False
        self.excl_paths = [norm(p) for p in cfg.exclude_paths if p.strip()]
        self.excl_names = {n.strip().lower() for n in cfg.exclude_dir_names if n.strip()}
        self.own = [norm(p) for p in cfg.own_dirs()]
        self.visited: set = set()
        self.vlock = threading.Lock()
        self._next_monitor = self._next_root_check = self._next_progress = 0.0
        self._mem_over_since = None
        self.target_workers = cfg.workers or self._choose_workers()
        self.pool = WorkerPool(self.target_workers)
        self.cache = None

    def _choose_workers(self) -> int:
        """R2-4: SSD는 CPU 코어 수, HDD는 2개, 네트워크는 4개."""
        hdd = any(incurs_seek_penalty(r) for r, _ in self.roots if not is_unc(r))
        if hdd:
            return 2
        if any(self.root_net):
            return 4
        return max(2, min(os.cpu_count() or 4, 16))

    def _add_bytes(self, n: int):
        with self._block:
            self._bytes += n
        self.last_progress = time.monotonic()

    def _log(self, msg: str):
        self.log.append(msg)
        self.on_event("log", msg)

    def _issue(self, path, kind, reason, failed=False):
        self.issues.append(Issue(path, kind, self.phase, reason, failed))

    def _skip(self, e: FileEntry, reason: str):
        if e.state is None:
            e.state = State.SKIPPED
            self._issue(e.path, "파일", reason)

    def _fail(self, e: FileEntry, reason: str):
        if e.state is None:
            e.state = State.FAILED
            self._issue(e.path, "파일", reason, True)

    def _groupable(self, lst) -> bool:
        if len(lst) < 2:
            return False
        if self.cfg.mode == "compare":  # 기준 폴더 파일과 대상 폴더 파일이 함께 있어야 함
            return any(e.is_base for e in lst) and any(not e.is_base for e in lst)
        return True

    def _regroup(self, entries, keyfn) -> list[list[FileEntry]]:
        """묶을 수 없는 파일은 '처리'로 확정하고, 묶음 목록을 돌려준다."""
        m = defaultdict(list)
        for e in entries:
            if e.state is None:
                m[keyfn(e)].append(e)
        out = []
        for lst in m.values():
            if self._groupable(lst):
                out.append(lst)
            else:
                for e in lst:
                    e.state = State.PROCESSED
        return out

    # ---- 단계

    def _phase_walk(self):
        self.phase = PHASE_WALK
        for idx, (root, _base) in enumerate(self.roots):
            if any(is_under(root, o) for o in self.own):
                self._issue(root, "폴더", "프로그램이 만든 폴더라 검사하지 않음")
                continue
            self._add_dir(root, 0, idx)
        self._drain(self._on_dir)

    def _add_dir(self, path, depth, idx):
        args = (path, self.root_dev.get(idx), self.visited, self.vlock, self.cancel_event, self.L.max_path_len)
        self.pending.append(Task(self, "dir", self.list_fn, args, (path, depth), idx))

    def _dir_excluded(self, d: str) -> bool:
        if os.path.basename(d).lower() in self.excl_names or is_drive_system_dir(d):
            return True
        nd = norm(d)
        for p in self.own + self.excl_paths:
            if nd == p or nd.startswith(p + os.sep):
                return True
        return False

    def _on_dir(self, t: Task, res):
        path, depth = t.ctx
        if res[0] == "visited":
            self._issue(path, "폴더", "이미 검사한 폴더(순환 또는 겹친 지정)")
            return
        if res[0] == "crossdev":
            self._issue(path, "폴더", "다른 드라이브로 넘어가지 않음")
            return
        _, dev, dirs, files, sub_issues = res
        if depth == 0:
            self.root_dev[t.root] = dev
        self.dirs_done += 1
        self.issues.extend(sub_issues)
        L, cfg = self.L, self.cfg
        is_base = self.roots[t.root][1]
        for d in dirs:
            if self._dir_excluded(d):
                continue
            if depth + 1 > L.max_depth:  # R5-B2
                self._issue(d, "폴더", f"폴더 깊이 {L.max_depth}단계를 넘어 검사하지 않음")
                continue
            self._add_dir(d, depth + 1, t.root)
        for fp, size, mtime, birth, attrs in files:
            if size < cfg.min_size or (cfg.max_size is not None and size > cfg.max_size):
                continue
            if birth > self.start_wall:  # R5-B5
                self.created_after += 1
                continue
            e = FileEntry(fp, size, mtime, birth, t.root, is_base)
            self.entries.append(e)
            if attrs & CLOUD_NOT_LOCAL:  # R5-C6
                self._skip(e, "클라우드 전용 파일(다운로드하지 않음)")
        if len(self.entries) > L.max_files and not self.asked_max_files:  # R5-B3
            self.asked_max_files = True
            c = self._ask(f"찾은 파일이 {L.max_files:,}개를 넘었습니다. 계속 검사할까요?",
                          ["중단", "계속"], "중단")
            if c != "계속":
                raise StopScan(Reason.LIMIT, f"파일 수 {L.max_files:,}개 초과")

    def _phase_size(self):
        self.phase = PHASE_SIZE
        cands = [e for g in self._regroup(self.entries, lambda e: e.size) for e in g]
        big = [e for e in cands if e.size > self.L.big_file_bytes]
        if big:  # R5-C5
            total = sum(e.size for e in big)
            c = self._ask(f"{self.L.big_file_bytes // 1024**3}GB를 넘는 파일이 {len(big)}개"
                          f"(합계 {total / 1024**3:.1f}GB) 있습니다. 검사할까요?", ["건너뛰기", "검사"], "건너뛰기")
            if c != "검사":
                for e in big:
                    self._skip(e, f"{self.L.big_file_bytes // 1024**3}GB 초과 파일이라 검사하지 않음")
                cands = [e for g in self._regroup(cands, lambda e: e.size) for e in g]
        self.cands = cands

    def _phase_partial(self):
        self.phase = PHASE_PARTIAL
        self._begin_phase(len(self.cands), sum(min(e.size, PARTIAL_BYTES) for e in self.cands))
        for e in self.cands:
            if e.state is not None:
                continue
            hit = self.cache.get(e.path, e.size, e.mtime_ns) if self.cache else None
            if hit and hit[1] is not None:
                e.fid, e.partial = hit[0], hit[1]
                e.full = e.partial if e.size <= PARTIAL_BYTES else hit[2]
                self.phase_done += 1
                self._add_bytes(min(e.size, PARTIAL_BYTES))
                continue
            args = (e.path, e.size, e.mtime_ns, PARTIAL_BYTES, self.cancel_event)
            self.pending.append(Task(self, "partial", self.hash_fn, args, e, e.root))
        self._drain(self._on_hash)
        # 하드링크: 같은 실제 파일이면 중복이 아님(R2-7)
        by_size = defaultdict(list)
        for e in self.cands:
            if e.state is None and e.partial is not None:
                by_size[e.size].append(e)
        for lst in by_size.values():
            seen: dict = {}
            for e in sorted(lst, key=lambda x: not x.is_base):
                if e.fid and e.fid[1]:
                    if e.fid in seen:
                        e.state, e.full, e.partial = State.PROCESSED, None, None
                        self.hardlinks.append((e.path, seen[e.fid].path))
                        continue
                    seen[e.fid] = e
        self.groups_partial = self._regroup(
            [e for e in self.cands if e.partial is not None], lambda e: (e.size, e.partial))

    def _phase_full(self):
        self.phase = PHASE_FULL
        need = [e for g in self.groups_partial for e in g if e.full is None and e.state is None]
        self._begin_phase(len(need), sum(e.size for e in need))
        for e in need:
            args = (e.path, e.size, e.mtime_ns, None, self.cancel_event)
            self.pending.append(Task(self, "full", self.hash_fn, args, e, e.root))
        self._drain(self._on_hash)
        self.phase = PHASE_GROUP
        for g in self.groups_partial:
            for e in g:
                if e.state is None and e.full is not None:
                    e.state = State.PROCESSED

    def _begin_phase(self, n_tasks, n_bytes):
        self.phase_total, self.phase_done = n_tasks, 0
        with self._block:
            self._bytes = 0
        self.bytes_total = n_bytes
        self.last_progress = time.monotonic()

    def _on_hash(self, t: Task, res):
        e: FileEntry = t.ctx
        fid, digest = res
        e.fid = fid
        if t.kind == "partial":
            e.partial = digest
            if e.size <= PARTIAL_BYTES:
                e.full = digest
        else:
            e.full = digest
        if self.cache:
            self.cache.put(e.path, e.size, e.mtime_ns, fid, e.partial, e.full)

    # ---- 조정 루프

    def _drain(self, handler):
        L = self.L
        while self.pending or self.delayed or self.inflight:
            now = time.monotonic()
            self._check_stop(now)
            while self.delayed and self.delayed[0][0] <= now:
                self.pending.append(heapq.heappop(self.delayed)[2])
            while self.pending and len(self.inflight) < self.target_workers:
                t = self.pending.popleft()
                if t.root in self.root_dead:
                    self._drop(t, self.root_dead[t.root])
                    continue
                self.inflight.add(t)
                self.pool.submit(t)
            try:
                t = self.pool.done.get(timeout=0.05)
                while True:
                    if t in self.inflight and not t.abandoned:
                        self.inflight.discard(t)
                        self._complete(t, handler)
                    t = self.pool.done.get_nowait()
            except queue.Empty:
                pass
            now = time.monotonic()
            for t in list(self.inflight):  # R5-C1: 진행 없는 시간 기준
                if t.started is not None and not t.finished and now - t.last > L.read_idle_timeout:
                    self._abandon(t)
            self._monitor(now)
            self._progress(now)

    def _complete(self, t: Task, handler):
        if t.root in self.root_dead:
            self._drop(t, self.root_dead[t.root])
            return
        if t.exc is None:
            self._resolved(t, ok=True)
            handler(t, t.result)
            return
        kind, msg = classify_error(t.exc)
        if kind == "cancel":
            return
        if kind == "lock":  # R5-C2
            delays = self.L.lock_retry_delays
            if t.locks < len(delays):
                c = t.clone()
                c.locks += 1
                heapq.heappush(self.delayed, (time.monotonic() + delays[t.locks], next(self._seq), c))
                return
            kind, msg = "fail", f"{msg}({len(delays)}회 다시 시도 후)"
        self._resolve_bad(t, msg, failed=(kind == "fail"))

    def _resolved(self, t: Task, ok: bool):
        self.task_done += 1
        if t.kind != "dir":
            self.phase_done += 1
        self.last_progress = time.monotonic()
        if ok:
            self.consec_err = 0

    def _resolve_bad(self, t: Task, msg: str, failed: bool):
        self._resolved(t, ok=False)
        if t.kind == "dir":
            self._issue(t.ctx[0], "폴더", msg, failed)
        elif failed:
            self._fail(t.ctx, msg)
        else:
            self._skip(t.ctx, msg)
        # R5-D3: 접근 거부는 권한 문제일 뿐 장치 이상 신호가 아니므로 중단 판단에 넣지 않는다
        if failed and not msg.startswith("접근 거부"):
            self.task_err += 1
            self.consec_err += 1
            L = self.L
            if self.consec_err >= L.max_consecutive_errors:
                raise StopScan(Reason.ERROR, f"오류가 연속 {self.consec_err}번 발생했습니다")
            if self.task_done >= L.error_ratio_min_samples and self.task_err / self.task_done > L.max_error_ratio:
                raise StopScan(Reason.ERROR, f"오류 비율이 {L.max_error_ratio:.0%}를 넘었습니다"
                                             f"({self.task_err}/{self.task_done})")

    def _abandon(self, t: Task, final_reason: str | None = None):
        """응답 없는 작업을 버리고 작업자를 교체한다."""
        L = self.L
        t.abandoned = True
        self.inflight.discard(t)
        self.abandoned += 1
        self.last_progress = time.monotonic()
        if t.worker is not None:
            self.pool.replace(t.worker)
        self._log(f"응답 없음: {t.path}")
        if self.abandoned > L.max_abandoned_tasks:
            raise StopScan(Reason.LIMIT, f"응답 없는 작업이 {L.max_abandoned_tasks}개를 넘었습니다"
                                         "(장치·네트워크 상태를 확인하세요)")
        if final_reason:
            self._resolve_bad(t, final_reason, failed=False)
        elif t.timeouts < L.read_retries:
            c = t.clone()
            c.timeouts += 1
            self.pending.append(c)
        else:
            self._resolve_bad(t, f"응답 없음({L.read_idle_timeout:.0f}초 × {L.read_retries + 1}회)", failed=True)

    def _drop(self, t: Task, reason: str):
        if t.kind == "dir":
            self._issue(t.ctx[0], "폴더", reason)
        else:
            self._skip(t.ctx, reason)

    # ---- 정지 조건 점검

    def _check_stop(self, now):
        if self.cancel_event.is_set():  # R5-F1
            raise StopScan(Reason.CANCELLED, "사용자가 취소했습니다")
        tl = self.L.time_limit
        if tl is not None and now - self.t0 > tl:  # R5-D1
            shown = f"{tl / 60:g}분" if tl >= 60 else f"{tl:g}초"
            raise StopScan(Reason.LIMIT, f"전체 시간 한도({shown})를 넘었습니다")

    def _monitor(self, now):
        L = self.L
        if now >= self._next_monitor:
            self._next_monitor = now + 1.0
            proc, total = memory_usage()  # R5-D4
            if total and proc > L.memory_ratio * total:
                if self._mem_over_since is None or (self.target_workers > 1 and now - self._mem_over_since > 2):
                    if self.target_workers > 1:
                        self.target_workers = max(1, self.target_workers // 2)
                        self._log(f"메모리 사용량이 높아 동시 처리 수를 {self.target_workers}개로 줄였습니다")
                    self._mem_over_since = now
                elif self.target_workers == 1 and now - self._mem_over_since > L.memory_grace_seconds:
                    raise StopScan(Reason.LIMIT, f"메모리 사용량이 시스템의 {L.memory_ratio:.0%}를 넘었습니다")
            else:
                self._mem_over_since = None
            try:  # R5-D5
                if shutil.disk_usage(self.cfg.app_dir).free < L.min_free_bytes:
                    raise StopScan(Reason.LIMIT, "캐시·작업 기록을 저장할 디스크 공간이 1GB 미만입니다")
            except OSError:
                pass
        if now >= self._next_root_check:
            self._check_roots()
            self._next_root_check = time.monotonic() + L.root_check_interval
        busy = self.pending or self.inflight
        if busy and time.monotonic() - self.last_progress > L.stall_seconds:  # R5-D2
            cur = next(iter(self.inflight), None) or (self.pending[0] if self.pending else None)
            where = cur.path if cur else "(알 수 없음)"
            self._log(f"진행 정체: {where} ({self.phase})")
            c = self._ask(f"{L.stall_seconds:.0f}초 동안 진행이 없습니다.\n위치: {where}\n단계: {self.phase}",
                          ["건너뛰기", "계속 기다리기", "전체 중단"], "건너뛰기")
            if c == "전체 중단":
                raise StopScan(Reason.LIMIT, f"진행 정체로 중단했습니다({where})")
            if c == "건너뛰기":
                for t in list(self.inflight):
                    self._abandon(t, final_reason="진행 정체로 건너뜀")
            self.last_progress = time.monotonic()

    def _check_roots(self):
        """R5-E1~E3: 검사 폴더가 아직 있는지 동시에 확인한다(전체 1.5초 이내)."""
        L = self.L
        live = [i for i in range(len(self.roots)) if i not in self.root_dead]
        boxes = {}
        threads = []
        for i in live:
            box = boxes[i] = [False]

            def chk(p=self.roots[i][0], b=box):
                try:
                    b[0] = os.path.isdir(long_path(p))
                except OSError:
                    b[0] = False
            th = threading.Thread(target=chk, daemon=True)
            th.start()
            threads.append(th)
        deadline = time.monotonic() + L.root_check_timeout
        for th in threads:
            th.join(max(0.0, deadline - time.monotonic()))
        for i in live:
            if boxes[i][0]:
                self.root_fail[i] = 0
                continue
            self.root_fail[i] += 1
            need = L.network_retries if self.root_net[i] else 1
            if self.root_fail[i] < need:
                continue
            root = self.roots[i][0]
            if self.root_net[i]:
                why = "네트워크 연결이 끊김"
            elif not os.path.exists(drive_root(root)):
                why = "드라이브가 분리됨"
            else:
                why = "검사 폴더가 삭제·이동됨"
            self.root_dead[i] = why
            self._issue(root, "폴더", why, True)
            self._log(f"{why}: {root}")
        if self.roots and len(self.root_dead) == len(self.roots):
            raise StopScan(Reason.ENVIRONMENT, "모든 검사 폴더에 접근할 수 없습니다")

    def _ask(self, question, options, default) -> str:
        if not self.interactive:
            self._log(f"[자동 응답] {question} → {default}")
            return default
        req = AskRequest(question, options, default, self.L.ask_timeout)
        self.on_event("ask", req)
        deadline = time.monotonic() + self.L.ask_timeout + 1.0
        while not req._ev.wait(0.1):
            if self.cancel_event.is_set():
                req.reply(default)
                raise StopScan(Reason.CANCELLED, "사용자가 취소했습니다")
            if time.monotonic() > deadline:
                req.reply(default)
        self._log(f"[질문] {question} → {req.choice}")
        self.last_progress = time.monotonic()
        return req.choice or default

    def _progress(self, now):
        if now < self._next_progress:
            return
        self._next_progress = now + 0.25
        dirs_pending = sum(1 for t in itertools.chain(self.pending, self.inflight) if t.kind == "dir")
        cur = next(iter(self.inflight), None)
        self.on_event("progress", {
            "phase": self.phase, "found": len(self.entries), "dirs_done": self.dirs_done,
            "dirs_pending": dirs_pending, "tasks_total": self.phase_total, "tasks_done": self.phase_done,
            "bytes_total": self.bytes_total, "bytes_done": min(self._bytes, self.bytes_total),
            "issues": len(self.issues), "elapsed": now - self.t0, "workers": self.target_workers,
            "current": cur.path if cur else "",
        })

    # ---- 마무리

    def _finalize(self, reason: Reason, detail: str) -> ScanResult:
        self.phase = PHASE_GROUP
        m = defaultdict(list)
        for e in self.entries:
            if e.full is not None and e.state in (None, State.PROCESSED):
                m[(e.size, e.full)].append(e)
        try:
            ignore = IgnoreList(self.cfg.app_dir).hashes
        except Exception:  # noqa: BLE001
            ignore = set()
        groups, ignored = [], 0
        for (size, full), lst in m.items():
            if not self._groupable(lst):
                continue
            for e in lst:  # 전체 내용을 비교했으므로 처리 완료
                e.state = State.PROCESSED
            hx = full.hex()
            if hx in ignore:
                ignored += 1
                continue
            lst.sort(key=lambda e: (not e.is_base, e.path.lower()))
            groups.append(DupGroup(size, hx, lst))
        groups.sort(key=lambda g: (-g.reclaimable, -len(g.files)))

        if reason != Reason.COMPLETED:  # R5-G4: 검사하지 못한 것 남기기
            leftover = list(self.pending) + [d[2] for d in self.delayed] + list(self.inflight)
            for t in leftover:
                if t.kind == "dir":
                    self._issue(t.ctx[0], "폴더", "검사하지 못함(중단)")
            for e in self.entries:
                if e.state is None:
                    self._issue(e.path, "파일", "미처리(중단)")

        n = defaultdict(int)
        for e in self.entries:
            n[e.state] += 1
        counts = {
            "found": len(self.entries), "processed": n[State.PROCESSED], "skipped": n[State.SKIPPED],
            "failed": n[State.FAILED], "unprocessed": n[None], "dirs": self.dirs_done,
            "created_after": self.created_after, "hardlinks": len(self.hardlinks),
            "ignored_groups": ignored, "cache_hits": self.cache.hits if self.cache else 0,
            "abandoned": self.abandoned,
        }
        if reason == Reason.COMPLETED and self.root_dead:
            reason, detail = Reason.ENVIRONMENT, "일부 검사 폴더에 접근할 수 없게 되었습니다"
        if reason == Reason.COMPLETED:  # R5-A2
            s = counts["processed"] + counts["skipped"] + counts["failed"]
            if s != counts["found"]:
                reason, detail = Reason.ERROR, f"개수 불일치: 찾은 {counts['found']} ≠ 처리·건너뜀·실패 합계 {s}"
        return ScanResult(groups, self.issues, reason, detail, reason == Reason.COMPLETED, counts,
                          self.hardlinks, time.monotonic() - self.t0, self.cfg.mode)
