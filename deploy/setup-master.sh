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
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.venv"
c_ok "依賴安裝完成（discord.py / aiohttp / dnspython）"

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

# ---------------------------------------------------------------- 6. systemd
c_info "安裝 systemd 服務"
cat > /etc/systemd/system/loadtest-bot.service <<EOF
[Unit]
Description=Load Test Platform · Discord Control Plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PYTHONUNBUFFERED=1"
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/bot.py
Restart=always
RestartSec=5
StandardOutput=append:${INSTALL_DIR}/logs/bot.log
StandardError=append:${INSTALL_DIR}/logs/bot.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable loadtest-bot >/dev/null
c_ok "服務已註冊（loadtest-bot）"

# ---------------------------------------------------------------- 7. 防火牆
ufw allow "${RMI_PORT}/tcp" comment 'jmeter master rmi' >/dev/null 2>&1 || true
ufw allow "${RMI_LOCAL_PORT}/tcp" comment 'jmeter master rmi data' >/dev/null 2>&1 || true

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

cat <<EOF

===============================================
 主服務器部署完成
===============================================
 安裝路徑 : ${INSTALL_DIR}
 設定檔   : ${INSTALL_DIR}/.env
 目標清單 : ${INSTALL_DIR}/config/targets.json
 節點清單 : ${INSTALL_DIR}/config/nodes.json
 稽核日誌 : ${INSTALL_DIR}/audit.log

 常用指令：
   sudo systemctl status loadtest-bot
   sudo systemctl restart loadtest-bot
   tail -f ${INSTALL_DIR}/logs/bot.log

 下一步：
   1. 填寫 .env（DISCORD_TOKEN / OWNER_ID / STAFF_IDS / GUILD_ID）
   2. 部署節點：sudo ./deploy-nodes.sh
   3. 把節點寫進 config/nodes.json 後重啟 bot
EOF
