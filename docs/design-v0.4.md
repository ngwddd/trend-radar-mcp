# v0.4 · 潜力新闻（opt-in 支路）

日期：2026-10-21
状态：已实现，默认路径不变

## 问题

凌晨查“今日热点”时，传播链还没起来：一条新闻只有 1 家来源，`comm` 到不了 0.6，
`core` 为空；把窗口放宽到 3 天只是把旧闻扫进来，并不提高命中率。

需要的是**变化率**：谁在被新来源跟进，而不是谁已经传开。

## 边界（最重要）

`radar_potential` 是 MCP 的第 4 个工具，但它是**可选支路**：

- `radar_scan` / `radar_pack` / `radar_check` 的参数、返回、耗时都不变
- 不新增表、不新增写路径之外的写入（复用 `scan()` 已有的 `runs` / `items`）
- 不传 `radar_potential` 时，响应里不会出现 `candidates`

合入门槛（`tests/test_contract.py`）：`radar_scan` 响应无 `candidates` / `potential` 字段。

## 数据来源

没有新表：`scan()` 本来就把完整 payload 写到 `~/.trend-radar/runs/<run_id>.json`。
潜力支路读最近 6 个同 scope 的 run，对每个 `event_id` 做来源集合差：

```
new_sources = 当前 run 的来源集合 - 历史 run 的来源并集
velocity    = new_sources / 历史跨度（小时）
burst       = (当前来源数 + 1) / (历史每次来源数均值 + 1)
tier        = max(SOURCE_WEIGHT[source])
```

没有历史 run 时，退化为 `tier + 新鲜度`，分数封顶 60，并标 `no_history`。

## 评分

```
score = 100 × (0.50 × velocity_norm + 0.30 × burst_norm + 0.20 × tier)
```

- `velocity_norm = min(1, velocity / 3)`（3 个新源/小时封顶）
- `burst_norm = log1p(burst) / log(5)`
- 无历史：`score = min(60, 100 × (0.65 × tier + 0.35 × freshness))`

排序后按主源去重（同源最多 2 条），输出 `top`（默认 8，上限 12）。

## 复用与耗时

- 5 分钟内有同 scope、同 `days` 的 run → 直接读，**0 网络**（实测 0.03 s）
- 否则复用 `scan()` 的 5 分钟源缓存
- 预排是 O(n) 内存计算；富集超过 1.5 s 就返回已算部分并标 `timeout`
- 响应约 3 KB

## 降级

| 情况 | 行为 |
|---|---|
| 无历史 run | `no_history`，按层级 + 新鲜度，封顶 60 |
| 富集超时 | `timeout`，返回已算部分 |
| 扫描/富集异常 | `error:<类型>`，返回空候选，不抛错 |
| 源失败 | 由现有 scan 跳过，支路不受影响 |

## 输出

```json
{
  "candidates": [
    {"event_id": "...", "title": "...", "url": "...", "source": "...",
     "score": 56, "signals": {"new_sources": 4, "velocity_per_hour": 0.67,
                              "burst": 4.0, "tier": 0.74},
     "why": ["4 个新源 / 6.0h", "突发 4.0×", "早期源权重 0.74"]}
  ],
  "meta": {"mode": "potential", "reused_run": false, "lookback_runs": 6,
           "degraded": [], "caveat": "潜力（未验证）..."}
}
```

`candidates` 永不与 `core` / `watch` 混排；模型负责语义判断与最终 3–5 条。

## 自动触发（v0.4.1）

本地 00:00–07:00，且 `radar_scan` 的 `core` 为空、或今天 `new=true` 的条目少于 3 条时，模型自动再调一次 `radar_potential`，不询问用户；简报末尾单独列「潜力（未验证）」。
其它时段只有用户明确要求才调用。

## 测试

- `tests/test_potential.py`：无历史封顶、历史速度、促销/热榜过滤、同源上限、降级
- `tests/test_contract.py`：`radar_scan` 响应无潜力字段
- `tests/test_server.py`：`tools/list` 含 4 个工具
