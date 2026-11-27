# 分散式壓測控制平台

以 Discord 為控制介面的分散式壓力測試平台。控制平面負責授權、目標驗證與任務分派，多台 Ubuntu 節點作為 JMeter 遠端引擎產生流量，結果自動彙總為統計報告。

另附一套**網頁控制台**（`web/`）：在同一個瀏覽器頁面看到所有節點的在線狀態、延遲走勢與容量，並可一鍵部署全部伺服器。

> **這個專案要求目標域名必須通過所有權驗證。** 未經驗證的域名不會產生任何流量。詳見 [安全性與合規](#安全性與合規)。

---

## 目錄

- [架構](#架構)
- [功能特性](#功能特性)
- [目錄結構](#目錄結構)
- [系統需求](#系統需求)
- [部署](#部署)
  - [步驟 0：前置準備](#步驟-0前置準備)
  - [步驟 1：建立 Discord 應用](#步驟-1建立-discord-應用)
  - [步驟 2：部署主服務器](#步驟-2部署主服務器)
  - [步驟 3：設定 .env](#步驟-3設定-env)
  - [步驟 4：部署壓測節點](#步驟-4部署壓測節點)
  - [步驟 5：驗證部署](#步驟-5驗證部署)
- [網頁控制台](#網頁控制台)
- [使用流程](#使用流程)
- [指令一覽](#指令一覽)
- [配置說明](#配置說明)
- [容量計算](#容量計算)
- [疑難排解](#疑難排解)
- [安全性與合規](#安全性與合規)

---

## 架構

```mermaid
flowchart LR
    U["Discord<br/>OWNER / STAFF"] -->|slash command| B["控制平面<br/>bot.py"]
    W["網頁控制台<br/>web/app.py :8080"] -->|"SSH 部署<br/>TCP 探測 1099"| N1
    W -->|讀寫| C["config/nodes.json"]
    B -->|核准與授權| B
    B -->|"DNS TXT / HTTP<br/>所有權驗證"| T["目標站點"]
    B -->|"RMI 1099"| N1["節點 hk-01<br/>jmeter-server"]
    B -->|"RMI 1099"| N2["節點 sg-01<br/>jmeter-server"]
    W -->|"SSH 部署"| N2
    N1 ==>|"壓測流量"| T
    N2 ==>|"壓測流量"| T
    B -.->|"彙總 jtl"| R["統計報告"]
```

| 元件 | 角色 | 說明 |
|---|---|---|
| 控制平面 | Discord Bot | 權限檢查、目標驗證、節點分配、結果統計 |
| 網頁控制台 | 節點運維 UI | 節點在線狀態、一鍵部署、部署日誌串流 |
| 壓測節點 | JMeter Server | 接收計畫並產生流量，可水平擴充 |
| 目標站點 | 待測系統 | 必須先通過所有權驗證 |

控制平面本身不產生流量，只負責下發 JMeter 計畫（`-R` 指定節點）與收集結果。網頁控制台只做節點運維，**不參與壓測授權流程**——壓測仍然只能從 Discord 由 OWNER 核准發起。

---

## 功能特性

**授權模型**

- 三層權限：`OWNER` / `STAFF` / 其他人（完全拒絕）
- 每次壓測執行都需要 OWNER 在 Discord 上按下核准按鈕
- `override` 僅 OWNER 可用，且永遠受全域硬上限約束
- 所有操作（含被拒絕的嘗試）寫入 `audit.log`

**目標治理**

- 目標生命週期：`pending_verify` → `verified` → `approved` → `suspended`
- 域名所有權驗證：DNS TXT 記錄或 HTTP 驗證檔，二擇一
- 拒絕解析到內網／保留位址的目標（防止被用來探測內部網路）
- 授權有到期日，過期自動失效

**分散式執行**

- 自動依節點容量選擇節點組合
- 執行緒數平均分配，並回報實際併發（可能略高於請求值）
- 爬升期自動限制在測試時長的三分之一內
- 執行中每 10 秒更新進度，支援隨時中止

**節點運維（網頁控制台）**

- 每 5 秒 TCP 探測節點 RMI 埠，即時反映在線／離線
- 延遲走勢圖、SSH 可達性、單節點詳細檢查（服務狀態、監聽埠、硬體、負載）
- 網頁上直接新增／編輯／刪除節點，不必手改 JSON
- 新增後可「儲存並部署」，一台新機器從填表到上線不用離開瀏覽器
- 一鍵部署全部節點，或單台重裝；部署輸出即時串流到頁面
- 節點啟停用開關直接改寫 `config/nodes.json`，容量即時重算

---

## 目錄結構

```
網絡安全機器人/
├── bot.py                  # 控制平面主程式
├── core/
│   ├── verify.py           # 域名所有權驗證（DNS TXT / HTTP）
│   ├── distribute.py       # 節點分配、容量計算、分散式執行
│   └── stats.py            # jtl 統計與達標判定
├── config/
│   ├── nodes.json          # 節點註冊表
│   └── targets.json        # 目標清單與全域上限
├── deploy/
│   ├── setup-master.sh     # 主服務器一鍵部署
│   ├── setup-node.sh       # 單一節點一鍵部署
│   └── deploy-nodes.sh     # 從主服務器批次部署所有節點
├── web/
│   ├── app.py              # 網頁控制台後端（FastAPI）
│   ├── requirements.txt
│   └── static/             # 前端（無需建置）
│       ├── index.html
│       ├── style.css
│       └── app.js
├── plans/
│   └── baseline.jmx        # 預設壓測計畫
├── results/                # 執行結果（不進版控）
├── requirements.txt
└── .env.example
```

---

## 系統需求

| 角色 | 規格 | 說明 |
|---|---|---|
| 主服務器 | Ubuntu 22.04 / 24.04<br/>2 vCPU / 2 GB | 需安裝 Python 3.10+、JMeter |
| 壓測節點 | Ubuntu 22.04 / 24.04<br/>4 vCPU / 8 GB 起 | 每 vCPU 約可承載 100~150 執行緒 |
| 網路 | 主服務器 → 節點 1099 / 4000 | 節點防火牆僅放行主服務器 IP |

所有機器的 **JMeter 版本必須完全一致**（本專案固定 5.6.3），否則 RMI 無法通訊。

---

## 部署

### 步驟 0：前置準備

在開始之前準備好：

1. 一台 Ubuntu 主服務器，以及至少一台 Ubuntu 節點
2. 主服務器可 SSH 登入各節點（批次部署需要）
3. 一個 Discord 伺服器，且你有管理權限

```bash
# 在主服務器上產生金鑰並複製到各節點（批次部署用）
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id root@<節點IP>
```

### 步驟 1：建立 Discord 應用

**1.1 建立應用與取得 Token**

1. 前往 <https://discord.com/developers/applications>
2. 點 **New Application**，輸入名稱
3. 左側選 **Bot** → 點 **Reset Token** → 複製 Token（**只會顯示一次**）
4. **Privileged Gateway Intents 三個全部保持關閉** — 本專案只用斜線指令，不需要任何特權意圖

**1.2 取得自己的使用者 ID**

1. Discord 設定 → 進階 → 開啟 **開發者模式**
2. 右鍵自己的頭像 → **複製使用者 ID**

**1.3 取得伺服器 ID**

右鍵伺服器圖示 → **複製伺服器 ID**

**1.4 邀請機器人**

1. 左側選 **OAuth2** → **URL Generator**
2. Scopes 勾選：`bot`、`applications.commands`
3. Bot Permissions 勾選：`Send Messages`、`Embed Links`、`Read Message History`
4. 複製最下方產生的 URL，在瀏覽器開啟並選擇你的伺服器

### 步驟 2：部署主服務器

**2.1 上傳專案**

```bash
scp -r 網絡安全機器人 root@<主服務器IP>:/root/
ssh root@<主服務器IP>
cd /root/網絡安全機器人/deploy
chmod +x *.sh
```

**2.2 執行部署腳本**

```bash
sudo ./setup-master.sh
```

腳本會自動完成：

| 步驟 | 內容 |
|---|---|
| 系統套件 | Python 3、venv、pip、OpenJDK 17、curl、tar、ufw、rsync |
| JMeter | 下載 5.6.3 至 `/opt/jmeter`，設定 `server.rmi.ssl.disable=true` |
| 服務帳號 | 建立 `loadtest` 系統使用者 |
| 檔案同步 | 專案複製到 `/opt/loadtest-platform`（排除 `.env`、`results`、`audit.log`） |
| Python 環境 | 建立 `.venv` 並安裝 `discord.py`、`aiohttp`、`dnspython`、`fastapi`、`uvicorn` |
| systemd | 註冊 `loadtest-bot` 與 `loadtest-web`（開機自啟、異常自動重啟） |
| SSH 金鑰 | 複製 root 的節點金鑰給 `loadtest` 使用者（Web 控制台部署節點用） |
| 存取權杖 | 自動產生 `WEB_TOKEN` 寫入 `.env`，並在結尾印出 |
| 防火牆 | 放行 1099 / 4000 / 8080 |

腳本可重複執行，已安裝的元件會跳過。

> 若 `.env` 尚未填寫 Token，腳本**不會**啟動服務，而是提示你完成設定。這是刻意設計。

### 步驟 3：設定 .env

```bash
sudo nano /opt/loadtest-platform/.env
```

填入以下四項（其餘保持預設）：

```ini
DISCORD_TOKEN=<步驟 1.1 的 Token>
OWNER_ID=<步驟 1.2 的使用者 ID>
STAFF_IDS=<安全人員 ID，逗號分隔；留空表示只有 OWNER 能用>
GUILD_ID=<步驟 1.3 的伺服器 ID>
```

| 變數 | 必填 | 說明 |
|---|---|---|
| `DISCORD_TOKEN` | 是 | Bot Token，洩漏等同他人可冒用你的 Bot |
| `OWNER_ID` | 是 | 最高權限，可核准目標與執行、使用 `override` |
| `STAFF_IDS` | 否 | 安全人員，可提交目標、驗證所有權、在核准範圍內發起壓測 |
| `GUILD_ID` | 建議 | 填入後斜線指令立即生效；不填為全域指令，最多需等 1 小時 |
| `JMETER_BIN` | 自動 | 由部署腳本自動填入 `/opt/jmeter/bin/jmeter` |
| `WEB_TOKEN` | 自動 | 網頁控制台存取權杖，部署腳本自動產生 |
| `WEB_PORT` | 否 | 控制台埠號，預設 `8080` |
| `SSH_USER` | 否 | 控制台部署節點時使用的 SSH 帳號，預設 `root` |
| `MASTER_IP` | 否 | 主服務器對外 IP，留空自動偵測 |

啟動服務：

```bash
sudo systemctl start loadtest-bot
sudo systemctl status loadtest-bot
tail -f /opt/loadtest-platform/logs/bot.log
```

看到 `登入為 xxx · OWNER xxx · STAFF n 人` 即成功。

### 步驟 4：部署壓測節點

#### 方式 A：批次部署（推薦）

**4.1 編輯節點清單**

```bash
sudo nano /opt/loadtest-platform/config/nodes.json
```

填入真實 IP：

```json
{
  "nodes": {
    "hk-01": {
      "host": "10.0.1.21",
      "port": 1099,
      "region": "HK",
      "max_threads": 600,
      "enabled": true,
      "note": "4 vCPU / 8G"
    }
  }
}
```

**4.2 執行批次部署**

```bash
cd /opt/loadtest-platform/deploy
sudo ./deploy-nodes.sh --dry-run   # 先確認要連哪些機器
sudo ./deploy-nodes.sh             # 實際部署
```

| 參數 | 說明 |
|---|---|
| `--dry-run` | 只顯示將執行的動作，不實際連線 |
| `--all` | 連同 `enabled: false` 的節點一起部署 |

環境變數 `SSH_USER` 可指定登入帳號（預設 `root`）：

```bash
SSH_USER=ubuntu sudo -E ./deploy-nodes.sh
```

#### 方式 B：單台手動部署

```bash
scp deploy/setup-node.sh root@<節點IP>:/root/
ssh root@<節點IP>
chmod +x /root/setup-node.sh
sudo /root/setup-node.sh <主服務器IP> <節點IP> <節點名稱>
#                        例: 10.0.1.10    10.0.1.21   hk-01
```

腳本執行完會印出可直接貼進 `nodes.json` 的 JSON 片段（含 CPU 與記憶體資訊）。

節點腳本會完成：

| 步驟 | 內容 |
|---|---|
| 系統套件 | OpenJDK 17、curl、tar、ufw |
| JMeter | 5.6.3，版本不符會自動重裝 |
| RMI 設定 | 關閉 SSL、固定資料埠 4000、綁定本機 IP |
| systemd | 註冊 `jmeter-server`（開機自啟、異常重啟） |
| 防火牆 | **僅放行主服務器 IP** 的 1099 / 4000 |
| 核心參數 | `somaxconn`、`ip_local_port_range`、`fs.file-max` 等 |

### 步驟 5：驗證部署

**5.1 重啟控制平面載入節點清單**

```bash
sudo systemctl restart loadtest-bot
```

**5.2 在 Discord 檢查節點**

```
/nodes
```

應顯示所有節點「在線」，footer 顯示有效容量。

**5.3 在網頁控制台檢查**

瀏覽器開啟 `http://<主服務器IP>:8080`，輸入 `.env` 中的 `WEB_TOKEN`。頁面應顯示所有節點為綠色在線，並給出有效容量。

**5.4 節點端診斷（若顯示離線）**

```bash
systemctl status jmeter-server
ss -lnt | grep -E '1099|4000'
journalctl -u jmeter-server -n 50
```

---

## 網頁控制台

`web/` 是一個獨立的 FastAPI 服務，與 Discord Bot 共用同一份 `config/nodes.json`。它**只做節點運維**，不涉及壓測授權——壓測仍然只能在 Discord 由 OWNER 核准發起。

### 啟動方式

部署主服務器時已自動註冊為 systemd 服務：

```bash
sudo systemctl status loadtest-web
sudo systemctl restart loadtest-web
tail -f /opt/loadtest-platform/logs/web.log
```

手動啟動（開發用）：

```bash
cd /opt/loadtest-platform
WEB_TOKEN=你的權杖 .venv/bin/python -m uvicorn web.app:app --host 0.0.0.0 --port 8080
```

首次啟動若未設定 `WEB_TOKEN`，會自動產生並寫入專案根目錄的 `.web_token`，同時印在日誌裡。

### 頁面功能

| 區域 | 內容 |
|---|---|
| 頂部狀態 | 在線節點數、有效容量、主服務器 IP、JMeter 版本 |
| 統計卡 | 在線節點、有效併發容量、主服務器、最後更新時間 |
| 節點卡 | 狀態燈、延遲、執行緒上限、最近檢查時間、延遲走勢圖、啟停用開關 |
| 節點卡按鈕 | 「檢查」讀取遠端服務狀態／監聽埠／硬體／負載；「部署」重裝該節點；主機列右側鉛筆／垃圾桶可編輯或刪除 |
| 一鍵部署 | 對所有啟用節點依序執行 `setup-node.sh`，可勾選含停用節點 |
| 部署輸出 | 即時串流 SSH 輸出，錯誤行標紅，可中止 |

### 新增伺服器

按動作列的「新增伺服器」，填寫表單即可，不需要手改 `nodes.json`：

| 欄位 | 說明 |
|---|---|
| 名稱 | 節點識別碼，英數字開頭，可用 `_ . -`，最長 32 字元（如 `hk-02`） |
| 主機 / IP | 只填 IP 或域名，不要含 `http://` 或路徑 |
| RMI 端口 | JMeter 遠端引擎埠，預設 `1099` |
| SSH 端口 | 部署時連線用，預設 `22` |
| 地區 | 顯示用標籤，如 `HK`、`SG` |
| 執行緒上限 | 該節點可承載的併發上限，建議每 vCPU 100~150 |
| 備註 | 硬體規格等說明 |
| 啟用 | 停用的節點不參與壓測與批次部署 |

三個按鈕的差別：

- **測試連線**：對填寫的 IP 同時探測 RMI 與 SSH 埠，確認可達再儲存。這是新增前最值得按的一下。
- **儲存**：只寫入 `config/nodes.json`，節點會出現在清單上但顯示離線。
- **儲存並部署**：寫入後立刻對該節點執行一次 `setup-node.sh`，裝完自動變在線。新機器用這個。

編輯時名稱欄可以改，等於重新命名節點（會檢查新名稱是否撞名）。刪除只會從 `nodes.json` 移除，**不會**動到那台伺服器上的 `jmeter-server`。

新增、編輯、刪除都會寫入 `audit.log`（`NODE-ADD` / `NODE-EDIT` / `NODE-DEL`）。

> 網址帶 `#add` 會直接開啟新增表單，方便加到書籤。

### 在線判定方式

- **每 5 秒**對節點 RMI 埠（預設 1099）發起 TCP 連線，成功即視為在線，並記錄握手延遲
- **每 20 秒**額外探測 SSH 埠（22），用於提示「可部署性」
- 節點卡上的延遲走勢保留最近 40 個採樣點

這是**埠層面的存活判斷**，不代表 `jmeter-server` 真的能接任務。要確認服務本身，按節點卡上的「檢查」，它會 SSH 進去執行 `systemctl is-active jmeter-server`、`ss -lnt`、`jmeter -v`。

### 部署流程

按下「一鍵部署全部節點」後，後端依序對每個節點執行：

```
ssh root@<節點> true                          # 連線預檢
scp deploy/setup-node.sh root@<節點>:/tmp/   # 傳送腳本
ssh root@<節點> "chmod +x /tmp/setup-node.sh && \
                 sudo -n bash /tmp/setup-node.sh <master_ip> <node_ip> <node_name>"
```

- 單一任務序列執行，不並行（避免同時重啟多台造成壓測中斷）
- 同一時間只允許一個部署任務，重複提交會回 409
- 任一台失敗不中斷其餘節點，最後彙總成功／失敗數
- `setup-node.sh` 本身可重複執行，已安裝的元件會跳過

### 存取控制

| 機制 | 說明 |
|---|---|
| 權杖登入 | `WEB_TOKEN` 以 HMAC 簽章寫入 HttpOnly Cookie，有效期 12 小時 |
| 常數時間比對 | 使用 `hmac.compare_digest`，避免時序側信道 |
| 稽核 | 登入、登入失敗、部署、中止、節點啟停用全部寫入 `audit.log` |
| 無使用者系統 | 不存帳號密碼，只有一組共用權杖；適合內部運維 |

> 這個頁面能對伺服器執行 root 級部署，**不要直接暴露在公網**。建議用 `ufw` 限制來源 IP，或放在 VPN / 跳板機之後。

```bash
sudo ufw delete allow 8080/tcp
sudo ufw allow from 203.0.113.0/24 to any port 8080 proto tcp
```

### 相關 API

| 方法 | 路徑 | 說明 |
|---|---|---|
| `POST` | `/api/login` | 以權杖換取 session cookie |
| `GET` | `/api/state` | 節點清單、在線狀態、容量、最近任務 |
| `POST` | `/api/nodes` | 新增節點 |
| `PUT` | `/api/nodes/{key}` | 修改節點（`key` 變更即重新命名） |
| `DELETE` | `/api/nodes/{key}` | 刪除節點 |
| `POST` | `/api/probe` | 探測任意主機的 RMI / SSH 埠（新增前測試用） |
| `POST` | `/api/nodes/{key}/enabled` | 啟用／停用節點，寫回 `nodes.json` |
| `GET` | `/api/nodes/{key}/inspect` | SSH 讀取節點詳細狀態 |
| `POST` | `/api/deploy` | 發起部署（`scope: all` 或 `node`） |
| `GET` | `/api/jobs/{id}` | 任務狀態與輸出 |
| `POST` | `/api/jobs/{id}/cancel` | 中止任務 |
| `GET` | `/api/jobs/{id}/stream` | SSE 即時輸出 |

---

## 使用流程

### 目標生命週期

```
pending_verify  ──/target verify──▶  verified  ──/target approve──▶  approved
      │                                  │                              │
      │                                  │                              ├─▶ /loadtest
      └──────────── 授權過期 ─────────────┴──── /target suspend ────────▶ suspended
```

### 完整操作範例

**① 提交目標（STAFF 或 OWNER）**

```
/target add domain:example.com client:"Acme Ltd"
```

Bot 會回傳驗證 token 與兩種驗證方式。

**② 客戶完成驗證（二擇一）**

方式 A — DNS TXT 記錄：

| 類型 | 名稱 | 值 |
|---|---|---|
| TXT | `_loadtest-verify.example.com` | `lt-xxxxxxxx...` |

方式 B — HTTP 驗證檔：

在 `https://example.com/.well-known/loadtest-verify.txt` 放置內容為 token 的純文字檔。

**③ 執行驗證**

```
/target verify key:example-com
```

通過後狀態變為 `verified`。

**④ OWNER 核准並設定上限**

```
/target approve key:example-com threads:2000 duration:600 days:30
```

狀態變為 `approved`，授權至 30 天後。

**⑤ 發起壓測**

```
/loadtest key:example-com threads:1000 duration:300
```

Bot 顯示待核准卡片（目標、節點分配、實際併發、時長），OWNER 按下「核准並執行」才開始產生流量。

**⑥ 查看結果**

執行結束後自動貼出統計：請求總數、錯誤率、平均／P50／P90／P99／最大延遲，並依 `P90 < 800ms 且錯誤率 < 1%` 判定是否達標。

---

## 指令一覽

| 指令 | 權限 | 說明 |
|---|---|---|
| `/target add` | STAFF | 提交目標，產生所有權驗證 token |
| `/target verify` | STAFF | 驗證域名所有權 |
| `/target approve` | **OWNER** | 核准目標並設定併發／時長上限 |
| `/target suspend` | **OWNER** | 立即暫停目標 |
| `/target list` | STAFF | 列出所有目標與授權狀態 |
| `/nodes` | STAFF | 檢查節點連線狀態與有效容量 |
| `/loadtest` | STAFF | 發起壓測（需 OWNER 按鈕核准） |
| `/loadtest override:true` | **OWNER** | 突破目標授權上限，受全域上限約束 |
| `/abort` | STAFF | 中止執行中的壓測 |

---

## 配置說明

### config/nodes.json

| 欄位 | 說明 |
|---|---|
| `host` | 節點 IP，需可從主服務器訪問 |
| `port` | RMI 埠，預設 1099 |
| `region` | 標記用，不影響執行 |
| `max_threads` | 該節點承載上限，超出會被分配器排除 |
| `enabled` | `false` 表示暫停使用 |

### config/targets.json

```json
{
  "limits": {
    "global_max_threads": 5000,
    "global_max_duration": 1800
  },
  "settings": {
    "allow_private_targets": false,
    "verify_txt_prefix": "_loadtest-verify",
    "verify_http_path": "/.well-known/loadtest-verify.txt"
  },
  "targets": { }
}
```

| 欄位 | 說明 |
|---|---|
| `limits.global_max_threads` | 全域併發硬上限，`override` 也不得超過 |
| `limits.global_max_duration` | 全域時長硬上限（秒） |
| `settings.allow_private_targets` | 是否允許目標解析到內網位址，**建議保持 `false`** |
| `settings.verify_txt_prefix` | DNS TXT 記錄前綴 |
| `settings.verify_http_path` | HTTP 驗證檔路徑 |

`targets` 由 Bot 自動維護，不建議手動編輯。

---

## 容量計算

**關鍵概念：分散式模式下每個節點執行相同的執行緒數。**

因此實際總併發不是各節點上限相加，而是：

```
有效容量 = min(各節點 max_threads) × 節點數
```

**範例**：節點 A 上限 800、節點 B 上限 600

| 請求併發 | 分配結果 | 說明 |
|---|---|---|
| 500 | 2 節點 × 250 = 500 | 正常 |
| 1200 | 2 節點 × 600 = 1200 | 剛好滿載 |
| 1300 | — | **容量不足**，有效容量僅 1200 |

要承載 2000 併發，需要 3 台 800 的節點（`min(800)×3 = 2400`），而不是兩台 1000。

**爬升期**自動取「每節點執行緒一半」與「時長三分之一」的較小值，避免執行緒還沒拉滿測試就結束。

---

## 疑難排解

| 症狀 | 可能原因 | 處理 |
|---|---|---|
| `/nodes` 全部離線 | 節點防火牆未放行主服務器 IP | 節點執行 `ufw status`，確認 1099 / 4000 |
| | `java.rmi.server.hostname` 綁到錯誤網卡 | 重跑 `setup-node.sh` 並明確指定第二個參數 |
| | 兩端 JMeter 版本不一致 | 兩端執行 `jmeter -v` 比對 |
| 壓測無 jtl 產出 | 主服務器 `server.rmi.ssl.disable` 未設 | 檢查 `/opt/jmeter/bin/jmeter.properties` |
| | 節點 `jmeter-server` 未啟動 | `systemctl status jmeter-server` |
| `bad interpreter` | 腳本含 CRLF | 執行 `sed -i 's/\r$//' *.sh` |
| 目標無法提交 | 域名解析到內網位址 | 確認是公網域名，或開啟 `allow_private_targets` |
| | 域名格式含 `http://` 或路徑 | 只填域名本身，例如 `example.com` |
| 驗證一直失敗 | DNS 尚未傳播 | 等待 TTL 過期，或改用 HTTP 驗證檔 |
| | TXT 記錄名稱錯誤 | 應為 `_loadtest-verify.` 加完整域名 |
| Bot 無回應 | Token 錯誤或過期 | 重新產生 Token 並更新 `.env` |
| | 未填 `GUILD_ID` 導致指令尚未同步 | 填入 `GUILD_ID` 並重啟 |
| | 缺少 `applications.commands` scope | 重新用正確 URL 邀請 Bot |
| 控制台顯示全部離線 | 節點未部署或 `jmeter-server` 未啟動 | 按「一鍵部署全部節點」，或看節點 `systemctl status jmeter-server` |
| | 探測的是 RMI 埠，被防火牆擋 | 節點需放行主服務器 IP 的 1099 |
| 控制台「SSH 不通」 | `loadtest` 使用者沒有節點金鑰 | `sudo -u loadtest ssh-copy-id root@<節點IP>` |
| 部署報 `sudo: a password is required` | 節點 sudo 需要密碼 | 節點加 `NOPASSWD`，或把 `SSH_USER` 設為 `root` |
| 控制台無法登入 | `WEB_TOKEN` 不符 | 看 `/opt/loadtest-platform/.env` 的 `WEB_TOKEN` |
| 控制台 502 / 連不上 | 服務未啟動或防火牆 | `systemctl status loadtest-web`、`ufw status` |

**查看日誌**

```bash
# 控制平面
sudo journalctl -u loadtest-bot -n 50
tail -f /opt/loadtest-platform/logs/bot.log

# 網頁控制台
sudo journalctl -u loadtest-web -n 50
tail -f /opt/loadtest-platform/logs/web.log

# 節點
sudo journalctl -u jmeter-server -n 50
```

**稽核記錄**

所有操作（含被拒絕的嘗試）記錄於 `/opt/loadtest-platform/audit.log`：

```
2026-11-26T23:27:09 | DENY         | user-xxx | 嘗試 loadtest example-com 500x120
2026-11-26T23:28:11 | TARGET-ADD   | user-xxx | example-com example.com client=Acme
2026-11-26T23:31:02 | TARGET-APPROVE | user-xxx | example-com 2000 併發 / 600s 至 2026-12-26
2026-11-26T23:35:40 | OVERRIDE     | user-xxx | example-com total=5000 per_node=2500 nodes=2
```

---

## 安全性與合規

### 目標所有權驗證

本專案**強制要求**目標域名通過所有權驗證（`core/verify.py`）。這不是可選功能，而是平台的法律基礎：

- 客戶口頭聲稱擁有某域名，不構成可驗證的授權
- DNS TXT 記錄或網站根目錄檔案，是可稽核的所有權證明
- 驗證記錄、核准人、授權期限全部留存在 `targets.json` 與 `audit.log`

**移除或繞過此模組，本專案即退化為壓力測試即服務（stresser）工具，可能違反《電腦犯罪相關法規》。**

### 營運建議

1. **保留書面授權**：與客戶簽署的壓測委託書應與 `targets.json` 的授權記錄對應
2. **設定測試窗口**：避開客戶業務高峰，並提前告知可能的使用者影響
3. **限制節點對外暴露**：節點防火牆只放行主服務器 IP，切勿對公網開放 RMI 埠
4. **保護 `.env`**：Bot Token 洩漏等同他人可冒用你的 Bot 身份
5. **定期審閱稽核日誌**：`audit.log` 中出現非預期的 `DENY` 記錄代表有人在嘗試越權
6. **授權到期即失效**：`authorized_until` 過期後 `/loadtest` 會直接拒絕
7. **`WEB_TOKEN` 等同節點 root 權限**：控制台能對「新增的任意 IP」用主服務器的 SSH 金鑰部署軟體。持有權杖的人可以把任意可達主機加進白名單並在其上安裝 JMeter。因此 8080 埠務必限制來源，權杖視同 root 密碼管理，離職即輪換
8. **新增節點後確認 IP 正確**：填錯 IP 的後果是對一台無關機器執行 `setup-node.sh`，那台機器會被裝上 JMeter 並開啟 RMI 埠

### 已知限制

- 單一控制平面，無高可用機制
- 壓測計畫需預先放置於 `plans/`，尚不支援從 Discord 上傳
- 結果以 JMeter 原始 jtl 統計呈現，未做時序圖分析

---

## 授權

本專案為私有商業工具，未附帶開源授權條款。未經授權不得再散布或用於對未授權目標的壓力測試。
