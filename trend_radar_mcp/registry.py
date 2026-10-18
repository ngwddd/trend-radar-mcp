"""Source registry, credibility tables, outlet identity and evidence typing.

All of it is DATA plus small pure functions. Nothing here touches the network.
Ported from v1: the weights encode measured failures (see the inline notes).
"""
from __future__ import annotations

import re

DOMAINS = {
    "social": "社会/人文（热点新闻、公共事件、政策）",
    "science": "科学进步（生物医学、大刊、科学进展）",
    "ai": "AI（模型、方法、基准、产业）",
}

# Errata are registered with Crossref as ordinary `journal-article`, so the
# server-side type filter does NOT remove them (measured). They must be dropped
# client-side or every journal feed is polluted with corrections.
ERRATA_RE = re.compile(
    r"(?i)^\s*(publisher|author|editor)?\s*correction\b|^\s*erratum\b|"
    r"^\s*retraction\b|^\s*retracted\b|expression of concern|"
    r"^\s*corrigendum\b|^\s*addendum\b|^\s*withdrawn\b"
)
ERRATA_DROP = True

SOURCE_WEIGHT = {
    # -- primary: origin of the claim --
    "crossref-science":    0.90,   # peer-reviewed, top-tier
    "crossref-nature":     0.90,
    "crossref-nejm":       0.90,
    "crossref-lancet":     0.90,
    "crossref-cell":       0.90,
    "crossref-natmed":     0.90,
    "crossref-natbiotech": 0.88,
    "crossref-natmi":      0.88,
    "crossref-natcomp":    0.86,
    "crossref-sciadv":     0.86,
    "crossref-pnas":       0.86,
    # 官方口径但自我利益相关：0.85 → 0.78。
    # 实测问题：0.85 让营销内容拿到 0.844，【高于】同行评审的 PNAS 0.896 只差一点，
    # 而 8 条客户案例全靠一个固定的 −0.25 才被压下去 —— 0.844−0.25=0.594，
    # 距阈值仅差 0.006，任何微调都让 8 条整体翻转。那是精确击穿，不是稳健判定。
    # 降低源权重是诚实的做法：官方声明本来就该低于独立评审的研究。
    "openai-news":         0.78,
    "arxiv-ai":            0.62,   # preprint: moderated, not peer-reviewed
    "arxiv-rss-ai":        0.62,
    "biorxiv":             0.60,
    "medrxiv":             0.58,   # clinical preprints: weakest primary tier
    # -- secondary: verified reporting about someone else's claim --
    "techreview":          0.78,
    "ieee-spectrum":       0.76,
    "ars-technica":        0.72,
    "phys-org":            0.62,
    "science-daily":       0.58,
    "hn-frontpage":        0.45,
    "hn-top":              0.45,
    "reddit-ml":           0.40,
    "ithome":              0.52,   # fast, lightly edited
    "36kr":                0.48,   # paid placement mixed in
    # 中新社主办的中新网。**实测它原本不在本表里**，拿的是 `SOURCE_WEIGHT.get(k, 0.50)`
    # 那个兜底默认值 —— 等于让一个未声明的默认值替我做权重决定，这不可审计。
    # 显式登记并写明理由（与同级中文源对比）：
    #   ithome 0.52  —— 快，但轻编辑
    #   36kr   0.48  —— 掺付费软文
    # 中新网取 0.50：通稿社背景、有编辑流程，但仍是**二次报道**
    # （它报的是别人的一手，不是自己的发现），所以不给到通讯社级。
    # ⚠️ 这个权重意味着**单源过不了真值闸门**（0.4×0.50+0.6×0.55 = 0.53 < 0.6），
    # 只能靠多家独立印证（+0.12 印证加成）才过。
    # **这是有意的**：社会新闻天然是"多家都报"，独立印证正是这个域该用的尺子。
    # 想让单源直接过闸就得把权重抬到 0.90 一档，那是拿打分迁就结果，不做。
    "chinanews":           0.50,
    # ── 中文 AI / 科技垂媒（2026-10-10 新增，全部二次报道）─────────────
    # 都是转述别人的发布，所以压在 0.50–0.62：低于同行评审与官方一手，
    # 高于 36kr（0.48，掺付费软文）。
    "infoq-cn":            0.62,   # 工程实践向，编辑质量最高的一档
    "qbitai":              0.58,   # 原创报道比例高
    "solidot":             0.58,   # 技术社区视角，短讯为主
    "leiphone":            0.55,   # AI 产业向
    "geekpark":            0.55,   # 产品/产业向
    "tmtpost":             0.52,   # 科技商业向，软文比例略高
    "oschina":             0.50,   # 开源生态，量大但编辑轻
    # ── 中文专业媒体 ────────────────────────────────────────────────
    "jiemian":             0.74,   # 对齐已有的 yicai（第一财经）档
    "eeo":                 0.72,   # 财经深度，同档取低
    # ── 中文国际视角（立场明确，压在大众媒体档）─────────────────────
    "dw-zh":               0.55,
    "bbc-zh":              0.55,
    "nyt-zh":              0.55,
    "rfi-zh":              0.52,
    # -- trend boards: popularity signal, NOT a credibility signal --
    "toutiao-hot":         0.35,
    "google-trends-us":    0.35,
}

PRIMARY_SOURCES = {
    "crossref-science", "crossref-nature", "crossref-nejm", "crossref-lancet",
    "crossref-cell", "crossref-natmed", "crossref-natbiotech", "crossref-sciadv",
    "crossref-natmi", "crossref-natcomp",
    "crossref-pnas", "openai-news", "arxiv-ai", "arxiv-rss-ai", "biorxiv", "medrxiv",
}

# Tier base for the evidence type actually attached to a claim.
# `journalism` exists because Crossref files Nature's front-matter (books in
# brief, picture essays, briefing chats) as ordinary journal-articles. Scoring
# those as peer-reviewed put a books column at the top of the run.
EVIDENCE_WEIGHT = {
    "peer-reviewed": 0.92,   # top tier: independent review before publication
    "official":      0.84,   # first-party statement: authoritative but self-interested
    "dataset":       0.78,
    # `journalism` sits BELOW preprint/secondary, not level with them.
    # Measured inversion: at 0.55 a Nature books column scored 0.69, above a bioRxiv
    # preprint (0.57) and an IEEE empirical report (0.634) -- a piece with no claim
    # ranked above research. Source weight dominates this formula (0.4 x 0.90 for
    # Nature), so capping the evidence term was not enough; the term itself has to
    # reflect that a review/photo-essay/briefing carries nothing to verify.
    # 0.30 keeps it above `rumor` while putting it below preprints. Measured result:
    # Nature books column 0.54, below bioRxiv 0.57.
    "journalism":    0.30,
    "secondary":     0.55,   # a report about someone else's claim
    "preprint":      0.55,   # moderated screening only, no peer review
    "rumor":         0.25,
}

# An independent stream carrying the same claim is real corroboration.
CROSS_CONFIRM_BONUS = 0.12
CROSS_CONFIRM_CAP = 1.0

# The gate the pipeline uses on 真实 / 传播报道.
CREDIBILITY_THRESHOLD = 0.6

# 单源硬证据通道的合格证据类型。
#
# ★ 只有 `peer-reviewed`。理由：这是唯一**本身就含独立核验**的类型 ——
# 同行评审是第三方做的。`official` 虽然是一手，但**没有独立核验**，
# 而且官方是自我利益相关方（厂商说自己的模型好，那是自述不是证据）。
# 实测教训：一开始把 official 也放进来，结果 openai-news 那 6 条客户案例
# 被"official"这个标签护送进了核心区 —— 营销内容进了本该放最硬证据的地方。
# 收紧到只剩 peer-reviewed 之后，核心区是 1 条 Nat Comput Sci 论文。
#
# 刻意【不】用源权重阈值来判（如 source_weight>=0.9）：那会变成第二套层级实现，
# 而实测教训是「同一件事两处实现必然打架」（identity_of/entity_id 就是活例）。
HARD_EVIDENCE_TYPES = frozenset({"peer-reviewed"})

# 评分过程中发现的「登记了但没给权重」的源。见 truth_weight 里的说明。
# 用集合而不是抛异常：一次跑批不该因为一个漏登记就整体失败，
# 但这件事必须能被查出来（`--list` 会打印，测试会断言为空）。
UNWEIGHTED_SOURCES: set[str] = set()

PLATFORM_WEIGHT = {
    # official / primary notices
    "gov-cn":       0.92, "cctv":       0.88, "xinhua":     0.88,
    # first-hand investigative reporting with named outlets
    "thepaper":     0.78, "people":     0.78, "guangming":  0.76,
    "cnr":          0.74, "chinadaily": 0.74, "yicai":     0.74,
    "stcn":         0.72, "bjnews":     0.72, "caixin":    0.80,
    # professional media, mostly secondary
    "36kr":         0.48, "ithome":     0.52, "ars-technica": 0.72,
    "techreview":   0.78, "ieee-spectrum": 0.76,
    # aggregators / community: lead only
    "toutiao":      0.35, "baidu":      0.35, "weibo":     0.35,
    "hn":           0.45, "reddit":     0.40,
    # unknown outlet: sits below the gate on purpose -- an unrecognised masthead
    # must not be able to clear 真实 just by existing
    "unknown":      0.40,
}

# Evidence FORM inside social reporting. This is the dominant axis for social.
SOCIAL_EVIDENCE_FORM = {
    "official-notice":   0.92,   # 官方通报/公告/法规原文
    "first-hand-report": 0.78,   # 一手调查，有具名记者/媒体/现场
    "named-entity":      0.68,   # 具名主体 + 具体数字，但转述
    "secondary-report":  0.52,   # 二手转述，无一手痕迹
    "unattributed":      0.30,   # 无主体、无出处、纯转述
    "rumor":             0.25,
}

# Concrete-detail regexes. Specificity is what separates reportable social news
# from vague chatter, and unlike a masthead it can be MEASURED from the text.
RE_QUANTITY = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|亿元|万元|元|人|人次|辆|例|天|小时|分钟|吨|公里|km|"
    r"亿美元|万人|个|家|所|起|倍|万|亿|"
    # SI / engineering units are common in Chinese tech reporting ("1600N 踩踏力").
    # Missing these made a concrete, number-rich story classify as 'unattributed'.
    r"N|kN|W|kW|V|kV|Hz|GHz|MHz|nm|mm|cm|m|kg|g|ms|s|A|mAh|Wh|°C|℃)")
RE_ORG = re.compile(
    r"[\u4e00-\u9fff]{2,12}(?:局|部|委|厅|办|署|院|大学|学院|医院|公司|集团|银行|"
    r"协会|中心|委员会|政府|法院|检察院|研究院|研究所)")
RE_DATE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|20\d{2}\s*年|\d{4}-\d{2}-\d{2}")
RE_PLACE = re.compile(
    # Negative lookahead on 值/场/民/委/政: without it, "江淮两日市值" matches as a
    # place named "江淮两日市". Measured false positive.
    r"[\u4e00-\u9fff]{2,8}(?:省|市|区|县|镇|乡|村|街道|自治区)(?![值场民委政])")

# 事故/损失类锚点词。加了它之后，真事故通报才排得到导购文上面。
# Measured failure without it: '广西贵港闪爆事故2名失联人员确认已无生命体征'
# scored 3 -> 'secondary-report', while '智界 R7 焕新版…23.98 万元起' scored 4 and
# 'AOC 16" AMOLED…' scored 6 -> 'named-entity'. Real news ranked BELOW adverts.
# These terms are highly specific and cannot be padded the way spec sheets can.
RE_CASUALTY = re.compile(
    r"(?:失联|遇难|死亡|受伤|被困|身亡|伤亡|溺亡|坠亡)[\u4e00-\u9fff]{0,2}(?:人员|人数|人)?"
    r"|闪爆|爆燃|爆炸|火灾|坍塌|垮塌|中毒|泄漏|泄露|追尾|脱轨|坠机|沉没"
    r"|召回|停产|整顿|处罚|罚款|判刑|逮捕|立案|问责|免职|停职"
    r"|直接经济损失|受灾|转移安置|撤离")

# Domain classifier tokens. DECLARED as a limitation: these are keyword lists,
# so a mis-worded request can be mis-classified. The defence is that a
# classification request is only honoured for the categories it actually names.
DOMAIN_TOKENS = {
    "social": ("社会", "热点", "新闻", "民生", "公众", "政策", "事件", "舆情"),
    "ai":     ("ai", "人工智能", "模型", "算法", "大模型", "机器学习", "深度学习"),
    "science": ("科学", "科研", "论文", "期刊", "生物", "医学", "物理", "化学",
                "nature", "science", "研究进展"),
}

SOURCES: dict[str, dict] = {
    # ---- AI / 科技 ----
    "arxiv-ai": {
        "kind": "atom",
        "domains": ["ai"],
        "class": "authority",
        "group": "ai",
        "url": "http://export.arxiv.org/api/query?search_query=cat:{cat}&sortBy=submittedDate&sortOrder=descending&max_results={n}",
        "cats": ["cs.AI", "cs.LG", "cs.CL", "cs.CV"],
        "label": "arXiv (cs.AI/LG/CL/CV)",
        "status": "OK",
        "note": "预印本，未经同行评审；日期用 published（首次提交）",
    },
    "arxiv-rss-ai": {
        "kind": "rss",
        "domains": ["ai"],
        "class": "authority",
        "group": "ai",
        "url": "https://export.arxiv.org/rss/cs.AI",
        "label": "arXiv cs.AI daily RSS",
        "status": "OK",
        "note": "只含当日新公告，量大（约 1MB）",
    },
    "techreview": {
        "kind": "rss",
        "domains": ["ai", "science"],
        "class": "social",
        "group": "ai",
        "url": "https://www.technologyreview.com/feed/",
        "label": "MIT Technology Review",
        "status": "OK",
        "note": "专业媒体，介于两路之间；观点文多，需过滤",
    },
    "ieee-spectrum": {
        "kind": "rss",
        "domains": ["ai", "science"],
        "class": "social",
        "group": "ai",
        "url": "https://spectrum.ieee.org/feeds/feed.rss",
        "label": "IEEE Spectrum",
        "status": "OK",
        "note": "专业媒体，行业风向",
    },
    "openai-news": {
        "kind": "rss",
        "domains": ["ai"],
        "class": "authority",
        "group": "ai",
        "url": "https://openai.com/news/rss.xml",
        "label": "OpenAI News",
        "status": "OK",
        "note": "官方口径：是一手来源，但不是独立验证。不能自己证明自己",
    },
    "ars-technica": {
        "kind": "rss",
        "domains": ["ai", "science", "social"],
        "class": "social",
        "group": "general",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
        "label": "Ars Technica",
        "status": "OK",
        "note": "科技媒体，报道较扎实但仍是二手",
    },
    "hn-frontpage": {
        "kind": "rss",
        "domains": ["ai", "science"],
        "class": "social",
        "group": "general",
        "url": "https://news.ycombinator.com/rss",
        # HN returns HTTP 419 to browser-like User-Agents (measured) but serves a
        # plain client string fine, so this source overrides the default UA.
        "ua": "curl/8.4.0",
        "label": "Hacker News front page",
        "status": "OK",
        "note": "社区热度，不等于价值；仅作线索来源。必须用 curl UA，浏览器 UA 会被 419",
    },
    # ---- 中文 / 公众热点 ----
    "36kr": {
        "kind": "rss",
        "domains": ["social", "ai"],
        "class": "social",
        "group": "general",
        "url": "https://www.36kr.com/feed",
        "label": "36氪",
        "status": "OK",
        "note": "含大量软文与融资 PR",
    },
    "ithome": {
        "kind": "rss",
        "domains": ["social", "ai"],
        "class": "social",
        "group": "general",
        "url": "https://www.ithome.com/rss/",
        "label": "IT之家",
        "status": "OK",
        "note": "快但噪声大",
    },
    # ---- 学术期刊：直连被挡，走 Crossref ----
    "crossref-science": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0036-8075",
        "label": "Science (via Crossref)",
        "status": "OK",
        "note": "只拿到元数据+摘要，拿不到正文（science.org 直连 403）",
    },
    "crossref-nature": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0028-0836",
        "label": "Nature (via Crossref)",
        "status": "OK",
        "note": "只拿到元数据+摘要（nature.com RSS 实测返回 bot 挡页）",
    },
    # ---- 科学类补充：生物医学与综合顶刊 ----
    "crossref-sciadv": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "2375-2548",
        "label": "Science Advances (via Crossref)",
        "status": "OK",
    },
    "crossref-pnas": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0027-8424",
        "label": "PNAS (via Crossref)",
        "status": "OK",
    },
    "crossref-nejm": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0028-4793",
        "label": "NEJM (via Crossref)",
        "status": "OK",
        "note": "临床医学顶刊；含大量通信与试验注册，需过滤",
    },
    "crossref-lancet": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0140-6736",
        "label": "The Lancet (via Crossref)",
        "status": "OK",
    },
    "crossref-cell": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0092-8674",
        "label": "Cell (via Crossref)",
        "status": "OK",
    },
    "crossref-natmed": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "1078-8956",
        "label": "Nature Medicine (via Crossref)",
        "status": "OK",
    },
    "crossref-natbiotech": {
        "kind": "crossref",
        "domains": ["science", "ai"],
        "class": "authority",
        "group": "journal",
        "issn": "1087-0156",
        "label": "Nature Biotechnology (via Crossref)",
        "status": "OK",
    },
    # ---- AI-domain authoritative sources ----
    # Measured gap these fill: before them the ENTIRE `ai` authority stream was
    # arxiv-ai + arxiv-rss-ai + openai-news + crossref-natbiotech. Both arXiv
    # entries are preprints, and a preprint tops out at 0.578 < the 0.6 gate, so
    # the AI authority stream could never emit a gated item (measured: 0 of 32
    # passed, with the only truth-passers being OpenAI's own self-interested
    # posts). That is a coverage hole, not a content-quality verdict.
    "crossref-natmi": {
        "kind": "crossref",
        "domains": ["ai", "science"],
        "class": "authority",
        "group": "journal",
        "issn": "2522-5839",
        "label": "Nature Machine Intelligence (via Crossref)",
        "status": "OK",
        "note": "AI 领域少有的同行评审权威源；实测近 7 天有在线发表",
    },
    "crossref-natcomp": {
        "kind": "crossref",
        "domains": ["ai", "science"],
        "class": "authority",
        "group": "journal",
        "issn": "2662-8457",
        "label": "Nature Computational Science (via Crossref)",
        "status": "OK",
        "note": "计算方法与 AI 交叉；实测近 7 天有在线发表",
    },
    "biorxiv": {
        "kind": "rss",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=all",
        "label": "bioRxiv (全部学科)",
        "status": "OK",
        "note": "预印本。RDF 格式，<link> 是纯文本（已兼容）。30 条/次；标题常为全大写",
    },
    "medrxiv": {
        "kind": "rss",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "url": "https://connect.medrxiv.org/medrxiv_xml.php?subject=all",
        "label": "medRxiv (临床预印本)",
        "status": "OK",
        "note": "预印本，未经同行评审。临床结论尤其不能当已证实",
    },
    "phys-org": {
        "kind": "rss",
        "domains": ["science"],
        "class": "social",
        "group": "journal",
        "url": "https://phys.org/rss-feed/",
        "label": "Phys.org",
        "status": "OK",
        "note": "科学新闻聚合，二手报道；常把预印本写成「研究显示」，需回溯原文",
    },
    "science-daily": {
        "kind": "rss",
        "domains": ["science"],
        "class": "social",
        "group": "journal",
        "url": "https://www.sciencedaily.com/rss/all.xml",
        "label": "ScienceDaily",
        "status": "OK",
        "note": "二手科普报道，标题夸张倾向明显，必须回溯一手论文",
    },
    # ---- 热度榜：主窗口在等待两个子代理时抓这一路 ----
    # These are the ONLY sources ranked by POPULARITY rather than recency.
    # Popularity is not importance -- treat as 风向, never as 事实.
    "toutiao-hot": {
        "kind": "toutiao_hot",
        "group": "trend",
        "domains": ["social"],
        "class": "trend",
        "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "label": "头条热榜 (official JSON)",
        "status": "OK",
        "note": "官方 JSON，含热度值 HotValue。按热度排序",
    },
    "hn-top": {
        "kind": "rss",
        "group": "trend",
        "domains": ["ai", "science", "social"],
        "class": "trend",
        "url": "https://news.ycombinator.com/rss",
        "ua": "curl/8.4.0",
        "label": "Hacker News (热度近似：排名序)",
        "status": "OK",
        "note": "RSS 无热度数值，靠条目顺序近似。URL 取原文链接，讨论页另存 discussion_url。必须用 curl UA",
    },
    "reddit-ml": {
        "kind": "rss",
        "group": "trend",
        "domains": ["ai"],
        "class": "trend",
        "url": "https://www.reddit.com/r/MachineLearning/top/.rss?t=day",
        "label": "r/MachineLearning 日榜",
        "status": "OK",
        "note": "真·按热度排序（top + t=day）。社区热度仅作线索。Reddit 会 429，只登记一次、别再加同 URL 的第二个 key",
    },
    "google-trends-us": {
        "kind": "google_trends",
        "group": "trend",
        "domains": ["social"],
        "class": "trend",
        "url": "https://trends.google.com/trending/rss?geo=US",
        "label": "Google Trends (US daily)",
        "status": "OK",
        "note": "英文世界搜索热度，含 approx_traffic 与原文链接；但榜单本体大量是人名/娱乐，"
                "只有 related_news 有信息量。geo=CN 实测 HTTP 400，不可用",
    },

    # ── 中文社会民生一手源 ──────────────────────────────────────────────
    # 为什么必须加：实测 `--domain social --class social` 只有 3 个源，
    # 且**全是科技媒体**（36氪创投 / IT之家 3C / Ars 英文科技）——
    # 真正的社会民生中文源一个都没有。后果是社会类每跑一次，
    # 八成内容靠 web_search 补，不是"有源可依"。
    #
    # `kind: rss_multi` 是为此新加的：中新网的 4 个版面是【不同内容】，
    # 不是同内容镜像，所以不能用 `urls`（那个字段的语义是
    # 「候选镜像，取第一个非空的」，见 L855 `urls = spec.get("urls") or [spec["url"]]`）。
    # 也不该拆成 4 个源 —— 那会污染 `scanned` 源计数与降级分母
    # （用户看到"7/11 源成功"，其实只有 8 家媒体）。
    "chinanews": {
        "kind": "rss_multi",
        "group": "general",
        "domains": ["social", "science"],
        "class": "social",
        # 实测 2026-10-10：4 个 feed 全部 HTTP 200、30 条、首条当天 21:47–21:49、带摘要。
        # `gj.xml`（国际）HTTP 404 —— 不登记猜测的 URL。
        # 注意必须用带 `www.` 的域名，裸域名会重定向。
        "feeds": [
            "https://www.chinanews.com.cn/rss/scroll-news.xml",   # 滚动（时效最强）
            "https://www.chinanews.com.cn/rss/society.xml",       # 社会
            "https://www.chinanews.com.cn/rss/china.xml",         # 国内
            "https://www.chinanews.com.cn/rss/finance.xml",       # 财经
            # 2026-10-10 实测新增（全部 200 / 首条当天）：
            "https://www.chinanews.com.cn/rss/world.xml",         # 国际
            "https://www.chinanews.com.cn/rss/health.xml",        # 健康
            "https://www.chinanews.com.cn/rss/edu.xml",           # 教育
            # ⚠️ `gj.xml` 实测 HTTP 404 —— 国际版面的正确路径是 `world.xml`。
            #    不登记猜测的 URL（这条是实测确认过的）。
        ],
        "label": "中国新闻网",
        "status": "OK",
        "note": "中新社主办，社会民生中文一手源。4 个版面【并发抓取后合并去重】。"
                "实测首条时间戳=当天，不是停更存档源"
                "（对比：人民网 RSS 返回 200+100 条但内容是 2025-06 的、"
                "新华网 200+300 条是 2022-12 的 —— 那类静默陷阱比 404 危险得多）",
    },

    # ── 中文 AI / 科技垂媒（2026-10-10 实测新增）────────────────────────
    # 背景：此前 `--domain ai` 的中文侧几乎是空的 —— 中国大模型发布、
    # 国内 AI 产业动向**连续多次跑批 0 覆盖**，只能靠 web_search 补。
    # 下面 6 个源全部实测：HTTP 200 / 首条=当天 / 有摘要。
    #
    # 权重区间 0.50–0.62：都是**二次报道**（转述别人的发布），
    # 所以低于同行评审（0.86–0.92）与官方一手（0.78–0.85），
    # 高于 36kr（0.48，掺付费软文）。
    # 单源过不了 0.6 真值闸门（0.4×0.55+0.6×0.55≈0.55）—— **这是有意的**，
    # 国内 AI 新闻天然多家都报，靠独立印证加成（+0.12）才是这个域该用的尺子。
    "qbitai": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.qbitai.com/feed",
        "label": "量子位", "status": "OK",
        "note": "中文 AI 垂媒，原创报道比例高。实测 1.1s / 10 条 / 首条当天",
    },
    "infoq-cn": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.infoq.cn/feed",
        "label": "InfoQ 中文", "status": "OK",
        "note": "工程实践向，偏架构与落地。实测 0.9s / 20 条 / 首条当天。"
                "⚠️ 正文页常需 JS 渲染，web_fetch 只拿到标题",
    },
    "oschina": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.oschina.net/news/rss",
        "label": "开源中国", "status": "OK",
        "note": "开源生态向，量大（50 条）。实测 3.3s / 首条当天",
    },
    "leiphone": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.leiphone.com/feed",
        "label": "雷锋网", "status": "OK",
        "note": "AI 产业向。实测 1.9s / 20 条 / 首条当天。"
                "⚠️ 其 AI 子站 leiphone-ai 实测 HTTP 500，不要用那个路径",
    },
    "tmtpost": {
        "kind": "rss", "group": "ai", "domains": ["ai", "social"], "class": "social",
        "url": "https://www.tmtpost.com/rss.xml",
        "label": "钛媒体", "status": "OK",
        "note": "科技商业向，兼有产业与政策。实测 3.6s / 18 条 / 首条当天",
    },
    "geekpark": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.geekpark.net/rss",
        "label": "极客公园", "status": "OK",
        "note": "产品与产业向。实测 3.8s / 30 条 / 首条当天",
    },
    "solidot": {
        "kind": "rss", "group": "ai", "domains": ["ai", "science"], "class": "social",
        "url": "https://www.solidot.org/index.rss",
        "label": "Solidot", "status": "OK",
        "note": "奇客资讯，科技/科学短讯，倾向技术社区视角。"
                "实测 2.6s / 20 条 / 首条当天",
    },

    # ── 中文专业媒体（2026-10-10 实测新增）─────────────────────────────
    "jiemian": {
        "kind": "rss", "group": "general", "domains": ["social", "ai"], "class": "social",
        "url": "https://a.jiemian.com/index.php?m=article&a=rss",
        "label": "界面新闻", "status": "OK",
        "note": "上海报业旗下财经/商业媒体。实测 5.1s / 30 条 / 首条当天。"
                "权重 0.74 对齐已有的 yicai（第一财经）档",
    },
    "eeo": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://www.eeo.com.cn/rss.xml",
        "label": "经济观察报", "status": "OK",
        "note": "财经深度报道。实测 1.4s / 11 条 / 首条当天（法治/社会类也进）",
    },

    # ── 中文国际视角（2026-10-10 实测新增）─────────────────────────────
    # ⚠️ 权重统一压在大众媒体档（0.52–0.55），且 note 必须写明立场属性 ——
    # 它们有编辑标准，但涉华议题立场明确，不能当不带观点的通稿源用。
    "dw-zh": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://rss.dw.com/rdf/rss-chi-all",
        "label": "德国之声中文", "status": "OK",
        "note": "德国公共广播。实测 1.3s / 53 条 / 首条当天。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场",
    },
    "bbc-zh": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
        "label": "BBC 中文", "status": "OK",
        "note": "实测 0.7s / 38 条 / 首条当天。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场",
    },
    "rfi-zh": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://www.rfi.fr/cn/rss",
        "label": "法国国际广播电台中文", "status": "OK",
        "note": "实测 0.2s / 30 条 / 首条当天。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场",
    },
    "nyt-zh": {
        "kind": "rss", "group": "general", "domains": ["social", "ai"], "class": "social",
        "url": "https://cn.nytimes.com/rss/",
        "label": "纽约时报中文网", "status": "OK",
        "note": "实测 0.3s / 20 条 / 首条 10-09。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场。"
                "⚠️ `/rss/technology/` 实测与根 feed 首条相同，疑为同一源，未单独登记",
    },
}

BLOCKED = [
    ("https://www.nature.com/nature.rss", "HTTP 200 但是 Fastly bot 挡页 (text/html, /_fs-ch-)"),
    ("https://www.science.org/rss/news_current.xml", "HTTP 403"),
    ("https://www.cell.com/cell/current.rss", "HTTP 403"),
    ("https://rsshub.app/zhihu/hotlist", "HTTP 403"),
    ("https://api.semanticscholar.org/graph/v1/...", "HTTP 429 限流"),
    ("https://hnrss.org/frontpage", "超时（改用 news.ycombinator.com/rss 正常）"),
    # 热度榜类：都试过，都不通
    ("https://weibo.com/ajax/side/hotSearch", "HTTP 403（微博热搜 API）"),
    ("https://s.weibo.com/top/summary", "HTTP 200 但返回 HTML 壳，热度值靠 JS 渲染，抓不到"),
    ("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total", "HTTP 401 需要授权"),
    ("https://www.douyin.com/aweme/v1/web/hot/search/list/", "HTTP 200 但返回 0 字节"),
    ("https://trends.google.com/trending/rss?geo=CN", "HTTP 400（只支持部分地区）"),
    ("https://github.com/trending", "HTTP 200 但纯 HTML，无 feed"),
    ("https://rsshub.app/weibo/search/hot", "HTTP 403（公共实例封禁）"),
]

SOURCE_IDENTITY = {
    "hn-frontpage": "hackernews",
    "hn-top": "hackernews",
    # Both front the same preprint server; an arXiv paper appearing in the API
    # feed AND the daily RSS is one source, not two.
    "arxiv-ai": "arxiv",
    "arxiv-rss-ai": "arxiv",
}
# Sources whose content is a re-syndication of another source's reporting.
SYNDICATION_PARENT = {
    "science-daily": "press-release",
    "phys-org": "press-release",
}

def identity_of(source_key: str, label: str = "") -> str:
    """Resolve a registry key OR a free-text outlet name to ONE identity.

    ★ 这个函数回答的是「**是不是同一家媒体**」，不是「**属于哪一档权重**」。
    两者必须分开，因为一个权重档里装着很多家不同的媒体。

    Measured failures, in BOTH directions -- the same alias table was doing both
    jobs and got each one wrong in turn:

      假阳性（同一家算多家）: an external entry whose `also_seen_in` named its OWN
        source counted itself as an independent reprint, taking pickups 1 -> 2 and
        comm 0.35 -> 0.60, exactly across the gate.

      假阴性（多家算一家）: 修上面那个时改成返回 `platform_of()` 的结果，而那是
        **权重桶**。实测 16 组独立媒体对了 11 组：新京报 + 南方都市报 → 都是
        `bjnews`；广州日报 + 北京日报 → `bjnews`；界面新闻 + 财新 → `caixin`；
        新华社 + 中新社 → `xinhua`；民政部 + 财政部 → `gov-cn`（30 个部委全塌成
        一个）。后果相反但同样致命：**真实的多源印证被吞掉**，一条真的被多家
        报道的新闻 pickups 永远等于 1，comm 卡在 0.35 进不了核心区。

    修法：身份 = 归一化后的**媒体名**，只有 OUTLET_ALIAS 里明确列出的
    「确实是同一家的不同写法」才合并。权重桶只用于查 PLATFORM_WEIGHT。
    """
    if source_key:
        mapped = SOURCE_IDENTITY.get(source_key) or SYNDICATION_PARENT.get(source_key)
        if mapped:
            return mapped
        if source_key in SOURCES:
            return source_key
    name = (label or source_key or "").strip()
    if not name:
        return ""
    low = name.lower()
    # ★ 两轮匹配：**先精确，再子串**。
    # 实测 bug：只做子串匹配时，短别名会把长名字抢先匹配掉 ——
    # `OUTLET_ALIAS` 里既有 `上游新闻`（裸形式）又有 `上游新闻客户端`（变体），
    # 而 `上游新闻` 是 `上游新闻客户端` 的前缀，谁先谁后完全由表内顺序决定。
    # 精确轮保证「名字就是表里那一项」时用最贴切的那条；
    # 子串轮留给「名字里包含某个别名」的情形（如 `光明日报社`）。
    for alias, canon in OUTLET_ALIAS:
        if low == alias:
            return canon
    for alias, canon in OUTLET_ALIAS:
        if alias in low:
            return canon
    # 别名表没命中 → 通用归一去载体后缀。
    # ★ 已登记的品牌（如 `上游新闻`）走的是上面那条：它们的所有写法
    #   （`上游新闻客户端` / `上游新闻网` / `上游新闻新闻客户端`）都在
    #   `_OUTLET_VARIANTS` 里显式登记，所以 100% 收敛。
    #   通用规则只负责没登记的名字，因此它可以保持"激进但可预测"——
    #   这也正是这个 bug 的教训：**别把品牌知识塞进通用后缀规则**，
    #   两者的边界一模糊就会互相打架（前五版都栽在这里）。
    return normalize_identity(name)


def _register_outlet_variants() -> None:
    """把 `OUTLET_ALIAS` 里每个品牌的常见写法都登记上，形如 `X → X`。

    为什么必须显式登记（而不是靠通用后缀剥离）：
    `上游新闻` 与 `上游新闻客户端` 的差异在**品牌名内部**（「新闻」是品牌的一部分），
    任何"剥载体重后缀"的通用规则都会在某一侧多剥或少剥：
      多剥 → `上游新闻客户端` → `上游`，与裸形式 `上游新闻` 不收敛
      少剥 → `新京报网` → `新京报网`，与裸形式 `新京报` 不收敛
    实测通用规则在这组矩阵上最多只能做到 **9/15 收敛**（穷举 7 种候选规则验证过）。
    显式登记是唯一能做到 15/15 且**意图可读**的办法 —— 表里写的是什么就是什么。
    """
    seen: set[str] = set()
    for alias, canon in list(OUTLET_ALIAS):
        base = alias.split("：")[0].split(":")[0].strip()
        for form in (base + "客户端", base + "网", base + "新闻客户端"):
            if form and form not in seen:
                seen.add(form)
                OUTLET_ALIAS.append((form, canon))

# 真正的「同一家的不同写法」。**只有确实同属一个编辑部/通讯社的才进来。**
# 判据：读者会认为这两条是「同一个来源」时才算合并。
# 反例（明确不合并，虽然它们共用权重档）：新京报 ≠ 南方都市报、
# 界面新闻 ≠ 财新、民政部 ≠ 财政部 —— 同档不等于同一家。
#
# 类型是 `list`（不是 tuple）：`_register_outlet_variants()` 会往里追加
# `X客户端 / X网 / X新闻客户端` 三种变体。调用必须在**这个定义之后**。
OUTLET_ALIAS: list[tuple[str, str]] = [
    # 央视系统：一个台
    ("中央电视台", "cctv"), ("央视新闻", "cctv"), ("央视网", "cctv"),
    ("央视", "cctv"), ("cctv", "cctv"), ("新闻联播", "cctv"),
    # 新华社系统：一个通讯社
    ("新华社", "xinhua"), ("新华网", "xinhua"), ("新华每日电讯", "xinhua"),
    # 中新社系统：**另一家**通讯社。实测教训：第一版把中新社/中国新闻网/中新网
    # 也映射到 `xinhua`，于是「新华社」和「中新社」被判同一家 —— 而这恰恰是
    # 本函数 docstring 自己举的原始反例，即【过合并原封未动】。
    # 更糟的是当时的测试断言写的是 `中新社 == 中国新闻网 → 'xinhua'`，
    # 把错误固化成期望值，两个方向的断言都抓不到它。
    # 中新社与新华社是两家独立通讯社，必须分开。
    ("中新社", "chinanews"), ("中国新闻网", "chinanews"), ("中新网", "chinanews"),
    # 人民日报社：一张报纸及其网站
    ("人民日报", "people"), ("人民网", "people"),
    # 央广/国际台
    ("中央广播电视总台", "cctv"), ("央广网", "cnr"), ("央广", "cnr"),
    # 其它单点别名（同一品牌的写法差异）
    ("澎湃新闻", "thepaper"), ("澎湃", "thepaper"),
    ("光明网", "guangming"), ("光明日报", "guangming"), ("光明", "guangming"),
    ("中国日报", "chinadaily"),
    ("第一财经", "yicai"), ("证券时报", "stcn"),
    ("it之家", "ithome"), ("ithome", "ithome"),
    ("36氪", "36kr"), ("36kr", "36kr"),
    # ── 品牌名【本身】必须登记（映射到自己）。
    # ★ 这是本轮最关键的教训：曾经只登记 `X网` 变体而漏了裸品牌名，于是
    #     `极目新闻`      → 别名未命中 → 通用剥离 → `极目新闻`
    #     `极目新闻客户端` → 别名未命中 → 通用剥离 → **`极目`**
    #       （一次性正则 `^(.+?)(?:新闻客户端|客户端|网)$` 的非贪婪 `.+?`
    #        会把「新闻」吃进 `客户端` 分支，一步剥掉「新闻客户端」5 个字）
    #   → 判成两家 → pickups=2、comm=0.60 → **单一媒体伪造进核心区**。
    # 登记裸名后两条都走【精确匹配】，各自都落在 `极目新闻`，收敛。
    #
    # ★ 映射目标是【自己的身份】，不是权重桶 ——
    #   新京报与南方都市报共用 `bjnews` 权重档，但它们是两家。
    ("上游新闻", "上游新闻"), ("极目新闻", "极目新闻"), ("封面新闻", "封面新闻"),
    ("界面新闻", "界面新闻"), ("红星新闻", "红星新闻"),
    ("每日经济新闻", "每日经济新闻"), ("新京报", "新京报"),
    ("南方都市报", "南方都市报"), ("财新", "财新"),
    ("广州日报", "广州日报"), ("浙江日报", "浙江日报"), ("北京日报", "北京日报"),
    ("中国青年报", "中国青年报"), ("南方周末", "南方周末"),
    ("经济观察报", "经济观察报"), ("21世纪经济报道", "21世纪经济报道"),
    ("观察者网", "观察者网"), ("环球时报", "环球时报"),
    ("半月谈", "半月谈"), ("健康中国", "健康中国"),
    # ── 机构网站的 `X网` 变体（裸名在上，变体交给自动注册补齐；这两条是
    #    名字本身不以品牌结尾的特例，必须手写）
    ("南方周末网", "南方周末"), ("经济观察网", "经济观察报"),
    ("21世纪经济报道网", "21世纪经济报道"), ("每日经济新闻网", "每日经济新闻"),
]

# 必须在 OUTLET_ALIAS 定义【之后】调用（初版放在定义前 → NameError）。
_register_outlet_variants()

def normalize_identity(text: str) -> str:
    """Strip CARRIER suffixes so two spellings of one outlet compare equal.

    ★ 只剥**纯载体**后缀：`客户端 / 官网 / 官方网站 / 网站`。
    `新闻网` 和单字 `网` **都不剥** —— 这是踩过两次的坑：

      坑一（原始 bug，5 个版本复现，E2E 能伪造进核心区）：
        后缀表里 `新闻客户端` 剥掉 5 个字，`上游新闻客户端` → `上游`，
        而裸形式是 `上游新闻` → 判成两家 → pickups=2、comm=0.60
        → **单一媒体伪造进核心区**。13 组里 7 组中招。
        当时 docstring 明写「「新闻」不在剥离列表里」—— **注释与代码直接矛盾**。

      坑二（修坑一时引入）：为了收敛，改成反复剥到不动、并把 `网` 也剥掉。
        但 `上游新闻` 结尾的 `新闻` 里的 `闻` 不是后缀，而 `新闻网` 剥完剩
        `上游新闻`、再剥单字 `网` 又变成 `上游` —— **两条路径仍然不收敛**，
        而且 `新京报网` → `新京报` 对、`上游新闻网` → `上游` 错，规则自相矛盾。

    最终做法：`网` 一律不动，`X报网` / `X新闻客户端` 这类**品牌相关的变体**
    交给 `OUTLET_ALIAS` 显式登记 —— 别名表就是干这个的，
    而通用后缀剥离只处理与之无关的纯载体词。
    """
    out = re.sub(r"[\s·・\-_（）()【】\[\]]+", "", (text or "").lower())
    # 三个约束，缺一个就有真实品牌被判成两家（每一版都在这里翻车，全记下来）：
    #
    #   ① 必须【反复试到不动】。`新京报客户端` 一轮只剥一次：
    #      先试 `新闻客户端`（不匹配）→ 再试 `客户端` → `新京报` ✓
    #      但 `新京报新闻客户端` 先剥 `客户端` → `新京报新闻`，
    #      必须回头再试一次才能收敛。单趟 for 循环不够。
    #
    #   ② `新闻客户端` 必须排在 `客户端` 前面。否则 `上游新闻客户端`
    #      先剥 `客户端` → `上游新闻`（恰好正确，但纯属运气）；
    #      换成 `上游新闻新闻客户端` 就会剩下 `上游新闻新闻`。
    #
    #   ③ 混用「新闻」的复合后缀（如 `新闻网`）会与品牌名里的「新闻」打架：
    #      `上游新闻网` 剥 `新闻网` → `上游`，与裸形式 `上游新闻` 不收敛。
    #      所以后缀表里**只有 `新闻客户端`** 含「新闻」，它是无歧义的
    #      （没有品牌叫 `X新闻新闻客户端`）。
    #
    # 不再剥 `官网/官方网站/网站`：它们不影响任何实测用例，
    # 而每多一条规则就多一次上面这类互相打架的机会。需要时用 OUTLET_ALIAS 登记。
    out = re.sub(r"[\s·・\-_（）()【】\[\]]+", "", (text or "").lower())
    # ── 规则（前五版都在这里翻车，把推导钉死，别再靠猜）──────────────────
    # 目标：X = X客户端 = X网 = X新闻客户端，且 X新闻 = X新闻客户端
    #       收敛（绝不能把品牌名里的「新闻」剥掉）。
    #
    # ★ 核心：**只剥一个后缀就停**，用一次性正则，不用循环。
    #   循环是罪魁 —— 它一旦启动就一路剥穿品牌名：
    #     `上游新闻客户端` → 剥 `客户端` → `上游新闻` → 循环再剥 → `上游` ✗
    #   而裸形式 `上游新闻` 没有可剥的载体重后缀，停在 `上游新闻` → 判成两家。
    #   实测 6 组失败全部出在这个循环上。
    #
    # ★ 后缀按**长度降序**排列，正则交替分支**从左到右**匹配：
    #     `新闻客户端`(5) 先于 `客户端`(3) 先于 `网`(1)
    #   于是 `新京报新闻客户端` 剥 5 字 → `新京报`（一轮到位，不需要循环）。
    #
    # ★ 后缀表里**不能有 `新闻网`**：`上游新闻网` 剥 `新闻网` → `上游`，
    #   与裸形式 `上游新闻` 不收敛。单字 `网` 才是对的（→ `上游新闻`）。
    #
    # 不再剥 `官网/官方网站/网站`：不影响任何实测用例，
    # 而每多一条规则就多一次互相打架的机会。需要时用 OUTLET_ALIAS 登记。
    # 守卫 `.+` 保证剥完至少剩 1 个字符（`网` 单独出现时不会被剥成空串）。
    out = re.sub(r"^(.+?)(?:新闻客户端|客户端|网)$", r"\1", out)
    return out

RE_NEWS_DOI = re.compile(r"^10\.\d{4,}/d41586-", re.I)

# 期刊栏目标题的形态。实测漏判来源：crossref-nejm 本期 6 条**全部**是
# Perspective / 病例影像 / 随笔，DOI 不是 d41586-，所以只靠 DOI 前缀抓不到。
# 这些形态词在**栏目标题**里高频、在**研究论文标题**里几乎不出现。
RE_FRONT_TITLE = re.compile(
    r"(?i)\b(perspective|editorial|commentary|correspondence|viewpoint|"
    r"images? in clinical medicine|clinical implications|letters? to the editor|"
    r"case report|in focus|book review|obituary|in memoriam|"
    r"a missed opportunity|"
    r"flushing in|not available in your country)\b")

def evidence_type_for(source_key: str, cls: str, entry: dict | None = None) -> str:
    """Infer the evidence tier from the source itself (no guessing by the model).

    For Crossref sources the DOI is inspected, because the registry key alone
    cannot tell a research paper from front-matter. `d41586-*` is Nature's
    news/books/picture desk; `reference_count == 0` corroborates it.
    """
    if source_key in PRIMARY_SOURCES:
        if source_key.startswith("crossref-"):
            if entry:
                doi = (entry.get("doi") or "").strip()
                if doi and RE_NEWS_DOI.match(doi):
                    entry["front_matter"] = True
                    entry["front_matter_reason"] = "DOI 前缀 d41586-（Nature 新闻/书评栏目）"
                    return "journalism"
                # ★ 栏目判定必须看【内容形态】，不能看"有没有摘要"。
                #
                # 这段历史值得留着，因为两版都在同一处翻过车：
                #
                #   v1（错）：`无摘要 AND 引用数==0 → journalism`。
                #     误伤真研究论文 —— Nature Comput Sci 的 -01070-1/-01071-0/-01064-z
                #     被判 journalism 0.674，而同刊同日带摘要的 -01063-0 拿 0.896。
                #     根因：**Crossref 的摘要覆盖率是不一致的**，"没有摘要"不等于
                #     "不是论文"。整条规则已删除。
                #
                #   v2（本版）：只看 d41586- 前缀。结果反向漏判 ——
                #     实测 `crossref-nature` 本期 15 条**全部**是新闻/书评/图片栏目
                #     （DOI 确实是 d41586-，这条抓到了），但
                #     `crossref-nejm` 6 条**全部**是 Perspective / 病例影像 / 随笔
                #     （DOI 不是 d41586-，**全部漏判成 peer-reviewed**），
                #     `crossref-natcomp` 4 条里 3 条是评论/Highlight，同样漏判。
                #
                # 本版的判据只用**栏目形态**，不用"引用数"：
                #   ① d41586- 前缀（Nature 前栏目，可靠）
                #   ② 标题形态：无作者、单作者、或标题本身是栏目标题
                #   ③ 无摘要 **且** 作者数 ≤1（单作者 + 无摘要 = 评论/随笔的典型形态；
                #      真研究论文极少单作者且无摘要）
                # 三条都是"形态"证据，与摘要覆盖率无关，所以不会重演 v1 的误伤。
                if entry:
                    au = entry.get("authors") or []
                    title = (entry.get("title") or "").strip()
                    no_abs = bool(entry.get("abstract_missing"))
                    refs = entry.get("is_referenced_by_count")
                    reason = None
                    if "not available in your country" in title.lower():
                        reason = "标题为占位/栏目标题"
                    elif RE_FRONT_TITLE.search(title):
                        reason = "标题形态属栏目（随笔/影像/述评）"
                    elif no_abs and len(au) <= 1:
                        reason = f"无摘要且作者数≤1（{len(au)} 位）—— 评论/随笔形态"
                    # ★ 试过再加一条「无摘要 + 作者数≤4 + 零被引 → 述评形态」，
                    # **已删除：它误伤真研究论文。**
                    # 实测：PNAS《GTP orchestrates CTP synthase via an allosteric
                    # effector–cycle》（3 作者、Crossref 无摘要、被引数 0）被判成
                    # journalism —— 而它是一篇真的研究论文。
                    # 根因和 v1 一模一样：**新上线论文的被引数天然是 0**，
                    # 「零被引」不能区分「评论」和「刚发表」。
                    # 教训：这个位置任何基于「缺失信号」的规则都会误伤，
                    # 因为 Crossref 的元数据覆盖率是不一致的。
                    #
                    # 因此本函数只保留【形态证据】：DOI 前缀 / 标题形态 / 作者数。
                    # 已知漏判（保守代价，明说）：3 作者以上的述评/栏目仍会被标
                    # peer-reviewed，例如 10.1038/s43588-026-01070-1。
                    # 这类条目通常 body_missing → 真值封顶 0.50 → 落丢弃区，
                    # 实际危害有限；而**误伤真论文会把 0.9 分的核心证据打掉**，
                    # 代价大得多。两害相权，选漏判。
                    if reason:
                        entry["front_matter"] = True
                        entry["front_matter_reason"] = reason
                        return "journalism"
            return "peer-reviewed"
        if source_key == "openai-news":
            return "official"
        return "preprint"
    if cls == "trend":
        return "rumor"
    return "secondary"


def set_source_timeouts() -> None:
    """Per-source HTTP timeouts, set from MEASURED maxima -- not from intuition.

    arxiv-ai is legitimately slow (4 categories, ARXIV_GAP=3s between calls:
    9.4 + 3x3 = 18.4s), so it needs a LARGER timeout, not a smaller one.
    reddit-ml deliberately has none: its measured 7.3s is backoff, not latency.
    """
    for key in ("arxiv-ai", "arxiv-rss-ai"):
        if key in SOURCES:
            SOURCES[key]["timeout"] = 40


set_source_timeouts()
