"""Stage-5 relevance: did the briefing still answer the question?

Ported from v1. Coverage is directional (what fraction of YOUR keywords the
result covers), never symmetric similarity.
"""
from __future__ import annotations

import re

from .registry import DOMAIN_TOKENS

DEFAULT_RELEVANCE_MIN = 0.6

# Words too generic to prove relevance on their own. If "AI" were allowed to
# count as coverage for an "AI 安全" request, the gate would never fire.
STOPWORD_TOKENS = {
    "the", "and", "for", "with", "that", "this", "from", "最新", "进展", "新闻",
    "消息", "情况", "相关", "方面", "一个", "什么", "怎么", "如何", "为什么",
    "ai", "人工智能",           # domain marker, not an intent keyword
    "news", "update", "latest", "about", "report",
}


def intent_keywords(request: str, explicit: list[str] | None = None) -> list[str]:
    """Keywords that define what the user actually wants.

    When BOTH `--request` and `--keywords` are given, the request's own tokens are
    MERGED IN rather than discarded. Measured bug: passing a request plus a few
    explicit keywords silently dropped every keyword that appeared only in the
    request text, so a user who wrote "夜间门诊 银鳕鱼 汞超标" as the request and
    then listed two of the three saw 汞 never evaluated at all.
    """
    raw: list[str] = []
    if explicit:
        raw += [k.strip() for k in explicit if k and k.strip()]
    if request:
        raw += re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{1,}|[\u4e00-\u9fff]{2,}", request)
    out, seen = [], set()
    for k in raw:
        low = k.lower()
        if low in STOPWORD_TOKENS or low in seen:
            continue
        seen.add(low)
        out.append(k)
    return out[:12]


def keyword_coverage(keywords: list[str], entries: list[dict],
                     strict: bool = True) -> dict:
    """Fraction of the user's keywords covered by the EXTRACTED items.

    Matching is case-insensitive substring over title + summary (+ merged sibling
    titles). Any number or proper noun counts double: losing a number the user
    asked for is a worse drift than losing a topic word.

    `strict` only matches the entries' own evidence text. Do NOT loosen this to
    "did the report mention the word anywhere": measured failure -- keywords
    {安全, 对齐, 风险} scored a perfect 1.0 against a pool of generic AI papers,
    because some paper mentioned "risk" in passing. Incidental mentions must not
    count as coverage or the gate never fires.
    """
    if not keywords:
        # ★ 不能返回 coverage=1.0。实测问题：没有关键词时这里给 1.0，控制台就打
        # 「关联度 1.0 / 阈值 0.6 [OK]」—— 而 `entries_checked` 是 0，闸门根本
        # 没跑。这正是本文件自己警告过的那件事：
        #   "a gate that silently passes everything is worse than no gate,
        #    because the report still claims it checked."
        # 没有可量的东西就说「没量」，不要报一个满分。
        return {"coverage": None, "checked": False, "keywords": [], "missing": [],
                "matched": [], "entries_checked": 0,
                "note": "未提供关键词，关联度【未检查】—— 不要把它读成通过"}

    def haystack(e: dict) -> str:
        parts = [e.get("title") or "", e.get("summary") or "",
                 " ".join(e.get("also_titles") or [])]
        return " ".join(parts).lower()

    hay = [haystack(e) for e in entries]
    matched, missing, weighted_hit, weighted_total = [], [], 0.0, 0.0
    for kw in keywords:
        low = kw.lower()
        weight = 2.0 if re.search(r"\d", kw) or re.match(r"^[A-Z]", kw) else 1.0
        weighted_total += weight
        if any(low in h for h in hay):
            matched.append(kw)
            weighted_hit += weight
        else:
            missing.append(kw)
    coverage = round(weighted_hit / weighted_total, 3) if weighted_total else 1.0
    return {"coverage": coverage, "keywords": keywords, "matched": matched,
            "missing": missing, "entries_checked": len(entries)}


def classify_request(request: str, explicit_domains: list[str] | None = None) -> dict:
    """Decide whether the request names CATEGORIES or TOPICS, and say why.

    Returns the verdict, a confidence in [0,1], and the matched tokens, so the
    classification can BE AUDITED. Measured need: a stage-1 misclassification
    propagates through every later stage and nothing downstream can detect it --
    stage 3 checks whether content is true, never whether the category was right.
    Recording the reason is the only way that mistake becomes visible.

    Rule change (was a measured bug): a single category hit is now ALWAYS a
    classifier request, whether or not --domain was passed. Before, `hits == 1`
    without --domain fell through to the keyword path, and Chinese requests then
    yielded one whole-clause "keyword" ('科学界有什么进展') that matched nothing,
    driving relevance to ~0 and firing a spurious second search.
    """
    text = (request or "").lower()
    if not text:
        return {"verdict": "keyword", "confidence": 0.0, "hits": 0,
                "matched": {}, "reason": "无 request 文本，默认按关键词型处理"}

    matched: dict[str, list[str]] = {}
    for dom, tokens in DOMAIN_TOKENS.items():
        got = [t for t in tokens if t in text]
        if got:
            matched[dom] = got
    hits = len(matched)

    if hits >= 2:
        conf, reason = 0.95, f"命中 {hits} 个类：{'/'.join(matched)}"
    elif hits == 1:
        dom = next(iter(matched))
        conf = 0.85 if explicit_domains else 0.70
        reason = (f"命中 1 个类「{dom}」（{','.join(matched[dom])}）"
                  + ("，且显式给了 --domain" if explicit_domains else
                     "，无 --domain；单类命中一律按分类型处理（否则中文整句会误触发二次搜寻）"))
    else:
        conf, reason = 0.30, "未命中任何类词，按关键词型处理"
    return {"verdict": "classifier" if hits >= 1 else "keyword",
            "confidence": conf, "hits": hits, "matched": matched, "reason": reason}


def is_classifier_request(request: str, explicit_domains: list[str] | None = None) -> bool:
    """Backwards-compatible boolean wrapper around classify_request()."""
    return classify_request(request, explicit_domains)["verdict"] == "classifier"


# 中文虚词：一句话里出现 2 个以上，基本可以断定是"整句"而不是"术语"。
# 用来把「给我社会和」「科学界有什么进展」这类从句子里剔出去。
CLAUSE_FUNCTION_CHARS = set("的了和与及或在是有为对从把被给这那什么怎么如何为什么"
                            "吗呢吧啊呀哦嗯个一二三四五六七八九十")

# 单字领域术语：单字中文关键词通常是噪声，但这些是真实可检索的术语。
# 不处理它，用户写「汞超标」里的「汞」就不算有效关键词。
SINGLE_CHAR_TERMS = {
    "汞", "砷", "铅", "镉", "铬", "镍", "锌", "铜", "硒", "碘", "氟", "氯",
    "癌", "癌", "糖", "盐", "油", "米", "茶", "酒", "药", "煤", "钢", "铝",
}


def effective_keyword_count(keywords: list[str]) -> int:
    """How many keywords can actually carry the relevance check.

    Measured problem: Chinese has no spaces, so extraction yields WHOLE CLAUSES
    ('科学界这几天有什么进展'), which can never match an article. Counting those as
    real keywords makes the gate fire a second search on every Chinese request.

    Two distinct failure shapes to reject, both measured:
      - a whole CLAUSE that happens to be 2-8 chars ('给我社会和' is 5 chars)
      - a clause containing non-CJK content
    and one to ACCEPT that a naive length rule misses:
      - a genuine single-character term ('汞' in '汞超标')
    """
    n = 0
    for k in keywords or []:
        s = (k or "").strip()
        if not s:
            continue
        cjk = re.findall(r"[\u4e00-\u9fff]", s)
        if cjk:
            # only pure-CJK strings can be Chinese terms
            if len(cjk) != len(s):
                continue
            if len(s) == 1:
                if s in SINGLE_CHAR_TERMS:
                    n += 1
                continue
            if not (2 <= len(s) <= 8):
                continue
            # Two independent clause signals, both measured:
            #   a) 2+ function characters anywhere  -> '给我社会和' (给/和)
            #   b) a function character at the START -> '的信息' (only one function
            #      char, so (a) misses it, but a real term does not begin with 的)
            if s[0] in CLAUSE_FUNCTION_CHARS:
                continue
            if sum(1 for ch in s if ch in CLAUSE_FUNCTION_CHARS) >= 2:
                continue
            n += 1
        else:
            # ASCII / mixed: length by letters, and generic words are not keywords
            letters = re.sub(r"[^A-Za-z]", "", s)
            if 2 <= len(letters) <= 30 and letters.lower() not in STOPWORD_TOKENS:
                n += 1
    return n
