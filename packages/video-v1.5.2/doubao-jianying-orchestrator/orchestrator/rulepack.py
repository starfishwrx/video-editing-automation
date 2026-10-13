"""品类规则包：全局规则 + 品类规则（卫品 / 纸品），v1.5.2。

规则按两层合并：``rules/global.json`` 是所有品类都要守的剪辑规则，
``rules/<category>.json`` 只放该品类自己的指令（例如卫品的吸水实验、配音 1.2 倍速）。
飞书表还没按品类拆开之前，品类先按商品名关键词识别；任务显式给了 ``category`` 时以它为准。
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

RULES_DIR = Path(__file__).resolve().parent / "rules"

CATEGORY_WEIPIN = "weipin"
CATEGORY_ZHIPIN = "zhipin"
CATEGORY_GENERAL = "general"
_CATEGORY_FILES = (CATEGORY_WEIPIN, CATEGORY_ZHIPIN)
_CATEGORY_ALIASES = {
    "weipin": CATEGORY_WEIPIN, "卫品": CATEGORY_WEIPIN, "卫生用品": CATEGORY_WEIPIN,
    "zhipin": CATEGORY_ZHIPIN, "纸品": CATEGORY_ZHIPIN, "生活用纸": CATEGORY_ZHIPIN,
    "general": CATEGORY_GENERAL, "通用": CATEGORY_GENERAL,
}
_DETECT_FIELDS = ("product", "product_name", "title", "task_title", "draft_name", "series")
DEFAULT_FPS = 30.0
DEFAULT_EDGE_KEEP_S = 0.025


@lru_cache(maxsize=None)
def _read_rules(name: str) -> dict[str, Any]:
    path = RULES_DIR / f"{name}.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_category(value: Any) -> str:
    return _CATEGORY_ALIASES.get(str(value or "").strip().lower(), "")


def detect_category(manifest: dict[str, Any] | None) -> tuple[str, str]:
    """返回 ``(品类, 依据)``。识别不了时返回 ``("general", "")``。"""
    source = manifest if isinstance(manifest, dict) else {}
    explicit = normalize_category(source.get("category"))
    if explicit:
        return explicit, "manifest.category"
    texts = [(field, str(source.get(field) or "")) for field in _DETECT_FIELDS]
    sources = source.get("material_sources") or []
    if isinstance(sources, list) and sources:
        texts.append(("material_sources", str(Path(str(sources[0])).parent)))
    for field, text in texts:
        if not text:
            continue
        for category in _CATEGORY_FILES:
            for keyword in _read_rules(category).get("product_keywords") or []:
                if keyword and keyword in text:
                    return category, f"{field}:{keyword}"
    return _detect_from_script(source)


def _detect_from_script(source: dict[str, Any]) -> tuple[str, str]:
    """商品名和素材目录都识别不了时看文案；两类关键词都出现时以命中多的为准，打平不判。"""
    script = str(source.get("full_script") or "")
    if not script:
        script = "".join(str(s.get("text") or "") if isinstance(s, dict) else str(s)
                         for s in source.get("segments") or [])
    if not script:
        return CATEGORY_GENERAL, ""
    hits = {category: [k for k in _read_rules(category).get("product_keywords") or [] if k and k in script]
            for category in _CATEGORY_FILES}
    ranked = sorted(hits.items(), key=lambda item: len(item[1]), reverse=True)
    (best, best_hits), (_, runner_up) = ranked[0], ranked[1]
    if not best_hits or len(best_hits) == len(runner_up):
        return CATEGORY_GENERAL, ""
    return best, f"full_script:{best_hits[0]}"


def load(category: str) -> dict[str, Any]:
    """合并后的规则：标量与 voice 字典由品类覆盖全局，列表规则按「全局 + 品类」拼接。"""
    base = _read_rules("global")
    extra = _read_rules(category) if category in _CATEGORY_FILES else {}
    merged: dict[str, Any] = {
        "category": category,
        "label": extra.get("label") or "通用",
        "rulepacks": [r for r in (base.get("rulepack"), extra.get("rulepack")) if r],
        "versions": {r.get("rulepack"): r.get("version") for r in (base, extra) if r.get("rulepack")},
        "auto_transition": bool(base.get("auto_transition", False)),
        "avoid_similar_shots": bool(extra.get("avoid_similar_shots",
                                              base.get("avoid_similar_shots", False))),
        "voice": {**(base.get("voice") or {}), **(extra.get("voice") or {})},
        "analysis_prompt_rules": [*(base.get("analysis_prompt_rules") or []),
                                  *(extra.get("analysis_prompt_rules") or [])],
        "match_prompt_rules": [*(base.get("match_prompt_rules") or []),
                               *(extra.get("match_prompt_rules") or [])],
        "scenes": [*(base.get("scenes") or []), *(extra.get("scenes") or [])],
    }
    return merged


def for_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    category, _ = detect_category(manifest)
    return load(category)


def gap_tail_pad_s(max_gap_frames: Any, *, fps: Any = DEFAULT_FPS,
                   edge_keep_s: float = DEFAULT_EDGE_KEEP_S) -> float:
    """句间气口 ≤ N 帧时，每句时间线尾部还能留多少秒。

    相邻两句之间听到的静音 = 上一句尾部保留的静音 + 尾留白 + 下一句头部保留的静音，
    再扣一帧给取整误差。
    """
    try:
        frames = max(1, int(max_gap_frames))
    except (TypeError, ValueError):
        frames = 5
    try:
        rate = float(fps) or DEFAULT_FPS
    except (TypeError, ValueError):
        rate = DEFAULT_FPS
    frame_s = 1.0 / rate
    return round(max(0.0, frames * frame_s - 2 * edge_keep_s - frame_s), 3)


_VOICE_SPEED_SUFFIX = re.compile(r"\d+(?:\.\d+)?\s*倍速\s*$")


def voice_has_speed(voice: Any) -> bool:
    """配音名称里运营已经写了倍速（「XX1.2倍速」）时，合成时就按它变速了。"""
    if isinstance(voice, dict):
        return voice.get("speed") not in (None, "", 1, 1.0)
    return bool(_VOICE_SPEED_SUFFIX.search(str(voice or "")))


def default_voice_tempo(rules: dict[str, Any], voice: Any) -> float:
    """品类配音倍速只是默认值；配音名称自带倍速时不再叠加。"""
    if voice_has_speed(voice):
        return 1.0
    return float((rules.get("voice") or {}).get("tempo", 1.0) or 1.0)


def apply_manifest_defaults(manifest: dict[str, Any]) -> dict[str, Any]:
    """把品类规则写进 manifest 的默认值。任务里显式给过的字段一律不覆盖。"""
    out = dict(manifest)
    category, basis = detect_category(out)
    rules = load(category)
    voice = rules.get("voice") or {}
    out["category"] = category
    out.setdefault("auto_transition", rules.get("auto_transition", False))
    out.setdefault("avoid_similar_shots", rules.get("avoid_similar_shots", False))
    out.setdefault("voice_tempo", default_voice_tempo(rules, out.get("voice")))
    out.setdefault("voice_edge_trim", bool(voice.get("edge_trim", True)))
    max_gap = os.environ.get("JY_VOICE_MAX_GAP_FRAMES") or voice.get("max_gap_frames", 5)
    if out.get("tail_pad_s") is None:
        out["tail_pad_s"] = gap_tail_pad_s(max_gap, fps=out.get("fps") or DEFAULT_FPS)
    out["rulepack"] = {
        "category": category,
        "label": rules.get("label"),
        "basis": basis,
        "rulepacks": rules.get("rulepacks"),
        "versions": rules.get("versions"),
        "voice_max_gap_frames": int(max_gap),
    }
    return out


def _haystack(shot: dict[str, Any] | None, fields: Any = None) -> str:
    """镜头里用来匹配场景画面关键词的文字；场景给了 ``shot_fields`` 时只看这些字段。"""
    shot = shot if isinstance(shot, dict) else {}
    if fields:
        return " ".join(str(shot.get(key) or "") for key in fields)
    parts = []
    for key in ("description", "visual_description", "action", "object", "result",
                "pointing_action", "gesture"):
        parts.append(str(shot.get(key) or ""))
    for key in ("visual_tags", "evidence_tags"):
        value = shot.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def _hits(text: str, keywords) -> bool:
    return any(k and k in text for k in keywords or [])


def scene_for(rules: dict[str, Any], text: str, shot: Optional[dict[str, Any]] = None) -> Optional[dict]:
    """这一句 + 选中的镜头命中哪条场景规则（按规则顺序取第一条）。

    文案关键词和镜头内容都要对上：只看文案会把「吸收快」这种卖点句也当成吸水实验，
    只看镜头会把背景里有液体的镜头当成实验。没有文案关键词的规则（如正面指产品）只看镜头。
    """
    sentence = re.sub(r"\s+", "", str(text or ""))
    looks = _haystack(shot)
    pointing = bool((shot or {}).get("pointing_to_text") or (shot or {}).get("explicit_pointing"))
    phase = str((shot or {}).get("action_phase") or "").strip().lower()
    for scene in rules.get("scenes") or []:
        text_kw = scene.get("text_keywords") or []
        shot_kw = scene.get("shot_keywords") or []
        # 只管动作正在进行的镜头：静态陈列不存在「切在动作中途」的问题。
        if scene.get("action_phases") and phase not in scene["action_phases"]:
            continue
        if scene.get("kind") == "require_visual":
            if _hits(sentence, text_kw):
                return scene
            continue
        text_ok = _hits(sentence, text_kw) if text_kw else True
        scene_looks = _haystack(shot, scene["shot_fields"]) if scene.get("shot_fields") else looks
        shot_ok = _hits(scene_looks, shot_kw) or (scene.get("id") == "point_front" and pointing)
        if text_ok and shot_ok:
            return scene
    return None


def text_hits_scene(scene: dict[str, Any], text: str) -> bool:
    return _hits(re.sub(r"\s+", "", str(text or "")), scene.get("text_keywords") or [])


def shot_has_visual(scene: dict[str, Any], shot: dict[str, Any] | None) -> bool:
    return _hits(_haystack(shot, scene.get("shot_fields")), scene.get("shot_keywords") or [])


def shot_keyword_hits(scene: dict[str, Any], shot: dict[str, Any] | None) -> int:
    looks = _haystack(shot, scene.get("shot_fields"))
    return sum(1 for k in scene.get("shot_keywords") or [] if k and k in looks)


def prompt_rules(manifest: dict[str, Any] | None, kind: str = "match") -> list[str]:
    rules = for_manifest(manifest)
    return list(rules.get(f"{kind}_prompt_rules") or [])
