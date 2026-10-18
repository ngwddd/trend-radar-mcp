"""Runtime constants shared by the engine.

Ported from v1 (MIT, same author). The SSL context is not optional on this
machine: Python's stock CA store is stale and two sources fail verification.
"""
from __future__ import annotations

import ssl
import threading

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
TIMEOUT = 25
RETRY_STATUS = {429, 500, 502, 503, 504}
# 退避曾经是 (4.0, 12.0) —— 实测代价：reddit-ml 撞 429 后单源耗 17.06s，
# 其中 16s 纯粹在 sleep。而 429 是"这个源此刻不给数据"，多等 12 秒并不会
# 让它给。缩短到 (2.0, 4.0)：仍给瞬时抖动一次机会，但不再为确定性拒绝付 16 秒。
RETRY_WAITS = (2.0, 4.0)
ARXIV_GAP = 3.0                # polite gap between arXiv API calls

def _ssl_context() -> ssl.SSLContext:
    """Prefer certifi's CA bundle.

    On this machine Python's default store is OpenSSL's own
    (C:\\Program Files\\Common Files\\SSL, 114 CAs) which is stale enough that
    technologyreview.com and news.ycombinator.com fail with
    CERTIFICATE_VERIFY_FAILED; certifi (121 CAs) verifies them fine.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # certifi missing -> stock store, still works for most hosts
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()
# 键是 (url, ua, timeout)。`_mirror_fetch` 现在用线程池并发竞速镜像，
# 所以这个 dict 会被多线程读写 —— 加锁。竞争窗口极小（只是 dict 存取），
# 但并发写 dict 在 CPython 之外不保证安全，而且"偶发损坏的缓存"
# 是最难查的一类 bug。
_HTTP_CACHE: dict[tuple[str, str, int], bytes] = {}
_CACHE_LOCK = threading.Lock()

