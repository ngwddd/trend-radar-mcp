"""Network adapters: RSS/Atom, arXiv, Crossref, Google Trends, Toutiao hot board.

Ported from v1. Each adapter carries the workaround for a specific measured
failure -- do not "simplify" them without re-running the live smoke test.
"""
from __future__ import annotations

import hashlib
import json
import re
import pathlib
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                as_completed, wait)
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

from .config import (ARXIV_GAP, RETRY_STATUS, RETRY_WAITS, SSL_CONTEXT,
                     TIMEOUT, UA, _CACHE_LOCK, _HTTP_CACHE)
from .registry import HARD_EVIDENCE_TYPES, SOURCES, evidence_type_for
from .store import home

def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    # RFC 822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # ISO 8601 (Atom)
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
        try:
            return datetime.strptime(value[:10], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


# ---- disk cache -----------------------------------------------------------
# A scan re-fetches the same 40+ endpoints every time. Feeds publish on the
# order of minutes, so a 5-minute TTL is invisible to the user and turns a
# repeat scan from ~30 s into <1 s. `set_cache_ttl(0)` forces a live run.
_CACHE_TTL = 300.0
_DISK_LOCK = threading.Lock()


def set_cache_ttl(seconds: float) -> None:
    global _CACHE_TTL
    _CACHE_TTL = max(0.0, float(seconds))


def _cache_path(url: str, ua: str | None) -> pathlib.Path:
    digest = hashlib.sha256(f"{url}\n{ua or UA}".encode("utf-8")).hexdigest()[:32]
    return home() / "cache" / f"{digest}.bin"


def _get(url: str, *, retries: int = 0, ua: str | None = None,
         timeout: int | None = None) -> bytes:
    """Live GET, unless a fresh copy exists on disk."""
    t = timeout or TIMEOUT
    key = (url, ua or UA, t)
    with _CACHE_LOCK:
        if key in _HTTP_CACHE:
            return _HTTP_CACHE[key]
    if _CACHE_TTL > 0:
        path = _cache_path(url, ua)
        try:
            if time.time() - path.stat().st_mtime < _CACHE_TTL:
                body = path.read_bytes()
                with _CACHE_LOCK:
                    _HTTP_CACHE[key] = body
                return body
        except FileNotFoundError:
            pass
        except OSError:
            pass
    body = _get_live(url, retries=retries, ua=ua, timeout=timeout)
    if _CACHE_TTL > 0:
        try:
            path = _cache_path(url, ua)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        except OSError:
            pass
    return body


def _get_live(url: str, *, retries: int = 0, ua: str | None = None,
         timeout: int | None = None) -> bytes:
    """GET with optional retry on rate-limit / transient 5xx.

    Responses are memoized per process: several registry entries legitimately
    share one endpoint (reddit-ml and reddit-ml-top are the same URL), and
    fetching it twice got us HTTP 429 on the second call.

    `timeout` lets a single source override the global one. Measured need:
    sources differ by two orders of magnitude (ithome 14.2s vs techreview 0.13s),
    and one slow source used to delay every source after it in the same stream.
    """
    t = timeout or TIMEOUT
    cache_key = (url, ua or UA, t)
    with _CACHE_LOCK:
        if cache_key in _HTTP_CACHE:
            return _HTTP_CACHE[cache_key]
    req = urllib.request.Request(url, headers={
        "User-Agent": ua or UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json, text/xml, */*",
        "Accept-Language": "en,zh-CN;q=0.9",
    })
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=t, context=SSL_CONTEXT) as resp:
                body = resp.read()
                _HTTP_CACHE[cache_key] = body
                return body
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt >= retries:
                raise
            time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
            attempt += 1
        except (urllib.error.URLError, TimeoutError):
            if attempt >= retries:
                raise
            time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
            attempt += 1


def _tag(elem) -> str:
    return elem.tag.rsplit("}", 1)[-1].lower()


def _find(node, *names):
    """Namespace-agnostic child lookup."""
    wanted = {n.lower() for n in names}
    for child in node:
        if _tag(child) in wanted:
            return child
    return None


def _text(node, *names) -> str | None:
    found = _find(node, *names)
    if found is None:
        return None
    if found.text:
        return found.text
    # atom <link href="..."/>
    for attr in ("href", "url"):
        if found.get(attr):
            return found.get(attr)
    return None


def _entry_link(node, prefer: str = "alternate") -> str | None:
    """Pick an entry link. `prefer='comments'` returns the discussion page
    (Hacker News item link) instead of the linked article."""
    best = None
    for child in node:
        if _tag(child) != "link":
            continue
        href = child.get("href")
        rel = (child.get("rel") or "alternate").lower()
        if not href and child.text and child.text.strip():
            href = child.text.strip()
            rel = "alternate"
        if not href:
            continue
        if prefer == "comments" and rel == "comments":
            return href
        if rel == "alternate" and best is None:
            best = href
        elif best is None:
            best = href
    return best


def parse_feed(payload: bytes, label: str, link_prefer: str = "alternate") -> list[dict]:
    """Parse RSS 2.0 or Atom into normalized entries."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"not parseable XML ({exc})") from exc

    items = [e for e in root.iter() if _tag(e) in ("item", "entry")]
    out = []
    for item in items:
        title = _text(item, "title") or ""
        summary = _text(item, "summary") or _text(item, "description") or _text(item, "content") or ""
        published = (_text(item, "published") or _text(item, "pubdate")
                     or _text(item, "updated") or _text(item, "date"))
        authors = []
        for child in item:
            if _tag(child) == "author":
                name = _find(child, "name")
                authors.append((name.text if name is not None and name.text else child.text) or "")
            elif _tag(child) == "creator" and child.text:
                authors.append(child.text)
        # `summary_truncated` 之前【只在 Crossref 路径】设置（见 fetch_crossref），
        # 于是 RSS 路径上被 _clip 截过的条目没有任何标记 —— 实测 16 条
        # （ieee-spectrum 最多，一条省了 7618 字）。同一个动作两条路径两套记账，
        # 下游就无法统一判断「这条摘要是不是完整的」。
        _sum_raw = _strip_html(summary)
        _sum_clipped = _clip(_sum_raw)
        out.append({
            "title": _strip_html(title),
            "url": _entry_link(item, link_prefer),
            "published_raw": published,
            "published": (_parse_date(published).isoformat() if _parse_date(published) else None),
            "summary": _sum_clipped,
            "summary_truncated": _is_clipped(_sum_raw),
            "authors": [a for a in authors if a][:4],
            "source_label": label,
        })
    return out


def fetch_rss(url: str, label: str, ua: str | None = None,
              link_prefer: str = "alternate", timeout: int | None = None) -> list[dict]:
    payload = _get(url, retries=2, ua=ua, timeout=timeout)
    head = payload[:400].lstrip().lower()
    if head.startswith(b"<!doctype html") or b"<html" in head:
        raise ValueError("returned HTML, not a feed (bot wall / consent page)")
    return parse_feed(payload, label, link_prefer)


def fetch_atom_arxiv(spec: dict, n: int) -> list[dict]:
    out: list[dict] = []
    # ★ 必须把 spec 上的 per-source timeout 传下去。
    # 实测 bug：这里曾经是 `_get(url, retries=2)`，不传 timeout → 回落全局 25s。
    # 而 `set_source_timeouts()` 特意给 arxiv-ai 设了 40s（因为它在 4 个分类下
    # 耗时 ≈ 9.4 + 3×3 = 18.4s，25s 余量太小会截断合法分类数据）。
    # 结果那个 40s 只作用在 arxiv-rss-ai（kind=="rss"）上 —— 而周末它是空 feed。
    # 被论证要放宽的那个源，从来没吃到过这个值。
    tmo = spec.get("timeout")
    for index, cat in enumerate(spec.get("cats", [])):
        if index:
            time.sleep(ARXIV_GAP)          # arXiv 429s on back-to-back calls
        url = spec["url"].format(cat=cat, n=n)
        for entry in parse_feed(_get(url, retries=2, timeout=tmo), f"{spec['label']} · {cat}"):
            entry["category"] = cat
            out.append(entry)
    return out


def _is_major_source(entry: dict) -> bool:
    """这条能不能享受「本周重大事件」宽窗口。

    判据刻意收窄到【领域级事件通常的发布形态】：
      ① `major_event: true` —— 由 `--extra-json` 显式标注（调用方最清楚
         哪条是诺奖这种级别，脚本不该猜）；
      ② 证据类型是 `peer-reviewed` / `official` —— 期刊与官方一手发布。

    **为什么聚合器不享受宽窗口**：实测「机构稿 09-28 → ScienceDaily 10-09
    才转发」正是这个问题的来源。让聚合器也拿宽窗口，等于奖励慢转载，
    还让「近 3 天」这个承诺悄悄失效。宽窗口只给**真正在该日发布的人**。

    ⚠️ 必须在这里**现算** evidence_type，不能读 `entry["evidence_type"]` ——
    那个字段在打分循环里才赋值，而窗口过滤发生在它**之前**。
    实测：读字段的版本让 `--major-days 7` 一条都放不进来
    （查询确实多抓回 7 条，但过滤器读到的全是 None）。
    `evidence_type_for` 是纯函数、幂等，算两次没有副作用。
    """
    if entry.get("major_event"):
        return True
    ev = entry.get("evidence_type")
    if not ev:
        key = entry.get("source_key", "")
        ev = evidence_type_for(key, SOURCES.get(key, {}).get("class", ""), entry)
    return ev in HARD_EVIDENCE_TYPES | {"official"}


def fetch_rss_multi(urls: list[str], label: str, ua: str | None = None,
                    link_prefer: str = "alternate",
                    timeout: int | None = None) -> tuple[list[dict], list[str]]:
    """Fetch several DIFFERENT feeds of one outlet, concurrently, and merge them.

    与 `_mirror_fetch` 的区别（这两个不能混用）：
      `_mirror_fetch` —— 同一内容的多个**镜像**，取**第一个非空的**就停（竞速）
      `fetch_rss_multi` —— 同一家的多个**不同版面**，**全部都要**（合并）

    实测：中新网的「滚动 / 社会 / 国内 / 财经」是四个不同版面，
    用 `urls` 只会拿到其中一个版面，另外三个的内容直接丢掉。

    并发而非串行：实测中新网单 feed 0.5–6.4s（`scroll-news` 那次 6.38s），
    串行 4 个最坏要 20s+；并发后取决于最慢的那个。
    返回 (条目, 失败的 feed 列表) —— 失败必须能被调用方看见，
    否则「4 个版面只成功 1 个」会静默地看起来像「这家今天内容少」。
    """
    out: list[dict] = []
    failed: list[str] = []
    pool = ThreadPoolExecutor(max_workers=max(1, len(urls)))
    try:
        futs = {pool.submit(fetch_rss, u, label, ua, link_prefer, timeout): u
                for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                got = fut.result()
                out.extend(got)
            except Exception as exc:                       # noqa: BLE001
                failed.append(f"{u} → {type(exc).__name__}: {str(exc)[:70]}")
    finally:
        # ★ 不能写成 `with ThreadPoolExecutor(...)` —— 它的 __exit__ 会 join
        #   全部线程，拿到结果后仍要等慢/挂掉的那些跑完。
        #   实测（`_mirror_fetch` 那次）：拿到 51 条结果却仍耗 18.90s。
        pool.shutdown(wait=False, cancel_futures=True)
    # 同一版面可能被多个 feed 重复收录，按 URL 去重
    seen, uniq = set(), []
    for e in out:
        k = e.get("url") or e.get("title")
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    # ★ 必须按时间【降序】排序后再交给下游的单源限流。
    #
    # 实测 bug（子代理用真实数据抓到）：`as_completed` 按**完成顺序**合并结果，
    # 于是先回来的那个版面独占 `--max-per-source` 的名额。
    # `chinanews` 4 个版面窗内共 **120 条**，实际只送出 **15 条，且全部来自
    # 财经版面 `/cj/`** —— 社会 `/sh/`、国内 `/gn/`、滚动版面**一条都没被送出来**。
    # 这不只是数量问题：**agent 看到的世界被"哪个 feed 先返回"决定了**，
    # 而它对每次网络抖动的结果都不一样、无法复现。
    #
    # 限流本身是对的（成本控制），错的是**限流的对象**：应该限"给多少条"，
    # 不该限"给哪个版面"。按时间降序之后，cap 拿到的是全版面里最新的一批。
    # 与 `ARCHIVE_SOURCES` 的增量截断用的是同一个办法。
    uniq.sort(key=lambda e: e.get("published") or "", reverse=True)
    return uniq, failed


def _mirror_fetch(spec: dict, label: str) -> list[dict]:
    """Fetch a mirrored source by RACING the mirrors concurrently.

    为什么必须并行：实测 `baidu-hot`（已于 2026-10-10 下架）配了 4 个镜像，
    其中 3 个已死
    （1 个 403、2 个 DNS 失败），而串行 for 循环的**最坏情况是
    4 × (3 次尝试 × TIMEOUT + 退避 2+4) = 4 × 81 = 324 秒**。
    实测确实出现过一次 94 秒的全挂（另一代理记录）。一个热度榜源不该
    主导整条管线的墙钟。

    改成并发后：墙钟 ≈ 最慢的那个镜像，而不是所有镜像之和。
    第一个返回非空结果的胜出；全部失败才报错（错误信息保留每个镜像的原因）。

    `spec["mirror_timeout"]` 控制单个镜像的超时（默认 10s）——
    热度榜是「有就有、没有就算了」的校准信号，不值得为它等 25 秒。

    ⚠️ **当前没有调用者**（唯一的 `baidu-hot` 于 2026-10-10 下架），
    但**故意保留**：
      ① 它是通用能力 —— 再登记任何带 `urls` 的镜像源立刻就能用；
      ② 上面那三条实测记录（324s 最坏 / 94s 全挂 / `with` 会 join 全部线程）
         是踩出来的，删了函数就得连教训一起重写一遍。
    `--list` 的源计数不会显示它，因为它是机制不是源。
    """
    urls = spec.get("urls") or [spec["url"]]
    per = spec.get("mirror_timeout", 10)
    box: dict = {}

    def _try(url: str) -> None:
        try:
            entries = fetch_rss(url, label, spec.get("ua"), "alternate", per)
            if not entries:
                box.setdefault("errors", []).append(f"{url}: empty")
                return
            for entry in entries:
                entry["mirror"] = url
            box["hit"] = entries
        except Exception as exc:  # noqa: BLE001 - 别的镜像可能成功
            box.setdefault("errors", []).append(f"{url}: {str(exc)[:60]}")

    # ★ 不能用 `with ThreadPoolExecutor(...)` —— 它的 __exit__ 会 join 所有线程，
    # 于是即便第一个镜像早就成功了，仍要等失败的镜像把重试跑完。实测：
    # 拿到 51 条结果却仍耗 18.90s，等待的正是那三个已死镜像。
    # 手动管理 executor 并在拿到结果后立即返回，剩下的线程让它自生自灭
    # （进程很快结束，不值得为它们阻塞）。
    pool = ThreadPoolExecutor(max_workers=len(urls))
    try:
        pending = {pool.submit(_try, u) for u in urls}
        while pending and not box.get("hit"):
            wait(pending, return_when=FIRST_COMPLETED)
            pending = {f for f in pending if not f.done()}
    finally:
        # cancel_futures 让还没开跑的镜像直接取消；已在跑的无法中断，
        # 但不再 join 它们 —— 这正是省下时间的地方。
        pool.shutdown(wait=False, cancel_futures=True)
    if box.get("hit"):
        return box["hit"]
    raise ValueError("all mirrors failed -> " + " | ".join(box.get("errors") or []))


def fetch_google_trends(spec: dict, n: int) -> list[dict]:
    """Google Trends daily RSS.

    The top-level <link> is the feed itself, which is useless. The real payoff is
    nested <ht:news_item> children (title + url + source) plus
    <ht:approx_traffic>. A generic RSS reader throws all of that away, so this
    lifts it out: you get the search term AND the article that spiked it.
    """
    payload = _get(spec["url"], retries=1)
    root = ET.fromstring(payload)
    out = []
    for rank, item in enumerate((e for e in root.iter() if _tag(e) == "item"), 1):
        if len(out) >= n:
            break
        term = _strip_html(_text(item, "title") or "")
        if not term:
            continue
        traffic = None
        news = []
        for child in item:
            tag = _tag(child)
            if tag == "approx_traffic" and child.text:
                traffic = child.text.strip()
            elif tag == "news_item":
                news.append({
                    "title": _strip_html(_text(child, "news_item_title") or ""),
                    "url": _text(child, "news_item_url"),
                    "source": _strip_html(_text(child, "news_item_source") or ""),
                })
        top = next((x for x in news if x["url"]), None)
        out.append({
            "title": term,
            "url": (top or {}).get("url") or spec["url"],
            "published_raw": None,
            "published": None,
            "summary": (f"搜索量 {traffic or '?'}｜相关报道："
                        + (top or {}).get("title", "")[:160]),
            "authors": [],
            "source_label": spec["label"],
            "trend_rank": rank,
            "trend_heat": traffic,
            "related_news": news[:3],
        })
    return out


def fetch_toutiao_hot(spec: dict, n: int) -> list[dict]:
    """Official 头条热榜 JSON: real popularity ranking with a heat score."""
    data = json.loads(_get(spec["url"], retries=1).decode("utf-8", "replace"))
    rows = data.get("data") or []
    out = []
    for rank, row in enumerate(rows, 1):
        title = _strip_html(row.get("Title") or "")
        if not title:
            continue
        out.append({
            "title": title,
            "url": row.get("Url") or row.get("url"),
            "published_raw": None,
            "published": None,
            "summary": f"热度值 {row.get('HotValue', '?')}｜榜位 {rank}",
            "authors": [],
            "source_label": spec["label"],
            "trend_rank": rank,
            "trend_heat": row.get("HotValue"),
        })
        if len(out) >= n:
            break
    return out


_CLIP_HEAD = 1400
_CLIP_TAIL = 500
_CLIP_LIMIT = _CLIP_HEAD + _CLIP_TAIL          # 1900：超过它 _clip 才会动手


def _clip(text: str, head: int = _CLIP_HEAD, tail: int = _CLIP_TAIL) -> str:
    """Trim a long abstract WITHOUT discarding its conclusion.

    Measured loss from a plain [:900] slice: 43 of 162 summaries were cut exactly
    at the cap, 42 of them mid-word, and the dropped portion is usually the
    structured abstract's conclusion and effect sizes. Worst case seen: a Nature
    Medicine abstract lost its entire closing finding ("documentation, dependency
    specification and executable structure") and the subagent had to re-query
    Crossref to recover it.

    So: keep the head, keep the tail, and mark the omission explicitly.
    """
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]} …[省略 {omitted} 字]… {text[-tail:]}"


def _is_clipped(text: str) -> bool:
    """Was this text long enough that _clip() replaced part of it?

    ★ 判据必须是【内容长度 > _CLIP_LIMIT】，不能是 `len(raw) > len(clipped)`。
    实测 off-by-N：`_clip` 插入的 `…[省略 N 字]…` 标记【让结果变长】，
    所以 raw ∈ [1901, 1913] 时 clipped 反而比 raw 长 ——
    `len(raw) > len(clipped)` 为 False，被截过的条目被报成「没截断」。
    而 Crossref 路径当时用的是 `len(abstract) > 1900`（正确），
    RSS 路径用的是长度比较（错误）—— **同一个动作两条路径两套判据**，
    于是 full JSON 说「没截断」、compact 说「截断了」，两者对同一条给出相反答案。
    """
    return len(text or "") > _CLIP_LIMIT


def fetch_crossref(spec: dict, n: int, since: datetime) -> list[dict]:
    """Fetch a journal's recent works from Crossref.

    Sorting/filtering on `published` (the issue publication date) is WRONG for a
    "last few days" window: big journals assign an issue date months after the
    paper actually appeared online, so a 3-day window silently returned nothing.
    Measured on Nature/Science: `sort=published` with rows>=20 yielded only ~1-6
    in-window items; `sort=published-online` + `from-online-pub-date` returns them
    correctly. Always prefer the online date, and say so in the output.
    """
    url = (f"https://api.crossref.org/journals/{spec['issn']}/works"
           f"?rows={max(n, 20)}&sort=published-online&order=desc"
           f"&filter=from-online-pub-date:{since.date().isoformat()}"
           # Crossref's select whitelist: `subtype` and `reference-count` are NOT
           # selectable (both made every journal answer HTTP 400 -- measured),
           # while `is-referenced-by-count` is. Use that as the citation signal.
           f"&select=DOI,title,abstract,published,published-online,URL,author,type,"
           f"update-to,is-referenced-by-count,container-title")
    data = json.loads(_get(url, retries=2).decode("utf-8", "replace"))
    out = []
    for item in data.get("message", {}).get("items", []):
        # prefer the online date; fall back to the issue date
        for field in ("published-online", "published"):
            parts = (item.get(field) or {}).get("date-parts") or [[]]
            dt = None
            if parts and parts[0]:
                try:
                    dt = datetime(*[int(x) for x in parts[0]], tzinfo=timezone.utc)
                except Exception:
                    dt = None
            if dt is not None:
                break
        authors = [f"{a.get('given','')} {a.get('family','')}".strip() for a in (item.get("author") or [])]
        # Crossref exposes retraction / correction notices on the WORK ITSELF via
        # `update-to`, sourced from retraction-watch and publishers. Measured:
        # this is how the arsenic-life paper's retraction is discoverable.
        notices = []
        for upd in (item.get("update-to") or []):
            label = (upd.get("label") or upd.get("type") or "").strip()
            if label:
                notices.append({"label": label, "source": upd.get("source"),
                                "doi": upd.get("DOI")})
        abstract = _strip_html(item.get("abstract") or "")
        # Carry the signals that separate research papers from front-matter. The
        # DOI alone is often enough, but reference-count and a missing abstract
        # corroborate it (measured against 10.1038/d41586-026-03217-6).
        out.append({
            "title": _strip_html((item.get("title") or [""])[0]),
            "url": item.get("URL"),
            "published_raw": dt.date().isoformat() if dt else None,
            "published": dt.isoformat() if dt else None,
            "summary": _clip(abstract),
            "summary_truncated": _is_clipped(abstract),
            "authors": [a for a in authors if a][:4],
            "doi": item.get("DOI"),
            "type": item.get("type"),
            "container_title": (item.get("container-title") or [None])[0],
            "is_referenced_by_count": item.get("is-referenced-by-count"),
            "abstract_missing": not abstract,
            "update_notices": notices,
            "source_label": spec["label"],
        })
    return out

