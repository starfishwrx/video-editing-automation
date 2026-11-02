"""Stable resource roots inside the independently installed orchestrator skill."""
from pathlib import Path

ORCHESTRATOR_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ORCHESTRATOR_ROOT.parent
PACKAGE_ROOT = SKILL_ROOT.parent
RULES_ROOT = ORCHESTRATOR_ROOT / "rules"
