"""階梯壓測：逐級加壓，找出容量拐點。

每一級都是一次完整的分散式壓測；級與級之間留間隔，讓 Cloudflare 這類
WAF 的速率限制與源站有時間恢復，避免後段數據被前一級的殘留污染。

為什麼需要間隔：實測 moodle.aris-edu.org 時，單一節點 IP 每秒約 1,080 請求
就會觸發 Cloudflare 限流並回 403，且限制會延續數十秒。若級間不留白，第二級
之後量到的全是 403，完全看不到源站行為。
"""
from __future__ import annotations

import html

MAX_STAGES = 8
MIN_STAGE_THREADS = 1
HEALTHY_ERR_PCT = 1.0
PLATEAU_GROWTH = 1.10


def parse_stages(text: str) -> list[int]:
    """解析 "50,100,200,400" → [50, 100, 200, 400]。嚴格遞增。"""
    cleaned = str(text).replace("，", ",").replace("、", ",").replace(" ", ",")
    raw = [x.strip() for x in cleaned.split(",") if x.strip()]
    if not raw:
        raise ValueError("請提供各級併發，例 `50,100,200,400`")
    if len(raw) > MAX_STAGES:
        raise ValueError(f"最多 {MAX_STAGES} 級，目前 {len(raw)} 級")

    stages: list[int] = []
    for item in raw:
        if not item.isdigit():
            raise ValueError(f"`{item}` 不是正整數")
        value = int(item)
        if value < MIN_STAGE_THREADS:
            raise ValueError(f"每級併發須 ≥ {MIN_STAGE_THREADS}")
        stages.append(value)

    for prev, cur in zip(stages, stages[1:]):
        if cur <= prev:
            raise ValueError(
                f"各級併發必須嚴格遞增（{prev} → {cur}），例 `50,100,200,400`"
            )
    return stages


def stage_metrics(threads: int, duration: int, stats: dict) -> dict:
    """把 compute_stats 的結果換算成每級摘要。"""
    total = int(stats.get("total") or 0)
    errors = int(stats.get("errors") or 0)
    ok = max(0, total - errors)
    span = duration or 1
    err_pct = float(stats.get("err_pct") or 0.0)
    return {
        "threads": threads,
        "total": total,
        "errors": errors,
        "ok": ok,
        "err_pct": round(err_pct, 2),
        "rps": round(total / span, 1),
        "ok_rps": round(ok / span, 1),
        "avg_ms": round(float(stats.get("avg") or 0), 1),
        "p50_ms": int(stats.get("p50") or 0),
        "p90_ms": int(stats.get("p90") or 0),
        "p99_ms": int(stats.get("p99") or 0),
        "max_ms": int(stats.get("max") or 0),
        "healthy": err_pct < HEALTHY_ERR_PCT,
    }


def analyze(rows: list[dict]) -> dict:
    """從各級結果找出容量特徵。

    knee     最後一個錯誤率 < 1% 的併發級
    saturate 第一個錯誤率 >= 1% 的併發級
    plateau  吞吐相對前一級成長 < 10% 的併發級（併發加了但流量沒上去）
    best     成功吞吐最高的那一級
    """
    valid = [r for r in rows if r.get("total")]
    if not valid:
        return {
            "knee_threads": None,
            "saturate_threads": None,
            "plateau_threads": None,
            "best_threads": None,
            "best_ok_rps": 0.0,
        }

    knee = None
    saturate = None
    plateau = None
    for idx, row in enumerate(valid):
        if row["healthy"]:
            knee = row["threads"]
        elif saturate is None:
            saturate = row["threads"]

        if idx > 0 and plateau is None:
            prev = valid[idx - 1]
            if prev["rps"] > 0 and row["rps"] <= prev["rps"] * PLATEAU_GROWTH:
                plateau = row["threads"]

    best = max(valid, key=lambda r: r["ok_rps"])
    return {
        "knee_threads": knee,
        "saturate_threads": saturate,
        "plateau_threads": plateau,
        "best_threads": best["threads"],
        "best_ok_rps": best["ok_rps"],
    }


def verdict_text(rows: list[dict], summary: dict) -> str:
    """給人看的一句話結論。"""
    if not rows:
        return "沒有任何一級取得結果。"
    knee = summary.get("knee_threads")
    sat = summary.get("saturate_threads")
    plateau = summary.get("plateau_threads")
    best_rps = summary.get("best_ok_rps") or 0

    if knee is None:
        return (
            f"所有級別錯誤率都超過 {HEALTHY_ERR_PCT:.0f}%，"
            f"最低一級就已經不健康，建議從更低的併發重新測。"
        )
    if sat is None:
        return (
            f"到 {knee} 併發為止全部健康，峰值成功吞吐約 {best_rps:.0f} req/s，"
            f"尚未測到飽和點，可再往上加級。"
        )

    text = (
        f"容量拐點落在 **{knee} 併發**：此級仍健康，"
        f"到 {sat} 併發時錯誤率突破 {HEALTHY_ERR_PCT:.0f}%。"
        f"峰值成功吞吐約 {best_rps:.0f} req/s。"
    )
    if plateau:
        text += f" 另在 {plateau} 併發觀察到吞吐停滯（併發增加但流量沒上去）。"
    return text


def _bars(rows: list[dict], key: str, unit: str, color_of, width: int = 760,
          height: int = 200) -> str:
    n = len(rows)
    if not n:
        return ""
    pad_l, pad_t, pad_b = 52, 18, 46
    plot_w = width - pad_l - 20
    plot_h = height - pad_b - pad_t
    maxv = max((float(r.get(key) or 0) for r in rows), default=0) or 1.0
    bw = plot_w / n

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for g in range(5):
        y = pad_t + plot_h * g / 4
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-20}" y2="{y:.1f}" '
            f'stroke="rgba(0,0,0,.08)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l-8}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#5F5E5A">{maxv*(1-g/4):.0f}</text>'
        )

    for i, row in enumerate(rows):
        value = float(row.get(key) or 0)
        h = plot_h * value / maxv
        x = pad_l + bw * i + bw * 0.18
        w = bw * 0.64
        y = pad_t + plot_h - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(h,1):.1f}" '
            f'rx="3" fill="{color_of(row)}"/>'
        )
        parts.append(
            f'<text x="{x+w/2:.1f}" y="{y-6:.1f}" text-anchor="middle" '
            f'font-size="11" fill="#2C2C2A">{value:.1f}{unit}</text>'
        )
        parts.append(
            f'<text x="{x+w/2:.1f}" y="{height-24:.1f}" text-anchor="middle" '
            f'font-size="11" fill="#5F5E5A">{row["threads"]}</text>'
        )

    parts.append(
        f'<text x="{pad_l+plot_w/2:.1f}" y="{height-6:.1f}" text-anchor="middle" '
        f'font-size="11" fill="#888780">併發</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _table(rows: list[dict]) -> str:
    head = (
        "<tr><th>併發</th><th>請求總數</th><th>成功</th><th>錯誤率</th>"
        "<th>總吞吐 req/s</th><th>成功吞吐 req/s</th>"
        "<th>P50</th><th>P90</th><th>P99</th><th>判定</th></tr>"
    )
    body = []
    for r in rows:
        if not r.get("total"):
            body.append(
                f'<tr><td>{r["threads"]}</td><td colspan="9" class="muted">'
                f'未取得結果（{html.escape(str(r.get("note") or "無"))}）</td></tr>'
            )
            continue
        cls = "ok" if r["healthy"] else "bad"
        tag = "健康" if r["healthy"] else "不健康"
        body.append(
            f'<tr><td><b>{r["threads"]}</b></td><td>{r["total"]:,}</td>'
            f'<td>{r["ok"]:,}</td><td>{r["err_pct"]:.2f}%</td>'
            f'<td>{r["rps"]:.1f}</td><td>{r["ok_rps"]:.1f}</td>'
            f'<td>{r["p50_ms"]}ms</td><td>{r["p90_ms"]}ms</td>'
            f'<td>{r["p99_ms"]}ms</td>'
            f'<td><span class="tag {cls}">{tag}</span></td></tr>'
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


def render_html(meta: dict, rows: list[dict], summary: dict) -> str:
    """產生單檔 HTML 容量曲線報告（無外部資源）。"""
    ok_color = lambda r: "#639922" if r.get("healthy") else "#E24B4A"
    err_color = lambda r: "#E24B4A" if not r.get("healthy") else "#639922"

    stages_text = ", ".join(str(r["threads"]) for r in rows)
    params = [
        ("目標", html.escape(str(meta.get("domain") or ""))),
        ("網址", html.escape(str(meta.get("url") or "—"))),
        ("各級併發", html.escape(stages_text)),
        ("每級時長", f'{meta.get("duration", 0)} 秒'),
        ("級間間隔", f'{meta.get("gap", 0)} 秒'),
        ("節點", html.escape(", ".join(meta.get("nodes") or []) or "—")),
        ("操作者", html.escape(str(meta.get("operator") or ""))),
        ("開始時間", html.escape(str(meta.get("started_at") or ""))),
    ]
    param_html = "".join(
        f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in params
    )

    css = """
:root{--ink:#2C2C2A;--muted:#5F5E5A;--line:rgba(0,0,0,.1);--bg:#fff;--soft:#F7F6F2}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.65}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
h2{font-size:15px;font-weight:600;margin:32px 0 12px}
.sub{color:var(--muted);font-size:13px;margin:0 0 24px}
.kv-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
  gap:10px 20px;background:var(--soft);border-radius:10px;padding:16px 18px}
.kv{display:flex;flex-direction:column;gap:1px;font-size:12px}
.kv span{color:var(--muted)}
.kv b{font-weight:500;font-size:13px;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:var(--soft)}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px}
.tag.ok{background:#EAF3DE;color:#3B6D11}
.tag.bad{background:#FCEBEB;color:#A32D2D}
.muted{color:var(--muted)}
.card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.verdict{background:#F7F6F2;border-left:3px solid #639922;border-radius:8px;
  padding:14px 18px;margin:20px 0;font-size:13px}
.legend{display:flex;gap:18px;font-size:12px;color:var(--muted);margin-bottom:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
footer{margin-top:36px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px}
"""
    legend = (
        '<div class="legend">'
        '<span><i style="background:#639922"></i>健康（錯誤率 &lt; 1%）</span>'
        '<span><i style="background:#E24B4A"></i>不健康</span></div>'
    )

    return "".join([
        '<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<title>階梯壓測 · {html.escape(str(meta.get("domain") or ""))}</title>',
        f"<style>{css}</style></head><body><div class=\"wrap\">",
        f'<h1>階梯壓測報告 · {html.escape(str(meta.get("domain") or ""))}</h1>',
        f'<p class="sub">{html.escape(str(meta.get("started_at") or ""))} · '
        f'共 {len(rows)} 級</p>',
        f'<div class="kv-grid">{param_html}</div>',
        f'<div class="verdict">{html.escape(verdict_text(rows, summary)).replace("**", "")}</div>',
        '<h2>成功吞吐（req/s）</h2>', legend,
        f'<div class="card">{_bars(rows, "ok_rps", "", ok_color)}</div>',
        '<h2>錯誤率（%）</h2>', legend,
        f'<div class="card">{_bars(rows, "err_pct", "%", err_color)}</div>',
        '<h2>各級明細</h2>',
        f'<div class="card" style="padding:0;overflow-x:auto">{_table(rows)}</div>',
        '<footer>由分散式壓測控制平台產生 · 每級之間留有間隔以避免 WAF 限流污染後段數據</footer>',
        "</div></body></html>",
    ])
