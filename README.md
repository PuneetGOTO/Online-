# 分散式壓測控制平台

以 Discord 為控制介面的分散式壓力測試平台。控制平面負責授權、目標驗證與任務分派，多台 Ubuntu 節點作為 JMeter 遠端引擎產生流量，結果自動彙總為統計報告。

另附一套**網頁控制台**（`web/`）：在同一個瀏覽器頁面看到所有節點的在線狀態、延遲走勢與容量，並可一鍵部署全部伺服器。

> **每次壓測都需要 OWNER 在 Discord 按下核准按鈕。** 目標可經 DNS／HTTP 驗證，或由 OWNER 直接核准（記為 `TARGET-APPROVE-UNVERIFIED`）。詳見 [安全性與合規](#安全性與合規)。

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
| 目標站點 | 待測系統 | 需通過所有權驗證，或由 OWNER 直接核准 |

控制平面本身不產生流量，只負責下發 JMeter 計畫（`-R` 指定節點）與收集結果。網頁控制台只做節點運維，**不參與壓測授權流程**——壓測仍然只能從 Discord 由 OWNER 核准發起。

---

## 功能特性

**授權模型**

- 三層權限：`OWNER` / 授權管理員（網頁設定）/ 其他人（完全拒絕）
- 目標必須先在網頁控制台定義，`/start`、`/stepped` 只能從已定義且啟用的目標中選，指令端無法臨時輸入網址
- 階梯壓測：`/stepped 網址 50,100,200,400`，逐級加壓找出容量拐點並產出曲線報告
- 走完整流程的 `/loadtest` 仍需 OWNER 在 Discord 按下核准按鈕
- `override` 僅 OWNER 可用，且永遠受全域硬上限約束
- 所有操作（含被拒絕的嘗試）寫入 `audit.log`

**目標治理**

- 目標生命週期：`pending_verify` → `verified` → `approved` → `suspended`
- 域名所有權驗證：DNS TXT 記錄或 HTTP 驗證檔，二擇一（OWNER 可跳過，見下）
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
- 網頁上直接新增／編輯／刪除節點，填 IP、SSH 帳密即可
- 測試連接會實際登入 SSH 並回報 OS／CPU／記憶體／sudo／JMeter 狀態
- 測試通過後一鍵部署，一台新機器從填表到上線不用離開瀏覽器
- 網頁直接填 Discord Token／Owner／Staff／Guild 並啟停 Bot，附即時日誌
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
| 網路 | 主服務器 → 節點 1099 / 4000<br/>節點 → 主服務器 4000~4010 | 雙向都要通；回傳通道不通會讓壓測卡住 |

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
| JMeter | 下載 5.6.3 至 `/opt/jmeter`；`server.rmi.ssl.disable=true`、`client.rmi.localport=4000`；`java.rmi.server.hostname` 寫入 `system.properties`（JVM 屬性，寫在 jmeter.properties 無效） |
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

> 也可以完全不碰 `.env`：部署完成後開控制台，在「Discord 機器人」面板填寫這四項並按「啟動」。

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
| 壓測狀態 | 是否有測試在跑、目標域名、進度條與 `30 / 60 s`、併發、節點、已送回筆數 |
| 統計卡 | 在線節點、有效併發容量、主服務器、最後更新時間 |
| 節點卡 | 狀態燈、延遲、執行緒上限、最近檢查時間、延遲走勢圖、啟停用開關 |
| 節點卡按鈕 | 「檢查」讀取遠端服務狀態／監聽埠／硬體／負載；「部署」重裝該節點；主機列右側鉛筆／垃圾桶可編輯或刪除 |
| 一鍵部署 | 對所有啟用節點依序執行 `setup-node.sh`，可勾選含停用節點 |
| Discord 面板 | 在網頁填 Token／Owner／Staff／Guild，直接啟動、重啟、停止 Bot，並顯示即時日誌與斜線指令清單 |
| 部署輸出 | 即時串流 SSH 輸出，錯誤行標紅，可中止 |

### 新增伺服器

按動作列的「新增伺服器」，填寫表單即可，不需要手改 `nodes.json`：

| 欄位 | 說明 |
|---|---|
| 名稱 | 節點識別碼，英數字開頭，可用 `_ . -`，最長 32 字元（如 `hk-02`） |
| 伺服器 IP | 只填 IP 或域名，不要含 `http://` 或路徑 |
| SSH 使用者 | 部署時登入的帳號，`root` 或具 sudo 權限的使用者（如 `ubuntu`） |
| SSH 密碼 | 部署時使用；編輯時留空則沿用已儲存的密碼 |
| RMI 端口 | JMeter 遠端引擎埠，預設 `1099` |
| SSH 端口 | 部署時連線用，預設 `22` |
| 地區 | 顯示用標籤，如 `HK`、`SG` |
| 執行緒上限 | 該節點可承載的併發上限，建議每 vCPU 100~150 |
| 備註 | 硬體規格等說明 |
| 啟用 | 停用的節點不參與壓測與批次部署 |

操作順序是 **測試連接 → 開始部署節點**：

1. **測試連接**：用填寫的帳密實際登入一次 SSH，回報作業系統、CPU 核數、記憶體、sudo 是否可用、JMeter 是否已安裝。認證失敗會直接說明是帳號密碼錯誤還是連不上。
2. 測試通過後「開始部署節點」才會亮起。它會先儲存節點，再立刻對該機器執行 `setup-node.sh`。
3. **僅儲存**：只寫入 `config/nodes.json`，不部署（節點會顯示離線）。

改了 IP、使用者、密碼或 SSH 埠之後，部署按鈕會重新鎖上，必須再測一次——避免拿著舊憑證去部署新機器。

編輯時名稱欄可以改，等於重新命名節點（會檢查新名稱是否撞名）。刪除只會從 `nodes.json` 移除，**不會**動到那台伺服器上的 `jmeter-server`。

新增、編輯、刪除都會寫入 `audit.log`（`NODE-ADD` / `NODE-EDIT` / `NODE-DEL`，只記使用者名稱，不記密碼）。

**密碼怎麼存**：以 `WEB_TOKEN` 派生金鑰加密後寫入 `config/credentials.json`（權限 600，已列入 `.gitignore`）。拿到檔案但沒有權杖無法解開；換掉 `WEB_TOKEN` 會讓既有密碼失效，需要重新輸入一次。

> 網址帶 `#add` 會直接開啟新增表單，方便加到書籤。

### Discord 機器人

不需要 SSH 進去改 `.env`，控制台下方就有一塊 Discord 面板：

| 欄位 | 說明 |
|---|---|
| Bot Token | Discord Developer Portal → Bot → Reset Token。已設定過會顯示 `MTIzND…ABCD` 這種遮罩，**留空則不變更** |
| Owner ID | 你的 Discord 使用者 ID，唯一能核准壓測的人 |
| Guild ID | 填了斜線指令**立即生效**；不填要等全域同步（最多 1 小時） |
| Staff IDs | 舊版欄位，保留相容；新設定請用下一欄 |
| 授權管理員 IDs | 可用 `/start` 與所有指令的 Discord 使用者 ID，逗號分隔；Owner 不需填 |

按「儲存設定」會寫回 `.env`；如果 Bot 正在執行，會自動重啟讓新設定生效。

右上三個按鈕：

- **啟動**：`systemctl start loadtest-bot`。沒填 Token 或 Owner ID 會直接被拒
- **重啟**：改完設定但沒自動重啟時用
- **停止**：`systemctl stop loadtest-bot`

面板底部會顯示 `logs/bot.log` 最後 25 行。啟動失敗（例如 Token 貼錯）時，這裡會直接看到 `discord.errors.LoginFailure: Improper token has been passed.`，不用再去翻 journalctl。

狀態標籤對應：`執行中` / `已停止` / `啟動失敗` / `未設定`。

> Bot 以斜線指令（`/`）操作，不支援舊式前綴指令。指令清單見面板右側，或 [指令一覽](#指令一覽)。

**Token 不會回傳給瀏覽器**：介面只顯示遮罩後的片段，存檔時留空即沿用原值。

### 壓測狀態視窗

控制台頂部（統計卡下方）有一條狀態列，回答一個問題：**現在到底有沒有在跑？**

| 狀態 | 顯示 |
|---|---|
| 沒有測試 | 灰線，`目前沒有壓測在執行` |
| 執行中 | 藍線＋脈動圓點，`壓測執行中 cloud.example.com`，進度條與 `30 / 60 s`，右側剩餘秒數 |
| 完成 | 綠線，`上一次壓測完成`，附請求數、錯誤率、平均延遲、P90、達標判定 |
| 失敗／中止 | 紅線，附原因 |
| 狀態不明 | 橙線，`機器人可能已離線` |

執行中的細節：併發數、參與節點、已送回筆數、操作者。秒數由瀏覽器每秒自行推算，不用等輪詢，所以是平滑跳動的；已送回筆數每 10 秒更新一次（JMeter 回報頻率）。

**資料來源**：`bot.py` 在壓測開始、每 10 秒進度回報、結束時，把狀態寫進 `results/loadtest-status.json`；控制台讀這個檔案。所以**網頁與 Discord 看到的是同一份事實**，不會出現兩邊不一致。

如果機器人在壓測中途掛掉，檔案會停在 `running`。控制台判斷「已超過時長 120 秒」就改顯示狀態不明，不會讓你以為還在跑。

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
| `POST` | `/api/nodes` | 新增節點（可附 SSH 帳密） |
| `PUT` | `/api/nodes/{key}` | 修改節點（`key` 變更即重新命名） |
| `DELETE` | `/api/nodes/{key}` | 刪除節點（同時清除已存憑證） |
| `POST` | `/api/ssh-test` | 用帳密實際登入 SSH，回報 OS／CPU／記憶體／sudo／JMeter 狀態 |
| `POST` | `/api/probe` | 探測任意主機的 RMI / SSH 埠（純 TCP，不需帳密） |
| `GET` | `/api/bot/status` | Bot 服務狀態、設定摘要（Token 遮罩）、日誌尾段、指令清單 |
| `GET` | `/api/loadtest` | 目前／最近一次壓測狀態（含進度、剩餘秒數、統計） |
| `POST` | `/api/bot/config` | 寫入 Token／Owner／Staff／Guild 到 `.env`，執行中會自動重啟 |
| `POST` | `/api/bot/action` | `start` / `stop` / `restart` Bot 服務 |
| `POST` | `/api/nodes/{key}/enabled` | 啟用／停用節點，寫回 `nodes.json` |
| `GET` | `/api/nodes/{key}/inspect` | SSH 讀取節點詳細狀態 |
| `POST` | `/api/deploy` | 發起部署（`scope: all` 或 `node`） |
| `GET` | `/api/jobs/{id}` | 任務狀態與輸出 |
| `POST` | `/api/jobs/{id}/cancel` | 中止任務 |
| `GET` | `/api/jobs/{id}/stream` | SSE 即時輸出 |

---

## 使用流程

### 先在網頁控制台定義目標（必做）

**壓測目標只能來自網頁控制台**，Discord 端不再接受臨時輸入的網址。控制台首頁的
「壓測目標」面板可新增、編輯、啟停與刪除目標：

| 欄位 | 說明 |
|---|---|
| 要壓測的完整網址 | 例 `https://example.com/login/index.php`，會自動拆出協定、端口、路徑 |
| 名稱 | 留空會由域名自動產生（`example-com`） |
| 客戶名稱 | 報告與 Discord 回覆會顯示 |
| 壓測計畫 | 下拉選單依用途分組顯示，選中後下方會說明該計畫打哪些端點、有無斷言與思考時間 |
| 併發上限 / 時長上限 | 該目標允許的最大值，Discord 端會以此為硬上限 |
| 授權天數 | 到期後該目標自動不可壓測 |
| 啟用 | 停用後 Discord 無法選用 |

目標一旦建立並啟用，Discord 的 `/start`、`/stepped`、`/loadtest` 就能選到它。
**沒在控制台定義過的網站，指令端打不到。**

### 一句話壓測（最常用）

```
/start moodle-aris-edu-org 300
```

| 位置 | 意義 | 例 |
|---|---|---|
| 1 | 目標名稱（自動完成選單，只能選已定義且啟用的） | `moodle-aris-edu-org` |
| 2 | 併發執行緒 | `300` |
| 3 | 持續秒數（選填） | `120`，預設 60 |
| 4 | 只用指定節點（選填） | `nodes:Huawei1` |
| 5 | 走代理池（選填） | `proxy:true` |

網址、端口、協定、方法都取自控制台的目標定義，指令端無法覆寫。

**權限**：只有 `OWNER` 與「網頁控制台授權的管理員」能用，其他人一律拒絕並記錄
`DENY-START`。管理員名單在控制台的 Discord 面板填寫，改完立即生效，不必重啟 Bot。

**限制**：併發與時長受該目標的 `max_threads` / `max_duration` 約束，同時也受
`config/targets.json` 的 `global_max_threads` / `global_max_duration` 限制；
同一時間只能有一場壓測在跑。

### 階梯壓測（找容量拐點）

```
/stepped moodle-aris-edu-org 50,100,200,400
```

| 位置 | 意義 | 例 |
|---|---|---|
| 1 | 目標名稱（只能選控制台已定義且啟用的） | `moodle-aris-edu-org` |
| 2 | 各級併發，逗號分隔且**嚴格遞增** | `50,100,200,400` |
| 3 | 每級持續秒數（選填） | 預設 `60` |
| 4 | 級間間隔秒數（選填） | 預設 `60` |
| 5 | 只用指定節點（選填） | `nodes:Huawei1` |
| 6 | 走代理池（選填） | `proxy:true` |

單場高併發壓測只會得到一個「整體錯誤率」，看不出容量在哪裡飽和。階梯壓測
逐級加壓，每一級都是一次完整的分散式壓測，跑完會產出：

- **容量拐點**：最後一個「錯誤率 < 1%」的併發級
- **飽和點**：第一個錯誤率突破 1% 的併發級
- **吞吐停滯點**：併發增加但吞吐成長不到 10% 的那一級
- **峰值成功吞吐**：所有級別中成功請求數最高者（req/s）

結果會以 Embed 逐級回報，最後附上單檔 HTML 容量曲線報告
（`results/<目標>-<時間>-stepped.html`），含成功吞吐與錯誤率兩張圖與各級明細表。

**為什麼級間要留間隔**：Cloudflare 這類 WAF 對單一來源 IP 有速率限制。
實測 `moodle.aris-edu.org` 時，單一節點 IP 每秒約 1,080 請求就會觸發限流並持續
回 `403`，且限制會延續數十秒。若級間不留白，第二級之後量到的全是 403，
完全看不到源站行為——這也是為什麼單場 1,200 併發的結果裡有 59.7% 是 403。

**中止**：`/abort` 會中止整場階梯壓測（包含間隔等待階段），並就已完成級別
產出報告。

### 只指定部分節點

`/start` 與 `/stepped` 都支援 `nodes` 參數，只讓指定節點參與，其餘節點不產生流量：

```
/start example-com 300 nodes:Huawei1
/stepped example-com 50,100,200 nodes:Huawei1
```

留空或填 `all` 表示全部啟用中的節點。節點名稱要與 `config/nodes.json` 的 key 一致。

**用途**：單節點對照實驗（確認是不是某台節點的網路問題）、或刻意只從特定地區發壓。
注意分散式模式下有效容量是 `min(各節點上限) × 節點數`，只留一台會讓可用併發變少。

### 多來源 IP 壓測（代理池）

當目標站台有 Cloudflare 這類以「單一 IP 速率」為基礎的防護時，兩台節點的併發再高，
來源 IP 也只有兩個，很容易被限流（實測約 1,080 req/s 就觸發 403）。
代理模式讓請求從大量不同 IP 發出，繞開單 IP 限制。

**① 準備清單**：把 udealproxy 的位址填進 `config/proxies.txt`
（該檔已列入 `.gitignore`，不會進版控；格式範本見 `config/proxies.example.txt`）：

```
http://user:pass@203.0.113.10:8080
http://user:pass@203.0.113.11:8080
socks5://user:pass@203.0.113.12:1080
```

**② 發壓時加上 `proxy`**：

```
/start example-com 600 proxy:true
/stepped example-com 50,100,200 proxy:true
```

**運作方式**：

- 每個執行緒在第一次請求時挑選一個代理，之後整個測試都沿用同一個
  （`執行緒序號 % 代理數量`），因此連線可以重用，不會每個請求都重新握手
- 代理清單以 base64 編碼後透過 `-G` 傳給遠端引擎，不需要把檔案複製到節點
- 帳密採用 HTTP Basic（`Proxy-Authorization`），代理先回 `407` 挑戰後 JMeter 自動重送

**限制與注意事項**：

| 項目 | 說明 |
|---|---|
| SOCKS5 | JMeter 的取樣器只能設定 HTTP/HTTPS 代理，**無法逐執行緒輪換 SOCKS**。清單若只有 SOCKS 會被拒絕；混用時會自動略過 SOCKS 並在回覆中提示 |
| 代理品質 | 代理延遲與失敗率會直接反映在壓測結果上，解讀 P90／錯誤率時要先確認不是代理本身的問題 |
| 代理數量 | 至少準備「併發數 ÷ 10」以上的代理，否則單一代理仍會成為瓶頸 |
| 用量與授權 | 代理流量通常按量計費，壓測前先確認配額 |

#### 輪換閘道（rotating gateway）型代理

有些供應商（例如 udealproxy）給的不是「一堆 IP」，而是**同一個入口**，由後端
在每次連線時自動換出口 IP：

```
as.udealproxy.com:6666:userId-11335-custom-33915-region-any:60njKQ
```

這種清單貼 100 行重複內容沒有意義——實際只需要 **1 行**。它的行為是：

- **每次新建連線換一個出口 IP**（實測 50 個並行連線拿到 42 個不同 IP）
- **同一條連線上維持同一個出口 IP**（keepalive 有效）
- 因此「每個執行緒固定一個代理 + 啟用 keepalive」正好等於「每個執行緒固定一個來源 IP」

這代表**必須保留 keepalive**（本平台的計畫預設已開啟）。若關閉 keepalive，每個請求都要
重新建立連線，除了多花 2~3 秒握手成本，還會瞬間對閘道打出大量新連線而失敗。

**實測數據**（目標 `api.ipify.org`，經 udealproxy 閘道）：

| 執行緒 | 吞吐 | 成功率 | P50 | P90 |
|---|---|---|---|---|
| 50 | 51 req/s | 99.7% | 566ms | 814ms |
| 100 | 222 req/s | 99.8% | 600ms | 863ms |

**爬升要放慢**：用 `curl` 一次打 100 個「新連線」時吞吐只剩 1.5 req/s（大量連線同時
建立會把閘道打爆），但用 JMeter 的 keepalive + 漸進爬升就能到 222 req/s。所以代理模式
建議把 `duration` 設長一點（≥ 120 秒），讓爬升期有足夠時間建立連線。

**地區參數**：帳號字串裡的 `region-any` 可以換成 `region-hk`、`region-sg`、`region-jp`、
`region-us`（小寫），實測都能拿到對應地區的出口 IP。

### 目標生命週期

```
pending_verify ──/target verify──▶ verified ──┐
      │                                        ├──/target approve──▶ approved ──▶ /loadtest
      └────────────/target approve─────────────┘        │                  │
                （OWNER 跳過驗證）                       │                  │
                                          /target suspend ────────────────┘
```

- **走驗證**：`pending_verify` → `/target verify` → `verified` → `/target approve` → `approved`
- **不走驗證**：`pending_verify` → `/target approve`（僅 OWNER）→ `approved`，記錄為 `verification: owner_override`

兩條路都一樣：`/loadtest` 只能對 `approved` 的目標發起，而且**每次執行都要 OWNER 按下核准按鈕**。

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

> 客戶來不及加 DNS 記錄時，OWNER 可以在 `pending_verify` 狀態直接執行這條指令，跳過驗證。回應會標註「此目標未通過所有權驗證」，稽核日誌記為 `TARGET-APPROVE-UNVERIFIED`。**客戶的書面授權書要記得存檔**——那是這種情況下唯一的憑證。

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
| `/start key threads` | **OWNER** / 授權管理員 | 對控制台已定義的目標發起壓測；可加 `nodes:` 指定節點、`proxy:true` 走代理池 |
| `/stepped key stages` | **OWNER** / 授權管理員 | 階梯壓測：逐級加壓找出容量拐點，例 `50,100,200,400`；同樣支援 `nodes:` 與 `proxy:true` |
| `/target add` | 授權管理員 | 提交目標，產生所有權驗證 token |
| `/target verify` | 授權管理員 | 驗證域名所有權 |
| `/target approve` | **OWNER** | 核准目標並設定併發／時長上限；`pending_verify` 狀態可直接核准（跳過驗證，記為 UNVERIFIED） |
| `/target suspend` | **OWNER** | 立即暫停目標 |
| `/target list` | 授權管理員 | 列出所有目標、授權狀態與驗證來源 |
| `/nodes` | 授權管理員 | 檢查節點連線狀態與有效容量 |
| `/loadtest` | 授權管理員 | 發起壓測（需 OWNER 按鈕核准） |
| `/loadtest override:true` | **OWNER** | 突破目標授權上限，受全域上限約束 |
| `/abort` | 授權管理員 | 中止執行中的壓測 |

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
| 網頁按「啟動」報權限不足 | 缺少 sudoers 規則 | 重跑 `setup-master.sh`，或檢查 `/etc/sudoers.d/loadtest-web` |
| Bot 顯示「啟動失敗」 | Token 錯誤或已被重置 | 看面板日誌，重新產生 Token 並貼上 |
| Bot 啟動後馬上停止 | `OWNER_ID` 為空或格式錯 | 填數字 ID，不是使用者名稱 |
| 部署卡在「安裝 JMeter」 | 官方 archive 鏡像只有 ~50 KB/s | 腳本已內建 dlcdn → 清華 → archive 三段回退，正常 1 分鐘內完成 |
| 壓測一直「執行中」、已送回 0 筆 | `java.rmi.server.hostname` 沒設成 JVM 屬性，master 對外宣告主機名（解析成 127.0.1.1），節點連不回 | 寫進 `/opt/jmeter/bin/system.properties` 後重跑；節點日誌會出現 `Connection refused to host: 127.0.1.1` |
| 同上，但日誌顯示公網 IP | 安全組沒放行 RMI 回傳埠 | 放行 master 的 `4000~4010/tcp`（JMeter 會連續佔用數個埠） |
| 壓測逾時被強制終止 | 上述兩者之一 | Bot 會在 `時長 + 180s` 後終止並提示；JMeter 日誌存在 `results/<目標>-<時間>-jmeter.log` || 部署長時間無輸出 | 正在下載 85 MB 的 JMeter | 每 30 秒會顯示「仍在執行」，可按中止（會一併殺掉遠端程序） |
| 節點顯示離線但部署成功 | 節點在 VPC 外，防火牆白名單寫成了內網 IP | 在 `.env` 設 `MASTER_IP=<公網IP>` 後重啟 `loadtest-web`，再重新部署節點 |

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

目標可以走兩條路核准，差別在於**有沒有留下可稽核的所有權證據**：

| 路徑 | 誰能做 | 證據 | 稽核記錄 |
|---|---|---|---|
| `/target verify` → `/target approve` | STAFF 驗證、OWNER 核准 | DNS TXT 或 HTTP 檔案 | `TARGET-VERIFY` + `TARGET-APPROVE` |
| `/target approve`（直接） | 僅 OWNER | 無 | `TARGET-APPROVE-UNVERIFIED` |

第二條路是刻意保留的：客戶常常還沒準備好改 DNS，或域名在客戶的雲廠商手上不好操作，OWNER 需要能先開工。但要知道代價——

- 客戶口頭聲稱擁有某域名，**不構成可驗證的授權**
- 走第二條路時，平台內沒有任何東西能證明那個域名是客戶的
- 唯一的責任憑證是你們與客戶簽的壓測委託書（SOW／授權書），出事時它必須拿得出來

所以實務上的分工是：**能驗證就驗證**（成本只是加一條 TXT 記錄），趕時間才由 OWNER 直接核准，並把 SOW 存檔對應到 `audit.log` 的那一行。

核准人、授權上限、授權期限仍然全部留在 `targets.json` 與 `audit.log`。**移除整個授權流程（讓任何人都能對任意域名壓測）才會退化為 stresser 工具**，那條線不要跨。

### 營運建議

1. **保留書面授權**：與客戶簽署的壓測委託書應與 `targets.json` 的授權記錄對應
2. **設定測試窗口**：避開客戶業務高峰，並提前告知可能的使用者影響
3. **限制節點對外暴露**：節點防火牆只放行主服務器 IP，切勿對公網開放 RMI 埠
4. **保護 `.env`**：Bot Token 洩漏等同他人可冒用你的 Bot 身份
5. **定期審閱稽核日誌**：`audit.log` 中出現非預期的 `DENY` 記錄代表有人在嘗試越權
6. **授權到期即失效**：`authorized_until` 過期後 `/loadtest` 會直接拒絕
7. **`WEB_TOKEN` 等同節點 root 權限**：控制台能對「新增的任意 IP」用主服務器的 SSH 金鑰或你填入的帳密部署軟體。持有權杖的人可以把任意可達主機加進白名單並在其上安裝 JMeter。因此 8080 埠務必限制來源，權杖視同 root 密碼管理，離職即輪換
8. **新增節點後確認 IP 正確**：填錯 IP 的後果是對一台無關機器執行 `setup-node.sh`，那台機器會被裝上 JMeter 並開啟 RMI 埠
9. **節點密碼的存在方式**：加密存放於 `config/credentials.json`（600、不進版控），金鑰由 `WEB_TOKEN` 派生。這防的是「檔案外流」，不防「主機被入侵」——入侵者能讀到 `.env` 就等於能解開所有節點密碼。因此節點帳號建議用專用帳號而非 root 的萬用密碼

### 已知限制

- 單一控制平面，無高可用機制
- 壓測計畫需預先放置於 `plans/`，尚不支援從 Discord 上傳
- 結果以 JMeter 原始 jtl 統計呈現，未做時序圖分析

---

## 授權

本專案為私有商業工具，未附帶開源授權條款。未經授權不得再散布或用於對未授權目標的壓力測試。

---

## 附屬工具

### `tools/loadtest-template.jmx`

用 JMeter GUI 直接打開就能跑。樹狀圖第一項就是「① 使用者定義的變數（改這裡）」，點它改這七個值：

| 變數 | 說明 | 預設 |
|---|---|---|
| `PROTOCOL` | `https` 或 `http` | https |
| `HOST` | 目標網域，不要含 `https://` 與路徑 | example.com |
| `PORT` | 埠號 | 443 |
| `PATH` | 要壓的路徑 | / |
| `THREADS` | 併發執行緒數 | 10 |
| `RAMP_SECONDS` | 爬坡秒數 | 10 |
| `DURATION_SECONDS` | 持續秒數 | 60 |

內含 HTTP 請求、標頭管理器、回應碼斷言（2xx/3xx）、彙總報告；
「結果樹」預設關閉，除錯時再打開。

打開方式：JMeter → 檔案 → 開啟，或把 `.jmx` 直接拖進 JMeter 視窗。

### `tools/jmeter-quick-test.bat`

不想開控制台、只想用手邊的 JMeter 快速壓一下時用的單檔腳本。Windows 雙擊即可。

- 自動尋找 JMeter（PATH、`%JMETER_HOME%`、`C://apache-jmeter-*`、`%USERPROFILE%\apache-jmeter-*`、`D://`），找不到會請你貼路徑
- 依序問：目標網址 → 併發數 → 持續秒數（Enter 用 60）
- 顯示摘要後要打 `Y` 才開始，避免手滑
- 自動產生計畫檔（GET、跟隨重導向、連線逾時 10s / 回應逾時 30s）
- 結果輸出到 `tools/runs/<時間>/`：`plan.jmx`、`result.jtl`、`report/index.html`（跑完自動開啟）

網址可以只寫 `example.com/path`，會自動補 `https://`；帶埠號 `http://host:8080/x` 也認得。
