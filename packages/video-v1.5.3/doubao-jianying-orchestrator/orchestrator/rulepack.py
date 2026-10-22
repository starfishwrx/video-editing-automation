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
        "intent_keywords": {**(base.get("intent_keywords") or {}), **(extra.get("intent_keywords") or {})},
        "excluded_claims": [*(base.get("excluded_claims") or []), *(extra.get("excluded_claims") or [])],
        "sentence_topics": [*(base.get("sentence_topics") or []), *(extra.get("sentence_topics") or [])],
        "shot_label_rules": [*(base.get("shot_label_rules") or []),
                             *(extra.get("shot_label_rules") or [])],
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


_VOICE_SPEED_SUFFIX = re.compile(r"(?P<speed>\d+(?:\.\d+)?)\s*倍速\s*$")


def voice_name_speed(voice: Any) -> Optional[float]:
    """从配音名称末尾解析运营写的倍速（「tvb女声1.2倍速」→ 1.2）。

    SAMI 合成本身不认这个后缀；倍速靠 ``voice_tempo`` → ffmpeg ``atempo`` 后处理。
    以前误以为「名称带倍速=合成时已变速」，把 tempo 置 1.0，结果口播一直是原速。
    """
    if isinstance(voice, dict):
        raw = voice.get("speed")
        if raw in (None, "", 1, 1.0):
            return None
        try:
            speed = float(raw)
        except (TypeError, ValueError):
            return None
        return speed if 0.1 <= speed <= 10 else None
    m = _VOICE_SPEED_SUFFIX.search(str(voice or ""))
    if not m:
        return None
    try:
        speed = float(m.group("speed"))
    except (TypeError, ValueError):
        return None
    return speed if 0.1 <= speed <= 10 else None


def voice_has_speed(voice: Any) -> bool:
    """配音名称/字段里是否写了显式倍速。"""
    return voice_name_speed(voice) is not None


def default_voice_tempo(rules: dict[str, Any], voice: Any) -> float:
    """口播后处理倍速：名称写了用名称的，否则用品类默认（卫品 1.2）。不叠加两次。"""
    named = voice_name_speed(voice)
    if named is not None:
        return float(named)
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


def _strip(text: str, words) -> str:
    for word in words or []:
        if word:
            text = text.replace(word, "")
    return text


def _scene_sentence(scene: dict[str, Any], text: str) -> str:
    """去空白后再去掉 ``text_ignore``（「卫生巾」里的「卫生」不算干净卫生卖点）。"""
    return _strip(re.sub(r"\s+", "", str(text or "")), scene.get("text_ignore"))


def scene_for(rules: dict[str, Any], text: str, shot: Optional[dict[str, Any]] = None) -> Optional[dict]:
    """这一句 + 选中的镜头命中哪条场景规则（按规则顺序取第一条）。

    文案关键词和镜头内容都要对上：只看文案会把「吸收快」这种卖点句也当成吸水实验，
    只看镜头会把背景里有液体的镜头当成实验。没有文案关键词的规则（如正面指产品）只看镜头。
    ``text_forces_visual`` 的规则（拆包）文案命中就算：选中的镜头没有该动作时由场景后处理换镜。
    """
    pointing = bool((shot or {}).get("pointing_to_text") or (shot or {}).get("explicit_pointing"))
    phase = str((shot or {}).get("action_phase") or "").strip().lower()
    for scene in rules.get("scenes") or []:
        sentence = _scene_sentence(scene, text)
        text_kw = scene.get("text_keywords") or []
        shot_kw = scene.get("shot_keywords") or []
        # 只管动作正在进行的镜头：静态陈列不存在「切在动作中途」的问题。
        if scene.get("action_phases") and phase not in scene["action_phases"]:
            continue
        if scene.get("kind") == "require_visual" or scene.get("text_forces_visual"):
            if _hits(sentence, text_kw):
                return scene
            continue
        text_ok = _hits(sentence, text_kw) if text_kw else True
        shot_ok = ((shot_has_visual(scene, shot) if shot_kw or scene.get("shot_exclude_keywords")
                    else True)
                   or (scene.get("id") == "point_front" and pointing))
        if text_ok and shot_ok:
            return scene
    return None


def text_hits_scene(scene: dict[str, Any], text: str) -> bool:
    return _hits(_scene_sentence(scene, text), scene.get("text_keywords") or [])


def strip_shot_ignore(scene: dict[str, Any], text: str) -> str:
    """去掉 ``shot_ignore``（「未拆封」里的「拆封」不是拆包动作）。"""
    return _strip(str(text or ""), scene.get("shot_ignore"))


def _scene_looks(scene: dict[str, Any], shot: dict[str, Any] | None) -> str:
    return strip_shot_ignore(scene, _haystack(shot, scene.get("shot_fields")))


def shot_has_visual(scene: dict[str, Any], shot: dict[str, Any] | None) -> bool:
    """镜头是否带这个场景要的画面。

    ``shot_keywords`` 命中才算有；``shot_exclude_keywords`` 命中则一票否决
    （例如透光轻薄镜头描述里写了「吸水层」，不能当成倒液吸水实验）。
    """
    looks = _scene_looks(scene, shot)
    if _hits(looks, scene.get("shot_exclude_keywords") or []):
        return False
    keywords = scene.get("shot_keywords") or []
    return (not keywords) or _hits(looks, keywords)


def shot_keyword_hits(scene: dict[str, Any], shot: dict[str, Any] | None) -> int:
    if not shot_has_visual(scene, shot):
        return 0
    looks = _scene_looks(scene, shot)
    return sum(1 for k in scene.get("shot_keywords") or [] if k and k in looks)


def shot_extends(scene: dict[str, Any], shot: dict[str, Any] | None) -> bool:
    """镜头在场景动作之外还拍到了 ``extend_keywords``（拆包后接着展开看面层）。"""
    return _hits(_scene_looks(scene, shot), scene.get("extend_keywords") or [])


# 这些出现在 evidence/action 里时，包装多半只是背景，不能当成「又选中袋面」。
_STATIC_PACK_BODY_MARKERS = (
    "抚摸面层", "面层特写", "面层卖点", "完整展示巾体", "展开巾体", "翻面展示",
    "荧光测试", "检测仪", "检测笔", "紫光", "倒水", "倾倒", "蓝水", "蓝色液体",
    "吸水实验", "倒液", "擦拭", "对光展示", "光线透过", "按压面层", "拉伸腰",
)


def static_pack_shot(scene: dict[str, Any], shot: dict[str, Any] | None) -> bool:
    """只拍了包装静态（袋面、未拆封展示），没有撕开、取出动作。

    背景里提到「包装」、或巾体/面层/检测演示画面里顺带露出包装，都不算袋面连拍。
    """
    if not shot or not scene.get("static_keywords"):
        return False
    if shot_has_visual(scene, shot):
        return False
    if not _hits(_haystack(shot, scene.get("shot_fields")), scene["static_keywords"]):
        return False
    focus = " ".join([
        str(shot.get("action") or ""),
        str(shot.get("object") or ""),
        " ".join(str(v) for v in (shot.get("evidence_tags") or []) if v),
        " ".join(str(v) for v in (shot.get("visual_tags") or []) if v),
    ])
    if any(marker in focus for marker in _STATIC_PACK_BODY_MARKERS):
        return False
    # 主语应是包装本身：evidence/action 里要有包装展示信号，避免只靠 description 里的背景词。
    pack_subject = any(word in focus for word in (
        "包装", "袋面", "未拆封", "外包装", "密封", "独立包装"))
    return pack_subject


def followup_scene(rules: dict[str, Any], text: str, previous_shot: dict[str, Any] | None,
                   shot: dict[str, Any] | None = None) -> Optional[dict]:
    """上一句是袋面/外包装静态时，这一句不要再叠同款包装展示，改配拆包动作镜。

    触发任一即可：
    - 文案仍在讲包装（``followup_keywords``）；
    - 或这一句选中的又是袋面/外包装静态（产品展示一直拍包装时，优先换成取出产品）。

    荧光、面层、吸水等有专属强制场景的句子，以及品牌/促销/报数量句，由调用方排除，
    避免把「不含荧光剂」拐去拆包，也避免结尾外包装被换走。
    """
    for scene in rules.get("scenes") or []:
        words = scene.get("followup_keywords") or []
        if not static_pack_shot(scene, previous_shot):
            continue
        pack_copy = bool(words) and _hits(_scene_sentence(scene, text), words)
        again_pack = static_pack_shot(scene, shot)
        if pack_copy or again_pack:
            return scene
    return None


def _rule_text(rule: Any) -> str:
    return str(rule.get("text") or "") if isinstance(rule, dict) else str(rule or "")


def prompt_rules(manifest: dict[str, Any] | None, kind: str = "match") -> list[str]:
    rules = for_manifest(manifest)
    return [text for text in map(_rule_text, rules.get(f"{kind}_prompt_rules") or []) if text]


def keyed_analysis_rules(manifest: dict[str, Any] | None) -> list[str]:
    """改了就要整份重看素材的分析要求（纯文字写法）。"""
    rules = for_manifest(manifest).get("analysis_prompt_rules") or []
    return [str(r) for r in rules if not isinstance(r, dict) and str(r or "")]


def analysis_rechecks(manifest: dict[str, Any] | None) -> list[tuple[str, list[str]]]:
    """写成 ``{"text", "recheck"}`` 的分析要求：新增或改动时不作废整份视觉缓存，
    只重看旧描述里出现 ``recheck`` 词的镜头；``recheck`` 为空就只管以后新分析的镜头。"""
    rules = for_manifest(manifest).get("analysis_prompt_rules") or []
    return [(_rule_text(r), [str(w) for w in r.get("recheck") or [] if w])
            for r in rules if isinstance(r, dict) and _rule_text(r)]


def _shot_observe_blob(shot: dict[str, Any] | None) -> str:
    """镜头已观察文本，供卖点标签规则匹配；不含文件路径（避免吃到人工文件夹名）。"""
    if not isinstance(shot, dict):
        return ""
    parts: list[str] = []
    for field in ("description", "visual_description", "action", "object", "result",
                  "visual_tags", "evidence_tags", "readable_claims"):
        value = shot.get(field)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if v)
        elif value:
            parts.append(str(value))
    for seg in shot.get("action_segments") or []:
        if isinstance(seg, dict):
            parts.append(f"{seg.get('action') or ''}{seg.get('result') or ''}")
    return "。".join(parts)


# 否定前缀：词出现在这些后面时不算「看见了」。
_LABEL_DENIAL_PREFIXES = (
    "未看到", "没有看到", "未见", "未进行", "未做", "没有", "不存在", "无", "未",
)
_PACK_PRINT_MARKERS = ("字样", "印有", "印着", "标注", "写着", "卖点图标")
_PACK_ONLY_MARKERS = ("只展示包装", "仅展示包装", "包装正面", "未拆封展示", "密封包装")
# 仅包装印字不应推出的动作/结构卖点规则 id。
_ACTION_LABEL_IDS = frozenset({
    "surface", "waist", "side_leak", "thin_breath", "absorb", "fluorescence",
})
_ACTION_EVIDENCE = (
    "拉伸", "撑开", "抚摸", "撕开", "取出", "倾倒", "倒入", "倒水", "检测笔",
    "紫光", "对光", "捏薄", "扇形", "展开",
)


def _keyword_affirmed(blob: str, word: str) -> bool:
    """``word`` 在观察文本里出现，且该处不是「未看到/没有…」否定。"""
    if not word or word not in blob:
        return False
    start = 0
    while True:
        found = blob.find(word, start)
        if found < 0:
            return False
        left = blob[max(0, found - 8):found]
        if any(left.endswith(p) for p in _LABEL_DENIAL_PREFIXES):
            start = found + 1
            continue
        return True


def _pack_print_only(blob: str) -> bool:
    """描述只说包装上印着卖点字，没有动作/结构演示证据。"""
    printed = any(m in blob for m in _PACK_PRINT_MARKERS)
    pack_only = any(m in blob for m in _PACK_ONLY_MARKERS)
    if not (printed or pack_only):
        return False
    return not any(a in blob for a in _ACTION_EVIDENCE)


def _fluorescence_observed(shot: dict[str, Any], blob: str) -> bool:
    """设备名称只是线索；派生读数标签还须已有接触/照射与数值观察。"""
    if shot.get("text_pass") in ("pending", "failed"):
        return False
    device = any(_keyword_affirmed(blob, word) for word in (
        "荧光测试", "荧光检测", "检测笔", "紫光"))
    contact = any(_keyword_affirmed(blob, word) for word in (
        "贴着", "贴在", "贴近", "接触", "照着", "照射", "抵着", "压在"))
    reading = any(_keyword_affirmed(blob, word) for word in ("读数", "数值", "数字"))
    unreadable = any(word in blob for word in (
        "读数不清", "读数不可见", "数值不清", "无法读取", "看不清读数", "读数未显示"))
    return device and contact and reading and not unreadable


def _strip_derived_labels(shot: dict[str, Any]) -> None:
    """撤回上次由规则派生的标签，再按当前规则重算（模型原事实保留）。"""
    sources = shot.get("shot_label_sources")
    if not isinstance(sources, dict) or not sources:
        return
    derived = {str(t) for t, src in sources.items() if str(src).startswith("derived:")}
    if not derived:
        return

    def keep(tags: list) -> list[str]:
        return [str(t) for t in tags if t and str(t) not in derived]

    shot["evidence_tags"] = keep(list(shot.get("evidence_tags") or []))
    shot["visual_tags"] = keep(list(shot.get("visual_tags") or []))
    shot["shot_labels"] = keep(list(shot.get("shot_labels") or []))
    shot["shot_label_sources"] = {
        str(t): str(src) for t, src in sources.items() if str(t) not in derived
    }


def annotate_shot_labels(shot: dict[str, Any], rules: dict[str, Any] | None = None,
                         *, category: str = "") -> list[str]:
    """按规则包 ``shot_label_rules`` 把可见卖点补进 ``evidence_tags`` / ``visual_tags``。

    只根据画面描述和已有标签推断，**不读素材路径/文件夹名**。返回本次新加的标签。
    否定句（未看到腰头）、仅包装印字、普通计时器不会变成动作/荧光证据。
    派生标签记在 ``shot_label_sources``（``derived:<rule_id>``），再次调用会先撤回再重算。
    """
    if not isinstance(shot, dict):
        return []
    pack = rules if isinstance(rules, dict) else load(category) if category else {}
    _strip_derived_labels(shot)
    blob = _shot_observe_blob(shot)
    if not blob:
        return []
    evidence = [str(t) for t in (shot.get("evidence_tags") or []) if t]
    visual = [str(t) for t in (shot.get("visual_tags") or []) if t]
    known = {t.casefold() for t in evidence + visual}
    sources = dict(shot.get("shot_label_sources") or {}) if isinstance(
        shot.get("shot_label_sources"), dict) else {}
    pack_only = _pack_print_only(blob)
    added: list[str] = []
    for rule in pack.get("shot_label_rules") or []:
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("id") or "")
        if rule_id == "fluorescence" and not _fluorescence_observed(shot, blob):
            continue
        if pack_only and rule_id in _ACTION_LABEL_IDS:
            continue
        exclude = [str(w) for w in rule.get("exclude_any") or [] if w]
        if exclude and any(w in blob for w in exclude):
            continue
        require_any = [str(w) for w in rule.get("require_any") or [] if w]
        if require_any and not any(_keyword_affirmed(blob, w) for w in require_any):
            continue
        require_all = [str(w) for w in rule.get("require_all") or [] if w]
        if require_all and not all(_keyword_affirmed(blob, w) for w in require_all):
            continue
        for tag in rule.get("emit_tags") or []:
            text = str(tag or "").strip()
            if not text or text.casefold() in known:
                continue
            evidence.append(text)
            visual.append(text)
            known.add(text.casefold())
            added.append(text)
            sources[text] = f"derived:{rule_id or 'rule'}"
    if added:
        shot["evidence_tags"] = evidence
        shot["visual_tags"] = visual
        shot["shot_labels"] = list(dict.fromkeys(
            [*(shot.get("shot_labels") or []), *added]))
        shot["shot_label_sources"] = sources
    return added
