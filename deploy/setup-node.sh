#!/usr/bin/env bash
# 壓測產生節點一鍵部署 · Ubuntu 22.04 / 24.04
# 用法：sudo ./setup-node.sh <master_ip> [node_ip] [node_name]
#   例：sudo ./setup-node.sh 10.0.1.10 10.0.1.21 hk-01
set -euo pipefail

MASTER_IP="${1:?用法: setup-node.sh <master_ip> [node_ip] [node_name]}"
NODE_IP="${2:-$(hostname -I | awk '{print $1}')}"
NODE_NAME="${3:-$(hostname -s)}"
JMETER_VER="5.6.3"
JMETER_HOME="/opt/jmeter"
RMI_PORT="1099"
RMI_LOCAL_PORT="4000"
HEAP="${JMETER_HEAP:--Xms1g -Xmx4g}"

c_ok()   { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m[ .. ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { c_err "請用 sudo 執行"; exit 1; }

echo "==============================================="
echo " 壓測平台 · 產生節點部署"
echo " 節點名稱 : ${NODE_NAME}"
echo " 節點 IP  : ${NODE_IP}"
echo " 主服務器 : ${MASTER_IP}"
echo "==============================================="

# ---------------------------------------------------------------- 1. 系統依賴
c_info "安裝系統套件"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq openjdk-17-jre-headless curl tar ufw >/dev/null
c_ok "Java $(java -version 2>&1 | head -1 | awk -F'"' '{print $2}')"

# ---------------------------------------------------------------- 2. JMeter
if [ -x "${JMETER_HOME}/bin/jmeter" ]; then
  INSTALLED_VER="$("${JMETER_HOME}/bin/jmeter" -v 2>/dev/null | grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+' | head -1 || echo unknown)"
  if [ "${INSTALLED_VER}" = "${JMETER_VER}" ]; then
    c_ok "JMeter ${JMETER_VER} 已存在，跳過安裝"
  else
    c_warn "偵測到 JMeter ${INSTALLED_VER}，與要求版本 ${JMETER_VER} 不符，重新安裝"
    rm -rf "${JMETER_HOME}"
  fi
fi

if [ ! -x "${JMETER_HOME}/bin/jmeter" ]; then
  c_info "安裝 JMeter ${JMETER_VER}"

  # 清掉先前中斷留下的下載：兩個 curl 同時寫同一檔案會互相覆寫，永遠下不完
  pkill -f "apache-jmeter-${JMETER_VER}.tgz" 2>/dev/null || true
  sleep 1

  TMP_TGZ="$(mktemp /tmp/jmeter-XXXXXX.tgz)"
  MIRRORS=(
    "https://dlcdn.apache.org/jmeter/binaries/apache-jmeter-${JMETER_VER}.tgz"
    "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VER}.tgz"
    "https://mirrors.tuna.tsinghua.edu.cn/apache/jmeter/binaries/apache-jmeter-${JMETER_VER}.tgz"
  )

  DOWNLOADED=0
  for url in "${MIRRORS[@]}"; do
    c_info "下載 ${url#https://}（約 85 MB，每 5 秒回報進度）"
    : > "${TMP_TGZ}"
    curl -fsSL --connect-timeout 10 --max-time 900 --retry 2 --retry-delay 2 \
      -o "${TMP_TGZ}" "${url}" &
    CURL_PID=$!
    while kill -0 "${CURL_PID}" 2>/dev/null; do
      sleep 5
      kill -0 "${CURL_PID}" 2>/dev/null || break
      printf '      已下載 %s\n' "$(du -h "${TMP_TGZ}" 2>/dev/null | cut -f1)"
    done
    wait "${CURL_PID}" 2>/dev/null || true

    if tar -tzf "${TMP_TGZ}" >/dev/null 2>&1; then
      c_ok "下載完成（$(du -h "${TMP_TGZ}" | cut -f1)）"
      DOWNLOADED=1
      break
    fi
    c_warn "此鏡像不可用，換下一個"
    : > "${TMP_TGZ}"
  done

  if [ "${DOWNLOADED}" -ne 1 ]; then
    rm -f "${TMP_TGZ}"
    c_err "JMeter 下載失敗：請檢查節點對外網路，或手動放到 ${JMETER_HOME}"
    exit 1
  fi

  tar -xzf "${TMP_TGZ}" -C /opt
  rm -rf "${JMETER_HOME}"
  mv "/opt/apache-jmeter-${JMETER_VER}" "${JMETER_HOME}"
  rm -f "${TMP_TGZ}"
  c_ok "JMeter ${JMETER_VER} 安裝完成"
fi

# ---------------------------------------------------------------- 3. RMI 設定
PROP="${JMETER_HOME}/bin/jmeter.properties"
set_prop() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$PROP"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$PROP"
  elif grep -q "^#\?${key}=" "$PROP"; then
    sed -i "s|^#\?${key}=.*|${key}=${val}|" "$PROP"
  else
    echo "${key}=${val}" >> "$PROP"
  fi
}
set_prop "server.rmi.ssl.disable" "true"
set_prop "server.rmi.localport" "${RMI_LOCAL_PORT}"
set_prop "server_port" "${RMI_PORT}"
set_prop "java.rmi.server.hostname" "${NODE_IP}"
c_ok "RMI 設定完成（SSL 關閉、資料埠 ${RMI_LOCAL_PORT}、綁定 ${NODE_IP}）"

# ---------------------------------------------------------------- 4. 服務帳號
id -u jmeter >/dev/null 2>&1 || useradd -r -m -s /bin/false jmeter
chown -R jmeter:jmeter "${JMETER_HOME}"
mkdir -p /var/log/jmeter && chown jmeter:jmeter /var/log/jmeter

# ---------------------------------------------------------------- 5. systemd
c_info "安裝 systemd 服務"
cat > /etc/systemd/system/jmeter-server.service <<EOF
[Unit]
Description=Apache JMeter Server (load generator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=jmeter
WorkingDirectory=${JMETER_HOME}/bin
Environment="JVM_ARGS=${HEAP} -XX:+UseG1GC"
ExecStart=${JMETER_HOME}/bin/jmeter-server \\
  -Dserver.rmi.ssl.disable=true \\
  -Dserver.rmi.localport=${RMI_LOCAL_PORT} \\
  -Djava.rmi.server.hostname=${NODE_IP}
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable jmeter-server >/dev/null
systemctl restart jmeter-server
c_ok "服務已註冊並啟動（jmeter-server）"

# ---------------------------------------------------------------- 6. 防火牆
c_info "設定防火牆（僅允許主服務器連入）"
ufw allow from "${MASTER_IP}" to any port "${RMI_PORT}" proto tcp comment 'jmeter rmi' >/dev/null 2>&1 || true
ufw allow from "${MASTER_IP}" to any port "${RMI_LOCAL_PORT}" proto tcp comment 'jmeter rmi data' >/dev/null 2>&1 || true
c_ok "已放行 ${MASTER_IP} → ${RMI_PORT}, ${RMI_LOCAL_PORT}"

# ---------------------------------------------------------------- 7. 核心參數
cat > /etc/sysctl.d/99-jmeter.conf <<EOF
net.ipv4.ip_local_port_range = 10240 65000
net.ipv4.tcp_tw_reuse = 1
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
fs.file-max = 200000
EOF
sysctl --system >/dev/null
c_ok "核心網路參數已調整"

# ---------------------------------------------------------------- 8. 驗證
sleep 3
if ! systemctl is-active --quiet jmeter-server; then
  c_err "jmeter-server 啟動失敗"
  echo "--- 最近日誌 ---"
  journalctl -u jmeter-server -n 30 --no-pager || true
  exit 1
fi

if ss -lnt 2>/dev/null | grep -q ":${RMI_PORT}"; then
  c_ok "監聽中：${NODE_IP}:${RMI_PORT}"
else
  c_warn "未偵測到 ${RMI_PORT} 埠監聽，請檢查 journalctl -u jmeter-server -n 50"
fi

cat <<EOF

===============================================
 節點部署完成
===============================================
 把下面這段貼進主服務器的 config/nodes.json 的 nodes 區塊：

    "${NODE_NAME}": {
      "host": "${NODE_IP}",
      "port": ${RMI_PORT},
      "region": "TODO",
      "max_threads": 600,
      "enabled": true,
      "note": "$(nproc) vCPU / $(free -g | awk '/^Mem:/{print $2}')G"
    },

 提醒：
   - max_threads 請依硬體調整，建議每個 vCPU 對應 100~150 執行緒
   - 改完 nodes.json 後在主服務器執行 sudo systemctl restart loadtest-bot
   - 主服務器可用 /nodes 指令確認節點是否在線

 診斷指令：
   systemctl status jmeter-server
   journalctl -u jmeter-server -n 50
   ss -lnt | grep -E '${RMI_PORT}|${RMI_LOCAL_PORT}'
EOF
