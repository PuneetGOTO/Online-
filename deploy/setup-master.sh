#!/usr/bin/env bash
# 主服務器（控制平面）一鍵部署 · Ubuntu 22.04 / 24.04
# 用法：sudo ./setup-master.sh [本機對外IP]
#   在專案根目錄或 deploy/ 目錄下執行皆可
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/loadtest-platform"
SERVICE_USER="loadtest"
JMETER_VER="5.6.3"
JMETER_HOME="/opt/jmeter"
RMI_PORT="1099"
RMI_LOCAL_PORT="4000"
SELF_IP="${1:-$(hostname -I | awk '{print $1}')}"

c_ok()   { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m[ .. ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { c_err "請用 sudo 執行"; exit 1; }
[ -f "${PROJECT_DIR}/bot.py" ] || { c_err "找不到 bot.py，請在專案目錄內執行"; exit 1; }

echo "==============================================="
echo " 壓測平台 · 主服務器部署"
echo " 專案來源 : ${PROJECT_DIR}"
echo " 安裝位置 : ${INSTALL_DIR}"
echo " 本機 IP  : ${SELF_IP}"
echo "==============================================="

# ---------------------------------------------------------------- 1. 系統依賴
c_info "安裝系統套件"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  openjdk-17-jre-headless curl tar ufw rsync >/dev/null
c_ok "Python $(python3 -V 2>&1 | awk '{print $2}') / Java $(java -version 2>&1 | head -1 | awk -F'"' '{print $2}')"

# ---------------------------------------------------------------- 2. JMeter（master 端作為 client）
if [ ! -x "${JMETER_HOME}/bin/jmeter" ]; then
  c_info "安裝 JMeter ${JMETER_VER}（master 與節點版本必須一致）"
  curl -fsSL -o /tmp/jmeter.tgz \
    "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VER}.tgz"
  tar -xzf /tmp/jmeter.tgz -C /opt
  rm -rf "${JMETER_HOME}"
  mv "/opt/apache-jmeter-${JMETER_VER}" "${JMETER_HOME}"
  rm -f /tmp/jmeter.tgz
else
  c_ok "JMeter 已存在，跳過安裝"
fi

PROP="${JMETER_HOME}/bin/jmeter.properties"
sed -i 's/^#\?server\.rmi\.ssl\.disable=.*/server.rmi.ssl.disable=true/' "$PROP"
grep -q '^server.rmi.ssl.disable' "$PROP" || echo 'server.rmi.ssl.disable=true' >> "$PROP"
sed -i "s/^#\?java\.rmi\.server\.hostname=.*/java.rmi.server.hostname=${SELF_IP}/" "$PROP"
grep -q '^java.rmi.server.hostname' "$PROP" || echo "java.rmi.server.hostname=${SELF_IP}" >> "$PROP"
c_ok "已設定 server.rmi.ssl.disable=true"

# ---------------------------------------------------------------- 3. 服務帳號與檔案
id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd -r -m -s /bin/bash "${SERVICE_USER}"
mkdir -p "${INSTALL_DIR}"

c_info "同步專案檔案到 ${INSTALL_DIR}"
tar -C "${PROJECT_DIR}" \
    --exclude=.venv --exclude=__pycache__ --exclude=results \
    --exclude=audit.log --exclude=.env --exclude=.git \
    -czf - . | tar -C "${INSTALL_DIR}" -xzf -

mkdir -p "${INSTALL_DIR}/results" "${INSTALL_DIR}/logs"
if [ -f "${PROJECT_DIR}/.env" ] && [ ! -f "${INSTALL_DIR}/.env" ]; then
  install -m 600 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${PROJECT_DIR}/.env" "${INSTALL_DIR}/.env"
  c_ok "已沿用既有 .env"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
c_ok "檔案就緒"

# ---------------------------------------------------------------- 4. Python 環境
c_info "建立虛擬環境並安裝依賴"
if [ ! -x "${INSTALL_DIR}/.venv/bin/python" ]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
"${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/web/requirements.txt"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.venv"
c_ok "依賴安裝完成（discord.py / aiohttp / dnspython / fastapi / uvicorn）"

# ---------------------------------------------------------------- 5. .env
if [ ! -f "${INSTALL_DIR}/.env" ]; then
  install -m 600 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  sed -i "s|^JMETER_BIN=.*|JMETER_BIN=${JMETER_HOME}/bin/jmeter|" "${INSTALL_DIR}/.env"
  c_warn ".env 已建立但尚未填寫，請編輯後重啟服務："
  c_warn "  sudo nano ${INSTALL_DIR}/.env"
  NEEDS_ENV=1
else
  sed -i "s|^JMETER_BIN=.*|JMETER_BIN=${JMETER_HOME}/bin/jmeter|" "${INSTALL_DIR}/.env"
  NEEDS_ENV=0
fi

# ---------------------------------------------------------------- 5.5 Web 控制台前置
WEB_PORT="${WEB_PORT:-8080}"
LHOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
mkdir -p "${LHOME}/.ssh"
chmod 700 "${LHOME}/.ssh"

# Web 控制台以 loadtest 身分執行 ssh，沿用 root 的節點金鑰
KEY_SRC=""
for k in id_ed25519 id_rsa; do
  if [ -f "/root/.ssh/${k}" ]; then KEY_SRC="/root/.ssh/${k}"; break; fi
done
if [ -n "${KEY_SRC}" ]; then
  cp -f "${KEY_SRC}" "${LHOME}/.ssh/" 2>/dev/null || true
  cp -f "${KEY_SRC}.pub" "${LHOME}/.ssh/" 2>/dev/null || true
  c_ok "已複製 SSH 金鑰給 ${SERVICE_USER}（Web 部署節點需要）"
else
  c_warn "root 沒有 SSH 金鑰，Web 控制台將無法部署節點。請執行："
  c_warn "  ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519 && ssh-copy-id root@<節點IP>"
fi
if [ -f /root/.ssh/known_hosts ]; then
  cp -f /root/.ssh/known_hosts "${LHOME}/.ssh/" 2>/dev/null || true
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${LHOME}/.ssh"
chmod 600 "${LHOME}/.ssh"/id_* 2>/dev/null || true
chmod 644 "${LHOME}/.ssh"/*.pub 2>/dev/null || true

# 產生 Web 存取權杖（已存在則沿用）
if ! grep -q '^WEB_TOKEN=.\+' "${INSTALL_DIR}/.env" 2>/dev/null; then
  WEB_TOKEN_VAL="$(openssl rand -hex 16 2>/dev/null \
    || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if grep -q '^WEB_TOKEN=' "${INSTALL_DIR}/.env" 2>/dev/null; then
    sed -i "s|^WEB_TOKEN=.*|WEB_TOKEN=${WEB_TOKEN_VAL}|" "${INSTALL_DIR}/.env"
  else
    echo "WEB_TOKEN=${WEB_TOKEN_VAL}" >> "${INSTALL_DIR}/.env"
  fi
  c_ok "已生成 Web 存取權杖"
fi
WEB_TOKEN_SHOW="$(grep '^WEB_TOKEN=' "${INSTALL_DIR}/.env" | head -1 | cut -d= -f2-)"

# ---------------------------------------------------------------- 6. systemd
c_info "安裝 systemd 服務"
cat > /etc/systemd/system/loadtest-bot.service <<EOF
[Unit]
Description=Load Test Platform · Discord Control Plane
After=network-online.target
Wants=network-online.target
# Token 錯誤時不要無限重啟：5 分鐘內失敗 5 次就停下
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PYTHONUNBUFFERED=1"
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/bot.py
Restart=on-failure
RestartSec=10
StandardOutput=append:${INSTALL_DIR}/logs/bot.log
StandardError=append:${INSTALL_DIR}/logs/bot.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable loadtest-bot >/dev/null
c_ok "服務已註冊（loadtest-bot）"

cat > /etc/systemd/system/loadtest-web.service <<EOF
[Unit]
Description=Load Test Platform · Node Console (Web)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="WEB_PORT=${WEB_PORT}"
Environment="SSH_USER=${SSH_USER:-root}"
ExecStart=${INSTALL_DIR}/.venv/bin/python -m uvicorn web.app:app --host 0.0.0.0 --port ${WEB_PORT}
Restart=always
RestartSec=5
StandardOutput=append:${INSTALL_DIR}/logs/web.log
StandardError=append:${INSTALL_DIR}/logs/web.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable loadtest-web >/dev/null
c_ok "服務已註冊（loadtest-web）"

# ---------------------------------------------------------------- 6.5 Web 啟停 Bot 的權限
SYSTEMCTL_BIN="$(command -v systemctl || echo /usr/bin/systemctl)"
cat > /etc/sudoers.d/loadtest-web <<EOF
# 允許 Web 控制台啟停 Discord Bot（僅限這三個動作，其餘一律拒絕）
${SERVICE_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} start loadtest-bot, ${SYSTEMCTL_BIN} stop loadtest-bot, ${SYSTEMCTL_BIN} restart loadtest-bot
EOF
chmod 440 /etc/sudoers.d/loadtest-web
if visudo -c -f /etc/sudoers.d/loadtest-web >/dev/null 2>&1; then
  c_ok "已授權 Web 控制台啟停 loadtest-bot"
else
  rm -f /etc/sudoers.d/loadtest-web
  c_warn "sudoers 規則語法錯誤，已移除（Web 控制台將無法啟停 Bot）"
fi

# ---------------------------------------------------------------- 7. 防火牆
ufw allow "${RMI_PORT}/tcp" comment 'jmeter master rmi' >/dev/null 2>&1 || true
ufw allow "${RMI_LOCAL_PORT}/tcp" comment 'jmeter master rmi data' >/dev/null 2>&1 || true
ufw allow "${WEB_PORT}/tcp" comment 'loadtest web console' >/dev/null 2>&1 || true
c_warn "已放行 ${WEB_PORT}/tcp。控制台可部署伺服器，建議限制來源："
c_warn "  sudo ufw delete allow ${WEB_PORT}/tcp"
c_warn "  sudo ufw allow from <你的辦公IP> to any port ${WEB_PORT} proto tcp"

# ---------------------------------------------------------------- 8. 啟動
echo ""
if [ "${NEEDS_ENV}" -eq 1 ] || ! grep -q '^DISCORD_TOKEN=.\+' "${INSTALL_DIR}/.env"; then
  c_warn "尚未設定 DISCORD_TOKEN，暫不啟動。完成 .env 後執行："
  c_warn "  sudo systemctl start loadtest-bot && sudo systemctl status loadtest-bot"
else
  systemctl restart loadtest-bot
  sleep 3
  if systemctl is-active --quiet loadtest-bot; then
    c_ok "機器人已啟動"
    echo ""
    echo "--- 最近日誌 ---"
    tail -n 12 "${INSTALL_DIR}/logs/bot.log" || true
  else
    c_err "啟動失敗，請看：journalctl -u loadtest-bot -n 50"
  fi
fi

# Web 控制台不依賴 Discord，直接啟動
systemctl restart loadtest-web
sleep 2
if systemctl is-active --quiet loadtest-web; then
  c_ok "Web 控制台已啟動：http://${SELF_IP}:${WEB_PORT}"
else
  c_err "Web 控制台啟動失敗，請看：journalctl -u loadtest-web -n 50"
fi

cat <<EOF

===============================================
 主服務器部署完成
===============================================
 安裝路徑 : ${INSTALL_DIR}
 設定檔   : ${INSTALL_DIR}/.env
 目標清單 : ${INSTALL_DIR}/config/targets.json
 節點清單 : ${INSTALL_DIR}/config/nodes.json
 稽核日誌 : ${INSTALL_DIR}/audit.log

 Web 控制台 : http://${SELF_IP}:${WEB_PORT}
 存取權杖   : ${WEB_TOKEN_SHOW}
 （也可在 .env 的 WEB_TOKEN 修改）

 常用指令：
   sudo systemctl status loadtest-web
   sudo systemctl restart loadtest-web
   tail -f ${INSTALL_DIR}/logs/web.log
   sudo systemctl status loadtest-bot

 下一步：
   1. 填寫 .env（DISCORD_TOKEN / OWNER_ID / STAFF_IDS / GUILD_ID）
   2. 開啟 Web 控制台，按「一鍵部署全部節點」
   3. 節點上線後即可在控制台看到在線狀態
EOF
