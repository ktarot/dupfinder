"""파일 해시(BLAKE3)와 '진행 없음' 기준 제한 시간 실행."""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

import blake3

from .winutil import long_path, open_error_code

CHUNK = 1 << 20          # 1MB씩 읽는다: 취소·진행 확인 간격
PARTIAL_BYTES = 64 * 1024  # R2-2 앞부분 해시 크기


class Cancelled(Exception):
    pass


class Changed(Exception):
    """읽는 동안 크기나 수정 시각이 바뀜(R5-C3)."""


class IdleTimeout(Exception):
    """정해진 시간 동안 진행이 없음(R5-C1)."""


def hash_file(path: str, size: int | None, mtime_ns: int | None, limit: int | None = None,
              cancel: threading.Event | None = None,
              touch: Callable[[int], None] | None = None) -> tuple[tuple[int, int], bytes]:
    """(파일 ID, 해시)를 돌려준다. limit가 있으면 앞부분 limit 바이트만 해시한다."""
    lp = long_path(path)
    st = os.stat(lp)
    if size is not None and (st.st_size != size or st.st_mtime_ns != mtime_ns):
        raise Changed()
    h = blake3.blake3()
    try:
        f = open(lp, "rb", buffering=0)
    except PermissionError as ex:
        code = getattr(ex, "winerror", None) or open_error_code(path)
        if code:
            raise OSError(13, ex.strerror, path, code) from None
        raise
    with f:
        fst = os.fstat(f.fileno())
        fid = (fst.st_dev, fst.st_ino)
        remaining = st.st_size if limit is None else min(limit, st.st_size)
        while remaining > 0:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            b = f.read(min(CHUNK, remaining))
            if not b:
                raise Changed()
            h.update(b)
            remaining -= len(b)
            if touch:
                touch(len(b))
    st2 = os.stat(lp)
    if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
        raise Changed()
    return fid, h.digest()


def run_watched(fn: Callable, idle_timeout: float, cancel: threading.Event | None, *args, **kwargs):
    """fn(touch=...)을 별도 스레드에서 실행한다.

    fn이 touch를 부르지 않은 채 idle_timeout이 지나면 IdleTimeout을 낸다.
    멈춘 스레드는 데몬이라 프로그램 종료를 막지 않는다.
    """
    box: dict = {}
    done = threading.Event()
    last = [time.monotonic()]

    def touch(_n: int = 0) -> None:
        last[0] = time.monotonic()

    def runner() -> None:
        try:
            box["r"] = fn(*args, touch=touch, **kwargs)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True).start()
    while not done.wait(0.1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if time.monotonic() - last[0] > idle_timeout:
            raise IdleTimeout()
    if "e" in box:
        raise box["e"]
    return box["r"]


def call_with_timeout(fn: Callable, timeout: float, default=None):
    """짧은 점검용: timeout 안에 끝나지 않으면 default."""
    box: dict = {}
    done = threading.Event()

    def runner() -> None:
        try:
            box["r"] = fn()
        except BaseException:  # noqa: BLE001
            box["r"] = default
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True).start()
    if not done.wait(timeout):
        return default
    return box.get("r", default)
