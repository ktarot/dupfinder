"""디스크에 남기는 것: 해시 캐시(R2-5), 무시 목록(R4-3), 실행 중 표시(R5-F4)."""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .config import norm


class HashCache:
    """경로·크기·수정일이 그대로면 저장된 해시를 다시 쓴다."""

    def __init__(self, app_dir: str):
        os.makedirs(app_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(app_dir, "cache.db"))
        # 볼륨 번호(dev)는 64비트 부호 없는 값이라 SQLite 정수에 안 들어가므로 문자열로 저장한다
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS h2 (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER,"
            " dev TEXT, ino TEXT, partial BLOB, full BLOB)"
        )
        self._pending: dict[str, tuple] = {}
        self._last_flush = time.monotonic()
        self.hits = 0

    def get(self, path: str, size: int, mtime_ns: int):
        key = os.path.normcase(path)
        row = self._pending.get(key)
        if row is None:
            row = self.db.execute(
                "SELECT path, size, mtime, dev, ino, partial, full FROM h2 WHERE path=?", (key,)
            ).fetchone()
        if row and row[1] == size and row[2] == mtime_ns:
            self.hits += 1
            return (int(row[3]), int(row[4])), row[5], row[6]
        return None

    def put(self, path: str, size: int, mtime_ns: int, fid, partial, full) -> None:
        key = os.path.normcase(path)
        old = self._pending.get(key)
        if full is None and old and old[1] == size and old[2] == mtime_ns:
            full = old[6]
        self._pending[key] = (key, size, mtime_ns, str(fid[0]), str(fid[1]), partial, full)
        if len(self._pending) >= 1000 or time.monotonic() - self._last_flush > 2:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            self.db.executemany("INSERT OR REPLACE INTO h2 VALUES (?,?,?,?,?,?,?)", list(self._pending.values()))
            self.db.commit()
            self._pending.clear()
        self._last_flush = time.monotonic()

    def prune(self, roots, seen_paths) -> int:
        """검사를 끝까지 마친 폴더 아래에서 이번에 보이지 않은 파일(지워졌거나 옮겨진 파일)의 기록을 지운다."""
        self.flush()
        prefixes = [norm(r) for r in roots]
        seen = {os.path.normcase(p) for p in seen_paths}
        gone = [(p,) for (p,) in self.db.execute("SELECT path FROM h2")
                if p not in seen and any(p == q or p.startswith(q + os.sep) for q in prefixes)]
        if gone:
            self.db.executemany("DELETE FROM h2 WHERE path=?", gone)
            self.db.commit()
        return len(gone)

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.db.close()


def clear_records(app_dir: str, include_ignore: bool = False) -> tuple[list[str], list[str]]:
    """해시 캐시와 작업 기록(필요하면 무시 목록까지)을 지운다. (지운 것, 못 지운 것)을 돌려준다."""
    targets = [os.path.join(app_dir, n) for n in ("cache.db", "cache.db-journal")]
    logs = os.path.join(app_dir, "logs")
    if os.path.isdir(logs):
        targets += [os.path.join(logs, n) for n in os.listdir(logs)]
    if include_ignore:
        targets.append(os.path.join(app_dir, "ignore.json"))
    removed, failed = [], []
    for p in targets:
        if not os.path.isfile(p):
            continue
        try:
            os.remove(p)
            removed.append(p)
        except OSError as e:
            failed.append(f"{p} ({e.strerror or e})")
    try:
        os.rmdir(logs)
    except OSError:
        pass
    return removed, failed


class IgnoreList:
    """내용 해시 단위로 묶음을 무시한다."""

    def __init__(self, app_dir: str):
        self.path = os.path.join(app_dir, "ignore.json")
        self.hashes: set[str] = set()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.hashes = set(json.load(f).get("hashes", []))
        except (OSError, ValueError):
            pass

    def add(self, hashes) -> None:
        self.hashes.update(hashes)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"hashes": sorted(self.hashes)}, f)
        os.replace(tmp, self.path)


class RunMarker:
    """검사 중에만 존재하는 파일. 다음 실행 때 남아 있으면 비정상 종료였다는 뜻."""

    def __init__(self, app_dir: str):
        self.path = os.path.join(app_dir, "running.json")

    def start(self, info: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)

    def finish(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass

    def leftover(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
