"""多節點分配與分散式執行。

JMeter 分散式模式的重點：每個 remote engine 會各自跑一份計畫，
所以「總併發 = 每節點執行緒 × 節點數」。本模組負責把請求的總併發
平均攤到可用節點，並回報實際併發（可能略高於請求值）。
"""
import asyncio
import math
import time
from pathlib import Path


async def node_alive(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def check_nodes(nodes: dict) -> dict:
    tasks = {
        key: asyncio.create_task(node_alive(cfg["host"], cfg.get("port", 1099)))
        for key, cfg in nodes.items()
        if cfg.get("enabled", True)
    }
    return {key: await task for key, task in tasks.items()}


def effective_capacity(nodes: dict, only: list = None) -> int:
    """實際可承載的總併發。

    JMeter 分散式模式下每個 engine 跑相同的執行緒數，所以 n 個節點的有效容量
    是 min(各節點上限) × n，而不是各節點上限相加。

    only: 限定參與的節點名稱；None 表示全部啟用中的節點。
    """
    active = sorted(
        (
            v.get("max_threads", 0)
            for k, v in nodes.items()
            if v.get("enabled", True) and (not only or k in only)
        ),
        reverse=True,
    )
    best = 0
    for n in range(1, len(active) + 1):
        best = max(best, active[n - 1] * n)
    return best


def allocate(total_threads: int, nodes: dict, only: list = None) -> tuple[list, int, int]:
    """回傳 (選中節點 [(key, cfg)], 每節點執行緒, 實際總併發)。

    only: 限定參與的節點名稱；None 表示全部啟用中的節點。
    """
    active = [
        (k, v) for k, v in nodes.items()
        if v.get("enabled", True) and (not only or k in only)
    ]
    active.sort(key=lambda kv: kv[1].get("max_threads", 0), reverse=True)
    for n in range(len(active), 0, -1):
        per_node = math.ceil(total_threads / n)
        chosen = active[:n]
        if all(per_node <= cfg.get("max_threads", 10 ** 9) for _, cfg in chosen):
            return chosen, per_node, per_node * n
    return [], 0, 0


def compute_ramp(per_node: int, duration: int) -> int:
    """爬升期不得超過測試時長的三分之一，否則執行緒還沒拉滿測試就結束。"""
    return max(5, min(per_node // 2 or 5, max(5, duration // 3)))


def build_command(
    jmeter_bin: str,
    plan: Path,
    nodes: list,
    per_node: int,
    duration: int,
    host: str,
    jtl: Path,
    report: Path,
    extra: dict = None,
    global_extra: dict = None,
) -> list[str]:
    """組 JMeter 命令。

    extra        : 以 -J 傳入（只在本機生效）
    global_extra : 以 -G 傳入（會轉發到所有遠端引擎，分散式模式必須用這個）
    """
    hosts = ",".join(cfg["host"] + (f":{cfg['port']}" if cfg.get("port", 1099) != 1099 else "")
                     for _, cfg in nodes)
    ramp = compute_ramp(per_node, duration)
    cmd = [
        jmeter_bin,
        "-n",
        "-t", str(plan),
        "-R", hosts,
        "-Dserver.rmi.ssl.disable=true",
        f"-Ghost={host}",
        f"-Gthreads={per_node}",
        f"-Gramp={ramp}",
        f"-Gduration={duration}",
        "-l", str(jtl),
        "-e", "-o", str(report),
    ]
    # 每個參數同時以 -J 與 -G 傳遞：
    #   -J 只在本機生效（非分散式、或 master 自己也跑時需要）
    #   -G 會轉發到遠端引擎（分散式時節點靠它拿到 host/path/proxylist）
    # 只給 -J 的話，遠端引擎會退回計畫裡的預設值（例如 path=/），
    # 造成「看似有跑、其實打錯路徑」的靜默錯誤。
    for key, value in (extra or {}).items():
        if value is not None and str(value) != "":
            cmd.append(f"-J{key}={value}")
            cmd.append(f"-G{key}={value}")
    for key, value in (global_extra or {}).items():
        if value is not None and str(value) != "":
            cmd.append(f"-J{key}={value}")
            cmd.append(f"-G{key}={value}")
    return cmd


async def run_distributed(cmd: list[str], jtl: Path, cwd: Path, on_progress,
                          poll: int = 10, log_path: Path = None,
                          deadline: float = None, on_start=None) -> int:
    """執行分散式壓測。

    log_path  : 把 JMeter 的 stdout/stderr 寫進檔案（否則出錯時完全看不到原因）
    deadline  : 絕對時間戳，超過就強制終止（避免 RMI 卡死時永遠不返回）
    on_start  : 取得 process 物件，供 /abort 終止
    """
    sink = open(log_path, "wb") if log_path else asyncio.subprocess.DEVNULL
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=sink,
            stderr=asyncio.subprocess.STDOUT,
        )
        if on_start is not None:
            on_start(proc)

        while proc.returncode is None:
            if deadline is not None and time.time() > deadline:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                return -9
            try:
                await asyncio.wait_for(proc.wait(), timeout=poll)
            except asyncio.TimeoutError:
                count = 0
                if jtl.exists():
                    with open(jtl, encoding="utf-8", errors="replace") as f:
                        count = max(0, sum(1 for _ in f) - 1)
                await on_progress(count)
        return proc.returncode
    finally:
        if log_path:
            sink.close()
