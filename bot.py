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
import sys
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import distribute as dist
from core import verify as vfy
from core.stats import compute_stats, judge

load_dotenv(Path(__file__).parent / ".env")

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
TARGETS_FILE = CONFIG_DIR / "targets.json"
NODES_FILE = CONFIG_DIR / "nodes.json"
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

RUNNING = {"active": False, "proc": None, "started": None, "label": ""}


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


def audit(action: str, user: str, detail: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} | {action:12s} | {user} | {detail}\n"
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def is_staff(uid: int) -> bool:
    return uid == OWNER_ID or uid in STAFF_IDS


def plan_path(name: str) -> Path | None:
    candidate = (PLANS_DIR / name).resolve()
    if PLANS_DIR.resolve() not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------- 執行流程
async def execute_test(channel, key: str, target: dict, chosen: list,
                       per_node: int, actual_total: int, duration: int,
                       operator: str, override: bool) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jtl = RESULTS_DIR / f"{key}-{ts}.jtl"
    report = RESULTS_DIR / f"{key}-{ts}-report"
    cmd = dist.build_command(
        JMETER_BIN, plan_path(target["plan"]), chosen, per_node,
        duration, target["domain"], jtl, report,
    )

    RUNNING.update(active=True, started=datetime.now(), label=key)
    audit(
        "OVERRIDE" if override else "START",
        operator,
        f"{key} domain={target['domain']} total={actual_total} "
        f"per_node={per_node} nodes={len(chosen)} duration={duration}s",
    )

    node_names = ", ".join(k for k, _ in chosen)
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
        embed.set_field_at(0, name="已送回結果", value=f"{count:,}", inline=True)
        embed.set_field_at(1, name="已執行", value=f"{elapsed}s / {duration}s", inline=True)
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

    try:
        rc = await dist.run_distributed(cmd, jtl, PLANS_DIR, on_progress)
    except FileNotFoundError:
        await channel.send(f"找不到 JMeter：`{JMETER_BIN}`，請檢查 .env。")
        audit("ERROR", operator, "JMeter 不存在")
        return
    finally:
        RUNNING.update(active=False, proc=None)

    if not jtl.exists():
        await channel.send(
            "執行失敗，未產生結果檔。常見原因：節點未啟動 `jmeter-server`、"
            "RMI 1099 埠不通、或 `server.rmi.ssl.disable` 未在 master 與節點同時設定。"
        )
        audit("FAIL", operator, f"{key} 無 jtl，rc={rc}")
        return

    s = compute_stats(jtl)
    ok = judge(s)
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
@app_commands.describe(domain="客戶域名，例如 example.com", client="客戶名稱", plan="壓測計畫檔名")
async def target_add(interaction: discord.Interaction, domain: str, client: str,
                     plan: str = "baseline.jmx"):
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
        "status": "pending_verify",
        "token": token,
        "submitted_by": str(interaction.user),
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "verified_at": None,
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
    embed.set_footer(text="完成後執行 /target verify 進行驗證")
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
        save_targets(data)
        audit("TARGET-VERIFY", str(interaction.user), f"{key} 通過：{msg}")
        await interaction.followup.send(
            f"`{key}` 所有權驗證通過。\n{msg}\n\n"
            f"下一步：由 OWNER 執行 `/target approve` 設定併發上限。"
        )
    else:
        audit("TARGET-VERIFY-FAIL", str(interaction.user), f"{key} {msg}")
        await interaction.followup.send(f"`{key}` 驗證失敗。\n{msg}")


@target_group.command(name="approve", description="[OWNER] 核准目標並設定併發上限")
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
    if t["status"] != "verified":
        await interaction.response.send_message(
            f"該目標狀態為 `{t['status']}`，必須先通過所有權驗證才能核准。", ephemeral=True
        )
        return

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
    )
    save_targets(data)
    audit("TARGET-APPROVE", str(interaction.user),
          f"{key} {threads} 併發 / {duration}s 至 {until}")

    await interaction.response.send_message(
        f"`{key}`（{t['domain']}）已核准。\n"
        f"上限 **{threads} 併發 / {duration} 秒**，授權至 **{until}**。"
    )


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
        embed.add_field(
            name=f"{k} — {v['domain']} [{v['status']}]",
            value=f"客戶 {v['client']}\n"
                  f"上限 {v['max_threads']} 併發 / {v['max_duration']}s · "
                  f"授權至 {v['authorized_until'] or '—'}",
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


@tree.command(name="abort", description="中止正在執行的壓測")
async def abort(interaction: discord.Interaction):
    if not is_staff(interaction.user.id):
        await interaction.response.send_message("你沒有使用權限。", ephemeral=True)
        return
    proc = RUNNING.get("proc")
    if not RUNNING["active"] or proc is None:
        await interaction.response.send_message("目前沒有執行中的測試。", ephemeral=True)
        return
    proc.kill()
    RUNNING.update(active=False, proc=None)
    audit("ABORT", str(interaction.user), "手動中止")
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
