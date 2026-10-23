"""Native file protocol and concurrency checks using a local fake CLI only."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import agy_transport as agy


FAKE_CLI = r'''
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
assert '--print' in args and '--sandbox' in args and '--dangerously-skip-permissions' in args
assert '--new-project' in args and '--continue' not in args and '--conversation' not in args
assert args[args.index('--print-timeout')+1]=='110s'
root=Path.cwd()
assert (root/'input.jpg').read_bytes()[:2]==b'\xff\xd8'
assert 'input.jpg' in (root/'task.md').read_text(encoding='utf8')
assert json.loads((root/'schema.json').read_text())['type']=='object'
Path(args[args.index('--log-file')+1]).write_text('fake CLI only; no network',encoding='utf8')
mode=os.environ.get('FAKE_AGY_MODE','ok')
response={'image_received':True,'model':args[args.index('--model')+1],'observation':'offline fake only'}
if mode=='location':
 print('HTTP 400 User location is not supported for this API use.',file=sys.stderr);sys.exit(1)
if mode=='sleep':time.sleep(30)
if mode=='missing':print(json.dumps(response));sys.exit(0)
if mode=='invalid':(root/'result.json').write_text('not-json');sys.exit(0)
(root/'result.json').write_text(json.dumps(response),encoding='utf8')
if mode=='nonzero':sys.exit(9)
print('fake completed')
'''


class AgyTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake_agy.py"
        self.fake.write_text(FAKE_CLI, encoding="utf-8")
        self.image = self.root / "frame.jpg"
        Image.new("RGB", (40, 40), "blue").save(self.image)
        self.env = mock.patch.dict(os.environ, {"JY_VISION_TRANSPORT": "agy", "JY_AGY_BIN": str(self.fake),
            "JY_AGY_MODEL": agy.DEFAULT_MODEL, "JY_AGY_SCRATCH_DIR": str(self.root/"scratch"),
            "JY_AGY_SLOT_DIR": str(self.root/"slots"), "JY_AGY_CONCURRENCY": "20", "FAKE_AGY_MODE": "ok"})
        self.env.start()
        self.addCleanup(self.env.stop)
        command = agy._command
        self.command_patch = mock.patch.object(agy, "_command", side_effect=lambda *args: [sys.executable, *command(*args)])
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def run_fake(self, output="reply.json", **kwargs):
        return agy.run(self.image, "Describe only the actual image, return JSON.", self.root/output, **kwargs)

    def test_file_result_and_explicit_model_use_minimal_new_workspaces(self):
        first = self.run_fake()
        second = self.run_fake(output="second.json")
        self.assertTrue(first[0], first)
        self.assertTrue(second[0], second)
        self.assertEqual(json.loads(first[1])["model"], agy.DEFAULT_MODEL)
        workspaces = list((self.root/"scratch").glob("*/invocation.json"))
        self.assertEqual(len(workspaces), 2)
        for receipt_file in workspaces:
            receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
            self.assertEqual(receipt["concurrency"], 20)
            self.assertEqual(receipt["outer_timeout_s"], 120)
            self.assertEqual((receipt_file.parent/".gitignore").read_text(), "*\n")
            self.assertNotIn(str(self.root/"reply.json"), (receipt_file.parent/"task.md").read_text(encoding="utf-8"))

    def test_stdout_nonzero_and_invalid_json_never_substitute_for_result_file(self):
        for mode in ("missing", "invalid", "nonzero"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"FAKE_AGY_MODE": mode}):
                ok, message = self.run_fake(output=mode+".json")
                self.assertFalse(ok)
                self.assertFalse((self.root/(mode+".json")).exists())

    def test_location_rejection_is_non_retryable_and_does_not_change_configuration(self):
        with mock.patch.dict(os.environ, {"FAKE_AGY_MODE": "location"}):
            before = dict(os.environ)
            ok, message = self.run_fake()
            self.assertFalse(ok)
            self.assertTrue(agy.is_non_retryable(message))
            self.assertIn("LOCATION_NOT_SUPPORTED", message)
            self.assertEqual(dict(os.environ), before)

    def test_outer_timeout_kills_only_its_cli_invocation(self):
        with mock.patch.dict(os.environ, {"FAKE_AGY_MODE": "sleep"}), \
                mock.patch.object(agy, "_kill_own_tree", wraps=agy._kill_own_tree) as cleanup:
            ok, message = self.run_fake(timeout=.25)
        self.assertFalse(ok)
        self.assertIn("AGY_TIMEOUT", message)
        self.assertEqual(cleanup.call_count, 1)
        self.assertIsNotNone(cleanup.call_args.args[0].poll())
        self.assertFalse((self.root/"reply.json").exists())

    def test_stable_independent_twenty_slots_reject_twenty_first_and_allow_reentry(self):
        gate = threading.Event()
        ready = threading.Barrier(21)
        def occupy():
            with agy.provider_slot(timeout=1) as lease:
                if not lease.acquired:
                    return False
                with agy.provider_slot(timeout=0) as nested:
                    self.assertIs(nested, lease)
                ready.wait(timeout=10)
                gate.wait(timeout=10)
                return True
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(occupy) for _ in range(20)]
            try:
                ready.wait(timeout=10)
                with agy.provider_slot(timeout=0) as extra:
                    self.assertFalse(extra.acquired)
            finally:
                gate.set()
            self.assertTrue(all(f.result(timeout=10) for f in futures))
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root/"local"), "JY_AGY_CONCURRENCY": "20"}):
            os.environ.pop("JY_AGY_SLOT_DIR", None)
            with mock.patch.dict(os.environ, {"TEMP": "arm-a"}):
                a = agy.slot_dir()
            with mock.patch.dict(os.environ, {"TEMP": "arm-b"}):
                self.assertEqual(agy.slot_dir(), a)
            self.assertEqual(a, (self.root/"local"/"doubao-jianying"/"agy_cli_slots").resolve())

    def test_route_identity_contains_no_auth_and_tracks_model_and_binary(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "do-not-disclose"}):
            first = agy.signature()
        self.assertNotIn("do-not-disclose", json.dumps(first))
        self.assertEqual(first["transport"], "agy-cli")
        with mock.patch.dict(os.environ, {"JY_AGY_MODEL": "gemini-3.8-flash-medium"}):
            self.assertNotEqual(first, agy.signature())
        self.fake.write_text(FAKE_CLI+"\n# updated fake binary\n", encoding="utf-8")
        self.assertNotEqual(first["cli_sha256"], agy.signature()["cli_sha256"])


if __name__ == "__main__":
    unittest.main()
