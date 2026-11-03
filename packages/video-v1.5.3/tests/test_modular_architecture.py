"""Architecture and compatibility regressions; no models or production services."""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import check_architecture as architecture

SKILL = architecture.ROOT.parent
sys.path.insert(0, str(SKILL))


class ArchitectureTests(unittest.TestCase):
    def test_release_inventory_keeps_media_source_and_excludes_private_media(self):
        spec = importlib.util.spec_from_file_location("_modular_inventory", SKILL.parent / "build_manifest.py")
        inventory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inventory)
        actual = {p.relative_to(inventory.ROOT).as_posix() for p in inventory.files()}
        self.assertIn("doubao-jianying-orchestrator/orchestrator/media/frames.py", actual)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = root / "doubao-jianying-orchestrator/orchestrator/media/frames.py"
            private = root / "media/private.json"
            nested = code.parent / "media/private.json"
            for path in (code, private, nested):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            with patch.object(inventory, "ROOT", root):
                self.assertEqual(inventory.files(), [code])

    def test_current_boundaries(self):
        self.assertEqual(architecture.check(), [])

    def test_forbidden_edges_and_legacy_imports_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orchestrator"
            shutil.copytree(architecture.ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
            (root / "media" / "forbidden.py").write_text(
                "from ..application import engine\nfrom ..selection import ai_editor\n"
                "def lazy():\n    from orchestrator import cli\n", encoding="utf-8")
            issues = architecture.check(root)
            for prefix in ("APPLICATION_DEPENDENCY:", "FOUNDATION_DEPENDENCY:", "LEGACY_IMPORT:"):
                self.assertTrue(any(issue.startswith(prefix) for issue in issues), issues)

    def test_business_logic_cannot_return_to_legacy_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orchestrator"
            shutil.copytree(architecture.ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
            with (root / "engine.py").open("a", encoding="utf-8") as stream:
                stream.write("\ndef business_logic():\n    return True\n")
            (root / "new_business.py").write_text("value = 1\n", encoding="utf-8")
            issues = architecture.check(root)
            self.assertIn("LEGACY_ENTRYPOINT_HAS_LOGIC:engine", issues)
            self.assertIn("ROOT_IMPLEMENTATION_FORBIDDEN:new_business.py", issues)

    def test_anchored_public_contracts_cannot_load_application_or_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orchestrator"
            shutil.copytree(architecture.ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
            contract = root / "ai_editor_contract.py"
            with contract.open("a", encoding="utf-8") as stream:
                stream.write("\ndef forbidden():\n    from .application import engine\n    from tools import draft_delivery\n")
            issues = architecture.check(root)
            self.assertTrue(any(x.startswith("APPLICATION_DEPENDENCY:ai_editor_contract.py:") for x in issues), issues)
            self.assertTrue(any(x.startswith("PUBLIC_CONTRACT_DEPENDENCY:ai_editor_contract.py:") for x in issues), issues)

    def test_tools_layers_reject_worker_and_reverse_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orchestrator"
            root.mkdir()
            (root / "module_map.json").write_text(json.dumps({
                "features": {"application": ["engine"]}, "anchored": []}), encoding="utf-8")
            (root / "application").mkdir()
            (root / "application/engine.py").write_text("", encoding="utf-8")
            (root / "engine.py").write_text(architecture._wrapper("engine", "application.engine"), encoding="utf-8")
            tools = Path(directory) / "tools"
            for feature in ("queueing", "acceptance", "workflow"):
                (tools / feature).mkdir(parents=True)
            (tools / "queueing" / "contracts.py").write_text(
                "def forbidden():\n    import batch_worker\n    from ..acceptance import finalization\n",
                encoding="utf-8")
            (tools / "acceptance" / "gate.py").write_text(
                "from ..workflow import closed_loop\nfrom .. import batch_dispatch\n"
                "from orchestrator import engine, OrchestrationEngine\n", encoding="utf-8")
            (tools / "workflow" / "run.py").write_text(
                "from ..queueing import contracts\nfrom ..acceptance import gate\nfrom ..batch_dispatch import dispatch\n",
                encoding="utf-8")
            issues = architecture.check(root, tools_root=tools)
            self.assertIn("TOOLS_DEPENDENCY:queueing/contracts.py:batch_worker", issues)
            self.assertIn("TOOLS_DEPENDENCY:queueing/contracts.py:tools.acceptance", issues)
            self.assertIn("TOOLS_DEPENDENCY:acceptance/gate.py:tools.workflow", issues)
            self.assertIn("TOOLS_DEPENDENCY:acceptance/gate.py:tools.batch_dispatch", issues)
            self.assertIn("TOOLS_DEPENDENCY:acceptance/gate.py:orchestrator.engine", issues)
            self.assertIn("TOOLS_DEPENDENCY:acceptance/gate.py:orchestrator.OrchestrationEngine", issues)
            self.assertFalse(any(":workflow/" in x for x in issues), issues)

    def test_all_legacy_imports_share_state_and_patch_targets(self):
        layout = json.loads((architecture.ROOT / "module_map.json").read_text(encoding="utf-8"))
        for feature, names in layout["features"].items():
            for name in names:
                with self.subTest(module=name):
                    legacy = importlib.import_module("orchestrator." + name)
                    actual = importlib.import_module(f"orchestrator.{feature}.{name}")
                    self.assertIs(legacy, actual)
                    marker = object()
                    with patch.object(legacy, "_architecture_marker", marker, create=True):
                        self.assertIs(actual._architecture_marker, marker)

    def test_pure_contracts_and_clip_do_not_load_engine(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from orchestrator import __version__, ai_editor_contract, visual_qc_contract; "
            "from orchestrator.domain.clips import Clip; "
            "assert Clip('video', 0, 1).parent_index == -1; "
            "assert 'orchestrator.application.engine' not in sys.modules; "
            "assert 'orchestrator.application.cli' not in sys.modules; "
            "from orchestrator import OrchestrationEngine; "
            "from orchestrator.engine import Clip as LegacyClip; assert LegacyClip is Clip"
        )
        subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(SKILL)],
                       check=True, capture_output=True, text=True, timeout=30)

    def test_copied_skill_uses_its_own_resources_and_standalone_contracts(self):
        with tempfile.TemporaryDirectory(prefix="模块 空格_") as directory:
            skill = Path(directory) / "skills" / "doubao-jianying-orchestrator"
            shutil.copytree(SKILL / "orchestrator", skill / "orchestrator",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (skill / "VERSION").write_text("0.0.0\n", encoding="utf-8")
            script = """
import importlib.util, sys
from pathlib import Path
skill = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(skill))
from orchestrator import __version__
from orchestrator.runtime.platform_env import SKILL_DIR, SKILLS_ROOT
assert __version__ == '0.0.0'
assert SKILL_DIR == skill and SKILLS_ROOT == skill.parent
from orchestrator import rulepack
assert rulepack.RULES_DIR == skill / 'orchestrator' / 'rules'
# Standalone Eval contracts must read the copied rules, even if normal package
# imports have a poisoned in-memory rule implementation from another runtime.
rulepack.editorial_context = lambda manifest: (_ for _ in ()).throw(AssertionError('wrong runtime'))
path = skill / 'orchestrator' / 'ai_editor_contract.py'
spec = importlib.util.spec_from_file_location('_standalone_contract', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.editorial_context({'category': 'zhipin'})['category'] == 'zhipin'
"""
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(skill)],
                                    cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
