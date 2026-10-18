"""Event-level de-duplication.

Titles for one event differ across outlets, so matching on the exact string
under-counts corroboration. Ported from v1.
"""
from __future__ import annotations

import hashlib
import re

def title_tokens(title: str) -> set:
    """Token set for near-duplicate matching across sources.

    Latin text splits on words; CJK has no spaces so it contributes character
    bigrams (a single char is too coarse: 美国 and 中国 would look alike).
    """
    t = (title or "").lower()
    words = set(re.findall(r"[a-z0-9]{3,}", t))
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    words |= {"".join(pair) for pair in zip(cjk, cjk[1:])}
    return words


def is_same_event(a: set, b: set, threshold: float = 0.62) -> bool:
    """Jaccard similarity on title tokens.

    The absolute-overlap floor is 2, not 3. Measured bug with 3: a short title
    yields few long tokens ("Atari Falcon" -> {atari, falcon}), so the floor
    rejected EVERY pair -- including titles that were byte-identical. Three such
    duplicate pairs survived into the output, inflating `kept`.
    """
    if not a or not b:
        return False
    inter = len(a & b)
    if inter < 2:
        return False
    union = len(a | b)
    return (inter / union) >= threshold


# Tokens that are pure framing, not part of the event. Stripped before computing an
# event signature so the SAME event worded two ways gets ONE id.
# Measured failure: '广西贵港闪爆事故2名失联人员确认已无生命体征' and
# '...最新进展：2名失联人员确认已无生命体征' produced different ids even though
# is_same_event() said they were the same event -- and my test passed anyway
# because it compared two IDENTICAL strings. A test that cannot fail is not a test.
EVENT_NOISE_TOKENS = {
    "最新", "进展", "最新进展", "确认", "已", "通报", "回应", "消息", "报道",
    "情况", "相关", "记者", "来源", "视频", "图片", "组图", "快讯", "独家",
    "breaking", "update", "latest", "report", "video", "photos",
}


def event_signature(title: str) -> str:
    """Normalise a title to the token set that identifies the EVENT.

    Three normalisations, each added because a real pair of wordings diverged:

      1. Strip parenthetical attributions: （市场监管总局） / [Reuters] / (Reuters)
      2. Strip a LEADING attribution prefix: 「市场监管总局：<事件>」 and
         「<事件>（市场监管总局）」 are the same event announced by the same body,
         and both forms occur in the wild. Measured: they hashed differently.
      3. Remove framing words from the RAW STRING before tokenising.
         Chinese has no spaces, so '最新进展' lives inside cross-boundary bigrams
         ('出最', '最新', '新进', '进展', '展：'); a token-set filter removes none of
         them, and the two wordings still hashed differently.
    """
    t = (title or "")
    t = re.sub(r"[（(\[][^）)\]]{1,20}[）)\]]", " ", t)
    # The prefix strip must be RESTRICTED to institution names. A loose
    # `^\S{2,12}：` also ate '广西贵港闪爆事故2名失联人员确认' from a title whose colon
    # was content, not attribution -- measured regression.
    t = re.sub(
        r"^\s*[\u4e00-\u9fff]{2,10}(?:总局|总署|管理局|委员会|监管局|政府|法院|"
        r"检察院|气象台|统计局|局|部|委|署|台|中心|公司|集团|协会|大学|医院)"
        r"\s*[：:]\s*", "", t)
    for noise in sorted(EVENT_NOISE_TOKENS, key=len, reverse=True):
        if len(noise) >= 2:
            t = t.replace(noise, " ")
    toks = title_tokens(t)
    return "|".join(sorted(toks))


def event_id(title: str) -> str:
    """Stable id for an EVENT, derived once and reused everywhere downstream.

    Measured need: dedupe, merging, cross-checking and heat-matching each derived
    their own notion of "same event", with different tokenisation and thresholds.
    One id means one definition.

    It is a matching AID, not a hash of the exact string: framing words are
    stripped first, so rewording does not change the id. Two DIFFERENT events can
    still collide if their content tokens coincide -- use is_same_event (Jaccard)
    when you need a graded closeness rather than an identity.
    """
    sig = event_signature(title)
    if not sig:
        return ""
    return hashlib.sha1(sig[:200].encode("utf-8")).hexdigest()[:16]


def entity_id(name: str) -> str:
    """Canonical identity for an outlet/organisation name, for cross-checking.

    ★ 这个函数**必须**委托给 identity_of —— 两个函数回答的是同一个问题。
    实测教训：修 identity_of 的过归一 bug 时（它曾返回 platform_of 的**权重桶**，
    导致新京报/南方都市报被判同一家、真实多源印证被吞），只改了 identity_of，
    entity_id 留着自己的 `platform_of(...)` 副本。于是补的 7 条
    「不应合并」断言全部失败 —— **测试抓到了不完整修复，这正是它该做的。**
    修法：删掉副本，只保留一个实现。
    """
    return identity_of("", str(name or ""))

