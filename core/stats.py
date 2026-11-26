"""JMeter jtl 結果統計。"""
import csv
from pathlib import Path


def _percentile(sorted_vals: list[int], p: float) -> int:
    if not sorted_vals:
        return 0
    return sorted_vals[min(int(len(sorted_vals) * p), len(sorted_vals) - 1)]


def compute_stats(jtl_path: Path) -> dict:
    times: list[int] = []
    errors = 0
    with open(jtl_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 8:
                continue
            try:
                times.append(int(row[1]))
            except ValueError:
                continue
            if row[7].strip().lower() != "true":
                errors += 1
    times.sort()
    total = len(times)
    return {
        "total": total,
        "errors": errors,
        "err_pct": (errors / total * 100) if total else 0.0,
        "avg": (sum(times) / total) if total else 0,
        "p50": _percentile(times, 0.50),
        "p90": _percentile(times, 0.90),
        "p99": _percentile(times, 0.99),
        "max": times[-1] if times else 0,
    }


def judge(stats: dict, p90_limit: int = 800, err_limit: float = 1.0) -> bool:
    return stats["err_pct"] < err_limit and stats["p90"] < p90_limit
