"""Deterministic credibility scoring.

Same input must always produce the same number, otherwise the 0.6 gate cannot be
audited. Ported from v1; the formulas and their evidence live in the comments.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .dedupe import (
    entity_id,
    event_id,
    event_signature,
    is_same_event,
    title_tokens,
)
from .registry import (
    CREDIBILITY_THRESHOLD,
    CROSS_CONFIRM_BONUS,
    CROSS_CONFIRM_CAP,
    DOMAIN_TOKENS,
    EVIDENCE_WEIGHT,
    HARD_EVIDENCE_TYPES,
    PLATFORM_WEIGHT,
    PRIMARY_SOURCES,
    RE_CASUALTY,
    RE_DATE,
    RE_ORG,
    RE_PLACE,
    RE_QUANTITY,
    SOCIAL_EVIDENCE_FORM,
    SOURCES,
    SOURCE_WEIGHT,
    UNWEIGHTED_SOURCES,
    evidence_type_for,
    identity_of,
    normalize_identity,
)

def domain_of_source(source_key: str) -> set:
    return set(SOURCES.get(source_key, {}).get("domains", []))


def domain_share(entries: list[dict], wanted_domains: set) -> dict:
    """Fraction of extracted items that belong to the requested domain(s).

    This is the classifier-request analogue of keyword coverage: it answers
    "did the run stay inside the categories I asked for", which is the only
    meaningful notion of relevance when the request names categories.
    """
    if not entries:
        return {"coverage": 0.0, "on_domain": 0, "off_domain": 0, "note": "无条目"}
    on, off = 0, []
    for e in entries:
        src_dom = domain_of_source(e.get("source_key", ""))
        if src_dom & wanted_domains:
            on += 1
        else:
            off.append(e.get("title", "")[:60])
    return {
        "coverage": round(on / len(entries), 3),
        "on_domain": on,
        "off_domain": len(off),
        "off_domain_examples": off[:5],
    }


def platform_of(source_key: str, label: str = "") -> str:
    """Map a registry key, or a free-text outlet name, to a platform weight key.

    Both arguments are searched, because a supplemental-search item has no
    registry key and carries its outlet only in `source_label` -- and conversely
    a registry item has a key but a generic label. Checking only one of them was
    a measured bug: a 36氪 story resolved to 'unknown' (0.40) and failed the
    truth gate even though 36kr carries 0.48.
    """
    key_s = (source_key or "").strip()
    text = (label or "").strip()

    # exact key first
    if key_s in PLATFORM_WEIGHT:
        return key_s

    # alias table: Chinese outlet names and lower-case key spellings. Checked
    # before the substring-key scan so "36氪" and "IT之家" resolve correctly.
    #
    # Government bodies are listed FIRST and checked structurally as well (below),
    # because missing them inverted the ranking: measured, a 市场监管总局 official
    # notice resolved to `unknown` (0.40) -> truth 0.628, while 光明网 merely
    # REPRINTING it scored 0.772. The primary source ranked below its own
    # syndication -- the exact opposite of what the model is for.
    alias_map = (
        ("市场监督管理总局", "gov-cn"), ("市场监管总局", "gov-cn"),
        ("国家发展改革委", "gov-cn"), ("发改委", "gov-cn"),
        ("交通运输部", "gov-cn"), ("教育部", "gov-cn"), ("工信部", "gov-cn"),
        ("工业和信息化部", "gov-cn"), ("公安部", "gov-cn"),
        ("国家卫生健康委", "gov-cn"), ("卫健委", "gov-cn"),
        ("应急管理部", "gov-cn"), ("农业农村部", "gov-cn"),
        ("生态环境部", "gov-cn"), ("民政部", "gov-cn"), ("财政部", "gov-cn"),
        ("人社部", "gov-cn"), ("人力资源社会保障部", "gov-cn"),
        ("国家医保局", "gov-cn"), ("国家疾控局", "gov-cn"),
        ("证监会", "gov-cn"), ("银保监会", "gov-cn"), ("金融监管总局", "gov-cn"),
        ("最高人民法院", "gov-cn"), ("最高法", "gov-cn"),
        ("最高人民检察院", "gov-cn"), ("最高检", "gov-cn"),
        ("住房城乡建设部", "gov-cn"), ("住建部", "gov-cn"),
        ("国家能源局", "gov-cn"), ("国家林草局", "gov-cn"),
        ("国家文物局", "gov-cn"), ("文化和旅游部", "gov-cn"),
        ("中央气象台", "gov-cn"), ("国家气候中心", "gov-cn"),
        ("中国地震台网", "gov-cn"), ("国家统计局", "gov-cn"),
        ("中国政府网", "gov-cn"), ("国务院", "gov-cn"),
        ("澎湃", "thepaper"), ("人民网", "people"), ("人民日报", "people"),
        ("光明", "guangming"),
        ("中央电视台", "cctv"), ("央视", "cctv"), ("cctv", "cctv"),
        ("新闻联播", "cctv"),
        ("新华社", "xinhua"), ("新华网", "xinhua"), ("中新社", "xinhua"),
        ("中国新闻网", "xinhua"), ("中新网", "xinhua"), ("环球时报", "xinhua"),
        ("央广", "cnr"), ("中国日报", "chinadaily"),
        ("第一财经", "yicai"), ("证券时报", "stcn"), ("新京报", "bjnews"),
        ("南方都市报", "bjnews"), ("南方周末", "bjnews"),
        ("红星新闻", "bjnews"), ("上游新闻", "bjnews"), ("极目新闻", "bjnews"),
        ("封面新闻", "bjnews"), ("北京日报", "bjnews"), ("广州日报", "bjnews"),
        ("浙江日报", "bjnews"), ("中国青年报", "bjnews"),
        ("界面新闻", "caixin"), ("财新", "caixin"), ("经济观察报", "caixin"),
        ("21世纪经济报道", "caixin"), ("每日经济新闻", "caixin"),
        ("观察者网", "caixin"), ("半月谈", "xinhua"),
        ("头条", "toutiao"), ("百度", "baidu"),
        ("36氪", "36kr"), ("36kr", "36kr"), ("it之家", "ithome"),
        ("ithome", "ithome"), ("ars", "ars-technica"),
        ("technology review", "techreview"), ("ieee", "ieee-spectrum"),
    )
    for hay in (key_s, text):
        if not hay:
            continue
        low = hay.lower()
        for alias, key in alias_map:
            if alias in low:
                return key
        # structural fallback: any *.gov.cn host, or an official-looking name.
        # Measured: 42 of 60 real institutions resolved to `unknown` before this.
        # The prefix requirement (国家/中国/中央/省市县) is what keeps 俱乐部 and
        # 干部 from being mistaken for government bodies.
        if "gov.cn" in low:
            return "gov-cn"
        if re.search(r"^(?:国家|中国|中央|中华|全国|[\u4e00-\u9fff]{2,4}[省市县区])"
                     r"[\u4e00-\u9fff]{0,12}(?:总局|总署|管理局|监督管理|委员会|"
                     r"委|部|局|署|法院|检察院|气象台|台|中心)$", hay):
            return "gov-cn"

    # last resort: a known key appearing inside the text. Guarded against the
    # empty-string trap -- `"" in anything` is True and would match the first key.
    for hay in (key_s, text):
        if len(hay) < 2:
            continue
        low = hay.lower()
        for key in PLATFORM_WEIGHT:
            if key in low:
                return key
    return "unknown"


def evidence_form_of(entry: dict) -> str:
    """Classify the FORM of evidence from the text itself.

    Deliberately measured, not declared: an entry has to show concrete detail
    (quantities, named organisations, dates, places) to earn a higher form.
    """
    blob = " ".join([entry.get("title") or "", entry.get("summary") or ""])
    n_qty = len(RE_QUANTITY.findall(blob))
    n_org = len(RE_ORG.findall(blob))
    n_date = len(RE_DATE.findall(blob))
    n_place = len(RE_PLACE.findall(blob))
    n_cas = len(RE_CASUALTY.findall(blob))
    n_anchor = n_org + n_date + n_cas
    # 事故锚点给 3 分：它比规格参数特异得多，刷不出来。
    score = n_qty * 2 + n_org * 2 + n_date + n_place + n_cas * 3
    # 合取条件从「有任一锚点」收紧为「≥3 个锚点」。实测：只有 1 个机构词的短摘要
    # （'漳州口腔医院等4家机构暂停夜间门诊，患者分流最远约12公里。'）靠数量词
    # 堆到 first-hand-report，而它并没有一手调查的密度。
    anchored = n_anchor >= 3
    label = (entry.get("source_label") or "") + " " + (entry.get("source") or "")
    if any(k in label for k in ("通报", "公告", "regulation", "政府", "gov")):
        return "official-notice"
    if score >= 10 and anchored:
        return "first-hand-report"
    if score >= 5:
        return "named-entity"
    if score >= 1:
        return "secondary-report"
    return "unattributed"


# 修饰词与感叹号：**它们是偏见检测信号，不是噪声。**
#
# skill 的硬规则是「修饰词和感叹号不要过滤掉 —— 放进 signal_words，同时不进 claim」。
# 实测问题：这条规则此前**完全落空** —— 抓取路径（RSS / Crossref / atom）
# 一个字都不往 `signal_words` 里写，该字段只由调用方的 `--extra-json` 注入。
# 实测 193 条里 0 条有值。规则写在文档里、代码里没有对应实现，等于没有。
#
# 抽的是**报道者的判断痕迹**：程度词、评价性形容词、情绪化表达、感叹号。
# 不做褒贬分类 —— 只标出「这里有主观色彩」，让下游自己去质疑。
RE_SIGNAL_ZH = re.compile(
    r"(?:极为|极其|极度|彻底|完全|根本|重大|巨大|显著|大幅|急剧|飞速|迅猛|空前"
    r"|首次|唯一|史上|最[高新强大低快慢多]|突破性|颠覆性|革命性|里程碑"
    r"|震惊|震撼|重磅|爆|惊[人呆]|罕见|惊人|可怕|恐怖|惨|悲剧"
    r"|令人|不禁|竟然|居然|万万没想到|不可思议|难以置信"
    r"|必将|势必|无疑|显然|众所周知|众所周知)"
    r"|！+|!{2,}")
RE_SIGNAL_EN = re.compile(
    r"\b(?:unprecedented|breakthrough|revolutionary|disruptive|game.?chang\w*"
    r"|groundbreaking|stunning|shocking|devastating|catastrophic|massive|huge"
    r"|dramatic\w*|drastically|dramatically|significantly|substantially|nearly"
    r"|unprecedented|definitively|unequivocally|remarkabl\w+|extraordinarily"
    r"|first-ever|record-breaking|scary|terrifying|alarming)\b"
    r"|!{2,}", re.I)


def signal_words(entry: dict, limit: int = 8) -> list[str]:
    """从标题+摘要里抽出报道者的判断痕迹，原样保留。

    返回原文片段（不改写），去重、按出现顺序，最多 `limit` 个。
    **不进 claim** —— 调用方负责把它们放在 claim 之外。

    ★ 必须**只从正文抽**，标题不算。实测两个假阳性都出在标题上：
      `drea de matteo` → `['huge']`（人名里的词）
      `I built ALHR: ... Alarming` → `['Alarming']`（技术词被当评价）
    标题天然短、缺上下文，一个孤立的形容词几乎不可能是"报道者的判断痕迹"。
    摘要有句子语境，抽出才可信。
    """
    blob = (entry.get("summary") or "")
    if not blob:
        return []                    # 没有正文就没什么可抽的
    seen, out = set(), []
    for rx in (RE_SIGNAL_ZH, RE_SIGNAL_EN):
        for m in rx.finditer(blob):
            w = m.group(0)
            k = w.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(w)
    return out[:limit]


TREND_TRUTH_CAP = 0.45
PROMO_PENALTY = 0.25
PROMO_BODY_PENALTY = 0.10

# 大归档源：整个归档都在一个 feed 里，时间窗过滤前要吃进全部条目。
# 实测 openai-news = 1287 条，是阶段 2 成本（raw 2124）的主要来源。
# 这些源按【已解析的日期】先降序截断，再进池 —— 与单源限流不同：
# 限流发生在去重之后、作用于输出；这里是解析后立刻截断，省掉的是解析与去重成本。
ARCHIVE_SOURCES = {"openai-news", "arxiv-rss-ai"}
ARCHIVE_SCAN_LIMIT = 150


def apply_trend_cap(entry: dict) -> bool:
    """Clamp a trend-board entry below the truth gate, wherever it was scored.

    Measured bug: the cap originally lived only inside `truth_weight_social`, so
    it applied ONLY to `--domain social` runs. In `--domain ai` / `--domain
    science` runs everything goes through `truth_weight` and the key
    `trend_capped` never appeared at all -- hot-search entries sat at 0.29/0.33
    merely because their source weight is low. That is safe by accident, not by
    design: a future source-weight change would silently let a celebrity headline
    into the core section. A headline has no body to verify, so its truth cannot
    clear the gate by construction.

    Called once, centrally, after scoring, so no scoring path can bypass it.
    """
    own = entry.get("source_key", "")
    is_board = SOURCES.get(own, {}).get("class") == "trend" or entry.get("is_trend")
    if not is_board:
        return False
    entry["trend_capped"] = True
    if not entry.get("evidence_form"):
        entry["evidence_form"] = "trend-board(unverifiable)"
    # ASSIGN, do not min(). Measured bug: `min(score, CAP)` is a no-op whenever the
    # score is ALREADY below the cap -- which is the normal case for these sources
    # (0.29/0.31/0.33). The result was an entry carrying `trend_capped: True`
    # alongside an uncapped score: the flag said "suppressed", the number said
    # "not suppressed". A cap that only acts when it is not needed is decoration.
    entry["truth_score"] = TREND_TRUTH_CAP
    return True


def external_truth_weight(entry: dict) -> float:
    """真实分 for a supplemental-search item (--extra-json).

    Uses the SAME 0.30/0.70 split as the in-domain social model. Measured bug: the
    two paths disagreed (0.40/0.60 here vs 0.30/0.70 there), so the same IT之家
    story scored differently depending on how it entered, and an auditor
    recomputing from the JSON's own `weights` block could not reproduce either.
    One formula, one place, used by both.
    """
    pk = platform_of(entry.get("source_key", ""), entry.get("source_label") or "")
    form = evidence_form_of(entry)
    entry["platform_key"] = pk
    entry["evidence_form"] = form
    return round(0.30 * PLATFORM_WEIGHT.get(pk, 0.40)
                 + 0.70 * SOCIAL_EVIDENCE_FORM.get(form, 0.30), 3)


def truth_weight_social(entry: dict, corroborated: bool = False) -> float:
    """社会域专用的真实分：证据形式为主，平台可信度为辅。

    Used when the entry's domain is `social` AND the run has no authority stream
    to cross-check against (問題4). The default weight model assumes an
    authoritative tier exists; in the social domain it does not, so scoring by
    masthead alone would be scoring by something the domain cannot provide.
    """
    # Trend boards are a CALIBRATION signal, never a content source. Measured bug:
    # the social domain tags them `domains:["social"]`, so this branch scored their
    # bare headlines by evidence form -- and "多家医院、卫生院暂停夜间门诊" scored
    # 0.65, putting a hot-search phrase into the core section. That directly
    # contradicts quality-bar.md ("热度榜条目默认一条都不进核心区"). A headline has
    # no body to verify, so its truth cannot clear the gate by construction.
    if SOURCES.get(entry.get("source_key", ""), {}).get("class") == "trend" or entry.get("is_trend"):
        entry["platform_key"] = platform_of(entry.get("source_key", ""),
                                            entry.get("source_label") or "")
        entry["evidence_form"] = "trend-board(unverifiable)"
        entry["trend_capped"] = True
        return 0.45          # below CREDIBILITY_THRESHOLD -> 存疑区, never core

    pk = platform_of(entry.get("source_key", ""), entry.get("source_label") or "")
    form = evidence_form_of(entry)
    entry["platform_key"] = pk
    entry["evidence_form"] = form
    score = 0.30 * PLATFORM_WEIGHT.get(pk, 0.40) + 0.70 * SOCIAL_EVIDENCE_FORM.get(form, 0.30)
    if corroborated:
        score = min(score + CROSS_CONFIRM_BONUS, CROSS_CONFIRM_CAP)
    sev = notice_severity(entry.get("update_notices") or [])
    if sev == "retracted":
        return 0.05
    if sev == "concern":
        score = min(score, 0.45)
    return round(score, 3)


def comm_weight_social(entry: dict, pickups: int, in_trend: bool) -> float:
    """社会域专用的传播分：独立转载 + 平台热度。

    热度 carries 0.40 here (vs a mere +0.08 bonus in the default path) because
    the authority stream does not exist in this domain. Without a heat term,
    social 传播 could only ever come from reprints, and a genuine public event
    that nobody has reprinted yet could not clear the gate.

    Calibration: the reprint curve is chosen so the numbers mean the SAME thing as
    the default branch. Default comm gives exactly 0.60 for 2 independent pickups,
    so the social branch must too -- otherwise it is silently stricter and good
    social items fail a gate they should clear.

    KNOWN LIMITATION, stated rather than hidden: `in_trend` requires the same
    story to be present in BOTH the social pool and the trend sample. In the
    current architecture those are separate invocations, so trend entries never
    reach this function and the heat bonus effectively never fires. It is kept as
    an additive term so that wiring the two streams together later needs no
    rescoring -- but do not rely on heat boosting anything today.
    """
    by_pickups = {1: 0.35, 2: 0.60, 3: 0.72, 4: 0.80}
    base = by_pickups.get(pickups, 0.88)
    boost = 0.30 if in_trend else 0.0
    return round(min(base + boost, 0.95), 3)


def truth_weight(source_key: str, cls: str, corroborated: bool = False,
                 notices: list | None = None, entry: dict | None = None) -> float:
    """Compute the 真实 score for one entry.

    Blends source credibility with the evidence tier, then adds a bounded
    corroboration bonus. Returns a float in [0, 1]; the pipeline passes it
    against CREDIBILITY_THRESHOLD (0.6).

    A retraction or expression of concern SHORT-CIRCUITS everything else.
    Measured failure this fixes: the arsenic-life paper scored 0.912 -- the
    highest score in the whole suite -- because the weights encode "peer-reviewed
    in Science", which says nothing about whether the finding is true. It has
    since been retracted. Editorial tier is a proxy for reliability, NOT a
    substitute for the record, so the record must be allowed to override it.
    """
    base = SOURCE_WEIGHT.get(source_key, 0.50)
    if source_key and source_key not in SOURCE_WEIGHT and source_key in SOURCES:
        # ★ 登记表里有、权重表里没有 = 静默拿默认值。
        # 实测：新加的 `chinanews` 就是这样拿到 0.50 的 —— 权重决定被一个
        # 没人看过的默认值替我做掉了，而且只在反推分数时才被发现。
        # 不抛异常（会让整跑失败），但必须留痕，让 `--list` 和测试能查出来。
        UNWEIGHTED_SOURCES.add(source_key)
    ev = EVIDENCE_WEIGHT[evidence_type_for(source_key, cls, entry)]
    score = 0.4 * base + 0.6 * ev
    if corroborated:
        score = min(score + CROSS_CONFIRM_BONUS, CROSS_CONFIRM_CAP)

    severity = notice_severity(notices or [])
    if severity == "retracted":
        return 0.05          # withdrawn: below every gate, sorted into 存疑区
    if severity == "concern":
        return min(score, 0.45)
    if severity == "correction":
        score = min(score, 0.60)
    return round(score, 3)


# Retraction / concern labels as they appear in Crossref `update-to`.
RETRACTION_LABELS = ("retraction", "retracted", "withdrawal", "withdrawn")
CONCERN_LABELS = ("expression of concern", "concern")
CORRECTION_LABELS = ("correction", "corrigendum", "erratum", "addendum")


def notice_severity(notices: list) -> str | None:
    """Map Crossref update-to labels onto a severity, worst wins."""
    found = set()
    for n in notices or []:
        label = str((n or {}).get("label") or (n or {}).get("type") or "").lower()
        if not label:
            continue
        if any(k in label for k in RETRACTION_LABELS):
            found.add("retracted")
        elif any(k in label for k in CONCERN_LABELS):
            found.add("concern")
        elif any(k in label for k in CORRECTION_LABELS):
            found.add("correction")
    for level in ("retracted", "concern", "correction"):
        if level in found:
            return level
    return None


def comm_weight(n_independent_sources: int, in_trend: bool) -> float:
    """Compute the 传播报道 score.

    Calibration note (measured): the first version gave a single source 0.20, which
    killed 770 of 1570 entries -- including every Nature paper (truth 0.91) merely
    for not being reprinted yet. That inverts the purpose of a "what's NEW" radar.
    So the scale is set by what the 0.6 gate MEANS:

      1 source  -> below gate  = exclusive; real but uncorroborated, needs checking
      2 sources -> just passes = independently picked up
      3+        -> solidly passes

    A trend-board appearance corroborates spread but is NEVER sufficient alone --
    that is how a celebrity name would otherwise pass the gate. Enforced upstream
    in estimate_pickups, which counts only NON-trend identities as corroboration,
    so the `+0.08` bump this function used to apply is GONE: it was the very thing
    that let two hot-search boards jointly clear the gate (0.60 + 0.08 = 0.68).
    """
    if n_independent_sources <= 1:
        base = 0.35 if in_trend else 0.30
    elif n_independent_sources == 2:
        base = 0.60
    elif n_independent_sources == 3:
        base = 0.72
    elif n_independent_sources == 4:
        base = 0.80
    else:
        base = 0.88
    return round(min(base, 0.95), 3)


def estimate_pickups(entry: dict, cls: str = "") -> int:
    """Distinct source IDENTITIES carrying this entry (mirrors do not count twice).

    An explicit `pickups` (supplied via --extra-json) wins: the caller counted
    outlets from a source this script cannot see, and re-deriving the number from
    `also_seen_in` would silently drop that.

    TREND BOARDS DO NOT CORROBORATE EACH OTHER. Measured bug this fixes: two
    hot-search boards (baidu-hot + toutiao-hot, 当时两个都在册) both listing the
    same celebrity story produced pickups=2 -> comm 0.68 -> the story cleared the
    gate, which directly contradicts comm_weight's own rule that a trend
    appearance "is NEVER sufficient alone -- that is how a celebrity name would
    otherwise pass the gate". Two boards tracking the same search volume is one
    signal, not two.

    ⚠️ `baidu-hot` 已于 2026-10-10 下架（4 个镜像常态 3 个已死；当天 5 次失败
    只成功 2 次；中文热度面由 `toutiao-hot` 独家覆盖，冗余已够）。
    但**这条规则必须留着** —— 只要再登记第二个中文热度榜，两榜互证的路径
    立刻就会被走通。规则的价值不随当时有几个榜单而变。
    """
    if isinstance(entry.get("pickups"), int) and entry["pickups"] > 0:
        return entry["pickups"]

    def is_trend(key: str) -> bool:
        return SOURCES.get(key, {}).get("class") == "trend"

    own = entry.get("source_key", "")
    own_label = entry.get("source_label") or entry.get("source") or ""
    own_id = identity_of(own, own_label)
    own_norm = normalize_identity(own_label)
    others = [k for k in (entry.get("also_seen_in") or []) if k]
    # Corroboration counts ONLY non-trend identities, and never the entry's own
    # source. A government notice that lists itself is not a second opinion.
    real = set()
    if own and not is_trend(own):
        real.add(own_id)
    for k in others:
        if is_trend(k):
            continue
        # `k` may be a registry key or a bare outlet name from --extra-json.
        # identity_of resolves known outlets through the alias table; unknown names
        # fall through as raw text and are compared with normalize_identity (which
        # strips generic suffixes like 网/新闻).
        #
        # Measured bug: applying normalize_identity to the RESOLVED value too broke
        # the platform keys -- "yicai" ends in i/N and became "yca", "cctv" became
        # "cc", so two unrelated outlets collapsed and real corroboration was lost.
        # Normalize only the RAW name, and only when it did not resolve to a key.
        kid = identity_of(k, k)
        if not kid or kid == own_id:
            continue
        raw_norm = normalize_identity(k)
        if raw_norm == own_norm:
            continue
        if kid == k and normalize_identity(kid) == normalize_identity(own_norm):
            continue
        real.add(kid)
    real.discard("")
    if real:
        return len(real)
    # Nothing but trend boards carried it. Count DISTINCT boards rather than
    # collapsing to 1: two different boards are a wider signal than one, but the
    # comm curve starts at 2 = 0.60, so this still cannot clear the gate alone --
    # which is the rule comm_weight states.
    return max(trend_saturation(entry), 1)


def trend_saturation(entry: dict) -> int:
    """How many DISTINCT trend boards carried this entry."""
    own = entry.get("source_key", "")
    boards = set()
    if SOURCES.get(own, {}).get("class") == "trend":
        boards.add(identity_of(own))
    for k in (entry.get("also_seen_in") or []):
        if SOURCES.get(k, {}).get("class") == "trend":
            boards.add(identity_of(k))
    return len(boards)

PROMO_TITLE_MARKERS = (
    "whitepaper", "white paper", "download this free", "sponsored content",
    "sponsored by", "paid post", "advertisement", "promotional", "buy now",
    "shop now", "register now", "request a demo", "book a demo",
    "直达链接", "购买链接", "优惠券", "到手价", "首发价", "开售", "领券",
    "促销", "限时抢购",
)
PROMO_BODY_MARKERS = (
    "volume pricing", "contact a content specialist", "sign up for our",
    "subscribe to our newsletter", "affiliate", "京东", "天猫", "限时",
)


def is_customer_story(entry: dict) -> bool:
    """厂商官网上「X 公司用我们的产品做到了 Y」这类客户案例／成果叙事。

    与 `is_promotional` 分开，因为**判据不同**：
      `is_promotional` 抓的是**广告形式**（白皮书/导购/赞助稿/价格）
      `is_customer_story` 抓的是**营销内容但非广告形式** —— 实测
      `openai-news` 落地 9 条里 5 条是这类，全部 truth=0.816，
      与真公告（GPT-6 发布、影响行动取缔通报）**同分同区，无法区分**。
      判据用标题的广告词完全抓不到它们（标题是「Sophos cuts … by 96%」）。

    实测样本（都是真阳性）：
      Sophos cuts threat investigation time by 96% with OpenAI Daybreak
      Asana cuts model costs 76x in browser tests with GPT-6.1 Sol
      How Oracle turns days of work into minutes with ChatGPT and Codex
      Pollo AI turns creative ideas into campaigns with OpenAI
      LegalOn halves Codex costs while maintaining development speed
      Radisson Hotel Group brings hotel discovery into ChatGPT

    形态：**一个具名第三方公司 + 一个数量化的收益 + 我们的产品**。
    """
    t = (entry.get("title") or "").lower()
    s = (entry.get("summary") or "").lower()
    blob = f"{t} {s}"
    if any(k in blob for k in ("customer story", "case study", "客户案例", "客户故事")):
        return True

    # 形态：一个具名第三方 + 一个「拿去做成了什么」的动作。
    #
    # ⚠️ 只保留**明确的客户叙事引子**和**第三方动作**。曾经加过
    # `how [a-z]+ is|are` 想覆盖「How Jump Trading is scaling…」，
    # 结果把 OpenAI 自己的公告也抓了（「How OpenAI is approaching text watermarking」
    # 「How to…」）—— 判据必须能区分「我们说我们怎么做」和「客户说他们怎么做」，
    # 而 `how X is -ing` 两者都匹配。所以去掉它，宁可漏判。
    cust = re.search(
        r"(?i)\b(discover how|learn how|see how)\b"
        # ★ 主体必须是**专有名词**（大写开头），不能是小写普通名词。
        # 实测误标：`[a-z]` 起头时，arXiv 论文摘要里
        # 「OpenAI agents exploited research infrastructure」也能匹配 ——
        # 「agents」被当成客户主体 → 一篇 agent 安全预印本被判成厂商客户案例。
        # 客户案例的主体永远是具名组织（Sophos / Asana / Oracle / Radiance…），
        # 所以要求大写。这条同时把「OpenAI turns…」这类厂商自述挡在外面，
        # 因为 OpenAI 自己不是自己的客户案例。
        r"|\b[A-Z][\w.&'’-]{1,30}\s+(?:uses?|used|partnered|teams? up|"
        r"cut|cuts|turns?|brings?|advances?|advancing|scales?|scaling|"
        r"halves?|helps?|expands?)\s+"
        r"(?:openai|chatgpt|codex|gpt|its|their|days|creative|hotel|quant|enterprise)"
        r"|\bexpand(?:s|ed|ing)? (?:its |their |the )?partnership\b",
        blob)
    # 「第三方公司 + 量化收益」同现在标题里，也算。
    quant_t = re.search(r"(?i)\b\d+(?:\.\d+)?\s*(?:x|×|%|percent|times|fold)\b", t)
    gain_t = re.search(r"(?i)\b(cuts?|cutting|halves?|halving|turns?|boosts?|speeds? up|"
                       r"saves?|reduces?|doubles?|triples?|slashes?|expands?)\b", t)
    return bool(cust or (quant_t and gain_t))


def promo_position(entry: dict) -> str | None:
    """Return 'title' | 'body' | None -- where the promotional signal sits."""
    title = (entry.get("title") or "").lower()
    body = (entry.get("summary") or "").lower()
    if any(k in title for k in PROMO_TITLE_MARKERS):
        return "title"
    if any(k in body for k in PROMO_TITLE_MARKERS + PROMO_BODY_MARKERS):
        return "body"
    # 价格型导购：标题里出现价格 + 型号 → 商品宣发。
    # 实测漏网：正则曾要求 元 紧跟数字，于是 '23.98 万元起'（汽车/家电/房产）
    # 全部漏过，一轮里所有真实产品稿都没被抓到。万 必须是可选分组。
    if re.search(r"\d+(?:\.\d+)?\s*万元?\s*(?:起|起售|预售)", title):
        return "title"
    if (re.search(r"\d+(?:\.\d+)?\s*(?:万)?\s*元", title)
            and re.search(r"(?:Pro|Max|Plus|Ultra|SE|\d{2,4}\s*(?:GB|TB|寸|英寸))", title)):
        return "title"
    return None


def is_promotional(entry: dict) -> bool:
    """这一条【是不是】广告/导购/赞助/客户案例。

    实测两个教训：
      1. 不能按【源标签】判。曾对整个 "OpenAI News" 源无条件返回 True —— 而该源
         的 label 就是 "OpenAI News"，于是整个官源恒真降分，连
         'Disrupting AI-enabled false front operations'（安全治理硬新闻）和
         'Helping teens learn…'（公益教育）都被丢掉。那 8 条当时恰好全是客户案例，
         但那是碰巧，不是判据。
      2. 判据必须看【位置】。见 PROMO_TITLE_MARKERS 的注释。
    """
    return promo_position(entry) == "title"


# 正文可核验性下限。实测：`summary` 字面值 "Comments"（HN 的 RSS 就是这么给的）
# 6 个字符拿到与 1900 字符摘要同等的真值分；另有 16 条【摘要完全为空】的条目
# 过了真值闸门进待观察区，其中 PNAS 一条拿到 peer-reviewed 0.896。
# 没有正文就没有可核验的主张 —— 真值分不该过闸。
BODY_MIN_CHARS = 30
PLACEHOLDER_SUMMARIES = {"comments", "comment", "no description", "摘要", "正文",
                         "read more", "详情", "原文", "查看全文"}


def body_missing(entry: dict) -> bool:
    s = (entry.get("summary") or "").strip()
    return len(s) < BODY_MIN_CHARS or s.lower() in PLACEHOLDER_SUMMARIES


