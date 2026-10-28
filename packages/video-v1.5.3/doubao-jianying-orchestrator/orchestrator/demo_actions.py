"""DEMO_ACTIONS V0.2 —— 匹配层的卖点/动作/证据闸门（2026-09-16 用户定案）。

来源：ref1–ref6 共 81 镜逐镜实证（`evidence_ref1-6.json`），规则表见
`DEMO_ACTIONS_V0.2.md`。本模块是那张表的可执行形式。

要解决的问题（用户原话「镜头太多重复，只有展示外包装的画面，关键卖点画面
也没有展示」）不是「有没有动作」，而是「**动作是否在证明卖点**」：

  · 参考片 81/81 镜都有可辨认动作（has_visible_action），但只有 47 镜的动作
    与对应口播相关，只有 17 镜构成直接证据。
  · 我们把量感/陈列类镜头（低证据档）配给了**功能卖点句**，而参考片把同类
    镜头配给的是**促销句**。旧 CLAIM_SYNONYMS 做的是名词性描述匹配，参考片
    的匹配单位是**动词**（揉、倒、拉、粘、比、蹭、装），结构上对不上。

三条硬规则（对应 DEMO_ACTIONS_V0.2 §4/§5/§7）：

  ① 证据分级：只有 role=direct_evidence 且 strength=A 的动作才可承担
     evidence_required=true 的句子（A = 动作的结果在画面内直接可见）。
  ② 反重复：禁止的是「同一机位 + 同一构图 + 同一动作阶段」**连续**出现；
     不同角度/不同动作阶段的同一动作**必须放行**（参考片 ref1#13→#14 就是
     连续两镜拉伸，属于正确做法，不能被误杀）。
  ③ 素材缺口：无合格动作且无相关产品特写时输出 MATERIAL_GAP；允许相关
     产品特写作为**可接受降级**，但禁止回退到无关包装展示或空镜。

向后兼容：旧分析结果没有 role/has_subject 字段时本闸门不拦（status="ungated"），
并把覆盖率写进报告 —— 不静默放过，而是让「这一批分析有没有被闸门覆盖」可审计。
"""

from __future__ import annotations

import math
import re
from typing import Any

# ── 角色与强度序 ──────────────────────────────────────────────────────
# 数值只用于「不低于」比较；顺序即门槛语义。
ROLE_RANK: dict[str, int] = {
    "product_display": 0,
    "context": 1,
    "CTA": 1,
    "visual_metaphor": 1,   # 隐喻**不高于**场景类：永远不能单独支撑卖点
    "usage_demo": 2,
    "direct_evidence": 3,
}
STRENGTH_RANK: dict[str, int] = {"C": 0, "B": 1, "A": 2}

# 动作阶段（行动作阶段只用于反重复判定，不参与证据强度）
ACTION_PHASES = ("prep", "perform", "result", "static")

# 这是证据合同 schema 的版本，不是发布包版本。旧参考片结论只记录来源，
# 不再决定新增素材是否可能满足卖点。
EVIDENCE_CONTRACT_VERSION = "1"


class Claim:
    """一条原子卖点。字段语义见 DEMO_ACTIONS_V0.2 §1/§6。"""

    __slots__ = ("claim_id", "name", "action", "object", "result", "role",
                 "evidence_required", "evidence_status", "keywords")

    def __init__(self, claim_id: str, name: str, action: str | None, obj: str | None,
                 result: str | None, role: str, evidence_required: bool,
                 evidence_status: str, keywords: tuple[str, ...]):
        self.claim_id = claim_id
        self.name = name
        self.action = action
        self.object = obj
        self.result = result
        self.role = role
        self.evidence_required = evidence_required
        self.evidence_status = evidence_status   # direct|none|metaphor|comparative|usage|context|display
        self.keywords = keywords

    @property
    def contract_defined(self) -> bool:
        """功能合同必须先定义动作、对象和结果；参考片未出现不等于缺素材。"""
        return not self.evidence_required or bool(self.action and self.object and self.result)

    @property
    def material_gap(self) -> None:
        """兼容旧读取方；静态卖点尚未检查当前素材，缺口状态只能是未知。"""
        return None

    @property
    def contract_kind(self) -> str:
        if not self.evidence_required:
            return "contextual"
        if not self.contract_defined:
            return "undefined"
        if self.claim_id == "C-GUARD-EXIST":
            return "structural"
        if self.evidence_status == "comparative":
            return "comparative"
        return "physical"

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id, "claim_name": self.name,
            "required_action": self.action, "required_object": self.object,
            "required_result": self.result, "required_role": self.role,
            "evidence_required": self.evidence_required,
            "evidence_status": self.evidence_status,
            "reference_evidence_status": self.evidence_status,
            "material_gap": self.material_gap,
            "evidence_contract": {"schema": "claim_evidence", "version": EVIDENCE_CONTRACT_VERSION,
                                  "kind": self.contract_kind,
                                  "status": "defined" if self.contract_defined else "undefined"},
        }


# ── 原子卖点表（23 条，拆自 V0.1 的 16 行复合卖点）─────────────────────
# `evidence_status` 是**语料实测**结论，不是配置偏好：
#   direct      ref1–ref6 里有动作结果可见的镜头，可承担证明
#   comparative 需要与参照物同框比较才成立（如薄度须与手指同框）
#   metaphor    只有装置/道具示意，**不得**当事实证明
#   none        旧语料未拍到；不表示当前/未来素材永久无法证明。
# 动作未定义的合同保持 CONTRACT_UNDEFINED，待定义可验证判据；不得冒充
# 已检查素材后的 MATERIAL_GAP，也不得靠相关主题词直接认证。
CLAIMS: tuple[Claim, ...] = (
    Claim("C-ABSORB-SPEED", "吸收速度（瞬吸）", "倾倒", "液体+巾体/裤体", "液体被吸入",
          "direct_evidence", True, "direct",
          ("瞬吸", "吸得快", "快速吸", "吸收速度", "一下就吸", "迅速吸", "马上吸")),
    Claim("C-ABSORB-CAPACITY", "吸收容量", "展示", "已吸水巾体", "液体锁在中央不扩散",
          "direct_evidence", True, "direct",
          ("量大", "吸得多", "容量", "锁得住", "一晚上", "一整晚", "吸量")),
    Claim("C-DRY-SURFACE", "表面干爽 / 不反渗", "按压", "已吸水巾体+干燥对照物", "接触面无水渗出",
          "direct_evidence", True, "direct",
          ("干爽", "反渗", "不回渗", "表面干", "不湿", "渗出来", "回渗")),
    Claim("C-LEAK-SIDE", "防侧漏（效果）", None, None, None,
          "direct_evidence", True, "none",
          ("侧漏", "防漏", "漏出来", "不漏")),
    Claim("C-GUARD-EXIST", "立体护边存在（结构）", "拉开", "侧边", "立体护边立起",
          "direct_evidence", True, "direct",
          ("立体护边", "防漏边", "护围", "立体边", "侧边")),
    Claim("C-SOFT", "面层柔软", None, None, None,
          "direct_evidence", True, "none",
          # 不用单字「软」「舒服」：会把「不舒服」里的「舒服」误判成柔软。
          ("柔软", "超软", "软乎", "软软", "舒适", "亲肤感", "丝滑", "细腻")),
    Claim("C-BREATHABLE", "透气", "空气穿透", "巾体+气流装置", "气流穿透巾体可见",
          "direct_evidence", True, "metaphor",
          ("透气", "不闷", "闷闷", "闷热", "捂着", "闷得慌")),
    Claim("C-THIN", "薄", "捏/抚", "巾体+手指", "与手指同框厚度对比可见",
          "usage_demo", True, "comparative",
          ("超薄", "薄", "轻薄")),
    Claim("C-ELASTIC", "弹性", "拉伸", "裤体/腰围", "可见弹性形变",
          "direct_evidence", True, "direct",
          ("弹力", "弹性", "拉伸", "怎么动", "不勒", "回弹")),
    Claim("C-WRAP", "全包裹式设计", None, None, None,
          "direct_evidence", True, "none",
          ("全包裹", "包裹式", "包住", "包裹")),
    Claim("C-FIT", "尺寸适配", None, None, None,
          "direct_evidence", True, "none",
          ("尺寸", "适配", "码数", "合身", "多少斤", "斤")),
    Claim("C-REOPEN", "重复粘贴", "反复撕开再粘", "同一粘扣", "可重复粘合，反复撕贴仍能粘住",
          "direct_evidence", True, "direct",
          ("反复粘", "重复粘", "可调节", "调节", "撕开再粘", "多次粘")),
    Claim("C-BOND", "粘合强度（粘得牢）", None, None, None,
          "direct_evidence", True, "none",
          ("粘牢", "粘得牢", "牢固", "不脱落", "粘得紧")),
    Claim("C-EASY-WEAR", "穿脱便利（不用脱裤子）", None, None, None,
          "direct_evidence", True, "none",
          ("不用脱裤子", "方便更换", "好穿", "穿脱", "更换")),
    Claim("C-NO-CHAFE", "不摩擦 / 亲肤", None, None, None,
          "direct_evidence", True, "none",
          # 「不舒服」留给透气/综合体感，避免「闷闷的不舒服」误进磨腿卖点。
          ("摩擦", "磨腿", "不磨", "磨裆", "勒腿", "亲肤")),
    Claim("C-COTTON", "100%纯棉", None, None, None,
          "direct_evidence", True, "none",
          ("纯棉", "100%棉", "全棉", "棉花")),
    Claim("C-MULTI-EQUIV", "一条顶3片", None, None, None,
          "direct_evidence", True, "metaphor",
          ("顶3片", "顶三片", "一条顶", "抵3片")),
    Claim("C-SPEC", "规格（每包几片）", None, None, None,
          "direct_evidence", True, "none",
          ("每包", "片数", "多少片", "一片装", "片装")),
    # 以下 5 条不要求直接证据：参考片把量感/陈列/形态镜头配给促销句与形态句，
    # 这是**正确做法**，不能被上面的闸门误杀（见 DEMO_ACTIONS_V0.2 §3）。
    Claim("C-SCENE", "使用场景", "塞入/比位置", "产品+包/腿", "场景动作完整",
          "usage_demo", False, "usage",
          ("睡觉", "上学", "久坐", "夜间", "经期", "平时", "外出")),
    Claim("C-BULK", "囤货量感", "堆叠/装箱/举一提", "多包产品(+纸箱)", "量感",
          "product_display", False, "context",
          ("囤", "一箱", "一提", "半年", "够用", "量都够", "囤货")),
    Claim("C-PROMO", "促销活动", "排列/推向镜头", "一整排产品", "数量可见",
          "product_display", False, "context",
          ("活动", "福利", "拍一发", "优惠", "周年庆", "到手", "价格", "搞活动")),
    Claim("C-BRAND", "品牌背书", "举起", "包装", "包装正面对镜头",
          "product_display", False, "display",
          ("官方", "旗舰", "品牌", "七度空间", "大牌")),
    Claim("C-FORM", "产品形态认知", "掏出/撕开/展开/竖举", "裤型产品/巾体", "由折叠到展开",
          "usage_demo", False, "usage",
          ("安睡裤", "萌睡裤", "裤型", "形态", "长什么样", "款式")),
)

CLAIMS_BY_ID: dict[str, Claim] = {c.claim_id: c for c in CLAIMS}

# 关键词按长度降序，保证「超薄」先于「薄」、「不闷」先于「闷」命中。
_KEYWORDS: tuple[tuple[str, str], ...] = tuple(sorted(
    ((kw, claim.claim_id) for claim in CLAIMS for kw in claim.keywords),
    key=lambda pair: -len(pair[0])))


def _keyword_in_text(keyword: str, blob: str) -> bool:
    """Substring hit with a few Chinese negation guards.

    「不舒服」must not fire soft keywords that live inside the negated phrase.
    """
    if not keyword or keyword not in blob:
        return False
    if keyword in ("舒服", "舒适") and f"不{keyword}" in blob:
        # Only reject when every occurrence is inside the negated form.
        if blob.count(keyword) <= blob.count(f"不{keyword}"):
            return False
    return True


def infer_claim_ids(text: str) -> list[str]:
    """把一句口播映射到原子卖点。命中顺序稳定（长的关键词优先）。"""
    blob = str(text or "")
    if not blob.strip():
        return []
    hits: list[str] = []
    for keyword, claim_id in _KEYWORDS:
        if _keyword_in_text(keyword, blob) and claim_id not in hits:
            hits.append(claim_id)
    return hits


def _category_rules(category: str) -> dict[str, Any]:
    if not category:
        return {}
    from . import rulepack
    return rulepack.load(rulepack.normalize_category(category) or category)


def requirements_for(text: str, claim_text: str = "", category: str = "") -> list[Claim]:
    """一句话要求的全部卖点（正文与 claim_text 合并去重）。

    给了品类时去掉该品类不适用的卖点：纸品的「大容量」是包装张数，不是卫生巾吸收容量。
    """
    ids = list(dict.fromkeys(infer_claim_ids(text) + infer_claim_ids(claim_text)))
    excluded = set(_category_rules(category).get("excluded_claims") or [])
    return [CLAIMS_BY_ID[cid] for cid in ids if cid not in excluded]


# 文案意图先于候选排序。意图只用于同等匹配候选之间的角色偏好，不得越过
# 直接证据/相关性/时长等硬闸门；这样 CTA 会偏向多包/陈列，功能卖点会偏向
# 动作与结果，但不会因为“看起来像 CTA”而放行无关画面。
_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cta", ("优惠", "福利", "活动", "到手", "下单", "购买", "买", "赠", "送",
              "划算", "囤", "库存", "不多", "链接", "入手", "多少钱", "什么价",
              "这个价", "这个数", "闭眼冲", "闭眼抽")),
    ("quantity", ("六大卷", "几卷", "多卷", "多少卷", "一包", "一提", "一箱", "几包",
                   "几提", "多少片", "片装", "数量", "规格", "大分量", "尺寸")),
    ("effect", ("瞬吸", "吸收", "干爽", "反渗", "湿水", "不掉屑", "柔软", "亲肤",
                 "透气", "厚实", "加厚", "耐用", "结实", "防漏", "侧漏")),
    ("usage", ("擦手", "擦脸", "擦桌", "擦拭", "清洁", "使用", "展开", "撕开",
                "按压", "揉搓", "拉伸", "抽出", "抽取", "一抽")),
    ("brand", ("品牌", "联名", "官方", "旗舰", "七度空间", "心相印")),
    # 参考片开场：痛点/共鸣/种草钩子没有功能卖点词，仍配产品展示或手持入画，
    # 不当成「素材待补」降级（「选不好」「谁懂啊」「生理期要到了」）。
    ("opening", ("今天", "看看", "开箱", "实测", "测给你看", "不信",
                 "谁懂", "选不好", "生理期", "姐妹们", "用一次就会爱", "要到了")),
)
# 「抽」「张」「片」前面带数字才是包装数量（70抽、100片、几抽）；「单手一抽一张」是抽取动作。
# 片数/抽数对齐包装字样时只用抽|片|张|pcs；包|提|卷|箱留给 quantity 意图，避免「一包…一百四十抽」误出 1。
_NUMBER_TOKEN = r"(?:\d+|[一二两三四五六七八九十百千万零〇几]+)"
_SHEET_COUNT_RE = re.compile(rf"({_NUMBER_TOKEN})\s*(抽|片|张|pcs)", re.I)
_COUNT_RE = re.compile(rf"({_NUMBER_TOKEN})\s*(抽|片|张|包|提|卷|箱|pcs)", re.I)
_LAYERS_RE = re.compile(rf"({_NUMBER_TOKEN})\s*层")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_count_to_arabic(token: str) -> str:
    """把口播里的中文数量收成阿拉伯数字，便于和包装 PCS/抽 字样对齐。"""
    raw = str(token or "").strip()
    if not raw:
        return raw
    if raw.isdigit():
        return str(int(raw))
    if "几" in raw:
        return raw
    if not any(unit in raw for unit in "十百千万"):
        return "".join(str(_CN_DIGITS[ch]) for ch in raw) if all(
            ch in _CN_DIGITS for ch in raw) else raw
    total = section = num = 0
    for ch in raw:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
        elif ch == "万":
            total += (section + num or 1) * 10000
            section = 0
            num = 0
        else:
            return raw
    return str(total + section + num)


def _count_text(text: str) -> str:
    # 「单手一抽一张」描述一次抽取动作，不是包装规格或 CTA 数量。
    return re.sub(r"(?:单手)?一抽一(?:张|片)", "抽取动作", re.sub(r"\s+", "", str(text or "")))


def _number_units(text: str) -> set[tuple[str, str]]:
    blob = _count_text(text)
    return {(_chinese_count_to_arabic(m.group(1)), "sheet")
            for m in _SHEET_COUNT_RE.finditer(blob)} | {
                (_chinese_count_to_arabic(m.group(1)), "layer")
                for m in _LAYERS_RE.finditer(blob)}


def count_mentions(text: str) -> list[str]:
    """句子里报的包装抽/片/张数字（「70抽大容量」→ ``["70"]``，「一百四十抽」→ ``["140"]``）。"""
    out: list[str] = []
    for m in _SHEET_COUNT_RE.finditer(_count_text(text)):
        out.append(_chinese_count_to_arabic(m.group(1)))
    return out


def infer_intents(text: str, claim_text: str = "", category: str = "") -> list[str]:
    """返回完整语义段的意图标签，顺序稳定且允许多意图共存。"""
    blob = re.sub(r"\s+", "", f"{text or ''}{claim_text or ''}")
    if not blob:
        return []
    extra = _category_rules(category).get("intent_keywords") or {}
    out = []
    for intent, keywords in _INTENT_KEYWORDS:
        words = (*keywords, *(extra.get(intent) or ()))
        if any(keyword in blob for keyword in words) or (intent == "quantity" and _COUNT_RE.search(_count_text(blob))):
            out.append(intent)
    return out


def classify_intent(text: str, claim_text: str = "", category: str = "") -> str:
    """返回主意图；多意图段仍通过 ``infer_intents`` 全量落盘。"""
    return (infer_intents(text, claim_text, category) or ["unknown"])[0]


def _strip_product(value: str, product: str) -> str:
    value = re.sub(r"\s+", "", str(value or ""))
    product = re.sub(r"\s+", "", str(product or ""))
    return value.replace(product, "") if product else value


def sentence_contract(text: str, claim_text: str = "", *, category: str = "",
                      product: str = "") -> dict[str, Any]:
    """一句口播的选镜合同：品类、意图、卖点和要配的画面主题，各阶段共用这一份。

    命中 ``sentence_topics``（抽取、挂墙、厚实不破、湿度、擦拭、冲散、包装标称、张数……）
    的是功能句，只能配同主题的画面；其余是展示句（开场、品牌、促销、「真的绝」），
    可以用产品展示衔接。商品名本身（「纯水湿厕纸」）不算卖点。
    """
    rules = _category_rules(category)
    blob = _strip_product(f"{text or ''}{claim_text or ''}", product)
    topics = []
    for topic in rules.get("sentence_topics") or []:
        hits = [k for k in topic.get("text_keywords") or [] if k and k in blob]
        if topic.get("match") == "count" and count_mentions(blob):
            hits.append("count")
        if hits:
            topics.append({"id": topic.get("id"), "label": topic.get("label"),
                           "match": topic.get("match") or "keywords", "hits": hits,
                           "focus_priority": int(topic.get("focus_priority") or 0),
                           "shot_keywords": list(topic.get("shot_keywords") or []),
                           "shot_exclude_keywords": list(topic.get("shot_exclude_keywords") or []),
                           "contradicts": dict(topic.get("contradicts") or {})})
    return {
        "category": rulepack_category(category),
        "product": product,
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "claim_evidence_contracts": {c.claim_id: c.as_dict()["evidence_contract"]
                                     for c in requirements_for(text, claim_text, category)},
        "intents": infer_intents(text, claim_text, category),
        "claims": [c.claim_id for c in requirements_for(text, claim_text, category)],
        "counts": count_mentions(blob),
        "sheet_counts": count_mentions(blob),
        "number_units": [list(pair) for pair in sorted(_number_units(blob))],
        "topics": topics,
        "kind": "functional" if topics else "display",
    }


def is_demo_contract(contract: dict[str, Any] | None) -> bool:
    """这句要演示效果（抽、挂、拉、擦、湿度、冲散……），不是包装标称、张数或展示。"""
    return any(t.get("match") not in ("count", "same_words")
               for t in (contract or {}).get("topics") or [])


def rulepack_category(category: str) -> str:
    if not category:
        return ""
    from . import rulepack
    return rulepack.normalize_category(category) or str(category)


# 报张数的句子：没有同一个数字时，能看出「多包、成排」的画面算相关，光一个纸箱不算。
_COUNT_RELATED = ("多包", "多提", "成排", "一排", "排列", "数量展示", "量感")
_FIT_FIELDS = ("description", "visual_description", "action", "object", "result",
               "visual_tags", "evidence_tags", "readable_claims")


def _observed_text(shot: dict[str, Any] | None, contract: dict[str, Any],
                   fields: tuple[str, ...]) -> str:
    values: list[str] = []
    for field in fields:
        values.extend(_flatten_observation((shot or {}).get(field)))
    return _strip_product("。".join(values), str(contract.get("product") or "")).lower()


# 包装上印的张数（「170抽」「70PCS」）；和口播报的数字不同就是另一个规格。
_SHOWN_COUNT_RE = _SHEET_COUNT_RE

# 「无酒精」「不含荧光剂」「0添加」：否定前缀 + 标称本体。
_LABEL_NEGATIONS = ("不含", "没有", "无", "不", "零", "0")


def _label_base(word: str) -> tuple[str, bool]:
    for prefix in _LABEL_NEGATIONS:
        if word.startswith(prefix) and len(word) > len(prefix) + 1:
            return word[len(prefix):], True
    return word, False


def _label_mentions(base: str, observed: str) -> tuple[bool, bool]:
    """包装上这个标称出现的方向：(印着否定的，印着肯定的)。"""
    negated = positive = False
    cursor = 0
    while True:
        found = observed.find(base, cursor)
        if found < 0:
            return negated, positive
        head = observed[max(0, found - 2):found]
        if any(head.endswith(p) for p in _LABEL_NEGATIONS):
            negated = True
        else:
            positive = True
        cursor = found + len(base)


def text_unverified(shot: dict[str, Any] | None) -> bool:
    """包装文字排了高清复核却没复核成：画面里读到的字、数、标称都不能当证据。"""
    return (shot or {}).get("text_pass") in ("pending", "failed")


def _has_number(number: str, observed: str) -> bool:
    if number.isdigit():
        return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", observed) is not None
    return number in observed


def contradiction(shot: dict[str, Any] | None, contract: dict[str, Any] | None) -> str:
    """画面里正面看到了口播否定的事实（不滴水却在滴水、无酒精却印着含酒精），返回原因。

    反证和缺证不同：缺证还能降级补位，反证任何路径都不能用。
    """
    if not contract or contract.get("kind") != "functional":
        return ""
    printed = _observed_text(shot, contract, _FIT_FIELDS)
    acted = _observed_text(shot, contract, tuple(f for f in _FIT_FIELDS if f != "readable_claims"))
    for topic in contract.get("topics") or []:
        if topic.get("match") == "count":
            spoken = contract.get("sheet_counts") or []
            shown = {_chinese_count_to_arabic(m.group(1))
                     for m in _SHOWN_COUNT_RE.finditer(printed)}
            if spoken and shown and not shown & set(spoken):
                return f"count:{'/'.join(sorted(shown))}≠{'/'.join(spoken)}"
            continue
        if topic.get("id") == "layers":
            expected = {tuple(p) for p in contract.get("number_units") or [] if p[1] == "layer"}
            shown = {p for p in _number_units(printed) if p[1] == "layer"}
            if expected and shown and not expected & shown:
                return "layers:PACKAGING_LAYER_COUNT_MISMATCH"
            continue
        if topic.get("match") == "same_words":
            for hit in topic.get("hits") or []:
                base, negated = _label_base(hit.lower())
                if negated and _label_mentions(base, printed)[1]:
                    return f"{topic.get('id')}:{hit}"
            continue
        for claim, words in (topic.get("contradicts") or {}).items():
            # 画面描述里原样写着「不滴水」时，里面的「滴水」不是反证。
            seen = acted.replace(claim.lower(), "。")
            if claim in (topic.get("hits") or []) and any(
                    _observed_keyword(w.lower(), seen) for w in words):
                return f"{topic.get('id')}:{claim}"
    return ""


def _topic_positive_or_excluded(shot: dict[str, Any], contract: dict[str, Any],
                                topic: dict[str, Any]) -> tuple[int, bool]:
    """Return (fit_score_for_topic, hard_excluded). hard_excluded = 排除词命中且无正面词。"""
    printed = _observed_text(shot, contract, _FIT_FIELDS)
    acted = _observed_text(shot, contract, tuple(f for f in _FIT_FIELDS if f != "readable_claims"))
    unverified = text_unverified(shot)
    observed = printed if topic.get("match") in ("count", "same_words") else acted
    if topic.get("match") == "count":
        expected = {(str(n), "sheet") for n in contract.get("counts") or []}
        if not unverified and expected & _number_units(observed):
            return 2, False
        if any(marker in observed for marker in _COUNT_RELATED) or re.search(
                r"\d+\s*(?:包|提|箱|抽|张|片|卷)", observed):
            return 1, False
        return 0, False
    if topic.get("match") == "same_words":
        if topic.get("id") == "layers":
            expected = {tuple(p) for p in contract.get("number_units") or [] if p[1] == "layer"}
            return (2 if not unverified and expected & _number_units(observed) else 0), False
        for hit in [] if unverified else topic.get("hits") or []:
            base, negated = _label_base(hit.lower())
            said_no, said_yes = _label_mentions(base, observed)
            if said_no if negated else said_yes:
                return 2, False
        return 0, False
    denied = {w.lower() for c, ws in (topic.get("contradicts") or {}).items()
              if c in (topic.get("hits") or []) for w in ws}
    excluded = any(_observed_keyword(w.lower(), observed)
                   for w in topic.get("shot_exclude_keywords") or [])
    positive = any(_observed_keyword(k.lower(), observed)
                   for k in topic.get("shot_keywords") or []
                   if k.lower() not in denied)
    if positive:
        return 1, False
    if excluded:
        return 0, True
    return 0, False


def topic_hard_excluded(shot: dict[str, Any], contract: dict[str, Any] | None) -> bool:
    """功能主题明确排除了这镜，且没有任何主题正面命中 —— AI 高分也不能抬成相关。"""
    if not contract or contract.get("kind") != "functional":
        return False
    any_positive = False
    any_hard = False
    for topic in contract.get("topics") or []:
        score, hard = _topic_positive_or_excluded(shot, contract, topic)
        if score > 0:
            any_positive = True
        if hard:
            any_hard = True
    return any_hard and not any_positive


# 选镜策略：这些主题必须以画面词命中，禁止 AI 高分把无关镜抬成 related。
KEYWORD_LOCKED_TOPICS = frozenset({
    "breath", "thin", "absorb", "fluorescence", "core", "side_leak",
    "lint_free", "texture", "layers", "count", "sheet_count", "cotton",
    "surface", "soft", "waist", "pants_open",
})
# 主题命中但常不在 AI top3 时，补到及格线参与排序的主题。
SCORE_BOOST_TOPICS = KEYWORD_LOCKED_TOPICS | frozenset({
    "surface", "fit", "cotton", "waist", "chafe", "soft",
    "lint_free", "layers", "count",
})


def opening_display_rank(shot: dict[str, Any] | None) -> tuple:
    """无卖点词开场的展示承接排序：手持入画/拆包优先于纯静置袋面。"""
    shot = shot or {}
    blob = " ".join([
        str(shot.get("description") or ""),
        " ".join(str(t) for t in (shot.get("evidence_tags") or [])),
        " ".join(str(t) for t in (shot.get("visual_tags") or [])),
        str(shot.get("action") or ""),
    ])
    active = int(any(k in blob for k in (
        "手持产品入画", "手持", "举起", "撕开包装", "取出", "入画", "托举")))
    static_only = int(any(k in blob for k in ("未拆封展示", "完整展示包装")) and not active)
    body = int(any(k in blob for k in ("完整展示巾体", "展开巾体", "面层", "翻转")))
    return (active, body, -static_only)


def surface_action_rank(shot: dict[str, Any] | None) -> int:
    """柔软/面层句：抚摸面层优先于只拆包装。"""
    shot = shot or {}
    blob = " ".join([
        str(shot.get("description") or ""),
        " ".join(str(t) for t in (shot.get("evidence_tags") or [])),
        str(shot.get("action") or ""),
    ])
    if any(k in blob for k in ("抚摸面层", "手掌抚摸", "按压面层", "面层特写", "面层卖点")):
        return 2
    if any(k in blob for k in ("撕开包装", "取出巾体", "取出产品")):
        return 0
    return 1


def lint_action_rank(shot: dict[str, Any] | None) -> int:
    """不掉絮句：揉搓/捏合优先于托展翻转。"""
    shot = shot or {}
    blob = " ".join([
        str(shot.get("description") or ""),
        " ".join(str(t) for t in (shot.get("evidence_tags") or [])),
        str(shot.get("action") or ""),
    ])
    if any(k in blob for k in ("揉搓", "捏合", "搓揉", "揉拢")):
        return 2
    if any(k in blob for k in ("翻转", "托展", "托平", "弯折")):
        return 0
    return 1


def count_readable_rank(shot: dict[str, Any] | None, contract: dict[str, Any] | None) -> int:
    """同数字且同单位才优先；高清文字复核未完成时不得提升包装候选。"""
    if text_unverified(shot):
        return 0
    spoken = {tuple(pair) for pair in (contract or {}).get("number_units") or []
              if isinstance(pair, (list, tuple)) and len(pair) == 2}
    if not spoken:
        spoken = {(str(n), "sheet") for n in (contract or {}).get("counts") or []
                  if str(n).isdigit()}
        for topic in (contract or {}).get("topics") or []:
            spoken.update(_number_units(" ".join(topic.get("hits") or [])))
    if not spoken:
        return 0
    shot = shot or {}
    blob = " ".join([
        str(shot.get("description") or ""),
        " ".join(str(t) for t in (shot.get("evidence_tags") or [])),
        " ".join(str(t) for t in (shot.get("readable_claims") or [])),
        str(shot.get("action") or ""),
    ])
    if spoken & _number_units(blob):
        return 2 if any(k in blob for k in ("指向", "特写", "侧面", "PCS", "pcs")) else 1
    return 0


# 这些词出现时即使暂时没挂上主题，也不能走开场 display_carry 抢镜。
_FUNCTIONAL_ACTION_GUARD = (
    "揉搓", "掉絮", "掉毛", "倒液", "倒水", "反渗", "透气", "对光", "荧光",
    "抽数", "PCS", "层加厚", "4层", "四层", "三层", "纯棉", "木浆",
)


def shot_fit(shot: dict[str, Any], contract: dict[str, Any] | None) -> int:
    """镜头和句子合同对不对得上：2 = 对上且是同一数字/同一标称，1 = 相关，0 = 无关或反证。

    展示句任何非空镜都算 1；功能句必须命中至少一个主题的画面词，且没有反证。
    """
    if not contract or contract.get("kind") != "functional":
        return 0 if is_empty_shot(shot or {}) else 1
    if contradiction(shot, contract):
        return 0
    best = 0
    for topic in contract.get("topics") or []:
        score, _hard = _topic_positive_or_excluded(shot, contract, topic)
        best = max(best, score)
    return best


_PRINTED_MARKERS = ("字样", "文字", "印有", "印着", "标注", "标签", "写着", "卖点图标")


def printed_only_match(claims: list[str], contract: dict[str, Any] | None) -> bool:
    """AI 判对上的理由只是包装上印着这句话，而这句要演示动作（抽、挂、拉、擦、冲散……）。

    包装标称、张数这类主题印在包装上就是证据，不算。
    """
    topics = (contract or {}).get("topics") or []
    if not topics or any(t.get("match") in ("count", "same_words") for t in topics):
        return False
    texts = [str(c) for c in claims or [] if str(c).strip()]
    return bool(texts) and all(any(m in c for m in _PRINTED_MARKERS) for c in texts)


def topic_segments(shot: dict[str, Any], contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """视觉分析切好的动作分段里，演的正是这句功能句主题的那几段（按时间顺序）。"""
    words = []
    for topic in (contract or {}).get("topics") or []:
        denied = {w.lower() for c, ws in (topic.get("contradicts") or {}).items()
                  if c in (topic.get("hits") or []) for w in ws}
        words.extend(w.lower() for w in topic.get("shot_keywords") or []
                     if w and w.lower() not in denied)
    if not words:
        return []
    out = []
    for row in (shot or {}).get("action_segments") or []:
        if not isinstance(row, dict):
            continue
        text = f"{row.get('action') or ''}。{row.get('result') or ''}".lower()
        if any(_observed_keyword(w, text) for w in words):
            out.append(row)
    return out


# 「未见破洞、散开或碎裂」「排水口无可见碎片」是在否认，同一分句里出现这些词就不算看到。
_FIT_NEGATIONS = ("未见", "没有", "没见", "未出现", "并未", "无可见", "看不到", "不见")
_CLAUSE_BREAK = re.compile(r"[，。；;,.!！?？\n]")


def _observed_keyword(keyword: str, observed: str) -> bool:
    if not keyword:
        return False
    cursor = 0
    while True:
        found = observed.find(keyword, cursor)
        if found < 0:
            return False
        clause = _CLAUSE_BREAK.split(observed[:found])[-1]
        whole = clause + _CLAUSE_BREAK.split(observed[found:])[0]
        if (not any(word in clause for word in _FIT_NEGATIONS)
                and not any(word in whole for word in _PRINTED_MARKERS)):
            return True
        cursor = found + len(keyword)


_INTENT_ROLE_PREFERENCE: dict[str, dict[str, int]] = {
    "cta": {"CTA": 6, "context": 5, "product_display": 4},
    "quantity": {"context": 6, "product_display": 5, "usage_demo": 2},
    "effect": {"direct_evidence": 6, "usage_demo": 5, "product_display": 2},
    "usage": {"usage_demo": 6, "direct_evidence": 5, "product_display": 1},
    "brand": {"product_display": 6, "context": 4},
    # 开场/痛点钩子：参考片用产品展示或手持入画，不当功能演示。
    "opening": {"product_display": 6, "context": 4, "usage_demo": 2, "direct_evidence": 1},
    "unknown": {},
}

# 包装卖点是一个很窄的直接证据例外：只有“明确指向包装上可读的同一卖点”
# 才能算 DIRECT。普通包装展示仍然只是降级/证据不足，不能把包装上的字当成
# 产品真实性或功能效果证明。
_PACKAGING_CLAIMS: dict[str, tuple[str, ...]] = {
    "ORIGINAL_WOOD_PULP": ("百分百原生木浆", "100%原生木浆", "原生木浆", "木浆"),
    "COTTON": ("100%纯棉", "百分百纯棉", "天然纯棉", "全棉", "纯棉", "100%棉"),
    "LAYERS": ("4层", "四层", "3层", "三层", "五层", "5层"),
    "FLUORESCENT_FREE": ("不含荧光剂", "无荧光剂", "不添加荧光剂"),
}
_POINTING_MARKERS = ("指向", "指着", "指到", "指给", "点到", "手指", "标注", "指示")
_QUANTITY_MARKERS = (
    "多包", "多提", "整箱", "成排", "一排", "堆叠", "堆放",
    "一提", "几提", "一箱", "几箱", "多箱", "装满产品", "摆满产品",
)


def _claim_compact(value: Any) -> str:
    """Normalize a spoken/observed claim for the narrow packaging exception."""
    text = re.sub(r"\s+", "", str(value or "")).lower()
    return text.replace("百分之百", "100%").replace("百分百", "100%").replace("％", "%")


def _flatten_observation(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def intent_role_score(shot: dict[str, Any], intents: list[str] | None = None) -> int:
    """同等候选的意图-角色偏好分；绝不替代匹配硬闸门。"""
    role = _norm_role(shot)
    return max((_INTENT_ROLE_PREFERENCE.get(intent, {}).get(role, 0)
                for intent in (intents or ["unknown"])), default=0)


# ── 素材侧字段读取 ────────────────────────────────────────────────────
def _norm_role(shot: dict[str, Any]) -> str:
    return str(shot.get("role") or shot.get("visual_role") or "").strip()


def _norm_strength(shot: dict[str, Any]) -> str:
    raw = str(shot.get("evidence_strength") or "").strip().upper()
    if raw.startswith("A"):
        return "A"
    if raw.startswith("B"):
        return "B"
    if raw.startswith("C"):
        return "C"
    return ""


def _has_subject(shot: dict[str, Any]) -> bool | None:
    """主体是否存在于画面（空镜判定）。字段缺失时返回 None（未知）。"""
    for key in ("has_subject", "subject_present"):
        if key in shot:
            return bool(shot.get(key))
    return None


def is_visual_metaphor(shot: dict[str, Any]) -> bool:
    return _norm_role(shot) == "visual_metaphor"


def is_empty_shot(shot: dict[str, Any]) -> bool:
    """空镜：明确 `has_subject=false`，或有效动作区间为空且无主体描述。

    实测口径（我方 #12 那类）：主体覆盖率 <0.026、`subject_runs` 为空。
    这里只认显式字段，不猜。
    """
    flag = _has_subject(shot)
    return flag is False


# ── 闸门 ──────────────────────────────────────────────────────────────
_TOKEN_SPLIT = re.compile(r"[+＋/、,，\s]+")
# 同一件东西的不同叫法。只做**归一**，不做同义扩展 —— 扩展会让匹配重新退化成
# 「名词像就算」，那正是要修的病。
_OBJECT_ALIASES = {
    "巾体": "巾体", "纸巾": "巾体", "面层": "巾体", "巾": "巾体", "卫生巾": "巾体",
    "液体": "液体", "水": "液体", "蓝色液体": "液体", "红色液体": "液体",
    "裤体": "裤体", "裤腰": "裤体", "腰围": "裤体", "裤型产品": "裤体",
    "粘扣": "粘扣", "魔术贴": "粘扣", "粘贴处": "粘扣",
    "侧边": "侧边", "立体护边": "侧边", "防漏边": "侧边",
    "手指": "手指", "手": "手", "手背": "手",
    "包装": "包装", "外包装": "包装",
}
# 这几条卖点的判据是**比较**：光有产品不行，必须同框出现参照物
# （参考片 ref3#7 捏巾体并与手指同框 → 薄度可对比；ref5#9 只抚摸没有参照物，
# 所以降级 usage_demo）。参照物缺失就判不成立。
_REFERENCE_REQUIRED: dict[str, tuple[str, ...]] = {
    "C-THIN": ("手指", "手"),
    "C-DRY-SURFACE": ("手", "手指", "巾体"),   # 「接触面」得真在画面里
}


def _tokens(value: Any) -> set[str]:
    out: set[str] = set()
    for part in _TOKEN_SPLIT.split(str(value or "")):
        part = part.strip()
        if not part:
            continue
        mapped = _OBJECT_ALIASES.get(part)
        if mapped:
            out.add(mapped)
            continue
        # 词表里没有整词时，看有没有已知词是它的子串 ——
        # 「已吸水巾体」要能归到「巾体」，「蓝色液体」要能归到「液体」。
        # 这一步只是**归一**，不做同义扩展。
        out.add(part)
        for raw, canon in _OBJECT_ALIASES.items():
            if raw in part:
                out.add(canon)
    return out


# 结果关键词表。判据是 `素材.result == required_result`（DEMO_ACTIONS_V0.2 §6），
# 但模型措辞必然有出入，所以用**小词表取交集**而不是字符串相等：
# 「倾倒」的镜头结果是「液体被吸入」，不该被当成「干爽」卖点的证据 ——
# 两者共享的只有「巾体」这个对象，结果本身对不上（回归 R6 抓到的洞）。
_RESULT_KEYWORDS: tuple[str, ...] = (
    "吸入", "渗出", "反渗", "扩散", "下降", "形变", "回弹", "立起",
    "粘合", "粘牢", "反复", "调节", "展开", "露出", "对比", "同框",
    "干爽", "无水", "锁在", "不扩散", "开口", "满箱", "量感", "正面",
)


def _result_keys(value: Any) -> set[str]:
    text = str(value or "")
    return {kw for kw in _RESULT_KEYWORDS if kw in text}


def _result_match(shot: dict[str, Any], claim: Claim) -> bool:
    """动作结果必须对得上，否则「证明吸收」的镜头会被当成「证明干爽」的证据。"""
    if not claim.result:
        return True
    shot_result = str(shot.get("result") or "").strip()
    if not shot_result:
        return False
    want = _result_keys(claim.result)
    got = _result_keys(shot_result)
    if want and got:
        return bool(want & got)
    # 词表覆盖不到的措辞：退回包含关系，再退回宽松放行 —— 宁可漏判也不要
    # 因为模型换了个说法就把整句判成缺素材（那会把缺口报告本身变成噪声）。
    return bool(want & _result_keys(shot_result)) or shot_result in claim.result \
        or claim.result in shot_result or not want


def _object_match(shot: dict[str, Any], claim: Claim) -> bool:
    """`素材.object ⊇ required_object`（DEMO_ACTIONS_V0.2 §6 的判据之一）。

    没有这一条，C-THIN（薄）会被**任意**一个 direct_evidence 镜头满足 ——
    镜头里根本没有手指可以对比，也照样算「证明薄」。这是交叉验证抓到的
    实现缺口：role/strength/result 三项都过，但对象不对，卖点仍然没被证明。
    """
    if not claim.object:
        return False
    shot_tokens = _tokens(shot.get("object") or shot.get("action_object"))
    if not shot_tokens:
        # 素材没给 object 字段：退回「动作必须对上」（旧结果兼容）。
        needed = _tokens(claim.action)
        shot_action = _tokens(shot.get("action"))
        return bool(needed & shot_action) if needed and shot_action else True
    required = _tokens(claim.object)
    if not (required & shot_tokens):
        return False
    refs = _REFERENCE_REQUIRED.get(claim.claim_id)
    if refs:
        # 参照物可能被模型写在 object 里（「巾体+手指」），也可能写在 result 里
        # （「与手指厚度对比」）—— 参考片 ref3#7 就属于后者。两边都找，
        # 否则会把语料里**唯一**能证明「薄」的那一镜判成不合格。
        seen = shot_tokens | _tokens(shot.get("result"))
        if not (set(refs) & seen):
            return False
    return True


def perception_reliability(shot: dict[str, Any]) -> dict[str, Any]:
    """业务例外之前的观察门槛；缺字段的旧产物兼容，显式坏值不能变成缺字段。"""
    if "analysis_confidence" not in shot or shot.get("analysis_confidence") is None:
        return {"ok": True, "reason": "LEGACY_CONFIDENCE_UNSPECIFIED",
                "basis": "legacy_observation"}
    raw = shot.get("analysis_confidence")
    try:
        confidence = float(raw)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "EVIDENCE_CONFIDENCE_INVALID"}
    if isinstance(raw, bool) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return {"ok": False, "reason": "EVIDENCE_CONFIDENCE_INVALID"}
    if confidence < 0.60:
        return {"ok": False, "reason": "EVIDENCE_CONFIDENCE_LOW", "confidence": confidence}
    return {"ok": True, "reason": "OBSERVATION_RELIABLE", "confidence": confidence,
            "basis": "analysis_confidence"}


_AIRFLOW_PRODUCT = r"(?:巾体|裤体|纸巾|面料|织物|产品本体)"
_AIRFLOW_SIGNAL = r"(?:气流|空气|通气|气泵|风机|吹气)"
_AIRFLOW_TRAVERSAL = re.compile(
    rf"(?:气流|空气).{{0,8}}(?:穿过|穿透|透过).{{0,8}}{_AIRFLOW_PRODUCT}"
    rf"|(?:气流|空气).{{0,6}}从{_AIRFLOW_PRODUCT}.{{0,6}}(?:穿过|穿透|透过)"
    rf"|{_AIRFLOW_PRODUCT}.{{0,6}}(?:被气流穿透|被空气穿透)")


def _positive_airflow(value: Any) -> bool:
    for clause in _CLAUSE_BREAK.split(re.sub(r"\s+", "", str(value or ""))):
        if not _AIRFLOW_TRAVERSAL.search(clause):
            continue
        if any(word in clause for word in (*_FIT_NEGATIONS, "并非", "不是", "无法", "不能", "未能",
                                           "未穿", "不穿", "未透", "不透", "无气流", "无空气", "不曾")):
            continue
        if any(word in clause for word in (*_PRINTED_MARKERS, "示意", "声称", "据称")):
            continue
        return True
    return False


def _breathability_observation(shot: dict[str, Any]) -> tuple[bool, str]:
    """只认可空气实际穿过产品的观察；透光、孔洞、标签或装置摆拍不是物理证明。"""
    obj = str(shot.get("object") or shot.get("action_object") or "")
    if not (re.search(_AIRFLOW_PRODUCT, obj) and re.search(_AIRFLOW_SIGNAL, obj)):
        return False, "OBJECT_MISMATCH"
    if not _positive_airflow(shot.get("action")):
        return False, "ACTION_MISMATCH"
    if not _positive_airflow(shot.get("result")):
        return False, "RESULT_MISMATCH"
    return True, "CLAIM_REQUIREMENT_MET"


def judge_claim(shot: dict[str, Any], claim: Claim) -> tuple[bool, str]:
    """单个镜头能否承担**某一条**卖点。返回 (ok, reason)。

    按 claim 逐条判而不是整句一起判，是因为一句话常常同时要求多条卖点
    （「…七度空间超薄萌睡裤」= 品牌 + 形态 + 超薄）。整句一起判会让一条
    好满足的卖点短路掉后面那条无法证明的，缺口就报不出来。
    """
    if not claim.contract_defined:
        return False, "CONTRACT_UNDEFINED"
    role = _norm_role(shot)
    strength = _norm_strength(shot)
    if claim.evidence_required:
        # 视觉模型明确给出低置信度时，只能进入证据不足/降级路径，不能被
        # role=A 这类字段单独抬成直接证据。旧结果没有该字段时保持兼容。
        reliability = perception_reliability(shot)
        if not reliability["ok"]:
            return False, reliability["reason"]
        if ROLE_RANK.get(role, -1) < ROLE_RANK.get(claim.role, 3):
            return False, "ROLE_BELOW_REQUIRED"
        if STRENGTH_RANK.get(strength, -1) < STRENGTH_RANK["A"]:
            return False, "STRENGTH_BELOW_A"
        # 「result 为空不得算 A」在**这里也要判一次**，不能只靠
        # vision_analyzer 的解析端压强度：素材分析结果可能来自旧版本、
        # 手工修订或另一条产线，闸门自己必须守住这条底线。
        # 这正是「抚摸面层 / 蹭手背 / 往身上比试」那三类镜头不能证明卖点的
        # 机器判据 —— 有动作、没可见结果。
        if not str(shot.get("result") or "").strip():
            return False, "NO_VISIBLE_RESULT"
        # 隐喻即使被模型标成 direct 也要拦：参考片里「堆棉花球证纯棉」这类
        # 镜头看起来像在证明卖点，实际什么都没证明（DEMO_ACTIONS_V0.2 §2）。
        if is_visual_metaphor(shot):
            return False, "VISUAL_METAPHOR"
        if claim.claim_id == "C-BREATHABLE":
            return _breathability_observation(shot)
        # 对象必须对上：动作作用在别的东西上，就证明不了这条卖点
        # （规范 §6 的 `素材.object ⊇ required_object`）。
        if not _object_match(shot, claim):
            return False, "OBJECT_MISMATCH"
        # 结果也要对上：动作作用对了地方，但结果不是这条卖点要的结果
        # （倾倒证明吸收，不证明干爽）。
        if not _result_match(shot, claim):
            return False, "RESULT_MISMATCH"
        return True, "CLAIM_REQUIREMENT_MET"
    # 促销/量感/形态句：允许 product_display / context 入库，
    # 但仍不接受空镜与「角色未知」。
    if not role:
        return False, "NO_ROLE_FIELD"
    return True, "CLAIM_REQUIREMENT_MET"


def judge(shot: dict[str, Any], claims: list[Claim]) -> dict[str, Any]:
    """功能卖点逐项通过；品牌、形态或容易满足的卖点不能短路其他功能。

    返回 `{status, ok, reason, claim_id, required, ...}`：
      ok=True     全部 evidence_required 卖点通过；纯展示句至少满足一条
      status="ungated"   旧分析结果没有 role/has_subject 字段，闸门不拦但要留痕
      status="blocked"   全部卖点都不满足，附 `reason` 与 `required`
    """
    if not claims:
        return {"status": "no_claim", "ok": True, "reason": ""}

    # 空镜一票否决，与卖点无关（运营「只有展示外包装的画面」的机器口径）。
    if is_empty_shot(shot):
        return {"status": "blocked", "ok": False, "reason": "EMPTY_SHOT",
                "detail": "该镜头 has_subject=false（空镜），不得进入匹配池",
                "claim_id": claims[0].claim_id}

    if not _norm_role(shot) and _has_subject(shot) is None:
        # 旧分析结果：没有视觉角色也没有主体字段。不拦，但要能让报告统计出
        # 「这一批有多少句是在无闸门状态下匹配的」。
        return {"status": "ungated", "ok": True, "reason": "NO_ROLE_FIELD",
                "detail": "视觉分析结果缺少 role/has_subject 字段（旧版本产物）",
                "claim_id": claims[0].claim_id}

    required = [claim for claim in claims if claim.evidence_required] or claims
    results = []
    for claim in required:
        ok, reason = judge_claim(shot, claim)
        results.append({"claim_id": claim.claim_id, "ok": ok, "reason": reason,
                        "required": claim.as_dict()})
    failures = [row for row in results if not row["ok"]]
    accepted = not failures if any(c.evidence_required for c in required) else any(row["ok"] for row in results)
    selected = (failures or results)[0]
    associations = []
    for claim in required:
        if claim.claim_id not in {row["claim_id"] for row in failures}:
            continue
        related, association_reason = fallback_match(shot, claim)
        if related:
            associations.append({"claim_id": claim.claim_id, "reason": association_reason,
                "evidence_contract": {"schema": "claim_evidence", "version": EVIDENCE_CONTRACT_VERSION,
                    "kind": "business_association", "status": "requires_review",
                    "formal_status": "UNCERTIFIED"}})
    payload = {"status": "ok" if accepted else "blocked", "ok": accepted,
            "reason": "CLAIM_REQUIREMENT_MET" if accepted else selected["reason"],
            "claim_id": selected["claim_id"], "required": selected["required"],
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "required_claim_ids": [c.claim_id for c in required],
            "unmet_claim_ids": [row["claim_id"] for row in failures],
            "claim_results": results,
            "business_associations": associations,
            "observed": {"role": _norm_role(shot) or None,
                         "evidence_strength": _norm_strength(shot) or None,
                         "action": shot.get("action"), "object": shot.get("object"),
                         "result": shot.get("result"),
                         "action_phase": shot.get("action_phase")}}
    return payload


def unmet_claims(shots: list[dict[str, Any]], claims: list[Claim]) -> list[Claim]:
    """这句话里**没有任何镜头能承担**的卖点（按 claim 粒度，不按句）。

    这是 MATERIAL_GAP 的真正判据：只要有一条 evidence_required 的卖点既没有
    直接证据，也没有相关产品特写降级，就要报缺口 —— 哪怕同一句话里别的卖点
    （品牌/形态）能满足。
    """
    out: list[Claim] = []
    for claim in claims:
        if not claim.evidence_required:
            continue
        if any(judge_claim(shot, claim)[0] for shot in shots):
            continue
        if any(fallback_match(shot, claim)[0] for shot in shots):
            continue
        out.append(claim)
    return out


# ── 素材缺口（禁止回退）──────────────────────────────────────────────
def material_gap(index: int, text: str, claim_text: str, claims: list[Claim],
                 available: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """构造 MATERIAL_GAP 载荷（DEMO_ACTIONS_V0.2 §7）。

    必须携带 claim_id / required_action / required_object / required_result /
    缺口镜头数 / 建议补拍动作，且**不得**被降级兜底吞掉。
    """
    claim = claims[0] if claims else None
    undefined = [c.claim_id for c in claims if not c.contract_defined]
    payload = {
        "index": index, "type": "CONTRACT_UNDEFINED" if undefined else "MATERIAL_GAP",
        "text": text, "claim_text": claim_text,
        "claim_id": claim.claim_id if claim else "",
        "claim_name": claim.name if claim else "",
        "required_action": claim.action if claim else None,
        "required_object": claim.object if claim else None,
        "required_result": claim.result if claim else None,
        "required_role": claim.role if claim else None,
        "evidence_status": claim.evidence_status if claim else "",
        "gap_shot_count": None if undefined else len(claims),
        "undefined_claim_ids": undefined,
        "evidence_contract": claim.as_dict()["evidence_contract"] if claim else {},
        "suggested_shoot": _suggest_shoot(claim),
        "message": ("该卖点尚未定义可验证的动作/对象/结果合同；先校准合同，不能据此判定需补拍"
                    if undefined else "当前素材没有该句直接证据或相关产品特写；相关特写只可降级留痕，禁止无关画面充数"),
    }
    return payload


def _suggest_shoot(claim: Claim | None) -> str:
    if claim is None:
        return "补充可证明该句卖点的演示动作镜头"
    if not claim.contract_defined:
        return f"先定义「{claim.name}」的动作/对象/可见结果验收合同，再检查当前素材"
    return f"补拍「{claim.name}」的直接证据镜头（当前语料只有演示/示意，无结果可见的镜头）"


# ── 反重复（DEMO_ACTIONS_V0.2 §4）────────────────────────────────────
def _setup_id(shot: dict[str, Any]) -> str:
    return str(shot.get("setup_id") or "").strip()


def _action_phase(shot: dict[str, Any]) -> str:
    phase = str(shot.get("action_phase") or "").strip().lower()
    return phase if phase in ACTION_PHASES else ""


def repeat_conflict(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """是否构成「同一机位 + 同一构图 + 同一动作阶段」的连续重复。

    用户定案：禁止的是这个组合，**不是**动作本身。参考片 ref1#13→ref1#14
    连续两镜拉伸属于正确做法（机位/阶段不同），必须放行；我方 12/21 镜的
    「木台排排包装 + 手推」才是要拦的（setup 相同、阶段同为 static）。

    任一侧缺字段 → 返回 False（不拦），由旧的「相邻不得同源」规则兜底。
    """
    if not previous:
        return False
    prev_setup, cur_setup = _setup_id(previous), _setup_id(current)
    prev_phase, cur_phase = _action_phase(previous), _action_phase(current)
    if not (prev_setup and cur_setup and prev_phase and cur_phase):
        return False
    return prev_setup == cur_setup and prev_phase == cur_phase


def fallback_allowed(shot: dict[str, Any]) -> bool:
    """兜底池准入（用户定案「禁止回退到包装展示或空镜」）。

    只在句子**要求直接证据**时使用。促销/量感/形态句不调用本函数 ——
    它们本来就该配 product_display/context 类镜头，误杀会重演运营反馈。
    """
    if is_empty_shot(shot):
        return False
    role = _norm_role(shot)
    if role in ("product_display", "context", "CTA", "visual_metaphor"):
        return False
    return True


# ── 产品卖点降级（治理规则 9.5）─────────────────────────────────────
#
# 直接证据闸门仍然只认 judge_claim() 的动作、对象、结果三项。这里单独提供
# 「相关产品特写」路径，避免把降级镜头伪装成 direct_evidence，同时解决运营
# 反馈的另一半：面层柔软、薄、干爽、瞬吸等卖点经常没有完整演示，但素材池里
# 明明有对应部位/材质的实拍特写。
#
# 额外的「结果特写」路径（2026-09-28）：有些镜头本身被视觉分析标成
# direct_evidence，但它展示的是结果阶段（例如吸收完成后的干爽表面），没有
# 完整满足该 claim 的动作闸门（例如缺少纸巾按压对照），旧逻辑会把它当成
# 完全不可用而留黑。结果特写可以降级承接，但必须满足：
#   1) role=direct_evidence 且结果/证据标签命中该卖点的结果词；
#   2) 不把它改成 claim_gate 通过，报告标记为 RESULT_CLOSEUP_DEGRADED；
#   3) 只对明确允许的结果型卖点开放，避免「泛巾体特写」承接任意功能。
_PRODUCT_CLOSEUP_ROLES = {"product_display", "usage_demo"}
_RESULT_CLOSEUP_CLAIMS: dict[str, tuple[str, ...]] = {
    "C-DRY-SURFACE": ("干爽", "表面", "不反渗", "防渗", "无浮液", "吸收完成", "变白"),
    "C-ABSORB-SPEED": ("吸收", "吸入", "液面缩小", "铺展", "变浅", "吸尽"),
    "C-ABSORB-CAPACITY": ("锁水", "不扩散", "容量", "吸得多"),
}
_ACTION_CLOSEUP_CLAIMS: dict[str, tuple[str, ...]] = {
    # 倾倒/承接画面与“整周期的量兜住”有明确动作关联，但本身不证明
    # 容量上限；允许作为动作级降级，避免把相关实拍直接留黑。
    "C-ABSORB-CAPACITY": ("倾倒", "倒液", "液面", "铺展", "承接", "兜"),
}
_PRODUCT_CLOSEUP_MARKERS = (
    "特写", "近景", "近拍", "局部", "细节", "材质", "纸张", "纸面", "面层",
    "纹理", "纤维", "巾体", "产品本体", "实物", "包装",
)
_CLAIM_CLOSEUP_HINTS: dict[str, tuple[str, ...]] = {
    "C-ABSORB-SPEED": ("瞬吸", "吸收", "吸水", "液体", "水", "材质", "纸张", "巾体"),
    "C-ABSORB-CAPACITY": ("容量", "吸得多", "锁水", "多层", "叠层", "巾体", "材质"),
    "C-DRY-SURFACE": ("干爽", "表面", "面层", "纸面", "不反渗", "防渗", "材质"),
    "C-LEAK-SIDE": ("侧漏", "护边", "侧边", "防漏边", "立体边", "结构"),
    "C-SOFT": ("柔软", "亲肤", "软", "手感", "面层", "纸张", "纸面", "纹理", "材质"),
    "C-BREATHABLE": ("透气", "不闷", "面层", "材质", "纹理", "透气孔", "结构"),
    "C-THIN": ("薄", "轻薄", "厚度", "巾体", "材质", "纸张"),
    "C-GUARD-EXIST": ("护边", "侧边", "立体边", "防漏边", "结构"),
    "C-WRAP": ("包裹", "裤型", "裤体", "腰围", "结构"),
    "C-FIT": ("尺寸", "适配", "合身", "裤型", "裤体", "腰围", "贴合"),
    "C-BOND": ("粘扣", "魔术贴", "粘合", "粘贴", "结构"),
    "C-EASY-WEAR": ("穿脱", "裤型", "裤体", "侧边", "开口", "结构"),
    "C-NO-CHAFE": ("摩擦", "亲肤", "柔软", "面层", "表层", "材质"),
    "C-COTTON": ("纯棉", "棉", "纤维", "材质", "纹理"),
    "C-SPEC": ("规格", "每包", "片数", "数量", "包装"),
}


def _shot_search_text(shot: dict[str, Any]) -> str:
    fields = (
        "description", "visual_description", "visual_tags", "evidence_tags",
        "frame_tags", "scene_tags", "asset_tags", "object", "action_object",
        "action", "result", "subject", "shot_type", "rationale",
    )
    values: list[str] = []
    for field in fields:
        value = shot.get(field)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return re.sub(r"\s+", "", " ".join(values)).lower()


def packaging_claim_direct(shot: dict[str, Any], claim_text: str) -> dict[str, Any]:
    """Return the narrow, auditable packaging-claim direct-evidence decision.

    A readable package mark is not enough. The frame must also record an
    explicit pointing/indicating action, and the pointed claim must be the same
    canonical claim as the spoken text. This keeps the user-confirmed
    “100% 原生木浆” exception from turning generic packaging into evidence for
    softness, absorption, or any other functional claim.
    """
    spoken = _claim_compact(claim_text)
    matched_key = next(
        (key for key, variants in _PACKAGING_CLAIMS.items()
         if any(_claim_compact(variant) in spoken for variant in variants)),
        "",
    )
    if not matched_key:
        return {"ok": False, "reason": "NOT_PACKAGING_CLAIM"}
    if text_unverified(shot):
        return {"ok": False, "reason": "PACKAGING_TEXT_UNVERIFIED"}
    reliability = perception_reliability(shot)
    if not reliability["ok"] and reliability["reason"] != "EVIDENCE_CONFIDENCE_LOW":
        return {"ok": False, "reason": reliability["reason"]}
    # 只减去这一个标称短语，再检查剩余功能要求。不能按 claim ID 豁免整句，
    # 否则「木浆又柔软」「纯棉吸收快」会因包装标称而被认证。
    residual = re.sub(rf"{_NUMBER_TOKEN}层加厚", "", spoken) if matched_key == "LAYERS" else spoken
    for variant in sorted(_PACKAGING_CLAIMS[matched_key], key=len, reverse=True):
        residual = residual.replace(_claim_compact(variant), "")
    if requirements_for(residual) or any(k in residual for k in (
            "吸收", "吸水", "不掉絮", "不掉毛", "揉搓", "防漏", "反渗", "干爽")):
        return {"ok": False, "reason": "FUNCTIONAL_CLAIM_REQUIRES_ACTION_RESULT"}
    observed_values: list[str] = []
    for field in ("readable_claims", "packaging_claims", "pointed_claims",
                  "evidence_tags", "visual_tags", "frame_tags", "visual_description",
                  "description", "rationale"):
        observed_values.extend(_flatten_observation(shot.get(field)))
    def readable_in(values: list[str]) -> bool:
        if matched_key == "LAYERS":
            spoken_layers = {p for p in _number_units(spoken) if p[1] == "layer"}
            # 保留各观察字段的边界，货号 DT55080 与下一条 4层不能拼成 550804层。
            observed_units = set().union(*(_number_units(value) for value in values))
            return bool(spoken_layers) and spoken_layers <= observed_units
        observed = _claim_compact(" ".join(values))
        return any(_claim_compact(variant) in observed for variant in _PACKAGING_CLAIMS[matched_key])

    readable = readable_in(observed_values)
    independent_text = (
        shot.get("text_pass") == "hires"
        and readable_in(_flatten_observation(shot.get("text_agreed")))
        and readable_in(_flatten_observation(shot.get("pointed_agreed")))
    )
    if not reliability["ok"] and not independent_text:
        return {"ok": False, "reason": reliability["reason"]}
    action_values: list[str] = []
    for field in ("action", "pointing_action", "gesture", "visual_description", "rationale"):
        action_values.extend(_flatten_observation(shot.get(field)))
    action_text = _claim_compact(" ".join(action_values))
    if "pointing_to_text" in shot or "explicit_pointing" in shot:
        explicit_pointing = bool(
            shot.get("pointing_to_text") is True
            or shot.get("explicit_pointing") is True
        )
    else:
        explicit_pointing = any(marker in action_text for marker in _POINTING_MARKERS)
    if not readable:
        return {"ok": False, "reason": "PACKAGING_CLAIM_NOT_READABLE", "claim_key": matched_key}
    if not explicit_pointing:
        return {"ok": False, "reason": "PACKAGING_CLAIM_NOT_POINTED", "claim_key": matched_key}
    return {
        "ok": True,
        "reason": "PACKAGING_CLAIM_DIRECT",
        "label": "PACKAGING_CLAIM_DIRECT",
        "claim_key": matched_key,
        "authenticity_not_verified": True,
        "observed_readable_claim": True,
        "explicit_pointing": True,
        "perception_basis": "independent_text_review" if independent_text else reliability["basis"],
        "evidence_contract": {"schema": "claim_evidence", "version": EVIDENCE_CONTRACT_VERSION,
                              "kind": "packaging_label", "status": "defined"},
    }


def absorb_quantity_context(text: str, category: str = "") -> bool:
    """只消除吸收容量/经量的数量歧义，购买号召和包装数量仍须量感证据。"""
    value = re.sub(r"\s+", "", str(text or ""))
    if "cta" in infer_intents(value, category=category):
        return False
    if any(term in value for term in ("囤", "多包", "成排", "整箱", "成箱", "一提", "堆叠")):
        return False
    if re.search(rf"{_NUMBER_TOKEN}(?:包|提|箱|抽|张|片|卷|件|pcs)", value, re.IGNORECASE):
        return False
    from . import rulepack
    return (any(term in value for term in ("吸水", "吸收", "吸量", "锁水", "经量"))
            or (rulepack.normalize_category(category) == rulepack.CATEGORY_WEIPIN
                and any(term in value for term in ("量大", "量多", "一整晚"))))


def cta_quantity_eligible(shot: dict[str, Any], text: str = "",
                          category: str = "") -> tuple[bool, str]:
    """Require a quantity/scale cue for CTA and value language.

    CTA needs visible purchase units (multiple packs, a filled bundle/carton,
    or a row/stack). Sheets/PCS inside one pack only support a matching content
    specification sentence, never bulk purchase language.
    """
    intents = infer_intents(text, category=category)
    if "cta" not in intents and "quantity" not in intents:
        return True, "NOT_CTA_OR_QUANTITY"
    if is_empty_shot(shot) or is_visual_metaphor(shot):
        return False, "CTA_REQUIRES_QUANTITY_SHOT"
    contract = sentence_contract(text, category=category)
    if ("cta" not in intents and any(unit == "sheet" for _, unit in _number_units(text))
            and count_readable_rank(shot, contract) > 0):
        return True, "READABLE_PACK_CONTENT_COUNT"
    observed = _shot_search_text(shot)
    def quantity_observed(keyword: str) -> bool:
        return any(_observed_keyword(keyword, clause)
                   and not any(word in clause for word in ("可读", "规格"))
                   for clause in _CLAUSE_BREAK.split(observed))

    # 否定的观察不能被另一个字段复制的「多包/整箱」标签翻案。
    negated = rf"(?:没有|未见|没见|并非|不是|并未|看不到|不见|无)(?:可见的?|任何)?(?:{'|'.join(_QUANTITY_MARKERS)})"
    if re.search(negated, observed) or re.search(r"(?:空纸箱|空箱|箱内无产品|箱里没有产品)", observed):
        return False, "QUANTITY_OBSERVATION_NEGATED"
    if any(quantity_observed(marker) for marker in _QUANTITY_MARKERS):
        return True, "VISIBLE_MULTI_PACK_OR_QUANTITY"
    for match in re.finditer(rf"({_NUMBER_TOKEN})\s*(包|提|箱|卷|件)", observed):
        count = _chinese_count_to_arabic(match.group(1))
        if not count.isdigit() or not quantity_observed(match.group(0)):
            continue
        # 一箱/一提是外部组合包装；一包/一卷/一件仍是单件展示。
        minimum = 1 if match.group(2) in ("箱", "提") else 2
        if int(count) >= minimum:
            return True, "VISIBLE_PURCHASE_UNIT_COUNT"
    # Explicit single-pack language without a count is exactly the failure mode
    # reported in the manual reviews; never let role preference override it.
    return False, "SINGLE_PACK_OR_NO_VISIBLE_QUANTITY"


def is_related_product_closeup(shot: dict[str, Any], claim: Claim) -> bool:
    """判断镜头是否能作为该卖点的相关产品特写降级。

    这是关联候选，不是证据通过：必须同时满足真实产品/部位特写信号与 claim
    相关词命中；「包装正面」对柔软、「整箱陈列」对瞬吸等无关画面不会放行。
    """
    if is_empty_shot(shot) or is_visual_metaphor(shot):
        return False
    text = _shot_search_text(shot)
    if not text or not any(marker in text for marker in _PRODUCT_CLOSEUP_MARKERS):
        return False
    role = _norm_role(shot)
    if role in _PRODUCT_CLOSEUP_ROLES:
        pass
    elif role == "direct_evidence":
        # 结果阶段有时已被分析为 direct_evidence，但没有满足当前 claim
        # 的完整动作/对照要求。只允许结果字段或证据标签命中白名单结果词，
        # 不接受仅有「巾体/包装/产品」等泛词的 direct_evidence 镜头。
        result_hints = _RESULT_CLOSEUP_CLAIMS.get(claim.claim_id, ())
        result_text = re.sub(
            r"\s+", "", " ".join(str(shot.get(k) or "")
                                for k in ("result", "evidence_tags", "visual_description")),
        ).lower()
        action_hints = _ACTION_CLOSEUP_CLAIMS.get(claim.claim_id, ())
        action_text = re.sub(
            r"\s+", "", " ".join(str(shot.get(k) or "")
                                for k in ("action", "visual_description", "evidence_tags")),
        ).lower()
        if (not result_hints or not any(h.lower() in result_text for h in result_hints)) and (
            not action_hints or not any(h.lower() in action_text for h in action_hints)
        ):
            return False
        return True
    else:
        return False
    hints = _CLAIM_CLOSEUP_HINTS.get(claim.claim_id)
    if hints is None:
        hints = tuple([claim.name, claim.object or "", claim.result or "", *claim.keywords])
    return any(str(hint).strip() and str(hint).strip().lower() in text for hint in hints)


def fallback_match(shot: dict[str, Any], claim: Claim) -> tuple[bool, str]:
    """Return whether a shot is an auditable degraded candidate for ``claim``."""
    if not claim.evidence_required:
        return False, "CLAIM_DOES_NOT_REQUIRE_EVIDENCE"
    if is_related_product_closeup(shot, claim):
        if _norm_role(shot) == "direct_evidence":
            result_text = re.sub(
                r"\s+", "", " ".join(str(shot.get(k) or "")
                                    for k in ("result", "evidence_tags", "visual_description")),
            ).lower()
            result_hints = _RESULT_CLOSEUP_CLAIMS.get(claim.claim_id, ())
            if result_hints and any(h.lower() in result_text for h in result_hints):
                return True, "PRODUCT_RESULT_CLOSEUP_DEGRADED"
            action_hints = _ACTION_CLOSEUP_CLAIMS.get(claim.claim_id, ())
            if action_hints:
                action_text = re.sub(
                    r"\s+", "", " ".join(str(shot.get(k) or "")
                                        for k in ("action", "visual_description", "evidence_tags")),
                ).lower()
                if any(h.lower() in action_text for h in action_hints):
                    return True, "PRODUCT_ACTION_DEGRADED"
            return True, "PRODUCT_RESULT_CLOSEUP_DEGRADED"
        return True, "PRODUCT_SELLING_POINT_CLOSEUP_DEGRADED"
    return False, "NO_RELATED_PRODUCT_CLOSEUP"


def fallback_reason(shot: dict[str, Any], claims: list[Claim]) -> str:
    """Return the explicit fallback reason for a candidate, if any."""
    for claim in claims:
        matched, reason = fallback_match(shot, claim)
        if matched:
            return reason
    return ""


def visual_role(shot: dict[str, Any] | None) -> str:
    """只读访问器：该镜头的画面角色（`role`，回退 `visual_role`）。

    v1.3.27（用户定案第 6 条）：变速范围由画面角色决定，所以时序契约层需要拿到
    role。这里**只暴露读取**，不扩表、不改规则语义 —— 规则包的修订另开版本。
    """
    return _norm_role(shot or {})


def gate_coverage(shots: list[dict[str, Any]]) -> dict[str, Any]:
    """报告用：这一批分析有多少镜头带齐了新字段。

    v1.3.27（用户定案第 10 条）：7A 规则包尚未落地，闸门实际处于
    **未认证**状态，不能靠 `fully_gated` 那个数字冒充「全部通过」。实测口径：
    上一轮 `fully_gated = 22/22` 看着满格，实际 11 句口播只有第 3 句命中 claim，
    这不是闸门生效，是闸门空转。所以这里改用五个明确字段把状态说清楚：

    - `fully_gated=False` —— 恒为布尔假，不再返回「看起来满格」的计数；
    - `coverage_status="RULEPACK_MISSING"` —— 未生效的原因；
    - `validation_passed=None` —— 未验证（不是「已验证通过」）；
    - `certification="UNCERTIFIED"` —— 未认证；
    - `uncertified_reasons=["RULEPACK_MISSING"]` —— 机读原因列表。

    各计数仍然原样保留，供诊断「字段补到什么程度了」。
    """
    total = len(shots)
    with_role = sum(1 for s in shots if _norm_role(s))
    with_subject = sum(1 for s in shots if _has_subject(s) is not None)
    with_action = sum(1 for s in shots if str(s.get("action") or "").strip())
    with_setup = sum(1 for s in shots if _setup_id(s) and _action_phase(s))
    missing = ["RULEPACK_MISSING"]
    return {
        "shots": total,
        "with_role": with_role,
        "with_has_subject": with_subject,
        "with_action": with_action,
        "with_setup_phase": with_setup,
        "fully_gated": False,
        "coverage_status": "RULEPACK_MISSING",
        "validation_passed": None,
        "certification": "UNCERTIFIED",
        "uncertified_reasons": missing,
        "note": ("7A 规则包（fail-closed 语义 + 悬挂抽 CLAIMS）尚未落地，本批闸门处于"
                 "未认证状态：未覆盖的镜头按旧行为放行，不得据此宣称质检通过。"
                 "要拿到完整闸门效果需先发布规则包，再重跑视觉分析补 "
                 "role/has_subject/action/setup_id/action_phase"),
    }
