#!/usr/bin/env python3
"""分散式壓測控制平台 · Discord 控制平面

角色
  OWNER  ：最高權限，核准目標、設定併發上限、可用 override 觸及全域上限
  STAFF  ：安全人員，提交目標、驗證所有權、在核准範圍內發起壓測（仍需 OWNER 按鈕核准）
  其他人 ：完全無法使用

目標生命週期
  pending_verify -> verified -> approved -> suspended
  只有 approved 的目標會產生流量。
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import distribute as dist
from core import proxy as px
from core import stepped as stp
from core import verify as vfy
from core.stats import compute_stats, judge

load_dotenv(Path(__file__).parent / ".env")

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
ADMINS_FILE = CONFIG_DIR / "admins.json"
TARGETS_FILE = CONFIG_DIR / "targets.json"
NODES_FILE = CONFIG_DIR / "nodes.json"
PROXIES_FILE = CONFIG_DIR / "proxies.txt"
PLANS_DIR = BASE_DIR / "plans"
RESULTS_DIR = BASE_DIR / "results"
AUDIT_LOG = BASE_DIR / "audit.log"
RESULTS_DIR.mkdir(exist_ok=True)

JMETER_BIN = os.getenv("JMETER_BIN", "jmeter")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
STAFF_IDS = {
    int(x)
    for x in os.getenv("STAFF_IDS", "").replace(",", " ").split()
    if x.strip().isdigit()
}
GUILD_ID = os.getenv("GUILD_ID")

RUNNING = {"active": False, "proc": None, "started": None, "label": "", "stop": False}

# 執行狀態檔：網頁控制台靠它顯示「正在壓測 xxx，30/60s」
STATUS_FILE = RESULTS_DIR / "loadtest-status.json"


def write_status(state: str, **fields) -> None:
    payload = {
        "state": state,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **fields,
    }
    try:
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception:
        pass


def patch_status(**fields) -> None:
    """保留既有欄位，只更新指定欄位（用於中止等沒有完整上下文的地方）。"""
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.update(fields)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception:
        pass


def write_meta(stem: str, data: dict) -> None:
    """把測試參數與統計寫成 <stem>.meta.json，供報告產生使用。"""
    try:
        path = RESULTS_DIR / f"{stem}.meta.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 設定讀寫
def _read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_targets() -> dict:
    return _read_json(TARGETS_FILE)


def save_targets(data: dict) -> None:
    _write_json(TARGETS_FILE, data)


def load_nodes() -> dict:
    return _read_json(NODES_FILE).get("nodes", {})


def parse_node_selection(text: str, nodes: dict) -> list:
    """解析 `nodes` 參數：`hk-1,hk-2` 或 `all`。

    回傳要參與的節點名稱清單；空字串或 all 回傳 None（表示全部啟用節點）。
    """
    raw = (text or "").strip()
    if not raw or raw.lower() in ("all", "全部", "所有"):
        return None
    wanted = [x.strip() for x in raw.replace("，", ",").replace("、", ",").split(",") if x.strip()]
    if not wanted:
        return None
    unknown = [w for w in wanted if w not in nodes]
    if unknown:
        raise ValueError(
            f"找不到節點 `{'、'.join(unknown)}`。可用節點：{'、'.join(nodes) or '（無）'}"
        )
    disabled = [w for w in wanted if not nodes[w].get("enabled", True)]
    if disabled:
        raise ValueError(f"節點 `{'、'.join(disabled)}` 已停用，請先在控制台啟用。")
    return wanted


def load_proxy_pool():
    """讀取代理清單。回傳 (proxies, errors)。"""
    return px.load(PROXIES_FILE)


def audit(action: str, user: str, detail: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} | {action:12s} | {user} | {detail}\n"
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def load_admins() -> set:
    """網站授權的管理員名單（config/admins.json）。每次讀取，改完立即生效。"""
    try:
        data = json.loads(ADMINS_FILE.read_text(encoding="utf-8"))
        return {int(x) for x in data.get("admins", []) if str(x).strip().isdigit()}
    except Exception:
        return set(STAFF_IDS)  # 尚未建立時沿用舊的 STAFF_IDS


def is_admin(uid: int) -> bool:
    return uid == OWNER_ID or uid in load_admins()


def is_staff(uid: int) -> bool:
    return is_admin(uid)


def plan_path(name: str) -> Path | None:
    candidate = (PLANS_DIR / name).resolve()
    if PLANS_DIR.resolve() not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------- 執行流程
async def execute_test(channel, key: str, target: dict, chosen: list,
                       per_node: int, actual_total: int, duration: int,
                       operator: str, override: bool, extra: dict = None,
                       plan: str = None, global_extra: dict = None) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jtl = RESULTS_DIR / f"{key}-{ts}.jtl"
    report = RESULTS_DIR / f"{key}-{ts}-report"
    plan_name = plan or target["plan"]
    cmd = dist.build_command(
        JMETER_BIN, plan_path(plan_name), chosen, per_node,
        duration, target["domain"], jtl, report, extra=extra,
        global_extra=global_extra,
    )

    RUNNING.update(active=True, started=datetime.now(), label=key)
    audit(
        "OVERRIDE" if override else "START",
        operator,
        f"{key} domain={target['domain']} total={actual_total} "
        f"per_node={per_node} nodes={len(chosen)} duration={duration}s",
    )

    node_names = ", ".join(k for k, _ in chosen)
    started_ts = RUNNING["started"].timestamp()
    base = {
        "key": key,
        "domain": target["domain"],
        "client": target.get("client", ""),
        "plan": plan_name,
        "threads": actual_total,
        "per_node": per_node,
        "nodes": [k for k, _ in chosen],
        "duration": duration,
        "started_ts": started_ts,
        "operator": operator,
        "override": override,
    }
    progress = {"count": 0, "elapsed": 0}
    write_status("running", elapsed=0, requests=0, **base)

    embed = discord.Embed(
        title=f"壓測執行中 · {key}",
        description=f"目標 `{target['domain']}`\n"
                    f"實際併發 **{actual_total}**（{len(chosen)} 節點 × {per_node}）\n"
                    f"節點：{node_names} · 持續 {duration}s",
        colour=0xEF9F27,
    )
    embed.add_field(name="已送回結果", value="0", inline=True)
    embed.add_field(name="已執行", value="0s", inline=True)
    msg = await channel.send(embed=embed)

    async def on_progress(count: int) -> None:
        elapsed = int((datetime.now() - RUNNING["started"]).total_seconds())
        progress["count"], progress["elapsed"] = count, elapsed
        write_status("running", elapsed=elapsed, requests=count, **base)
        embed.set_field_at(0, name="已送回結果", value=f"{count:,}", inline=True)
        embed.set_field_at(1, name="已執行", value=f"{elapsed}s / {duration}s", inline=True)
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

    jmeter_log = RESULTS_DIR / f"{key}-{ts}-jmeter.log"

    def register_proc(proc) -> None:
        RUNNING["proc"] = proc

    try:
        rc = await dist.run_distributed(
            cmd, jtl, PLANS_DIR, on_progress,
            log_path=jmeter_log,
            deadline=time.time() + duration + 180,
            on_start=register_proc,
        )
    except FileNotFoundError:
        await channel.send(f"找不到 JMeter：`{JMETER_BIN}`，請檢查 .env。")
        audit("ERROR", operator, "JMeter 不存在")
        write_status("failed", elapsed=0, requests=0, error="找不到 JMeter", **base)
        return
    finally:
        RUNNING.update(active=False, proc=None)
        write_status(
            "finished",
            elapsed=int(datetime.now().timestamp() - started_ts),
            requests=progress["count"],
            **base,
        )

    if rc == -9:
        write_status("failed", elapsed=duration, requests=progress["count"],
                     error=f"逾時未結束（超過 {duration + 180}s）", **base)
        audit("TIMEOUT", operator, f"{key} 超過 {duration + 180}s 強制終止")
        await channel.send(
            f"`{key}` 壓測逾時未結束（超過時長 {duration}s + 180s 寬限），已強制終止。\n"
            f"常見原因：節點回傳通道不通（master 的 `java.rmi.server.hostname` "
            f"未設為公網 IP，或安全組未放行 RMI 回傳埠）。\n"
            f"JMeter 日誌：`{jmeter_log.name}`"
        )
        return

    if not jtl.exists():
        await channel.send(
            "執行失敗，未產生結果檔。常見原因：節點未啟動 `jmeter-server`、"
            "RMI 1099 埠不通、或 `server.rmi.ssl.disable` 未在 master 與節點同時設定。"
        )
        audit("FAIL", operator, f"{key} 無 jtl，rc={rc}")
        write_status("failed", elapsed=duration, requests=progress["count"],
                     error="未產生結果檔", **base)
        return

    s = compute_stats(jtl)
    ok = judge(s)
    write_meta(f"{key}-{ts}", {
        **base,
        "total": s["total"],
        "err_pct": round(s["err_pct"], 2),
        "avg_ms": round(s["avg"], 1),
        "p50_ms": round(s["p50"], 1),
        "p90_ms": round(s["p90"], 1),
        "p99_ms": round(s["p99"], 1),
        "ok": ok,
        "ended_at": datetime.now().isoformat(timespec="seconds"),
    })
    write_status(
        "done",
        elapsed=duration,
        requests=s["total"],
        ok=ok,
        err_pct=round(s["err_pct"], 2),
        avg_ms=round(s["avg"], 1),
        p90_ms=round(s["p90"], 1),
        **base,
    )
    result = discord.Embed(
        title=f"壓測完成 · {key}",
        description=f"目標 `{target['domain']}` · {len(chosen)} 節點 × {per_node} = "
                    f"實際 {actual_total} 併發 · {duration}s",
        colour=0x639922 if ok else 0xE24B4A,
    )
    result.add_field(name="請求總數", value=f"{s['total']:,}", inline=True)
    result.add_field(name="錯誤率", value=f"{s['err_pct']:.2f}%", inline=True)
    result.add_field(name="平均延遲", value=f"{s['avg']:.0f}ms", inline=True)
    result.add_field(name="P50", value=f"{s['p50']}ms", inline=True)
    result.add_field(name="P90", value=f"{s['p90']}ms", inline=True)
    result.add_field(name="P99", value=f"{s['p99']}ms", inline=True)
    result.add_field(name="最大延遲", value=f"{s['max']}ms", inline=True)
    result.add_field(
        name="判定",
        value="通過（P90 < 800ms 且錯誤率 < 1%）" if ok else "未達標，需檢視瓶頸",
        inline=False,
    )
    result.set_footer(
        text=f"報告 {report.name}/index.html · 執行者 {operator}"
        + (" · 最高權限 override" if override else "")
    )
    await channel.send(embed=result)
    audit(
        "DONE", operator,
        f"{key} total={s['total']} err={s['err_pct']:.2f}% p90={s['p90']}ms",
    )


class ApproveView(discord.ui.View):
    def __init__(self, key: str, target: dict, chosen: list, per_node: int,
                 actual_total: int, duration: int, override: bool):
        super().__init__(timeout=300)
        self.key, self.target = key, target
        self.chosen, self.per_node = chosen, per_node
        self.actual_total, self.duration = actual_total, duration
        self.override = override

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "只有 OWNER 可以核准壓測執行。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="核准並執行", style=discord.ButtonStyle.danger)
    async def approve(self, interaction: discord.Interaction, _):
        if RUNNING["active"]:
            await interaction.response.send_message("已有測試在執行中。", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        await execute_test(
            interaction.channel, self.key, self.target, self.chosen,
            self.per_node, self.actual_total, self.duration,
            str(interaction.user), self.override,
        )

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="已取消，未產生任何流量。", embed=None, view=self
        )
        audit("CANCEL", str(interaction.user), f"{self.key} 未執行")
        self.stop()


client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)


# ---------------------------------------------------------------- 自動完成
async def target_autocomplete(interaction: discord.Interaction, current: str):
    try:
        targets = load_targets().get("targets", {})
    except Exception:
        return []
    cur = current.lower()
    return [
        app_commands.Choice(name=f"{k} — {v['domain']} [{v['status']}]", value=k)
        for k, v in targets.items()
        if cur in k.lower() or cur in v["domain"].lower()
    ][:25]


# ---------------------------------------------------------------- 目標管理
target_group = app_commands.Group(name="target", description="壓測目標管理")


@target_group.command(name="add", description="提交壓測目標，系統會產生所有權驗證 token")
@app_commands.describe(domain="客戶域名，例如 example.com", client="客戶名稱",
                       plan="壓測計畫檔名",
                       url="要壓測的完整網址，留空則用 https://域名/")
async def target_add(interaction: discord.Interaction, domain: str, client: str,
                     plan: str = "baseline.jmx", url: str = ""):
    if not is_staff(interaction.user.id):
        await interaction.response.send_message("你沒有使用權限。", ephemeral=True)
        audit("DENY", str(interaction.user), f"target add {domain}")
        return

    domain = domain.lower().strip().strip(".")
    if not domain or "/" in domain or " " in domain:
        await interaction.response.send_message(
            "域名格式不正確，請只填域名本身（不含 http:// 與路徑）。", ephemeral=True
        )
        return
    if "." not in domain:
        await interaction.response.send_message(
            "請輸入完整域名（需含後綴，例如 example.com）。", ephemeral=True
        )
        return
    if plan_path(plan) is None:
        await interaction.response.send_message(
            f"計畫檔 `{plan}` 不存在於 plans/ 目錄。", ephemeral=True
        )
        return

    # 網址一併存進目標：/start 與 /stepped 只從目標取網址，不接受臨時輸入
    raw_url = (url or "").strip() or f"https://{domain}/"
    if "://" not in raw_url:
        raw_url = f"https://{raw_url}"
    parsed_url = urlparse(raw_url)
    if (parsed_url.scheme not in ("http", "https")
            or (parsed_url.hostname or "").lower() != domain):
        await interaction.response.send_message(
            f"網址需為 `http(s)://{domain}/...`，目前是 `{raw_url}`。", ephemeral=True
        )
        return
    target_protocol = parsed_url.scheme
    target_port = parsed_url.port or (443 if target_protocol == "https" else 80)
    target_path = parsed_url.path or "/"
    if parsed_url.query:
        target_path = f"{target_path}?{parsed_url.query}"

    data = load_targets()
    key = domain.replace(".", "-")
    if key in data["targets"]:
        await interaction.response.send_message(
            f"目標 `{key}` 已存在（狀態 {data['targets'][key]['status']}）。", ephemeral=True
        )
        return

    settings = data.get("settings", {})
    ok, msg = vfy.check_target_address(
        domain, settings.get("allow_private_targets", False)
    )
    if not ok:
        await interaction.response.send_message(f"無法受理：{msg}", ephemeral=True)
        audit("DENY", str(interaction.user), f"target add {domain} 位址被拒")
        return

    token = vfy.new_token()
    prefix = settings.get("verify_txt_prefix", "_loadtest-verify")
    http_path = settings.get("verify_http_path", "/.well-known/loadtest-verify.txt")
    data["targets"][key] = {
        "domain": domain,
        "client": client,
        "plan": plan,
        "url": raw_url,
        "protocol": target_protocol,
        "port": target_port,
        "path": target_path,
        "method": "GET",
        "status": "pending_verify",
        "token": token,
        "submitted_by": str(interaction.user),
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "verified_at": None,
        "verification": None,
        "approved_by": None,
        "max_threads": 0,
        "max_duration": 0,
        "authorized_until": None,
    }
    save_targets(data)
    audit("TARGET-ADD", str(interaction.user), f"{key} {domain} client={client}")

    embed = discord.Embed(
        title=f"目標已提交 · {key}",
        description=f"`{domain}`（{client}）\n狀態：**待驗證所有權**\n\n{msg}",
        colour=0x378ADD,
    )
    embed.add_field(
        name="驗證方式 A · DNS TXT",
        value=f"記錄名稱：`{prefix}.{domain}`\n記錄值：`{token}`",
        inline=False,
    )
    embed.add_field(
        name="驗證方式 B · HTTP 檔案",
        value=f"在 `https://{domain}{http_path}` 放置內容為\n`{token}` 的純文字檔",
        inline=False,
    )
    embed.set_footer(text="完成後執行 /target verify 驗證；OWNER 也可直接 /target approve 跳過驗證核准")
    await interaction.response.send_message(embed=embed)


@target_group.command(name="verify", description="驗證目標的域名所有權")
@app_commands.autocomplete(key=target_autocomplete)
async def target_verify(interaction: discord.Interaction, key: str):
    if not is_staff(interaction.user.id):
        await interaction.response.send_message("你沒有使用權限。", ephemeral=True)
        return
    data = load_targets()
    t = data["targets"].get(key)
    if not t:
        await interaction.response.send_message("找不到該目標。", ephemeral=True)
        return
    if t["status"] == "approved":
        await interaction.response.send_message("該目標已通過核准，無需驗證。", ephemeral=True)
        return

    await interaction.response.defer()
    settings = data.get("settings", {})
    ok, msg = await vfy.verify_ownership(
        t["domain"], t["token"],
        settings.get("verify_txt_prefix", "_loadtest-verify"),
        settings.get("verify_http_path", "/.well-known/loadtest-verify.txt"),
    )
    if ok:
        t["status"] = "verified"
        t["verified_at"] = datetime.now().isoformat(timespec="seconds")
        t["verification"] = "http" if "HTTP" in msg.upper() else "dns"
        save_targets(data)
        audit("TARGET-VERIFY", str(interaction.user), f"{key} 通過：{msg}")
        await interaction.followup.send(
            f"`{key}` 所有權驗證通過。\n{msg}\n\n"
            f"下一步：由 OWNER 執行 `/target approve` 設定併發上限。"
        )
    else:
        audit("TARGET-VERIFY-FAIL", str(interaction.user), f"{key} {msg}")
        await interaction.followup.send(f"`{key}` 驗證失敗。\n{msg}")


@target_group.command(name="approve", description="[OWNER] 核准目標並設定併發上限（可跳過所有權驗證）")
@app_commands.describe(key="目標", threads="允許的最大總併發", duration="允許的最大時長（秒）",
                       days="授權有效天數")
@app_commands.autocomplete(key=target_autocomplete)
async def target_approve(interaction: discord.Interaction, key: str, threads: int,
                         duration: int, days: int = 30):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("只有 OWNER 可以核准目標。", ephemeral=True)
        audit("DENY-APPROVE", str(interaction.user), f"{key}")
        return

    data = load_targets()
    t = data["targets"].get(key)
    if not t:
        await interaction.response.send_message("找不到該目標。", ephemeral=True)
        return

    # OWNER 可直接核准未驗證的目標：驗證只是給客戶看的證據，
    # 真正的閘門是 OWNER 的核准與每次壓測前的按鈕確認。
    if t["status"] not in ("pending_verify", "verified", "approved", "suspended"):
        await interaction.response.send_message(
            f"該目標狀態為 `{t['status']}`，無法核准。", ephemeral=True
        )
        return

    was_verified = bool(t.get("verified_at"))

    limits = data.get("limits", {})
    g_t = limits.get("global_max_threads", 5000)
    g_d = limits.get("global_max_duration", 1800)
    if not 1 <= threads <= g_t:
        await interaction.response.send_message(f"併發上限須介於 1 ~ {g_t}。", ephemeral=True)
        return
    if not 10 <= duration <= g_d:
        await interaction.response.send_message(f"時長上限須介於 10 ~ {g_d} 秒。", ephemeral=True)
        return

    until = (datetime.now() + timedelta(days=days)).date().isoformat()
    t.update(
        status="approved",
        approved_by=str(interaction.user),
        approved_at=datetime.now().isoformat(timespec="seconds"),
        max_threads=threads,
        max_duration=duration,
        authorized_until=until,
        verification=t.get("verification") if was_verified else "owner_override",
    )
    save_targets(data)
    audit(
        "TARGET-APPROVE" if was_verified else "TARGET-APPROVE-UNVERIFIED",
        str(interaction.user),
        f"{key} {threads} 併發 / {duration}s 至 {until}"
        + ("" if was_verified else "（未經驗證，Owner 授權）"),
    )

    body = (
        f"`{key}`（{t['domain']}）已核准。\n"
        f"上限 **{threads} 併發 / {duration} 秒**，授權至 **{until}**。"
    )
    if was_verified:
        body += f"\n所有權驗證：已通過（{t.get('verification', '—')}）"
    else:
        body += (
            "\n\n⚠️ **此目標未通過所有權驗證**，由 OWNER 直接核准。\n"
            "稽核日誌已記錄為 `TARGET-APPROVE-UNVERIFIED`。"
            "請確認已取得客戶的書面壓測授權，該文件是唯一的責任憑證。"
        )

    await interaction.response.send_message(body)


@target_group.command(name="suspend", description="[OWNER] 立即暫停目標，停止一切壓測")
@app_commands.autocomplete(key=target_autocomplete)
async def target_suspend(interaction: discord.Interaction, key: str):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("只有 OWNER 可以暫停目標。", ephemeral=True)
        return
    data = load_targets()
    t = data["targets"].get(key)
    if not t:
        await interaction.response.send_message("找不到該目標。", ephemeral=True)
        return
    t["status"] = "suspended"
    save_targets(data)
    audit("TARGET-SUSPEND", str(interaction.user), key)
    await interaction.response.send_message(f"`{key}` 已暫停，無法再發起壓測。")


@target_group.command(name="list", description="列出所有目標與授權狀態")
async def target_list(interaction: discord.Interaction):
    if not is_staff(interaction.user.id):
        await interaction.response.send_message("你沒有使用權限。", ephemeral=True)
        return
    data = load_targets()
    targets = data.get("targets", {})
    limits = data.get("limits", {})
    embed = discord.Embed(
        title="目標清單",
        description=f"全域硬上限 {limits.get('global_max_threads')} 併發 / "
                    f"{limits.get('global_max_duration')}s",
        colour=0x378ADD,
    )
    if not targets:
        embed.add_field(name="尚無目標", value="使用 `/target add` 提交", inline=False)
    for k, v in targets.items():
        ver = v.get("verification")
        ver_text = {
            "dns": "所有權：DNS TXT 已驗證",
            "http": "所有權：HTTP 檔案已驗證",
            "owner_override": "⚠️ 所有權：未驗證（Owner 直接核准）",
        }.get(ver, "所有權：未驗證")
        embed.add_field(
            name=f"{k} — {v['domain']} [{v['status']}]",
            value=f"客戶 {v['client']}\n"
                  f"上限 {v['max_threads']} 併發 / {v['max_duration']}s · "
                  f"授權至 {v['authorized_until'] or '—'}\n{ver_text}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


tree.add_command(target_group)


# ---------------------------------------------------------------- 節點
@tree.command(name="nodes", description="檢查各壓測節點連線狀態")
async def nodes_cmd(interaction: discord.Interaction):
    if not is_staff(interaction.user.id):
        await interaction.response.send_message("你沒有使用權限。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    nodes = load_nodes()
    health = await dist.check_nodes(nodes)
    embed = discord.Embed(title="壓測節點狀態", colour=0x378ADD)
    online = 0
    for key, cfg in nodes.items():
        if not cfg.get("enabled", True):
            embed.add_field(name=f"{key}（停用）", value=f"{cfg['host']}:{cfg.get('port', 1099)}",
                            inline=False)
            continue
        alive = health.get(key, False)
        online += 1 if alive else 0
        embed.add_field(
            name=f"{key} — {'在線' if alive else '離線'}",
            value=f"{cfg['host']}:{cfg.get('port', 1099)} · {cfg.get('region', '—')}\n"
                  f"承載上限 {cfg.get('max_threads')} 執行緒",
            inline=False,
        )
    embed.set_footer(
        text=f"在線 {online} / {sum(1 for c in nodes.values() if c.get('enabled', True))}"
             f" · 有效容量 {dist.effective_capacity(nodes)} 併發"
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------- 壓測
@tree.command(name="loadtest", description="對已核准目標發起分散式壓測")
@app_commands.describe(key="目標", threads="總併發（所有節點合計）", duration="持續秒數",
                       override="[OWNER] 突破該目標的授權上限，仍受全域硬上限約束")
@app_commands.autocomplete(key=target_autocomplete)
async def loadtest(interaction: discord.Interaction, key: str, threads: int,
                   duration: int, override: bool = False):
    uid = interaction.user.id
    if not is_staff(uid):
        await interaction.response.send_message("你沒有使用權限。", ephemeral=True)
        audit("DENY", str(interaction.user), f"loadtest {key} {threads}x{duration}")
        return
    if override and not is_owner(uid):
        await interaction.response.send_message(
            "只有 OWNER 可以使用 override。", ephemeral=True
        )
        audit("DENY-OVERRIDE", str(interaction.user), f"{key} {threads}x{duration}")
        return

    data = load_targets()
    t = data.get("targets", {}).get(key)
    if not t:
        await interaction.response.send_message("找不到該目標。", ephemeral=True)
        return
    if t["status"] != "approved":
        await interaction.response.send_message(
            f"目標狀態為 `{t['status']}`，只有 `approved` 可以壓測。", ephemeral=True
        )
        return
    until = t.get("authorized_until")
    if until and until < datetime.now().date().isoformat():
        await interaction.response.send_message(
            f"授權已於 {until} 到期，請重新執行 /target approve。", ephemeral=True
        )
        return

    limits = data.get("limits", {})
    if override:
        cap_t = limits.get("global_max_threads", 5000)
        cap_d = limits.get("global_max_duration", 1800)
    else:
        cap_t, cap_d = t["max_threads"], t["max_duration"]
    if not 1 <= threads <= cap_t:
        await interaction.response.send_message(
            f"併發數超出上限（1 ~ {cap_t}）。", ephemeral=True
        )
        return
    if not 10 <= duration <= cap_d:
        await interaction.response.send_message(
            f"時長超出上限（10 ~ {cap_d} 秒）。", ephemeral=True
        )
        return
    if RUNNING["active"]:
        await interaction.response.send_message("已有測試在執行中。", ephemeral=True)
        return

    nodes = load_nodes()
    chosen, per_node, actual = dist.allocate(threads, nodes)
    if not chosen:
        cap = dist.effective_capacity(nodes)
        await interaction.response.send_message(
            f"節點容量不足：目前有效容量為 **{cap}** 併發。\n"
            f"分散式模式下每個節點執行相同的執行緒數，因此有效容量是 "
            f"min(各節點上限) × 節點數，而非各節點上限相加。\n"
            f"請減少併發，或部署更多節點。",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="壓測待核准 · 最高權限 override" if override else "壓測待核准",
        description=f"目標 `{t['domain']}`（{t['client']}）\n"
                    f"計畫 `{t['plan']}`\n"
                    f"請求併發 **{threads}** → 實際 **{actual}**"
                    f"（{len(chosen)} 節點 × {per_node}）\n"
                    f"持續 **{duration}s**",
        colour=0xE24B4A if override else 0x378ADD,
    )
    embed.add_field(
        name="參與節點",
        value="\n".join(
            f"{k} · {cfg['host']} · 上限 {cfg['max_threads']}" for k, cfg in chosen
        ),
        inline=False,
    )
    if override:
        embed.add_field(
            name="自訂授權",
            value=f"已突破目標授權上限（{t['max_threads']} 併發 / "
                  f"{t['max_duration']}s），受全域上限 "
                  f"{cap_t} / {cap_d}s 約束。將以 OVERRIDE 記錄稽核。",
            inline=False,
        )
    embed.add_field(name="授權期限", value=t.get("authorized_until") or "—", inline=True)
    embed.set_footer(text="按下核准才會產生流量")
    await interaction.response.send_message(
        embed=embed,
        view=ApproveView(key, t, chosen, per_node, actual, duration, override),
    )


# ---------------------------------------------------------------- 一句話壓測
def parse_mode(text: str) -> tuple[str, str]:
    """https-get → ("https", "GET")"""
    raw = (text or "").strip().lower().replace("_", "-")
    protocol, _, method = raw.partition("-")
    if protocol not in ("http", "https"):
        raise ValueError("協定只能是 http 或 https，例：`https-get`")
    method = (method or "get").upper()
    if method not in ("GET", "POST"):
        raise ValueError("方法只能是 get 或 post，例：`https-get`")
    return protocol, method


def make_key(host: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:40]) or "target"


def target_request_spec(target: dict) -> tuple:
    """從網頁定義的目標取出 (protocol, port, path, method, url)。

    網址由網頁控制台設定，Discord 端不再接受臨時輸入的網址。
    """
    raw = (target.get("url") or target.get("last_url") or "").strip()
    if not raw:
        raise ValueError("此目標尚未設定網址，請到網頁控制台編輯後再試")
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    protocol = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    try:
        port = int(target.get("port") or parsed.port or (443 if protocol == "https" else 80))
    except (TypeError, ValueError):
        port = 443 if protocol == "https" else 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    method = (target.get("method") or target.get("last_method") or "GET").upper()
    return protocol, port, path, method, raw


def pick_target(key: str) -> tuple:
    """取出可用的目標，回傳 (targets 資料, target)。不可用時丟 ValueError。"""
    data = load_targets()
    targets = data.get("targets", {})
    target = targets.get(key)
    if not target:
        raise ValueError(
            f"找不到目標 `{key}`。\n"
            "目標只能在網頁控制台的「壓測目標」新增，建立後才能在這裡選用。"
        )
    if target.get("status") != "approved":
        raise ValueError(
            f"目標 `{key}` 目前狀態為 `{target.get('status')}`，不可壓測。\n"
            "請到網頁控制台啟用。"
        )
    until = target.get("authorized_until")
    if until and until < datetime.now().date().isoformat():
        raise ValueError(f"目標 `{key}` 授權已於 {until} 到期，請到網頁控制台延長。")
    return data, target


@tree.command(name="start", description="對網頁控制台預先定義的目標發起壓測")
@app_commands.describe(
    key="目標（只能選網頁控制台已定義並啟用的目標）",
    threads="併發執行緒數，例 300",
    duration="持續秒數，預設 60",
    nodes="只使用指定節點，逗號分隔，例 HK-Huawei,hk-huawei2；留空為全部",
    proxy="透過代理池發壓，模擬多來源 IP（需先填 config/proxies.txt）",
)
@app_commands.autocomplete(key=target_autocomplete)
async def start_cmd(interaction: discord.Interaction, key: str, threads: int,
                    duration: int = 60, nodes: str = "", proxy: bool = False):
    uid = interaction.user.id
    if not is_admin(uid):
        await interaction.response.send_message(
            "你沒有權限。只有 OWNER 與網站授權的管理員可以發起壓測。", ephemeral=True)
        audit("DENY-START", str(interaction.user), f"{key} {threads}x{duration}")
        return

    try:
        data, target = pick_target(key)
        protocol, port, path, method, raw = target_request_spec(target)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        audit("DENY-START", str(interaction.user), f"{key} 目標不可用")
        return

    limits = data.get("limits", {})
    global_t = int(limits.get("global_max_threads", 5000))
    global_d = int(limits.get("global_max_duration", 1800))
    cap_t = min(int(target.get("max_threads") or global_t), global_t)
    cap_d = min(int(target.get("max_duration") or global_d), global_d)
    if not 1 <= threads <= cap_t:
        await interaction.response.send_message(
            f"目標 `{key}` 的併發上限是 **{cap_t}**，請調整 threads。", ephemeral=True)
        return
    if not 5 <= duration <= cap_d:
        await interaction.response.send_message(
            f"目標 `{key}` 的時長上限是 **{cap_d}** 秒，請調整 duration。", ephemeral=True)
        return
    if RUNNING["active"]:
        await interaction.response.send_message(
            "已有測試在執行中，請先 `/abort`。", ephemeral=True)
        return

    node_map = load_nodes()
    try:
        only = parse_node_selection(nodes, node_map)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    http_pool: list = []
    socks_note = ""
    if proxy:
        pool, perrors = load_proxy_pool()
        http_pool, socks_pool = px.split_by_scheme(pool)
        if not pool:
            detail = "\n".join(perrors[:5]) if perrors else "檔案不存在或為空。"
            await interaction.response.send_message(
                f"代理模式需要 `config/proxies.txt`，但目前沒有可用代理。\n{detail}",
                ephemeral=True)
            return
        if not http_pool:
            await interaction.response.send_message(
                f"清單中只有 {len(socks_pool)} 個 SOCKS 代理。JMeter 的取樣器只能"
                "設定 HTTP/HTTPS 代理，無法逐執行緒輪換 SOCKS。\n"
                "請改用 HTTP/HTTPS 代理入口。", ephemeral=True)
            return
        if socks_pool:
            socks_note = (f"\n（已略過 {len(socks_pool)} 個 SOCKS 代理，"
                          f"本次只用 {len(http_pool)} 個 HTTP 代理）")

    chosen, per_node, actual = dist.allocate(threads, node_map, only=only)
    if not chosen:
        await interaction.response.send_message(
            f"節點容量不足：目前有效容量 {dist.effective_capacity(node_map, only)} 併發。",
            ephemeral=True)
        return

    mode_note = f" proxy={len(http_pool)}" if proxy else ""
    audit("START", str(interaction.user),
          f"{key} {protocol}://{target.get('domain', '')}:{port}{path} "
          f"{threads}x{duration}s {method}"
          f" nodes={','.join(k for k, _ in chosen)}{mode_note}")

    await interaction.response.defer()
    await interaction.followup.send(
        f"**開始壓測** · `{raw}`\n"
        f"目標 `{key}`（{target.get('client') or '—'}）· {method}\n"
        f"併發 **{threads}**（{len(chosen)} 節點 × {per_node}）"
        f" · 持續 **{duration} 秒**\n"
        f"節點：{'、'.join(k for k, _ in chosen)}\n"
        + (f"來源：**{len(http_pool)} 個代理輪換**{socks_note}\n" if proxy else "")
        + f"操作者 {interaction.user.mention}"
    )
    await execute_test(
        interaction.channel, key, target, chosen, per_node, actual,
        duration, str(interaction.user), False,
        extra={"protocol": protocol, "port": port, "path": path, "method": method},
        plan="oneshot-proxy.jmx" if proxy else (target.get("plan") or "oneshot.jmx"),
        global_extra={"proxylist": px.encode_for_jmeter(http_pool)} if proxy else None,
    )


# ---------------------------------------------------------------- 階梯壓測
async def execute_stepped(channel, key: str, target: dict, stages: list[int],
                          duration: int, gap: int, operator: str,
                          url: str, extra: dict, only: list = None,
                          plan: str = "oneshot.jmx",
                          global_extra: dict = None) -> None:
    """逐級加壓，找出容量拐點。

    每級都是一次完整的分散式壓測；級與級之間留 gap 秒，讓 WAF 速率限制
    與源站有時間恢復，否則後段量到的全是 403，看不到源站真實行為。
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{key}-{ts}-stepped"
    total_stages = len(stages)
    started = datetime.now()
    started_ts = started.timestamp()
    rows: list[dict] = []
    nodes_used: list[str] = []

    RUNNING.update(active=True, proc=None, started=started, label=key, stop=False)
    audit("STEPPED", operator,
          f"{key} domain={target['domain']} stages={','.join(map(str, stages))} "
          f"duration={duration}s gap={gap}s plan={plan}")

    header = discord.Embed(
        title=f"階梯壓測 · {key}",
        description=(f"目標 `{target['domain']}`\n"
                     f"各級併發 **{' → '.join(map(str, stages))}**\n"
                     f"每級 {duration}s · 級間間隔 {gap}s · 預計 "
                     f"{total_stages * duration + (total_stages - 1) * gap}s"),
        colour=0x378ADD,
    )
    header.set_footer(text=f"操作者 {operator} · /abort 可中止整場")
    msg = await channel.send(embed=header)

    def stage_embed(idx: int, threads: int, per_node: int, node_list: list,
                    state_text: str, counts: int = 0,
                    elapsed: int = 0) -> discord.Embed:
        e = discord.Embed(
            title=f"階梯壓測 · 第 {idx}/{total_stages} 級 · {threads} 併發",
            description=(f"目標 `{target['domain']}`\n{state_text}\n"
                         f"節點：{', '.join(node_list)}（每節點 {per_node} 執行緒）"),
            colour=0xEF9F27,
        )
        e.add_field(name="已送回結果", value=f"{counts:,}", inline=True)
        e.add_field(name="已執行", value=f"{elapsed}s / {duration}s", inline=True)
        done = "\n".join(
            f"{r['threads']} 併發 · {r['err_pct']:.2f}% 錯誤 · {r['ok_rps']:.1f} req/s"
            for r in rows if r.get("total")
        )
        if done:
            e.add_field(name="已完成級別", value=done, inline=False)
        return e

    for idx, threads in enumerate(stages, 1):
        if RUNNING.get("stop"):
            break

        nodes = load_nodes()
        chosen, per_node, actual = dist.allocate(threads, nodes, only=only)
        if not chosen:
            rows.append({
                "threads": threads, "total": 0, "ok": 0, "errors": 0,
                "err_pct": 0.0, "rps": 0.0, "ok_rps": 0.0, "avg_ms": 0.0,
                "p50_ms": 0, "p90_ms": 0, "p99_ms": 0, "max_ms": 0,
                "healthy": False,
                "note": f"節點容量不足（有效容量 {dist.effective_capacity(nodes, only)}）",
            })
            audit("STEPPED-SKIP", operator, f"{key} {threads} 併發：容量不足")
            continue

        node_list = [k for k, _ in chosen]
        for name in node_list:
            if name not in nodes_used:
                nodes_used.append(name)

        stem = f"{run_id}-s{idx}-{actual}t"
        jtl = RESULTS_DIR / f"{stem}.jtl"
        report = RESULTS_DIR / f"{stem}-report"
        jlog = RESULTS_DIR / f"{stem}-jmeter.log"
        cmd = dist.build_command(
            JMETER_BIN, plan_path(plan), chosen, per_node,
            duration, target["domain"], jtl, report, extra=extra,
            global_extra=global_extra,
        )

        base = {
            "key": key, "domain": target["domain"],
            "client": target.get("client", ""), "plan": plan,
            "threads": actual, "per_node": per_node, "nodes": node_list,
            "duration": duration, "started_ts": started_ts,
            "operator": operator, "override": False,
            "mode": "stepped", "stage_index": idx,
            "stage_total": total_stages, "stages": stages,
        }
        write_status("running", elapsed=0, requests=0, stage_elapsed=0,
                     stage_threads=actual, **base)

        state = {"count": 0}
        stage_start = datetime.now()
        await msg.edit(embed=stage_embed(idx, actual, per_node, node_list, "加壓中…"))

        async def on_progress(count: int) -> None:
            elapsed = int((datetime.now() - started).total_seconds())
            stage_elapsed = int((datetime.now() - stage_start).total_seconds())
            state["count"] = count
            write_status("running", elapsed=elapsed, requests=count,
                         stage_elapsed=stage_elapsed, stage_threads=actual, **base)
            try:
                await msg.edit(embed=stage_embed(
                    idx, actual, per_node, node_list, "加壓中…", count, elapsed))
            except discord.HTTPException:
                pass

        def register_proc(proc) -> None:
            RUNNING["proc"] = proc

        try:
            rc = await dist.run_distributed(
                cmd, jtl, PLANS_DIR, on_progress, poll=5,
                log_path=jlog, deadline=time.time() + duration + 180,
                on_start=register_proc,
            )
        except FileNotFoundError:
            RUNNING.update(active=False, proc=None)
            audit("ERROR", operator, "JMeter 不存在")
            write_status("failed", elapsed=0, requests=0,
                         error="找不到 JMeter", **base)
            await channel.send(f"找不到 JMeter：`{JMETER_BIN}`")
            return
        finally:
            RUNNING["proc"] = None

        if not jtl.exists():
            rows.append({
                "threads": actual, "total": 0, "ok": 0, "errors": 0,
                "err_pct": 0.0, "rps": 0.0, "ok_rps": 0.0, "avg_ms": 0.0,
                "p50_ms": 0, "p90_ms": 0, "p99_ms": 0, "max_ms": 0,
                "healthy": False, "note": f"未產生結果檔（rc={rc}）",
            })
            audit("STEPPED-FAIL", operator, f"{key} {actual} 併發無 jtl rc={rc}")
            continue

        row = stp.stage_metrics(actual, duration, compute_stats(jtl))
        row["stem"] = stem
        rows.append(row)
        write_meta(stem, {
            **base,
            "total": row["total"], "err_pct": row["err_pct"],
            "avg_ms": row["avg_ms"], "p50_ms": row["p50_ms"],
            "p90_ms": row["p90_ms"], "p99_ms": row["p99_ms"],
            "ok": row["healthy"],
            "ended_at": datetime.now().isoformat(timespec="seconds"),
        })
        audit("STEPPED-STAGE", operator,
              f"{key} {actual} 併發 total={row['total']} "
              f"err={row['err_pct']:.2f}% ok_rps={row['ok_rps']}")

        await msg.edit(embed=stage_embed(
            idx, actual, per_node, node_list,
            f"本級完成 · 錯誤 {row['err_pct']:.2f}% · 成功 {row['ok_rps']:.1f} req/s",
            row["total"], duration))

        if idx < total_stages and not RUNNING.get("stop"):
            for left in range(gap, 0, -1):
                if RUNNING.get("stop"):
                    break
                write_status("running", elapsed=duration, requests=0,
                             stage_elapsed=duration, stage_wait=left,
                             stage_threads=actual, **base)
                try:
                    await msg.edit(embed=stage_embed(
                        idx, actual, per_node, node_list,
                        f"間隔等待 {left}s（讓 WAF 限流重置）…",
                        row["total"], duration))
                except discord.HTTPException:
                    pass
                await asyncio.sleep(1)

    RUNNING.update(active=False, proc=None, stop=False)

    if not rows:
        write_status("failed", elapsed=0, requests=0, error="未取得任何結果",
                     key=key, domain=target["domain"], mode="stepped")
        await channel.send("階梯壓測未取得任何結果。")
        return

    summary = stp.analyze(rows)
    verdict = stp.verdict_text(rows, summary)
    meta = {
        "key": key, "domain": target["domain"], "client": target.get("client", ""),
        "plan": plan, "mode": "stepped", "url": url, "stages": stages,
        "duration": duration, "gap": gap, "nodes": nodes_used,
        "operator": operator,
        "started_at": started.isoformat(timespec="seconds"),
        "summary": summary,
    }

    html_path = RESULTS_DIR / f"{run_id}.html"
    try:
        html_path.write_text(stp.render_html(meta, rows, summary), encoding="utf-8")
    except Exception as exc:
        audit("ERROR", operator, f"階梯報告產生失敗：{exc}")
        html_path = None
    (RESULTS_DIR / f"{run_id}.json").write_text(
        json.dumps({**meta, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    total_req = sum(r["total"] for r in rows)
    total_err = sum(r["errors"] for r in rows)
    err_all = (total_err / total_req * 100) if total_req else 0.0

    final = discord.Embed(
        title=f"階梯壓測完成 · {key}",
        description=(f"目標 `{target['domain']}` · {total_stages} 級 · "
                     f"共 {total_req:,} 請求 · 整體錯誤 {err_all:.2f}%"),
        colour=0x639922 if summary.get("knee_threads") else 0xE24B4A,
    )
    for r in rows:
        if not r.get("total"):
            final.add_field(name=f"{r['threads']} 併發",
                            value=f"未取得結果（{r.get('note', '')}）", inline=False)
            continue
        final.add_field(
            name=f"{r['threads']} 併發 · {'健康' if r['healthy'] else '不健康'}",
            value=(f"{r['ok_rps']:.1f} req/s 成功 · 錯誤 {r['err_pct']:.2f}% · "
                   f"P90 {r['p90_ms']}ms"),
            inline=False,
        )
    final.add_field(name="結論", value=verdict, inline=False)
    final.add_field(
        name="容量",
        value=(f"拐點 {summary.get('knee_threads') or '—'} 併發 · "
               f"峰值成功吞吐 {summary.get('best_ok_rps') or 0:.1f} req/s"),
        inline=False,
    )
    if html_path:
        final.set_footer(text=f"報告 {html_path.name}")
    await channel.send(embed=final)

    write_status("done", elapsed=int((datetime.now() - started).total_seconds()),
                 requests=total_req, ok=bool(summary.get("knee_threads")),
                 err_pct=round(err_all, 2), mode="stepped",
                 key=key, domain=target["domain"], stages=stages,
                 stage_total=total_stages, stage_index=total_stages,
                 nodes=nodes_used, operator=operator,
                 started_ts=started_ts, duration=duration,
                 knee_threads=summary.get("knee_threads"),
                 best_ok_rps=summary.get("best_ok_rps"),
                 report=html_path.name if html_path else "")
    audit("STEPPED-DONE", operator,
          f"{key} 拐點={summary.get('knee_threads')} "
          f"峰值成功吞吐={summary.get('best_ok_rps')} req/s")


@tree.command(name="stepped",
              description="對網頁控制台預先定義的目標做階梯壓測，找出容量拐點")
@app_commands.describe(
    key="目標（只能選網頁控制台已定義並啟用的目標）",
    stages="各級併發，逗號分隔且遞增，例 50,100,200,400",
    duration="每級持續秒數，預設 60",
    gap="級間間隔秒數，預設 60（讓 WAF 限流重置）",
    nodes="只使用指定節點，逗號分隔；留空為全部",
    proxy="透過代理池發壓，模擬多來源 IP（需先填 config/proxies.txt）",
)
@app_commands.autocomplete(key=target_autocomplete)
async def stepped_cmd(interaction: discord.Interaction, key: str, stages: str,
                      duration: int = 60, gap: int = 60,
                      nodes: str = "", proxy: bool = False):
    uid = interaction.user.id
    if not is_admin(uid):
        await interaction.response.send_message(
            "你沒有權限。只有 OWNER 與網站授權的管理員可以發起壓測。", ephemeral=True)
        audit("DENY-STEPPED", str(interaction.user), f"{key} {stages}")
        return

    try:
        stage_list = stp.parse_stages(stages)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    try:
        data, target = pick_target(key)
        protocol, port, path, method, raw = target_request_spec(target)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        audit("DENY-STEPPED", str(interaction.user), f"{key} 目標不可用")
        return

    limits = data.get("limits", {})
    global_t = int(limits.get("global_max_threads", 5000))
    global_d = int(limits.get("global_max_duration", 1800))
    max_threads = min(int(target.get("max_threads") or global_t), global_t)
    max_duration = min(int(target.get("max_duration") or global_d), global_d)

    peak = stage_list[-1]
    if peak > max_threads:
        await interaction.response.send_message(
            f"最高一級 {peak} 併發超過全域上限 {max_threads}。", ephemeral=True)
        return
    if not 5 <= duration <= max_duration:
        await interaction.response.send_message(
            f"每級時長須介於 5 ~ {max_duration} 秒。", ephemeral=True)
        return
    if not 0 <= gap <= 600:
        await interaction.response.send_message(
            "級間間隔須介於 0 ~ 600 秒。", ephemeral=True)
        return
    if RUNNING["active"]:
        await interaction.response.send_message(
            "已有測試在執行中，請先 `/abort`。", ephemeral=True)
        return

    node_map = load_nodes()
    try:
        only = parse_node_selection(nodes, node_map)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    http_pool: list = []
    socks_note = ""
    if proxy:
        pool, perrors = load_proxy_pool()
        http_pool, socks_pool = px.split_by_scheme(pool)
        if not pool:
            detail = "\n".join(perrors[:5]) if perrors else "檔案不存在或為空。"
            await interaction.response.send_message(
                f"代理模式需要 `config/proxies.txt`，但目前沒有可用代理。\n{detail}",
                ephemeral=True)
            return
        if not http_pool:
            await interaction.response.send_message(
                f"清單中只有 {len(socks_pool)} 個 SOCKS 代理。JMeter 的取樣器只能"
                "設定 HTTP/HTTPS 代理，無法逐執行緒輪換 SOCKS。\n"
                "請改用 HTTP/HTTPS 代理入口。", ephemeral=True)
            return
        if socks_pool:
            socks_note = (f"\n（已略過 {len(socks_pool)} 個 SOCKS 代理，"
                          f"本次只用 {len(http_pool)} 個 HTTP 代理）")

    chosen, per_node, actual = dist.allocate(peak, node_map, only=only)
    if not chosen:
        await interaction.response.send_message(
            f"節點容量不足：最高一級需要 {peak} 併發，目前有效容量 "
            f"{dist.effective_capacity(node_map, only)}。", ephemeral=True)
        return

    total_secs = len(stage_list) * duration + (len(stage_list) - 1) * gap
    mode_note = f" proxy={len(http_pool)}" if proxy else ""
    audit("STEPPED-START", str(interaction.user),
          f"{key} {protocol}://{target.get('domain', '')}:{port}{path} stages={stages} "
          f"{duration}s gap={gap}s {method}"
          f" nodes={','.join(k for k, _ in chosen)}{mode_note}")

    await interaction.response.defer()
    await interaction.followup.send(
        f"**開始階梯壓測** · `{raw}`\n"
        f"目標 `{key}`（{target.get('client') or '—'}）· {method}\n"
        f"各級 **{' → '.join(map(str, stage_list))}** 併發\n"
        f"每級 {duration}s · 間隔 {gap}s · 預計 {total_secs}s\n"
        f"節點：{'、'.join(k for k, _ in chosen)}\n"
        + (f"來源：**{len(http_pool)} 個代理輪換**{socks_note}\n" if proxy else "")
        + f"操作者 {interaction.user.mention}"
    )
    await execute_stepped(
        interaction.channel, key, target, stage_list, duration, gap,
        str(interaction.user), url=raw, only=only,
        extra={"protocol": protocol, "port": port, "path": path, "method": method},
        plan="oneshot-proxy.jmx" if proxy else (target.get("plan") or "oneshot.jmx"),
        global_extra={"proxylist": px.encode_for_jmeter(http_pool)} if proxy else None,
    )


@tree.command(name="abort", description="中止正在執行的壓測（階梯壓測會中止整場）")
async def abort(interaction: discord.Interaction):
    if not is_staff(interaction.user.id):
        await interaction.response.send_message("你沒有使用權限。", ephemeral=True)
        return
    if not RUNNING["active"]:
        await interaction.response.send_message("目前沒有執行中的測試。", ephemeral=True)
        return
    RUNNING["stop"] = True
    proc = RUNNING.get("proc")
    if proc is not None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    RUNNING.update(active=False, proc=None)
    audit("ABORT", str(interaction.user), "手動中止")
    patch_status(state="aborted", error="手動中止", updated_by=str(interaction.user))
    await interaction.response.send_message("已中止，流量停止。")


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"指令已同步到伺服器 {GUILD_ID}")
    else:
        await tree.sync()
        print("已同步全域指令（可能需數分鐘生效）")
    print(f"登入為 {client.user} · OWNER {OWNER_ID} · STAFF {len(STAFF_IDS)} 人")
    if OWNER_ID == 0:
        print("警告：未設定 OWNER_ID，所有指令都會被拒絕。")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("缺少 DISCORD_TOKEN，請建立 .env")
        sys.exit(1)
    client.run(token)
