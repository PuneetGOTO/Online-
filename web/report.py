"""壓測結果分析與報告產生。

讀 results/*.jtl（JMeter CSV）算出統計，輸出可下載的 HTML / Markdown 報告。
報告不依賴外部資源，單一檔案就能寄給客戶。
"""
from __future__ import annotations

import csv
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

MAX_LINES = 400_000


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, int(len(values) * q))
    return values[index]


def analyze(jtl_path: Path) -> Optional[dict]:
    rows: list[dict] = []
    try:
        with open(jtl_path, encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                rows.append(row)
                if len(rows) >= MAX_LINES:
                    break
    except Exception:
        return None
    if not rows:
        return None

    elapsed = sorted(float(r.get("elapsed") or 0) for r in rows)
    ok_rows = [r for r in rows if str(r.get("success", "")).lower() == "true"]
    ok_lat = sorted(float(r.get("elapsed") or 0) for r in ok_rows)

    stamps = [int(r["timeStamp"]) for r in rows if str(r.get("timeStamp", "")).isdigit()]
    # JMeter 的 timeStamp 是毫秒
    start_ms = min(stamps) if stamps else 0
    end_ms = max(stamps) if stamps else 0
    span = max(1.0, (end_ms - start_ms) / 1000) if stamps else 1.0

    per_sec = Counter((int(r["timeStamp"]) - start_ms) // 1000
                      for r in rows if str(r.get("timeStamp", "")).isdigit())
    series = [per_sec.get(i, 0) for i in range(int(span) + 1)]

    codes = Counter(str(r.get("responseCode", "")).strip() for r in rows)
    errors = Counter(
        (r.get("responseMessage") or "").strip()[:90]
        for r in rows
        if str(r.get("success", "")).lower() != "true"
    )

    # 舊結果沒有 meta.json 時，從 jtl 的 allThreads 推回併發數
    seen_threads = []
    for r in rows:
        value = str(r.get("allThreads", ""))
        if value.isdigit():
            seen_threads.append(int(value))
    threads_seen = max(seen_threads) if seen_threads else None

    # 沒有 meta 時，從 URL 欄位取主機名
    host = ""
    for r in rows:
        url = (r.get("URL") or "").strip()
        if "://" in url:
            host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
            if host:
                break

    total = len(rows)
    bad = total - len(ok_rows)
    return {
        "total": total,
        "ok": len(ok_rows),
        "bad": bad,
        "err_pct": round(bad / total * 100, 2) if total else 0.0,
        "rps": round(total / span, 1),
        "span": round(span, 1),
        "avg": round(sum(elapsed) / total, 0) if total else 0,
        "p50": round(_pct(elapsed, 0.5), 0),
        "p90": round(_pct(elapsed, 0.9), 0),
        "p99": round(_pct(elapsed, 0.99), 0),
        "max": round(elapsed[-1], 0) if elapsed else 0,
        "ok_p50": round(_pct(ok_lat, 0.5), 0) if ok_lat else None,
        "ok_p90": round(_pct(ok_lat, 0.9), 0) if ok_lat else None,
        "codes": codes.most_common(8),
        "errors": errors.most_common(6),
        "series": series,
        "threads_seen": threads_seen,
        "host": host,
        "started": datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if start_ms else "",
        "ended": datetime.fromtimestamp(end_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if end_ms else "",
    }


def read_meta(jtl_path: Path) -> dict:
    meta_path = jtl_path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def list_results(results_dir: Path, limit: int = 20) -> list[dict]:
    items = []
    for jtl in sorted(results_dir.glob("*.jtl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        meta = read_meta(jtl)
        items.append({
            "stem": jtl.stem,
            "size_kb": round(jtl.stat().st_size / 1024),
            "mtime": datetime.fromtimestamp(jtl.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "domain": meta.get("domain") or jtl.stem.rsplit("-", 2)[0],
            "threads": meta.get("threads"),
            "duration": meta.get("duration"),
            "ok": meta.get("ok"),
            "has_meta": jtl.with_suffix(".meta.json").exists(),
        })
    return items


def _verdict(meta: dict, stats: dict) -> tuple[bool, str]:
    if "ok" in meta:
        return bool(meta["ok"]), "依平台標準（P90 < 800ms 且錯誤率 < 1%）"
    return stats["p90"] < 800 and stats["err_pct"] < 1.0, "依平台標準（P90 < 800ms 且錯誤率 < 1%）"


def _bar_chart(series: list[int], width: int = 760, height: int = 150) -> str:
    if not series:
        return ""
    peak = max(series) or 1
    n = len(series)
    gap = 1 if n > 120 else 2
    bar_w = max(1.0, (width - 40) / n - gap)
    bars = []
    for i, value in enumerate(series):
        h = (height - 30) * value / peak
        x = 40 + i * (bar_w + gap)
        y = height - 20 - h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="#2563eb" opacity="0.85"><title>{i}s · {value} 樣本</title></rect>'
        )
    step = max(1, n // 8)
    ticks = []
    for i in range(0, n, step):
        x = 40 + i * (bar_w + gap) + bar_w / 2
        ticks.append(f'<text x="{x:.0f}" y="{height - 4}" font-size="10" fill="#8a94a6" '
                     f'text-anchor="middle">{i}s</text>')
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">'
        f'<line x1="40" y1="{height - 20}" x2="{width}" y2="{height - 20}" stroke="#e4e9f0"/>'
        f'<text x="4" y="16" font-size="10" fill="#8a94a6">{peak}</text>'
        f'<text x="4" y="{height - 20}" font-size="10" fill="#8a94a6">0</text>'
        + "".join(bars) + "".join(ticks) + "</svg>"
    )


def _rows_html(pairs: list[tuple[str, str]]) -> str:
    return "".join(
        f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in pairs
    )


def render_html(jtl_path: Path) -> str:
    stats = analyze(jtl_path)
    if not stats:
        return "<p>無可用資料</p>"
    meta = read_meta(jtl_path)
    passed, basis = _verdict(meta, stats)

    target = meta.get("domain") or stats.get("host") or "—"
    client = meta.get("client") or ""
    threads = meta.get("threads") or stats.get("threads_seen")
    nodes = meta.get("nodes") or []
    duration = meta.get("duration")
    operator = meta.get("operator") or "—"
    override = meta.get("override")

    summary = _rows_html([
        ("目標", f'<span class="mono">{html.escape(str(target))}</span>'
                 + (f"　（{html.escape(str(client))}）" if client else "")),
        ("併發", f"{threads:,}（{len(nodes)} 節點 × {meta.get('per_node', '—')}）"
                 if threads and nodes else (f"{threads:,}" if threads else "—")),
        ("節點", "、".join(html.escape(n) for n in nodes) if nodes else "—"),
        ("時長", f"{duration} 秒" if duration else f"{stats['span']} 秒（實測）"),
        ("樣本數", f"{stats['total']:,}（{stats['rps']} 樣本/秒）"),
        ("失敗率", f"{stats['err_pct']}%（{stats['bad']:,} / {stats['total']:,}）"),
        ("延遲 p50 / p90 / p99", f"{stats['p50']:,.0f} / {stats['p90']:,.0f} / {stats['p99']:,.0f} ms"),
        ("延遲 平均 / 最大", f"{stats['avg']:,.0f} / {stats['max']:,.0f} ms"),
    ])
    if stats["ok_p50"] is not None:
        summary += _rows_html([
            ("成功請求 p50 / p90", f"{stats['ok_p50']:,.0f} / {stats['ok_p90']:,.0f} ms"),
        ])
    summary += _rows_html([
        ("開始 / 結束", f"{stats['started']} ～ {stats['ended']}"),
        ("操作者", html.escape(str(operator)) + ("　（override）" if override else "")),
    ])

    code_rows = "".join(
        f"<tr><td class='mono'>{html.escape(str(code)) or '—'}</td>"
        f"<td class='num'>{n:,}</td><td class='num'>{n / stats['total'] * 100:.2f}%</td></tr>"
        for code, n in stats["codes"]
    )
    err_rows = "".join(
        f"<tr><td class='num'>{n:,}</td><td>{html.escape(msg) or '（無訊息）'}</td></tr>"
        for msg, n in stats["errors"]
    ) or "<tr><td colspan='2' class='muted'>無失敗樣本</td></tr>"

    verdict_class = "pass" if passed else "fail"
    verdict_text = "達標" if passed else "未達標"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>壓測報告 · {html.escape(str(target))}</title>
<style>
  :root {{ --text:#1f2733; --soft:#4a5568; --muted:#8a94a6; --line:#e4e9f0; --accent:#2563eb; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:40px 24px 64px; background:#f6f8fb; color:var(--text);
         font:14px/1.7 -apple-system,"Segoe UI","Noto Sans TC","PingFang TC",sans-serif; }}
  .wrap {{ max-width:920px; margin:0 auto; }}
  header {{ margin-bottom:26px; }}
  h1 {{ margin:0 0 6px; font-size:23px; letter-spacing:-.3px; }}
  .sub {{ color:var(--muted); font-size:12.5px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:12px;
           padding:20px 22px; margin-bottom:18px; }}
  h2 {{ margin:0 0 14px; font-size:14.5px; letter-spacing:.02em; color:var(--soft); }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
  th {{ width:34%; font-weight:500; color:var(--muted); font-size:12.5px; }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  tr:last-child th, tr:last-child td {{ border-bottom:none; }}
  .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }}
  .muted {{ color:var(--muted); }}
  .verdict {{ display:flex; align-items:center; gap:12px; padding:16px 20px; border-radius:12px;
              margin-bottom:18px; border:1px solid; }}
  .verdict.pass {{ background:#f0fbf4; border-color:#b7e4c7; }}
  .verdict.fail {{ background:#fef4f4; border-color:#f5c2c2; }}
  .tag {{ font-size:15px; font-weight:650; }}
  .verdict.pass .tag {{ color:#1a7f45; }}
  .verdict.fail .tag {{ color:#c0392b; }}
  .basis {{ color:var(--muted); font-size:12.5px; }}
  footer {{ color:var(--muted); font-size:11.5px; text-align:center; margin-top:26px; }}
  @media print {{ body {{ background:#fff; padding:0; }} .card {{ break-inside:avoid; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>分散式壓力測試報告</h1>
    <div class="sub">{html.escape(str(target))}　·　產生於 {generated}</div>
  </header>

  <div class="verdict {verdict_class}">
    <span class="tag">{verdict_text}</span>
    <span class="basis">{basis}</span>
  </div>

  <section class="card"><h2>測試概要</h2><table>{summary}</table></section>

  <section class="card"><h2>每秒樣本數</h2>{_bar_chart(stats['series'])}</section>

  <section class="card"><h2>回應碼分布</h2>
    <table><tr><th>回應碼</th><th class="num">數量</th><th class="num">佔比</th></tr>{code_rows}</table>
  </section>

  <section class="card"><h2>失敗原因</h2>
    <table><tr><th class="num">數量</th><th>訊息</th></tr>{err_rows}</table>
  </section>

  <footer>資料來源：{html.escape(jtl_path.name)}　·　由分散式壓測控制平台產生</footer>
</div>
</body>
</html>"""


def render_markdown(jtl_path: Path) -> str:
    stats = analyze(jtl_path)
    if not stats:
        return "# 壓測報告\n\n無可用資料。\n"
    meta = read_meta(jtl_path)
    passed, _basis = _verdict(meta, stats)
    target = meta.get("domain") or stats.get("host") or jtl_path.stem
    nodes = meta.get("nodes") or []
    threads = meta.get("threads") or stats.get("threads_seen")
    lines = [
        f"# 壓測報告 · {target}",
        "",
        f"- 判定：**{'達標' if passed else '未達標'}**",
        f"- 時間：{stats['started']} ～ {stats['ended']}",
        "",
        "| 項目 | 數值 |",
        "|---|---|",
        f"| 併發 | {threads:,}（{len(nodes)} 節點 × {meta.get('per_node', '—')}） |"
        if threads and nodes else f"| 併發 | {threads or '—'} |",
        f"| 節點 | {'、'.join(nodes) or '—'} |",
        f"| 時長 | {meta.get('duration') or stats['span']} 秒 |",
        f"| 樣本數 | {stats['total']:,}（{stats['rps']} 樣本/秒） |",
        f"| 失敗率 | {stats['err_pct']}%（{stats['bad']:,} / {stats['total']:,}） |",
        f"| 延遲 p50 / p90 / p99 | {stats['p50']:,.0f} / {stats['p90']:,.0f} / {stats['p99']:,.0f} ms |",
        f"| 延遲 平均 / 最大 | {stats['avg']:,.0f} / {stats['max']:,.0f} ms |",
    ]
    if stats["ok_p50"] is not None:
        lines.append(f"| 成功請求 p50 / p90 | {stats['ok_p50']:,.0f} / {stats['ok_p90']:,.0f} ms |")
    lines += ["", "## 失敗原因", "", "| 數量 | 訊息 |", "|---|---|"]
    if stats["errors"]:
        for msg, count in stats["errors"]:
            lines.append(f"| {count:,} | {msg or '（無訊息）'} |")
    else:
        lines.append("| — | 無失敗樣本 |")
    lines.append("")
    return "\n".join(lines)
