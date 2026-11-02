"""功能包入口；引擎延迟加载，版本从已安装技能的 VERSION 读取。"""
from .paths import SKILL_ROOT

__version__ = (SKILL_ROOT / "VERSION").read_text(encoding="utf-8").strip()
__all__ = ["OrchestrationEngine"]


def __getattr__(name: str):
    if name == "OrchestrationEngine":
        from .application.engine import OrchestrationEngine
        globals()[name] = OrchestrationEngine
        return OrchestrationEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
