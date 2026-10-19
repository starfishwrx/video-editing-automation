import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import draft_safety, file_safety  # noqa: E402


class FileSafetyTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fs_test_")
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"JY_SLOT_DIR": str(self.root / "slots")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class FileLockTests(FileSafetyTestBase):
    def test_second_handle_waits_until_first_releases(self):
        target = self.root / "registry.json"
        with file_safety.file_lock(target, timeout=1):
            with self.assertRaises(TimeoutError):
                with file_safety.file_lock(target, timeout=0.2):
                    pass
        with file_safety.file_lock(target, timeout=0.2) as lock:
            self.assertEqual(lock.name, "registry.json.lock")

    def test_lock_is_exclusive_across_threads(self):
        target = self.root / "counter.json"
        inside = []
        overlap = []

        def worker():
            with file_safety.file_lock(target, timeout=10):
                inside.append(1)
                if len(inside) > 1:
                    overlap.append(True)
                time.sleep(0.02)
                inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(overlap, [])


class AtomicWriteTests(FileSafetyTestBase):
    def test_atomic_write_replaces_and_leaves_no_temp(self):
        target = self.root / "a" / "out.json"
        file_safety.atomic_write_text(target, "第一版")
        file_safety.atomic_write_text(target, "第二版")
        self.assertEqual(target.read_text(encoding="utf-8"), "第二版")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["out.json"])

    def test_replace_retries_transient_permission_error(self):
        target = self.root / "busy.json"
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(32, "being used by another process")
            return real_replace(src, dst)

        with mock.patch.object(file_safety.os, "replace", side_effect=flaky), \
                mock.patch.object(file_safety.time, "sleep"):
            file_safety.atomic_write_text(target, "ok")
        self.assertEqual(target.read_text(encoding="utf-8"), "ok")
        self.assertEqual(len(calls), 3)

    def test_inplace_fallback_when_replace_keeps_failing(self):
        target = self.root / "held.json"
        target.write_text("old", encoding="utf-8")
        with mock.patch.object(file_safety.os, "replace",
                               side_effect=PermissionError(32, "busy")), \
                mock.patch.object(file_safety.time, "sleep"):
            with self.assertRaises(PermissionError):
                file_safety.atomic_write_text(target, "new")
            file_safety.atomic_write_text(target, "new", inplace_fallback=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), ["held.json"])


class LockedJsonUpdateTests(FileSafetyTestBase):
    def test_reads_bom_mutates_and_writes(self):
        target = self.root / "root_meta_info.json"
        target.write_bytes(b"\xef\xbb\xbf" + json.dumps({"all_draft_store": []}).encode("utf-8"))

        def add(data):
            data["all_draft_store"].insert(0, {"draft_name": "草稿-rev-1"})

        result = file_safety.locked_json_update(target, add)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved, result)
        self.assertEqual(saved["all_draft_store"][0]["draft_name"], "草稿-rev-1")
        self.assertIn("草稿", target.read_text(encoding="utf-8"))

    def test_missing_file_uses_default_and_skip_keeps_file(self):
        target = self.root / "new.json"
        file_safety.locked_json_update(target, lambda d: dict(d, n=1), default={"n": 0})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"n": 1})

        def skip(_):
            raise file_safety.SkipUpdate

        before = target.read_bytes()
        file_safety.locked_json_update(target, skip)
        self.assertEqual(target.read_bytes(), before)

    def test_concurrent_updates_are_not_lost(self):
        target = self.root / "count.json"

        def bump():
            for _ in range(10):
                file_safety.locked_json_update(
                    target, lambda d: {"n": int(d.get("n", 0)) + 1}, default={"n": 0})

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["n"], 40)

    def test_sync_root_meta_keeps_compact_structure(self):
        meta = self.root / "root_meta_info.json"
        meta.write_text(json.dumps({"all_draft_store": [
            {"draft_name": "old", "draft_fold_path": str(self.root / "old"), "extra": 1}],
            "root_path": "x"}, ensure_ascii=False), encoding="utf-8")
        (self.root / "old").mkdir()
        draft_safety.sync_root_meta(self.root, "新草稿", duration_us=5)
        raw = meta.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertNotIn("\n", raw)
        self.assertEqual(data["root_path"], "x")
        self.assertEqual(data["all_draft_store"][0]["draft_name"], "新草稿")
        self.assertEqual(data["all_draft_store"][0]["extra"], 1)
        self.assertEqual(data["all_draft_store"][0]["tm_duration"], 5)

    def test_sync_root_meta_skips_unreadable_registry(self):
        meta = self.root / "root_meta_info.json"
        meta.write_bytes(b"\x00encrypted")
        with mock.patch.object(file_safety.time, "sleep"):
            draft_safety.sync_root_meta(self.root, "x")
        self.assertEqual(meta.read_bytes(), b"\x00encrypted")


class ReserveDirTests(FileSafetyTestBase):
    def test_threads_never_share_a_name(self):
        names = []
        lock = threading.Lock()

        def claim():
            path = file_safety.reserve_unique_dir(self.root, "任务", start=5)
            with lock:
                names.append(path.name)

        threads = [threading.Thread(target=claim) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(names), 12)
        self.assertEqual(len(set(names)), 12)
        self.assertEqual(sorted(names, key=lambda n: int(n.rsplit("-", 1)[1]))[0], "任务-rev-5")

    def test_exact_reservation_and_release(self):
        path = file_safety.reserve_exact_dir(self.root, "draft-rev-1", token="tok")
        self.assertEqual(file_safety.read_reservation(path)["token"], "tok")
        with self.assertRaises(FileExistsError):
            file_safety.reserve_exact_dir(self.root, "draft-rev-1")
        (path / "draft_info.json").write_text("{}", encoding="utf-8")
        self.assertFalse(file_safety.release_reserved_dir(path))
        (path / "draft_info.json").unlink()
        self.assertTrue(file_safety.release_reserved_dir(path))
        self.assertFalse(path.exists())

    def test_pre_write_check_allows_only_own_reserved_dir(self):
        path = file_safety.reserve_exact_dir(self.root, "mine", token="tok-a")
        with mock.patch.object(draft_safety, "is_jianying_running", return_value=True), \
                mock.patch.dict(os.environ, {"JY_DRAFT_RESERVATION": "tok-a"}):
            self.assertTrue(draft_safety.pre_write_check("mine", self.root, self.root).ok)
        other = self.root / "other"
        other.mkdir()
        (other / ".reserved").write_text(json.dumps({"pid": 1, "token": "tok-b"}), encoding="utf-8")
        with mock.patch.object(draft_safety, "is_jianying_running", return_value=True), \
                mock.patch.dict(os.environ, {"JY_DRAFT_RESERVATION": "tok-a"}):
            verdict = draft_safety.pre_write_check("other", self.root, self.root)
        self.assertEqual(verdict.status, "BLOCKED")
        self.assertTrue(path.exists())


class PrivateTempDirTests(FileSafetyTestBase):
    def test_each_call_is_unique_and_removed(self):
        with file_safety.private_temp_dir("jy_match_", self.root) as a, \
                file_safety.private_temp_dir("jy_match_", self.root) as b:
            self.assertNotEqual(a, b)
            (a / "prompt.txt").write_text("x", encoding="utf-8")
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())

    def test_cleanup_never_raises_while_file_is_held(self):
        holder = {}
        with file_safety.private_temp_dir("jy_match_", self.root, cleanup_timeout=0.2) as temp:
            handle = open(temp / "prompt.txt", "w", encoding="utf-8")
            holder["handle"] = handle
            holder["path"] = temp
        handle.close()
        if holder["path"].exists():
            file_safety._remove_tree(holder["path"], 1.0)
        self.assertFalse(holder["path"].exists())

    def test_cleanup_succeeds_once_holder_releases(self):
        with file_safety.private_temp_dir("jy_match_", self.root, cleanup_timeout=5.0) as temp:
            handle = open(temp / "prompt.txt", "w", encoding="utf-8")
            timer = threading.Timer(0.3, handle.close)
            timer.start()
        timer.join()
        self.assertFalse(temp.exists())


if __name__ == "__main__":
    unittest.main()
