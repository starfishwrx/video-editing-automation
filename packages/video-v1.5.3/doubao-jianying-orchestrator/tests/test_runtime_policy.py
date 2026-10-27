import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import runtime_policy as rp


class RuntimePolicyTests(unittest.TestCase):
    def test_legacy_environment_cannot_expand_account(self):
        with patch.dict(os.environ, {"JY_VISION_SLOTS": "160", "JY_VISION_BATCH_WORKERS": "0",
                                      "JY_TASK_WORKERS": "99"}, clear=True):
            policy = rp.load_policy()
            self.assertEqual((policy.vision_slots, policy.api_workers, policy.task_workers), (20, 20, 4))
        with patch.dict(os.environ, {"JY_VISION_SLOTS": "3"}, clear=True):
            self.assertEqual(rp.load_policy().vision_slots, 3)

    def test_explicit_policy_is_authoritative_and_invalid_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"vision_slots": 2, "slot_dir": directory}))
            with patch.dict(os.environ, {"JY_CONCURRENCY_POLICY_FILE": str(path),
                                         "JY_VISION_SLOTS": "160", "JY_SLOT_DIR": "other"}, clear=True):
                self.assertEqual(rp.load_policy().vision_slots, 2)
                self.assertEqual(rp.load_policy().slot_dir, str(Path(directory).resolve()))
                path.write_text('{"vision_slots": 0}')
                with self.assertRaises(ValueError):
                    rp.load_policy()

    def test_failed_slot_is_never_unlimited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"vision_slots": 1, "slot_dir": directory}))
            with patch.dict(os.environ, {"JY_CONCURRENCY_POLICY_FILE": str(path)}, clear=True):
                with rp.vision_slot(timeout=0) as first:
                    self.assertTrue(first.acquired)
                    with rp.vision_slot(timeout=0) as second:
                        self.assertFalse(second.acquired)

    def test_separate_entry_processes_share_exact_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            policy.write_text(json.dumps({"vision_slots": 2, "slot_dir": directory}))
            script = root / "entry.py"
            script.write_text('''import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from orchestrator.runtime_policy import vision_slot
from orchestrator.file_safety import locked_json_update
state=Path(sys.argv[2])
def enter(v):
 v['active']=v.get('active',0)+1;v['maximum']=max(v.get('maximum',0),v['active']);v['calls']=v.get('calls',0)+1
def leave(v):v['active']-=1
with vision_slot(timeout=10) as handle:
 assert handle.acquired
 locked_json_update(state,enter,{})
 time.sleep(.2)
 locked_json_update(state,leave,{})
''', encoding="utf-8")
            env = dict(os.environ, JY_CONCURRENCY_POLICY_FILE=str(policy), JY_VISION_SLOTS="160")
            processes = [subprocess.Popen([sys.executable, str(script), str(Path(__file__).resolve().parents[1]),
                          str(root / "state.json")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                         for _ in range(5)]
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["calls"], 5)
            self.assertEqual(state["maximum"], 2)
            self.assertEqual(state["active"], 0)


if __name__ == "__main__":
    unittest.main()
