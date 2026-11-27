"""節點控制台 · Web 控制平面

讀取 config/nodes.json，即時探測各節點 JMeter RMI 埠判斷在線狀態，
並透過 SSH 觸發 deploy/setup-node.sh 完成一鍵部署。
支援金鑰認證與密碼認證（密碼以 WEB_TOKEN 派生金鑰加密存放）。

啟動：
    python -m uvicorn web.app:app --host 0.0.0.0 --port 8080
或：
    python web/app.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:  # 以 python -m uvicorn web.app:app 啟動時
    from web import report as report_mod
except ImportError:  # 以 python web/app.py 啟動時
    import report as report_mod

# ---------------------------------------------------------------- 路徑與設定

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
CONFIG_FILE = PROJECT_DIR / "config" / "nodes.json"
CRED_FILE = PROJECT_DIR / "config" / "credentials.json"
DEPLOY_DIR = PROJECT_DIR / "deploy"
STATIC_DIR = BASE_DIR / "static"
LOG_DIR = PROJECT_DIR / "results" / "deploys"
AUDIT_FILE = PROJECT_DIR / "audit.log"

SSH_USER = os.getenv("SSH_USER", "root")
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=8",
]
PROBE_INTERVAL = 5.0        # 秒，RMI 埠探測間隔
SSH_PROBE_INTERVAL = 20.0   # 秒，SSH 埠探測間隔
HISTORY_LEN = 40            # 延遲走勢保留點數


def _load_dotenv() -> None:
    """輕量讀取 .env，避免額外依賴。"""
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def load_token() -> str:
    token = os.getenv("WEB_TOKEN", "").strip()
    if token:
        return token
    token_file = PROJECT_DIR / ".web_token"
    if token_file.exists():
        saved = token_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    token = uuid.uuid4().hex[:24]
    try:
        token_file.write_text(token, encoding="utf-8")
        token_file.chmod(0o600)
    except Exception:
        pass
    print(f"[web] 未設定 WEB_TOKEN，已生成臨時權杖：{token}")
    print(f"[web] 建議寫入 .env：WEB_TOKEN={token}")
    return token


WEB_TOKEN = load_token()


def session_value() -> str:
    return hmac.new(WEB_TOKEN.encode(), b"loadtest-web", hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    cookie = request.cookies.get("lw_session", "")
    return bool(cookie) and hmac.compare_digest(cookie, session_value())


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="未登入")


def audit(action: str, who: str, detail: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} WEB {action:<8} {who} {detail}\n"
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------- 設定讀寫

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"nodes": {}}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_config(data: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(CONFIG_FILE)


# ---------------------------------------------------------------- 節點憑證
# 密碼以 WEB_TOKEN 派生金鑰加密存放：拿到檔案但沒有權杖無法解開。
# 換掉 WEB_TOKEN 會使既有密碼失效，需重新輸入一次。

def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(WEB_TOKEN.encode()).digest())
    return Fernet(key)


def load_creds() -> dict:
    if not CRED_FILE.exists():
        return {}
    try:
        return json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_creds(data: dict) -> None:
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CRED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CRED_FILE)
    try:
        CRED_FILE.chmod(0o600)
    except Exception:
        pass


def store_cred(key: str, user: str, password: str) -> None:
    data = load_creds()
    data[key] = {
        "user": user,
        "password": _fernet().encrypt(password.encode()).decode(),
    }
    save_creds(data)


def update_cred_user(key: str, user: str) -> None:
    data = load_creds()
    if key in data:
        data[key]["user"] = user
        save_creds(data)


def drop_cred(key: str) -> None:
    data = load_creds()
    if data.pop(key, None) is not None:
        save_creds(data)


def get_cred(key: str) -> Optional[dict]:
    item = load_creds().get(key)
    if not item:
        return None
    try:
        return {
            "user": item.get("user", "root"),
            "password": _fernet().decrypt(item["password"].encode()).decode(),
        }
    except Exception:
        return None


# ---------------------------------------------------------------- Discord 設定

ENV_FILE = PROJECT_DIR / ".env"
ADMINS_FILE = PROJECT_DIR / "config" / "admins.json"
BOT_UNIT = "loadtest-bot"
BOT_LOG = PROJECT_DIR / "logs" / "bot.log"


def read_admins() -> list[str]:
    try:
        data = json.loads(ADMINS_FILE.read_text(encoding="utf-8"))
        return [str(x).strip() for x in data.get("admins", []) if str(x).strip().isdigit()]
    except Exception:
        return []


def write_admins(ids: list[str]) -> None:
    ADMINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_comment": "Discord 使用者 ID，可用 /start 與其他指令；OWNER 不需列在這裡",
               "admins": ids}
    tmp = ADMINS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ADMINS_FILE)
    try:
        ADMINS_FILE.chmod(0o640)
    except Exception:
        pass

SLASH_COMMANDS = [
    ("/start url port threads https-get", "一句話壓測：網址 埠 併發 協定-方法（OWNER 與授權管理員）"),
    ("/stepped url 50,100,200,400", "階梯壓測：逐級加壓找出容量拐點（OWNER 與授權管理員）"),
    ("/target add", "提交壓測目標，產生所有權驗證 token"),
    ("/target verify", "驗證目標的域名所有權"),
    ("/target approve", "OWNER 核准目標並設定併發上限"),
    ("/target suspend", "OWNER 立即暫停目標"),
    ("/target list", "列出所有目標與授權狀態"),
    ("/loadtest", "對已核准目標發起分散式壓測"),
    ("/nodes", "檢查各節點連線狀態"),
    ("/abort", "中止正在執行的壓測"),
]


def read_env() -> dict:
    data: dict[str, str] = {}
    if not ENV_FILE.exists():
        return data
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        data[key.strip()] = value.strip()
    return data


def write_env(updates: dict) -> None:
    """只更新指定鍵，保留註解與原有順序。"""
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = set()
    output = []
    for line in lines:
        text = line.strip()
        if text and not text.startswith("#") and "=" in text:
            key = text.partition("=")[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except Exception:
        pass


def mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "已設定"
    return f"{value[:6]}…{value[-4:]}"


def systemctl_bot(action: str) -> tuple[int, str]:
    if action in ("start", "stop", "restart"):
        cmd = ["sudo", "-n", "systemctl", action, BOT_UNIT]
    else:
        cmd = ["systemctl", action, BOT_UNIT]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except FileNotFoundError:
        return 127, "systemctl 不存在"
    except Exception as exc:
        return 1, str(exc)
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    return proc.returncode, text


def tail_log(path: Path, lines: int = 25) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            data = handle.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception:
        return ""


def bot_status() -> dict:
    env = read_env()
    token = env.get("DISCORD_TOKEN", "").strip()
    _rc, state = systemctl_bot("is-active")
    service = (state.splitlines()[0].strip() if state else "") or "unknown"
    if service not in ("active", "inactive", "failed", "activating", "deactivating", "unknown"):
        service = "unknown"
    return {
        "service": service,
        "configured": bool(token),
        "token_hint": mask_secret(token),
        "owner_id": env.get("OWNER_ID", ""),
        "staff_ids": env.get("STAFF_IDS", ""),
        "guild_id": env.get("GUILD_ID", ""),
        "admins": read_admins(),
        "log": tail_log(BOT_LOG, 25),
        "commands": [{"cmd": c, "desc": d} for c, d in SLASH_COMMANDS],
    }


def _discord_me(token: str) -> dict:
    """直接向 Discord 驗證 Token，回傳 Bot 身分或明確錯誤。"""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (loadtest-console, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
            return {
                "ok": True,
                "username": data.get("username", ""),
                "id": data.get("id", ""),
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "error": "Token 無效或已被重置（401）"}
        if exc.code == 429:
            return {"ok": False, "error": "請求過於頻繁，請稍後再試（429）"}
        return {"ok": False, "error": f"Discord 回應 {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"無法連線 Discord：{exc}"}


def append_bot_log_marker(action: str) -> None:
    """在日誌插入分隔線，避免舊的錯誤輸出被誤認為本次執行。"""
    try:
        BOT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(BOT_LOG, "a", encoding="utf-8") as handle:
            handle.write(
                f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {action} =====\n"
            )
    except Exception:
        pass


# ---------------------------------------------------------------- 壓測狀態
# bot.py 執行壓測時會把狀態寫進 results/loadtest-status.json

LOADTEST_STATUS = PROJECT_DIR / "results" / "loadtest-status.json"


def loadtest_status() -> dict:
    if not LOADTEST_STATUS.exists():
        return {"state": "idle"}
    try:
        data = json.loads(LOADTEST_STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "idle"}

    now = time.time()
    started = float(data.get("started_ts") or 0)
    duration = int(data.get("duration") or 0)
    elapsed = int(now - started) if started else int(data.get("elapsed") or 0)
    state = data.get("state") or "idle"

    # 機器人掛掉時檔案會停在 running，超過時長太多就視為狀態不明
    if state == "running" and duration and elapsed > duration + 120:
        state = "stale"

    data["state"] = state
    data["elapsed"] = max(0, elapsed)
    data["duration"] = duration
    data["remaining"] = max(0, duration - elapsed) if duration else 0
    data["progress"] = min(100, round(elapsed / duration * 100)) if duration else 0
    data["age"] = int(now - started) if started else None
    return data


def _run_text(cmd: list[str], timeout: float = 10) -> str:
    """執行指令並以容錯方式解碼（Windows 中文主機常回 GBK）。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return ""
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    for encoding in ("utf-8", "cp950", "cp936", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def detect_master_ip() -> str:
    env_ip = os.getenv("MASTER_IP", "").strip()
    if env_ip:
        return env_ip
    if os.name != "nt":
        parts = _run_text(["hostname", "-I"], timeout=5).split()
        if parts:
            return parts[0]
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


MASTER_IP = detect_master_ip()
_JMETER_CACHE: dict[str, str] = {}


def jmeter_version() -> str:
    if "v" in _JMETER_CACHE:
        return _JMETER_CACHE["v"]
    binary = os.getenv("JMETER_BIN", "jmeter")
    ver = "未安裝"
    text = _run_text([binary, "-v"], timeout=25)
    match = re.search(r"(\d+\.\d+\.\d+)", text)
    if match:
        ver = match.group(1)
    _JMETER_CACHE["v"] = ver
    return ver


# ---------------------------------------------------------------- 節點狀態

class NodeState:
    __slots__ = ("online", "latency", "ssh_ok", "checked", "history", "error")

    def __init__(self) -> None:
        self.online = False
        self.latency: Optional[float] = None
        self.ssh_ok = False
        self.checked = 0.0
        self.history: deque = deque(maxlen=HISTORY_LEN)
        self.error: str = ""


STATE: dict[str, NodeState] = {}


def state_of(key: str) -> NodeState:
    if key not in STATE:
        STATE[key] = NodeState()
    return STATE[key]


async def tcp_probe(host: str, port: int, timeout: float = 3.0) -> tuple[bool, float]:
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        elapsed = (time.perf_counter() - started) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, round(elapsed, 1)
    except Exception:
        return False, 0.0


async def probe_node(key: str, cfg: dict) -> None:
    st = state_of(key)
    ok, latency = await tcp_probe(cfg["host"], int(cfg.get("port", 1099)))
    st.online = ok
    st.latency = latency if ok else None
    st.checked = time.time()
    st.history.append(latency if ok else None)
    if not ok:
        st.error = "RMI 埠無回應"


async def probe_ssh(key: str, cfg: dict) -> None:
    ok, _ = await tcp_probe(cfg["host"], int(cfg.get("ssh_port", 22)), timeout=4.0)
    state_of(key).ssh_ok = ok


async def monitor_loop() -> None:
    tick = 0
    while True:
        try:
            nodes = load_config().get("nodes", {})
            for key in list(STATE):
                if key not in nodes:
                    STATE.pop(key, None)
            tasks = [
                asyncio.create_task(probe_node(key, cfg))
                for key, cfg in nodes.items()
            ]
            if tick % max(1, int(SSH_PROBE_INTERVAL / PROBE_INTERVAL)) == 0:
                tasks += [
                    asyncio.create_task(probe_ssh(key, cfg))
                    for key, cfg in nodes.items()
                ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            tick += 1
        except Exception as exc:  # 監控迴圈不能因單次錯誤退出
            print(f"[monitor] {exc}")
        await asyncio.sleep(PROBE_INTERVAL)


# ---------------------------------------------------------------- 部署任務

class Job:
    def __init__(self, kind: str, title: str, keys: list[str]) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.title = title
        self.keys = keys
        self.status = "running"          # running | success | failed | cancelled
        self.started = time.time()
        self.ended: Optional[float] = None
        self.lines: deque = deque(maxlen=4000)
        self.subs: set[asyncio.Queue] = set()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.cancel_flag = False
        self.nodes = {k: "等待中" for k in keys}

    def log(self, text: str, level: str = "info") -> None:
        entry = {
            "t": time.strftime("%H:%M:%S"),
            "level": level,
            "text": text,
        }
        self.lines.append(entry)
        for queue in list(self.subs):
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def set_node(self, key: str, status: str) -> None:
        self.nodes[key] = status

    def finish(self, status: str) -> None:
        self.status = status
        self.ended = time.time()
        for queue in list(self.subs):
            try:
                queue.put_nowait({"end": True})
            except asyncio.QueueFull:
                pass

    def summary(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "nodes": self.nodes,
        }


JOBS: dict[str, Job] = {}
CURRENT: dict[str, Optional[Job]] = {"job": None, "last": None}


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text: str) -> str:
    """移除 shell 腳本輸出的 ANSI 色碼。"""
    return ANSI_RE.sub("", text)


def _classify(line: str) -> str:
    low = line.lower()
    if "[fail]" in low or "error" in low or "denied" in low:
        return "err"
    if "[warn]" in low:
        return "warn"
    if "[ ok ]" in low or "完成" in line:
        return "ok"
    return "info"


async def stream_cmd(job: Job, cmd: list[str], timeout: float = 1800) -> int:
    job.log("$ " + " ".join(cmd), "cmd")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        job.log(f"找不到指令：{cmd[0]}，請確認已安裝", "err")
        return 127
    except Exception as exc:
        job.log(f"啟動失敗：{exc}", "err")
        return 1

    job.proc = proc
    deadline = time.time() + timeout
    try:
        assert proc.stdout is not None
        while True:
            if job.cancel_flag:
                job.log("使用者中止，終止子程序", "warn")
                proc.kill()
                break
            if time.time() > deadline:
                job.log("指令逾時，已強制終止", "err")
                proc.kill()
                break
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), 1.0)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                job.log(_clean(line), _classify(line))
        return await proc.wait()
    finally:
        job.proc = None


def _kill_remote(host: str, port: int, user: str, password: str) -> None:
    """中止部署時清掉遠端殘留腳本與下載，否則它會在背景繼續跑。"""
    try:
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, port=port, username=user, password=password,
                       timeout=10, allow_agent=False, look_for_keys=False)
        client.exec_command(
            "pkill -f setup-node.sh 2>/dev/null; "
            "pkill -f 'apache-jmeter-5.6.3.tgz' 2>/dev/null; true",
            timeout=10,
        )
        time.sleep(1)
        client.close()
    except Exception:
        pass


def _deploy_blocking(job: Job, emit, host: str, ssh_port: int, user: str,
                     password: str, master_ip: str, name: str) -> bool:
    """以密碼認證部署單一節點（阻塞，需在執行緒中呼叫）。"""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        emit(f"$ ssh {user}@{host}:{ssh_port}（密碼認證）", "cmd")
        client.connect(
            host, port=ssh_port, username=user, password=password,
            timeout=20, banner_timeout=20, auth_timeout=20,
            allow_agent=False, look_for_keys=False,
        )
        emit("SSH 認證成功", "ok")

        sftp = client.open_sftp()
        sftp.put(str(DEPLOY_DIR / "setup-node.sh"), "/tmp/setup-node.sh")
        sftp.close()
        emit("已上傳 setup-node.sh", "ok")

        prefix = "bash" if user == "root" else "sudo -S -p '' bash"
        remote = (
            f"chmod +x /tmp/setup-node.sh && "
            f"{prefix} /tmp/setup-node.sh {master_ip} {host} {name} 2>&1"
        )
        emit(f"$ {remote}", "cmd")
        stdin, stdout, stderr = client.exec_command(remote, timeout=1800, get_pty=False)
        if user != "root":
            stdin.write(password + "\n")
            stdin.flush()

        channel = stdout.channel
        channel.settimeout(5.0)
        buffer = b""
        idle = 0
        while True:
            if job.cancel_flag:
                emit("使用者中止，終止遠端程序", "warn")
                _kill_remote(host, ssh_port, user, password)
                return False
            try:
                chunk = channel.recv(4096)
            except socket.timeout:
                idle += 5
                if idle % 30 == 0:
                    emit(f"… 仍在執行（已 {idle} 秒無新輸出，下載或編譯可能較久）", "info")
                continue
            except Exception:
                break
            if not chunk:
                break
            idle = 0
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if line.strip():
                    emit(_clean(line), _classify(line))

        if buffer.strip():
            emit(_clean(buffer.decode("utf-8", "replace")), "info")

        rc = channel.recv_exit_status()
        if rc != 0:
            err = stderr.read().decode("utf-8", "replace").strip()
            if err:
                emit(err, "err")
            emit(f"遠端腳本結束碼 {rc}", "err")
            return False
        return True
    except paramiko.AuthenticationException:
        emit("SSH 認證失敗：帳號或密碼錯誤", "err")
        return False
    except Exception as exc:
        emit(f"部署失敗：{exc}", "err")
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


async def deploy_one(job: Job, key: str, cfg: dict, master_ip: str) -> bool:
    host = cfg["host"]
    target = f"{SSH_USER}@{host}"
    job.log(f"── 節點 {key} ({host}) ──", "info")
    job.set_node(key, "連線檢查中")

    cred = get_cred(key)
    if cred:
        job.set_node(key, "部署中（密碼）")
        loop = asyncio.get_running_loop()

        def emit(text: str, level: str = "info") -> None:
            loop.call_soon_threadsafe(job.log, text, level)

        ok = await asyncio.to_thread(
            _deploy_blocking, job, emit, host, int(cfg.get("ssh_port", 22)),
            cred["user"], cred["password"], master_ip, key,
        )
        if job.cancel_flag:
            job.set_node(key, "已中止")
            return False
        job.set_node(key, "完成" if ok else "失敗")
        if ok:
            job.log(f"節點 {key} 部署完成", "ok")
        return ok

    if not shutil.which("ssh"):
        job.log("本機找不到 ssh 指令，無法遠端部署", "err")
        job.set_node(key, "失敗：無 ssh")
        return False

    rc = await stream_cmd(job, ["ssh", *SSH_OPTS, target, "true"], timeout=30)
    if rc != 0:
        job.log("SSH 連線失敗：請確認金鑰免密登入、節點 sshd 與防火牆", "err")
        job.set_node(key, "失敗：SSH 不通")
        return False

    job.set_node(key, "傳送腳本")
    rc = await stream_cmd(
        job,
        ["scp", *SSH_OPTS, "-q",
         str(DEPLOY_DIR / "setup-node.sh"), f"{target}:/tmp/setup-node.sh"],
        timeout=120,
    )
    if rc != 0:
        job.log("腳本傳送失敗", "err")
        job.set_node(key, "失敗：scp")
        return False

    job.set_node(key, "安裝中")
    remote = (
        f"chmod +x /tmp/setup-node.sh && "
        f"sudo -n bash /tmp/setup-node.sh {master_ip} {host} {key}"
    )
    rc = await stream_cmd(job, ["ssh", *SSH_OPTS, target, remote], timeout=1200)
    if job.cancel_flag:
        job.set_node(key, "已中止")
        return False
    if rc != 0:
        job.log(
            "部署失敗。若訊息含 'sudo: a password is required'，"
            "請在節點設定 NOPASSWD 或改用 root 帳號",
            "err",
        )
        job.set_node(key, "失敗")
        return False

    job.log(f"節點 {key} 部署完成", "ok")
    job.set_node(key, "完成")
    return True


async def run_deploy(job: Job, keys: list[str], master_ip: str) -> None:
    ok = fail = 0
    try:
        for key in keys:
            if job.cancel_flag:
                break
            cfg = load_config().get("nodes", {}).get(key)
            if not cfg:
                job.log(f"節點 {key} 已不在設定檔中，略過", "warn")
                job.set_node(key, "略過")
                continue
            if await deploy_one(job, key, cfg, master_ip):
                ok += 1
            else:
                fail += 1

        job.log("", "info")
        if job.cancel_flag:
            job.log(f"任務已中止（成功 {ok} · 失敗 {fail}）", "warn")
            job.finish("cancelled")
        elif fail == 0:
            job.log(f"全部完成：成功 {ok} 台", "ok")
            job.finish("success")
        else:
            job.log(f"完成：成功 {ok} 台 · 失敗 {fail} 台", "err")
            job.finish("failed")
    except Exception as exc:
        job.log(f"任務異常：{exc}", "err")
        job.finish("failed")
    finally:
        CURRENT["job"] = None
        CURRENT["last"] = job
        # 部署完立刻重探一次，讓畫面狀態即時反映
        nodes = load_config().get("nodes", {})
        await asyncio.gather(
            *(probe_node(k, c) for k, c in nodes.items()), return_exceptions=True
        )


# ---------------------------------------------------------------- 生命週期

@asynccontextmanager
async def lifespan(app: FastAPI):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(monitor_loop())
    print(f"[web] 主服務器 IP {MASTER_IP} · 監控 {PROBE_INTERVAL}s 間隔")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="節點控制台", lifespan=lifespan)


# ---------------------------------------------------------------- API

class LoginBody(BaseModel):
    token: str


class DeployBody(BaseModel):
    scope: str = "all"           # all | node
    key: Optional[str] = None
    include_disabled: bool = False


class ToggleBody(BaseModel):
    enabled: bool


class NodeBody(BaseModel):
    key: str
    host: str
    port: int = 1099
    ssh_port: int = 22
    region: str = "—"
    max_threads: int = 600
    enabled: bool = True
    note: str = ""
    ssh_user: str = "root"
    ssh_password: Optional[str] = None


class SshTestBody(BaseModel):
    host: str
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str = ""
    key: Optional[str] = None


class ProbeBody(BaseModel):
    host: str
    port: int = 1099
    ssh_port: int = 22


KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")


def validate_node(body: NodeBody) -> dict:
    key = body.key.strip()
    if not KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="名稱只能使用英數字、底線、連字號、點，需以英數字開頭，最長 32 字元",
        )
    host = body.host.strip()
    if not HOST_RE.match(host):
        raise HTTPException(
            status_code=400,
            detail="主機格式不正確，只填 IP 或域名，不要含 http:// 或路徑",
        )
    if not 1 <= body.port <= 65535:
        raise HTTPException(status_code=400, detail="RMI 端口需在 1 ~ 65535 之間")
    if not 1 <= body.ssh_port <= 65535:
        raise HTTPException(status_code=400, detail="SSH 端口需在 1 ~ 65535 之間")
    if not 1 <= body.max_threads <= 10000:
        raise HTTPException(status_code=400, detail="執行緒上限需在 1 ~ 10000 之間")
    ssh_user = body.ssh_user.strip() or "root"
    if not USER_RE.match(ssh_user):
        raise HTTPException(status_code=400, detail="使用者名稱格式不正確")
    password = (body.ssh_password or "").strip()
    if password and len(password) > 256:
        raise HTTPException(status_code=400, detail="密碼過長")
    return {
        "key": key,
        "host": host,
        "port": body.port,
        "ssh_port": body.ssh_port,
        "region": (body.region.strip() or "—")[:16],
        "max_threads": body.max_threads,
        "enabled": body.enabled,
        "note": body.note.strip()[:200],
        "ssh_user": ssh_user,
        "ssh_password": password or None,
    }


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    if not hmac.compare_digest(body.token.strip(), WEB_TOKEN):
        audit("LOGIN-FAIL", request.client.host if request.client else "-", "")
        raise HTTPException(status_code=401, detail="權杖不正確")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "lw_session",
        session_value(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,
        path="/",
    )
    audit("LOGIN", request.client.host if request.client else "-", "")
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("lw_session", path="/")
    return response


@app.get("/api/state")
async def get_state(_=Depends(require_auth)):
    config = load_config()
    nodes_cfg = config.get("nodes", {})
    creds = load_creds()
    now = time.time()
    items = []
    online = 0
    enabled_capacity = []

    for key, cfg in nodes_cfg.items():
        st = state_of(key)
        if st.online:
            online += 1
        if cfg.get("enabled", True):
            enabled_capacity.append(int(cfg.get("max_threads", 0)))
        items.append({
            "key": key,
            "host": cfg.get("host", ""),
            "port": int(cfg.get("port", 1099)),
            "ssh_port": int(cfg.get("ssh_port", 22)),
            "ssh_user": cfg.get("ssh_user", SSH_USER),
            "has_password": key in creds,
            "region": cfg.get("region", "—"),
            "max_threads": int(cfg.get("max_threads", 0)),
            "enabled": bool(cfg.get("enabled", True)),
            "note": cfg.get("note", ""),
            "online": st.online,
            "latency_ms": st.latency,
            "ssh_ok": st.ssh_ok,
            "checked_ago": round(now - st.checked, 1) if st.checked else None,
            "history": [h for h in st.history],
        })

    # 有效容量 = min(啟用節點上限) × 啟用節點數（JMeter 每節點跑相同執行緒數）
    capacity = min(enabled_capacity) * len(enabled_capacity) if enabled_capacity else 0
    job = CURRENT["job"] or CURRENT["last"]
    version = await asyncio.to_thread(jmeter_version)

    return {
        "server": {
            "hostname": socket.gethostname(),
            "master_ip": MASTER_IP,
            "jmeter": version,
            "ssh": bool(shutil.which("ssh")),
            "time": datetime.now().isoformat(timespec="seconds"),
        },
        "summary": {
            "total": len(items),
            "enabled": len(enabled_capacity),
            "online": online,
            "offline": [i["key"] for i in items if not i["online"]],
            "capacity": capacity,
        },
        "nodes": items,
        "job": job.summary() if job else None,
        "bot": await asyncio.to_thread(bot_status),
        "loadtest": await asyncio.to_thread(loadtest_status),
    }


@app.post("/api/nodes/{key}/enabled")
async def toggle_node(key: str, body: ToggleBody, request: Request, _=Depends(require_auth)):
    config = load_config()
    nodes = config.get("nodes", {})
    if key not in nodes:
        raise HTTPException(status_code=404, detail="節點不存在")
    nodes[key]["enabled"] = body.enabled
    save_config(config)
    audit("TOGGLE", request.client.host if request.client else "-", f"{key}={body.enabled}")
    return {"ok": True, "key": key, "enabled": body.enabled}


def _node_entry(data: dict) -> dict:
    return {
        "host": data["host"],
        "port": data["port"],
        "ssh_port": data["ssh_port"],
        "region": data["region"],
        "max_threads": data["max_threads"],
        "enabled": data["enabled"],
        "note": data["note"],
        "ssh_user": data["ssh_user"],
    }


@app.post("/api/nodes")
async def create_node(body: NodeBody, request: Request, _=Depends(require_auth)):
    data = validate_node(body)
    config = load_config()
    nodes = config.setdefault("nodes", {})
    if data["key"] in nodes:
        raise HTTPException(status_code=409, detail=f"節點名稱 {data['key']} 已存在")
    nodes[data["key"]] = _node_entry(data)
    save_config(config)
    if data["ssh_password"]:
        store_cred(data["key"], data["ssh_user"], data["ssh_password"])
    audit("NODE-ADD", request.client.host if request.client else "-",
          f"{data['key']} {data['host']}:{data['port']} user={data['ssh_user']} "
          f"auth={'password' if data['ssh_password'] else 'key'}")
    asyncio.create_task(probe_node(data["key"], nodes[data["key"]]))
    return {"ok": True, "key": data["key"], "nodes": len(nodes)}


@app.put("/api/nodes/{key}")
async def update_node(key: str, body: NodeBody, request: Request, _=Depends(require_auth)):
    data = validate_node(body)
    config = load_config()
    nodes = config.get("nodes", {})
    if key not in nodes:
        raise HTTPException(status_code=404, detail="節點不存在")
    new_key = data["key"]
    if new_key != key and new_key in nodes:
        raise HTTPException(status_code=409, detail=f"節點名稱 {new_key} 已存在")

    if new_key == key:
        nodes[key] = _node_entry(data)
    else:
        rebuilt = {}
        for k, v in nodes.items():
            rebuilt[new_key if k == key else k] = _node_entry(data) if k == key else v
        config["nodes"] = rebuilt
        STATE.pop(key, None)
        if key in load_creds():
            creds = load_creds()
            creds[new_key] = creds.pop(key)
            save_creds(creds)
    save_config(config)

    if data["ssh_password"]:
        store_cred(new_key, data["ssh_user"], data["ssh_password"])
    else:
        update_cred_user(new_key, data["ssh_user"])

    audit("NODE-EDIT", request.client.host if request.client else "-",
          f"{key}->{new_key} {data['host']}:{data['port']} user={data['ssh_user']} "
          f"threads={data['max_threads']}")
    asyncio.create_task(probe_node(new_key, config["nodes"][new_key]))
    return {"ok": True, "key": new_key, "renamed": new_key != key}


@app.delete("/api/nodes/{key}")
async def delete_node(key: str, request: Request, _=Depends(require_auth)):
    config = load_config()
    nodes = config.get("nodes", {})
    if key not in nodes:
        raise HTTPException(status_code=404, detail="節點不存在")
    removed = nodes.pop(key)
    save_config(config)
    STATE.pop(key, None)
    drop_cred(key)
    audit("NODE-DEL", request.client.host if request.client else "-",
          f"{key} {removed.get('host', '')}")
    return {"ok": True, "key": key, "nodes": len(nodes)}


@app.post("/api/probe")
async def probe_target(body: ProbeBody, _=Depends(require_auth)):
    host = body.host.strip()
    if not HOST_RE.match(host):
        raise HTTPException(status_code=400, detail="主機格式不正確")
    if not 1 <= body.port <= 65535 or not 1 <= body.ssh_port <= 65535:
        raise HTTPException(status_code=400, detail="端口需在 1 ~ 65535 之間")
    rmi_ok, rmi_ms = await tcp_probe(host, body.port, timeout=4.0)
    ssh_ok, _ms = await tcp_probe(host, body.ssh_port, timeout=4.0)
    return {"rmi": rmi_ok, "rmi_ms": rmi_ms if rmi_ok else None, "ssh": ssh_ok}


def _ssh_probe_blocking(host: str, port: int, user: str, password: str) -> dict:
    import paramiko

    info = {
        "ok": False, "user": user, "sudo": None, "service": "unknown",
        "os": "", "cpu": None, "mem_mb": None, "error": "",
    }
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host, port=port, username=user, password=password,
            timeout=12, banner_timeout=12, auth_timeout=12,
            allow_agent=False, look_for_keys=False,
        )
    except paramiko.AuthenticationException:
        info["error"] = "帳號或密碼錯誤"
        return info
    except paramiko.SSHException as exc:
        info["error"] = f"SSH 協商失敗：{exc}"
        return info
    except Exception as exc:
        info["error"] = f"無法連線：{exc}"
        return info

    info["ok"] = True
    try:
        script = (
            "echo @@USER; id -un; "
            "echo @@OS; (. /etc/os-release 2>/dev/null && echo $PRETTY_NAME) || echo unknown; "
            "echo @@CPU; nproc; "
            "echo @@MEM; free -m | awk '/^Mem:/{print $2}'; "
            "echo @@SVC; systemctl is-active jmeter-server 2>/dev/null || echo none"
        )
        _, out, _ = client.exec_command(script, timeout=15)
        section = ""
        for line in out.read().decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("@@"):
                section = line[2:]
                continue
            if section == "OS":
                info["os"] = line
            elif section == "CPU":
                info["cpu"] = int(line) if line.isdigit() else None
            elif section == "MEM":
                info["mem_mb"] = int(line) if line.isdigit() else None
            elif section == "SVC":
                info["service"] = line

        _, out2, _ = client.exec_command(
            "sudo -n true 2>/dev/null && echo yes || echo no", timeout=10
        )
        sudo_ok = out2.read().decode("utf-8", "replace").strip() == "yes"
        if not sudo_ok and user != "root":
            stdin, out3, _ = client.exec_command(
                "sudo -S -p '' true >/dev/null 2>&1; echo RC=$?", timeout=15
            )
            stdin.write(password + "\n")
            stdin.flush()
            sudo_ok = "RC=0" in out3.read().decode("utf-8", "replace")
        info["sudo"] = sudo_ok
    except Exception as exc:
        info["error"] = f"讀取系統資訊失敗：{exc}"
    finally:
        client.close()
    return info


@app.post("/api/ssh-test")
async def ssh_test(body: SshTestBody, _=Depends(require_auth)):
    host = body.host.strip()
    if not HOST_RE.match(host):
        raise HTTPException(status_code=400, detail="主機格式不正確")
    if not 1 <= body.ssh_port <= 65535:
        raise HTTPException(status_code=400, detail="SSH 端口需在 1 ~ 65535 之間")
    user = (body.ssh_user or "root").strip()
    if not USER_RE.match(user):
        raise HTTPException(status_code=400, detail="使用者名稱格式不正確")

    password = body.ssh_password or ""
    if not password and body.key:
        cred = get_cred(body.key)
        if cred:
            password = cred["password"]
            user = cred["user"]
    if not password:
        raise HTTPException(status_code=400, detail="請輸入伺服器密碼")

    return await asyncio.to_thread(_ssh_probe_blocking, host, body.ssh_port, user, password)


class BotConfigBody(BaseModel):
    token: Optional[str] = None
    owner_id: str = ""
    staff_ids: str = ""
    guild_id: str = ""
    admin_ids: Optional[str] = None


class BotActionBody(BaseModel):
    action: str


@app.get("/api/bot/status")
async def get_bot_status(_=Depends(require_auth)):
    return await asyncio.to_thread(bot_status)


@app.post("/api/bot/config")
async def save_bot_config(body: BotConfigBody, request: Request, _=Depends(require_auth)):
    owner = body.owner_id.strip()
    guild = body.guild_id.strip()
    if owner and not owner.isdigit():
        raise HTTPException(status_code=400, detail="Owner ID 必須是數字")
    if guild and not guild.isdigit():
        raise HTTPException(status_code=400, detail="Guild ID 必須是數字")

    staff = body.staff_ids.replace(",", " ").split()
    for item in staff:
        if not item.isdigit():
            raise HTTPException(status_code=400, detail="Staff IDs 只能是數字，以逗號分隔")

    updates = {
        "OWNER_ID": owner,
        "STAFF_IDS": ",".join(staff),
        "GUILD_ID": guild,
    }
    token = (body.token or "").strip()
    if token:
        if len(token) < 50:
            raise HTTPException(status_code=400, detail="Token 長度不正確，請確認複製完整")
        updates["DISCORD_TOKEN"] = token

    admins = None
    if body.admin_ids is not None:
        admins = []
        for item in body.admin_ids.replace(",", " ").split():
            if not item.isdigit():
                raise HTTPException(status_code=400, detail="管理員 ID 只能是數字，以逗號分隔")
            admins.append(item)

    before = await asyncio.to_thread(read_env)
    changed = any(before.get(key) != value for key, value in updates.items())

    await asyncio.to_thread(write_env, updates)
    if admins is not None:
        await asyncio.to_thread(write_admins, admins)

    audit("BOT-CONFIG", request.client.host if request.client else "-",
          f"token={'updated' if token else 'kept'} owner={owner} staff={len(staff)} "
          f"admins={len(admins) if admins is not None else '-'}")

    status = await asyncio.to_thread(bot_status)
    restarted = False
    # 管理員名單由 bot 每次執行時讀取，改完不必重啟；只有 .env 真的變了才重啟
    if changed and status["service"] == "active":
        await asyncio.to_thread(systemctl_bot, "restart")
        await asyncio.sleep(2.5)
        status = await asyncio.to_thread(bot_status)
        restarted = True
    return {"ok": True, "restarted": restarted, "bot": status}


class BotTokenBody(BaseModel):
    token: Optional[str] = None


@app.post("/api/bot/test-token")
async def test_bot_token(body: BotTokenBody, _=Depends(require_auth)):
    token = (body.token or "").strip()
    if not token:
        token = (await asyncio.to_thread(read_env)).get("DISCORD_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="尚未填寫 Token")
    return await asyncio.to_thread(_discord_me, token)


@app.post("/api/bot/clear-log")
async def clear_bot_log(_=Depends(require_auth)):
    try:
        BOT_LOG.parent.mkdir(parents=True, exist_ok=True)
        BOT_LOG.write_text("", encoding="utf-8")
    except PermissionError:
        raise HTTPException(status_code=500, detail="沒有寫入日誌的權限")
    return {"ok": True}


@app.post("/api/bot/action")
async def bot_action(body: BotActionBody, request: Request, _=Depends(require_auth)):
    action = body.action
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="action 只能是 start / stop / restart")

    env = await asyncio.to_thread(read_env)
    if action in ("start", "restart"):
        if not env.get("DISCORD_TOKEN", "").strip():
            raise HTTPException(status_code=400, detail="尚未填寫 Bot Token")
        if not env.get("OWNER_ID", "").strip():
            raise HTTPException(status_code=400, detail="尚未填寫 Owner ID")
        await asyncio.to_thread(append_bot_log_marker, action)

    rc, out = await asyncio.to_thread(systemctl_bot, action)
    if rc != 0:
        audit("BOT-FAIL", request.client.host if request.client else "-", f"{action} {out[:120]}")
        hint = out or "未知錯誤"
        if "password is required" in out or "not allowed" in out:
            hint = "沒有啟停服務的權限（缺少 sudoers 規則），請重跑 setup-master.sh"
        raise HTTPException(status_code=500, detail=f"systemctl {action} 失敗：{hint}")

    await asyncio.sleep(2.5)
    status = await asyncio.to_thread(bot_status)
    audit("BOT-" + action.upper(), request.client.host if request.client else "-", status["service"])
    return {"ok": True, "action": action, "bot": status}


@app.get("/api/loadtest")
async def get_loadtest(_=Depends(require_auth)):
    return await asyncio.to_thread(loadtest_status)


# ---------------------------------------------------------------- 報告下載

RESULTS_DIR = PROJECT_DIR / "results"


def _result_path(stem: str) -> Path:
    if not stem or "/" in stem or "\\" in stem or stem.startswith("."):
        raise HTTPException(status_code=400, detail="結果名稱不合法")
    path = RESULTS_DIR / f"{stem}.jtl"
    if not path.exists():
        raise HTTPException(status_code=404, detail="找不到該次結果")
    return path


@app.get("/api/results")
async def list_results_api(_=Depends(require_auth)):
    items = await asyncio.to_thread(report_mod.list_results, RESULTS_DIR, 30)
    return {"items": items}


@app.get("/api/results/{stem}/report")
async def download_report(stem: str, format: str = "html", _=Depends(require_auth)):
    path = _result_path(stem)
    if format == "md":
        body = await asyncio.to_thread(report_mod.render_markdown, path)
        return Response(
            content=body,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}.md"'},
        )
    body = await asyncio.to_thread(report_mod.render_html, path)
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}.html"'},
    )


@app.get("/api/results/{stem}/raw")
async def download_raw(stem: str, kind: str = "jtl", _=Depends(require_auth)):
    path = _result_path(stem)
    target = path if kind != "log" else RESULTS_DIR / f"{stem}-jmeter.log"
    if not target.exists():
        raise HTTPException(status_code=404, detail="找不到該檔案")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@app.get("/api/nodes/{key}/inspect")
async def inspect_node(key: str, _=Depends(require_auth)):
    cfg = load_config().get("nodes", {}).get(key)
    if not cfg:
        raise HTTPException(status_code=404, detail="節點不存在")
    if not shutil.which("ssh"):
        raise HTTPException(status_code=400, detail="本機沒有 ssh 指令")

    remote = (
        "echo '== 服務狀態 =='; systemctl is-active jmeter-server 2>&1; "
        "echo; echo '== 監聽埠 =='; ss -lnt 2>/dev/null | grep -E ':1099|:4000' || echo '未監聽'; "
        "echo; echo '== JMeter =='; /opt/jmeter/bin/jmeter -v 2>&1 | head -2; "
        "echo; echo '== 硬體 =='; nproc; free -m | head -2; "
        "echo; echo '== 負載 =='; uptime; "
        "echo; echo '== 磁碟 =='; df -h / | tail -1"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTS, f"{SSH_USER}@{cfg['host']}", remote,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        text = out.decode("utf-8", "replace").strip()
        return {"ok": proc.returncode == 0, "output": text or "（無輸出）"}
    except asyncio.TimeoutError:
        return {"ok": False, "output": "連線逾時（25 秒）"}
    except Exception as exc:
        return {"ok": False, "output": f"執行失敗：{exc}"}


@app.post("/api/deploy")
async def start_deploy(body: DeployBody, request: Request, _=Depends(require_auth)):
    if CURRENT["job"] is not None:
        raise HTTPException(status_code=409, detail="已有部署任務在執行中")

    nodes = load_config().get("nodes", {})
    if body.scope == "all":
        keys = [
            k for k, v in nodes.items()
            if body.include_disabled or v.get("enabled", True)
        ]
        if not keys:
            raise HTTPException(status_code=400, detail="沒有符合條件的節點")
        title = f"部署全部節點（{len(keys)} 台）"
    elif body.scope == "node":
        if not body.key or body.key not in nodes:
            raise HTTPException(status_code=400, detail="節點不存在")
        keys = [body.key]
        title = f"部署節點 {body.key}"
    else:
        raise HTTPException(status_code=400, detail="scope 只能是 all 或 node")

    if not shutil.which("ssh"):
        raise HTTPException(status_code=400, detail="本機沒有 ssh 指令，無法遠端部署")

    job = Job(body.scope, title, keys)
    JOBS[job.id] = job
    CURRENT["job"] = job
    audit("DEPLOY", request.client.host if request.client else "-",
          f"{job.id} {body.scope} {','.join(keys)}")
    asyncio.create_task(run_deploy(job, keys, MASTER_IP))
    return {"job_id": job.id, "title": title, "keys": keys}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, _=Depends(require_auth)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任務不存在")
    return {**job.summary(), "lines": list(job.lines)[-200:]}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _=Depends(require_auth)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任務不存在")
    if job.status != "running":
        return {"ok": False, "detail": "任務已結束"}
    job.cancel_flag = True
    if job.proc and job.proc.returncode is None:
        try:
            job.proc.kill()
        except Exception:
            pass
    audit("CANCEL", "-", job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str, request: Request):
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="未登入")
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任務不存在")

    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    async def event_stream():
        for entry in list(job.lines):
            yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        if job.status != "running":
            yield "event: end\ndata: {}\n\n"
            return
        job.subs.add(queue)
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    if job.status != "running" and queue.empty():
                        yield "event: end\ndata: {}\n\n"
                        return
                    yield ": keepalive\n\n"
                    continue
                if entry.get("end"):
                    yield "event: end\ndata: {}\n\n"
                    return
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        finally:
            job.subs.discard(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/session")
async def session_info(request: Request):
    return {"authed": is_authed(request)}


# ---------------------------------------------------------------- 靜態資源

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8080")),
        log_level="info",
    )
