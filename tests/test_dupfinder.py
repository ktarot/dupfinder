"""요구사항 명세 v0.1 검증 테스트. 실행: python -m unittest discover -s tests -v"""
from __future__ import annotations

import ctypes
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dupfinder import actions  # noqa: E402
from dupfinder.actions import QUARANTINE, ActionRunner, auto_select, plan  # noqa: E402
from dupfinder.config import Limits, ScanConfig, parse_exts  # noqa: E402
from dupfinder.engine import Engine  # noqa: E402
from dupfinder.hashing import hash_file  # noqa: E402
from dupfinder.models import Reason  # noqa: E402
from dupfinder.store import IgnoreList, clear_records  # noqa: E402
from dupfinder.winutil import can_recycle  # noqa: E402


def data(n: int, seed: int = 0) -> bytes:
    return bytes((i * 31 + seed) % 251 for i in range(n))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dftest_")
        self.root = os.path.join(self.tmp, "root")
        self.app = os.path.join(self.tmp, "app")
        self.q = os.path.join(self.tmp, "quarantine")
        os.makedirs(self.root)

    def tearDown(self):
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", self.tmp], capture_output=True)

    def write(self, rel: str, content: bytes) -> str:
        p = os.path.normpath(os.path.join(self.root, rel))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
        return p

    def cfg(self, **kw) -> ScanConfig:
        limits = kw.pop("limits", Limits())
        return ScanConfig(roots=kw.pop("roots", [self.root]), app_dir=self.app, quarantine_dir=self.q,
                          limits=limits, **kw)

    def assertCountsBalanced(self, r):
        c = r.counts
        self.assertEqual(c["found"], c["processed"] + c["skipped"] + c["failed"] + c["unprocessed"])


class TestDetection(Base):
    def test_exact_duplicates_only(self):
        big = data(300_000)
        a = self.write("a.bin", big)
        b = self.write("sub/b.bin", big)
        self.write("c.bin", big[:-1] + b"\x00")       # 같은 크기, 끝만 다름(앞부분 해시 통과)
        self.write("d.bin", data(300_000, seed=7))    # 같은 크기, 앞부분부터 다름
        s1 = self.write("s1.txt", b"x" * 5000)        # 64KB 이하 작은 파일
        s2 = self.write("s2.txt", b"x" * 5000)
        self.write("tiny1", b"ab")                    # 1KB 미만 제외(R1-4)
        self.write("tiny2", b"ab")
        r = Engine(self.cfg()).run()
        self.assertEqual(r.reason, Reason.COMPLETED, r.detail)
        got = sorted(sorted(e.path for e in g.files) for g in r.groups)
        self.assertEqual(got, sorted([sorted([a, b]), sorted([s1, s2])]))
        self.assertEqual(r.counts["found"], 6)
        self.assertCountsBalanced(r)
        self.assertEqual(r.counts["unprocessed"], 0)

    def test_excluded_dir_names_and_own_dirs(self):
        self.write("a.bin", data(5000))
        self.write("node_modules/b.bin", data(5000))
        self.write(".git/c.bin", data(5000))
        r = Engine(self.cfg(roots=[self.tmp])).run()   # app·격리 폴더가 검사 범위 안에 있어도 제외
        os.makedirs(self.q, exist_ok=True)
        with open(os.path.join(self.q, "x.bin"), "wb") as f:
            f.write(data(5000))
        r = Engine(self.cfg(roots=[self.tmp])).run()
        self.assertEqual(r.groups, [])
        self.assertEqual(r.counts["found"], 1)

    def test_junction_loop_is_not_followed(self):
        self.write("a/x.bin", data(5000))
        self.write("a/y.bin", data(5000))
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(self.root, "a", "loop"),
                        os.path.join(self.root, "a")], capture_output=True, check=True)
        r = Engine(self.cfg()).run()
        self.assertEqual(r.reason, Reason.COMPLETED)
        self.assertEqual(len(r.groups), 1)
        self.assertEqual(len(r.groups[0].files), 2)
        self.assertTrue(any(i.kind == "링크" for i in r.issues))

    def test_hardlink_is_not_duplicate(self):
        a = self.write("a.bin", data(5000))
        os.link(a, os.path.join(self.root, "a_link.bin"))
        r = Engine(self.cfg()).run()
        self.assertEqual(r.groups, [])
        self.assertEqual(r.counts["hardlinks"], 1)
        self.assertCountsBalanced(r)

    def test_depth_limit(self):
        self.write("1/2/3/4/deep.bin", data(5000))
        self.write("top.bin", data(5000))
        r = Engine(self.cfg(limits=Limits(max_depth=2))).run()
        self.assertEqual(r.groups, [])
        self.assertTrue(any("깊이" in i.reason for i in r.issues))

    def test_cache_reused(self):
        self.write("a.bin", data(200_000))
        self.write("b.bin", data(200_000))
        Engine(self.cfg()).run()
        r = Engine(self.cfg()).run()
        self.assertEqual(len(r.groups), 1)
        self.assertGreaterEqual(r.counts["cache_hits"], 2)

    def cache_paths(self):
        db = sqlite3.connect(os.path.join(self.app, "cache.db"))
        try:
            return {p for (p,) in db.execute("SELECT path FROM h2")}
        finally:
            db.close()

    def test_cache_disabled_leaves_no_cache_file(self):
        self.write("a.bin", data(5000))
        self.write("b.bin", data(5000))
        r = Engine(self.cfg(use_cache=False)).run()
        self.assertEqual(len(r.groups), 1)
        self.assertFalse(os.path.exists(os.path.join(self.app, "cache.db")))

    def test_cache_prunes_removed_files(self):
        a = self.write("a.bin", data(5000))
        b = self.write("b.bin", data(5000))
        c = self.write("c.bin", data(5000))
        Engine(self.cfg()).run()
        self.assertEqual(self.cache_paths(), {os.path.normcase(p) for p in (a, b, c)})
        os.remove(c)
        Engine(self.cfg()).run()
        self.assertEqual(self.cache_paths(), {os.path.normcase(p) for p in (a, b)})

    def test_cache_prune_keeps_other_roots(self):
        a = self.write("a.bin", data(5000))
        b = self.write("b.bin", data(5000))
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        Engine(self.cfg()).run()
        Engine(self.cfg(roots=[other])).run()   # 다른 폴더만 검사해도 기존 기록은 그대로
        self.assertEqual(self.cache_paths(), {os.path.normcase(p) for p in (a, b)})

    def test_clear_records(self):
        self.write("a.bin", data(5000))
        self.write("b.bin", data(5000))
        Engine(self.cfg()).run()
        os.makedirs(os.path.join(self.app, "logs"))
        with open(os.path.join(self.app, "logs", "actions-20260101.jsonl"), "w") as f:
            f.write("{}\n")
        IgnoreList(self.app).add(["abc"])
        removed, failed = clear_records(self.app)
        self.assertEqual(failed, [])
        self.assertFalse(os.path.exists(os.path.join(self.app, "cache.db")))
        self.assertFalse(os.path.exists(os.path.join(self.app, "logs")))
        self.assertTrue(os.path.exists(os.path.join(self.app, "ignore.json")))   # 묻지 않으면 남김
        clear_records(self.app, include_ignore=True)
        self.assertFalse(os.path.exists(os.path.join(self.app, "ignore.json")))

    def test_extension_include_and_exclude(self):
        a = self.write("a.JPG", data(5000))
        b = self.write("b.jpeg", data(5000))
        c = self.write("c.txt", data(5000, seed=3))
        d = self.write("d.log", data(5000, seed=3))
        r = Engine(self.cfg(ext_mode="include", extensions=parse_exts("jpg, .jpeg"))).run()
        self.assertEqual([sorted(e.path for e in g.files) for g in r.groups], [sorted([a, b])])
        self.assertEqual(r.counts["ext_filtered"], 2)
        r = Engine(self.cfg(ext_mode="exclude", extensions=["jpg", "jpeg"])).run()
        self.assertEqual([sorted(e.path for e in g.files) for g in r.groups], [sorted([c, d])])
        r = Engine(self.cfg()).run()
        self.assertEqual(len(r.groups), 2)

    def test_parse_exts_and_validation(self):
        self.assertEqual(parse_exts(" .JPG, png;*.mp4  jpg "), ["jpg", "png", "mp4"])
        with self.assertRaises(ValueError):
            self.cfg(ext_mode="include", extensions=[]).validate()

    def test_compare_mode(self):
        base = os.path.join(self.tmp, "base")
        os.makedirs(base)
        with open(os.path.join(base, "orig.bin"), "wb") as f:
            f.write(data(5000))
        t1 = self.write("copy.bin", data(5000))
        self.write("only1.bin", data(6000, 3))   # 대상 폴더 안에서만 중복 → 결과에 없음
        self.write("only2.bin", data(6000, 3))
        r = Engine(self.cfg(mode="compare", base_roots=[base])).run()
        self.assertEqual(len(r.groups), 1)
        sel = auto_select(r.groups, [], "compare")
        self.assertEqual(sel, {t1})

    def test_compare_overlap_rejected(self):
        with self.assertRaises(ValueError):
            self.cfg(mode="compare", base_roots=[os.path.join(self.root, "x")]).validate()


class TestStopConditions(Base):
    def test_locked_file_retried_then_skipped(self):
        p = self.write("a.bin", data(5000))
        self.write("b.bin", data(5000))
        k32 = ctypes.WinDLL("kernel32")
        k32.CreateFileW.restype = ctypes.c_void_p
        h = k32.CreateFileW(p, 0x80000000, 0, None, 3, 0, None)  # 공유 없이 열어 잠금
        try:
            r = Engine(self.cfg(limits=Limits(lock_retry_delays=(0.05, 0.05, 0.05)))).run()
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
        self.assertEqual(r.reason, Reason.COMPLETED)
        self.assertEqual(r.groups, [])
        self.assertTrue(any("사용 중" in i.reason and "3회" in i.reason for i in r.issues))
        self.assertCountsBalanced(r)

    def test_idle_timeout_retries_then_fails(self):
        slow = self.write("slow.bin", data(5000))
        self.write("ok.bin", data(5000))
        calls = []

        def fake(path, size, mtime, limit, cancel, touch):
            if path == slow:
                calls.append(1)
                time.sleep(3)  # touch 없이 멈춤
            return hash_file(path, size, mtime, limit, cancel, touch)
        eng = Engine(self.cfg(limits=Limits(read_idle_timeout=0.3, read_retries=2)))
        eng.hash_fn = fake
        t0 = time.monotonic()
        r = eng.run()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("응답 없음" in i.reason and i.failed for i in r.issues))
        self.assertEqual(r.reason, Reason.COMPLETED)
        self.assertCountsBalanced(r)

    def test_stall_asks_and_skips(self):
        stuck = self.write("stuck.bin", data(5000))
        self.write("ok.bin", data(5000))
        asked = []

        def fake(path, size, mtime, limit, cancel, touch):
            if path == stuck:
                time.sleep(4)
            return hash_file(path, size, mtime, limit, cancel, touch)

        def on_event(kind, d):
            if kind == "ask":
                asked.append(d.question)
                d.reply("건너뛰기")
        eng = Engine(self.cfg(limits=Limits(stall_seconds=0.5, read_idle_timeout=100)), on_event, interactive=True)
        eng.hash_fn = fake
        r = eng.run()
        self.assertTrue(asked and "진행이 없습니다" in asked[0])
        self.assertTrue(any("정체" in i.reason for i in r.issues))
        self.assertCountsBalanced(r)

    def test_error_threshold_stops(self):
        for i in range(10):
            self.write(f"f{i}.bin", data(5000))

        def broken(*a, **k):
            raise OSError(1117, "I/O 장치 오류")
        eng = Engine(self.cfg(limits=Limits(max_consecutive_errors=5)))
        eng.hash_fn = broken
        r = eng.run()
        self.assertEqual(r.reason, Reason.ERROR)
        self.assertIn("연속", r.detail)
        self.assertFalse(r.complete)

    def test_access_denied_folders_do_not_stop_scan(self):
        for i in range(150):
            os.makedirs(os.path.join(self.root, f"deny{i:03d}"))
        self.write("a.bin", data(5000))
        self.write("b.bin", data(5000))
        from dupfinder.engine import list_dir

        def guarded(path, *a, **k):
            if os.path.basename(path).startswith("deny"):
                raise PermissionError(13, "Access is denied")
            return list_dir(path, *a, **k)
        eng = Engine(self.cfg())
        eng.list_fn = guarded
        r = eng.run()
        self.assertEqual(r.reason, Reason.COMPLETED, r.detail)
        self.assertEqual(len(r.groups), 1)
        self.assertEqual(sum(1 for i in r.issues if i.reason == "접근 거부"), 150)

    def test_drive_system_dirs(self):
        from dupfinder.engine import is_drive_system_dir
        self.assertTrue(is_drive_system_dir(r"N:\found.002"))
        self.assertTrue(is_drive_system_dir(r"E:\$RECYCLE.BIN"))
        self.assertTrue(is_drive_system_dir(r"F:\System Volume Information"))
        self.assertFalse(is_drive_system_dir(r"N:\사진\found.002"))
        self.assertFalse(is_drive_system_dir(r"N:\found.txt"))

    def test_cancel_within_two_seconds(self):
        for i in range(6):
            self.write(f"f{i}.bin", data(200_000))

        def slow(path, size, mtime, limit, cancel, touch):
            for _ in range(1000):
                if cancel.is_set():
                    from dupfinder.hashing import Cancelled
                    raise Cancelled()
                touch(1)
                time.sleep(0.05)
        eng = Engine(self.cfg(use_cache=False))
        eng.hash_fn = slow
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", eng.run()))
        th.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        eng.cancel()
        th.join(5)
        self.assertLess(time.monotonic() - t0, 2.0)
        r = out["r"]
        self.assertEqual(r.reason, Reason.CANCELLED)
        self.assertTrue(any("미처리" in i.reason for i in r.issues))
        self.assertCountsBalanced(r)
        self.assertFalse(os.path.exists(os.path.join(self.app, "running.json")))

    def test_root_removed_mid_scan(self):
        for i in range(4):
            self.write(f"f{i}.bin", data(5000))

        def slow(path, size, mtime, limit, cancel, touch):
            for _ in range(100):
                if cancel.is_set():
                    raise RuntimeError
                touch(1)
                time.sleep(0.05)
            return (0, 0), b"x"
        eng = Engine(self.cfg(use_cache=False, limits=Limits(root_check_interval=0.2)))
        eng.hash_fn = slow
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", eng.run()))
        th.start()
        time.sleep(0.5)
        os.rename(self.root, self.root + "_moved")
        th.join(10)
        r = out["r"]
        self.assertEqual(r.reason, Reason.ENVIRONMENT)
        self.assertTrue(any("삭제·이동" in i.reason for i in r.issues))


class TestActions(Base):
    def scan(self):
        r = Engine(self.cfg()).run()
        self.assertEqual(r.reason, Reason.COMPLETED, r.detail)
        return r

    def test_quarantine_same_volume(self):
        a = self.write("a.bin", data(5000))
        b = self.write("b.bin", data(5000))
        r = self.scan()
        sel = auto_select(r.groups, [os.path.dirname(b)] and [], "all")
        items, problems = plan(r.groups, sel)
        self.assertEqual(problems, [])
        res = ActionRunner(self.app).run(items, QUARANTINE, self.q)
        self.assertTrue(all(x.ok for x in res), res)
        moved = [x.path for x in res]
        kept = ({a, b} - set(moved)).pop()
        self.assertTrue(os.path.exists(kept))
        self.assertFalse(os.path.exists(moved[0]))
        self.assertTrue(os.path.exists(res[0].dest))
        self.assertTrue(os.path.exists(os.path.join(self.app, "logs")))

    def test_preferred_folder_wins(self):
        self.write("old.bin", data(5000))
        time.sleep(0.05)
        keep = self.write("keep/new.bin", data(5000))
        r = self.scan()
        sel = auto_select(r.groups, [os.path.join(self.root, "keep")])
        self.assertNotIn(keep, sel)

    def test_changed_after_scan_is_skipped(self):
        self.write("a.bin", data(5000))
        self.write("b.bin", data(5000))
        r = self.scan()
        sel = auto_select(r.groups, [])
        target = next(iter(sel))
        with open(target, "r+b") as f:
            f.write(b"Z")
        os.utime(target, ns=(time.time_ns(), time.time_ns() + 10**9))
        items, _ = plan(r.groups, sel)
        res = ActionRunner(self.app).run(items, QUARANTINE, self.q)
        self.assertFalse(res[0].ok)
        self.assertIn("변경", res[0].detail)
        self.assertTrue(os.path.exists(target))

    def test_all_selected_group_refused(self):
        a = self.write("a.bin", data(5000))
        b = self.write("b.bin", data(5000))
        r = self.scan()
        items, problems = plan(r.groups, {a, b})
        self.assertEqual(items, [])
        self.assertEqual(len(problems), 1)

    def test_cross_volume_copy_verify_and_rollback(self):
        self.write("a.bin", data(200_000))
        self.write("b.bin", data(200_000))
        r = self.scan()
        sel = auto_select(r.groups, [])
        items, _ = plan(r.groups, sel)
        orig = actions._same_volume
        actions._same_volume = lambda x, y: False
        try:
            res = ActionRunner(self.app).run(items, QUARANTINE, self.q)
        finally:
            actions._same_volume = orig
        self.assertTrue(res[0].ok, res[0].detail)
        self.assertTrue(os.path.exists(res[0].dest))
        self.assertFalse(os.path.exists(res[0].path))
        leftovers = [f for _, _, fs in os.walk(self.q) for f in fs if f.endswith(".dfpartial")]
        self.assertEqual(leftovers, [])

    def test_ignore_list(self):
        self.write("a.bin", data(5000))
        self.write("b.bin", data(5000))
        r = self.scan()
        ActionRunner(self.app).ignore(r.groups)
        r2 = self.scan()
        self.assertEqual(r2.groups, [])
        self.assertEqual(r2.counts["ignored_groups"], 1)

    def test_recycle_guard(self):
        self.assertIsNotNone(can_recycle("C:\\" + "a" * 300))
        self.assertIsNone(can_recycle(os.path.join(self.root, "x.bin")) if os.path.splitdrive(self.root)[0].upper() == "C:" else None)


if __name__ == "__main__":
    unittest.main()
