#!/usr/bin/env bash
# 壓測節點自註冊腳本
#
# 用途：在雲端新開的機器上執行，安裝 Java + JMeter Server、設定 RMI 回傳位址、
#       註冊成系統服務，最後回報給壓測平台登記為節點。
#
# 由 /api/hooks/ecs/node.sh 動態填入 master 位址與權杖，不需手動改參數：
#   curl -fsSL "http://<master>:8080/api/hooks/ecs/node.sh?token=<token>" | bash
set -euo pipefail

MASTER_IP="__MASTER_IP__"
HOOK_TOKEN="__HOOK_TOKEN__"
MASTER_PORT="${MASTER_PORT:-8080}"
JMETER_VER="${JMETER_VER:-5.6.3}"
JMETER_HOME="/opt/jmeter"
LOG="/var/log/lt-join.log"

exec > >(tee -a "$LOG") 2>&1
echo "=== 壓測節點自註冊 $(date -u '+%F %T') UTC ==="

# ---------------------------------------------------------------- 1. 公網 IP
PUBLIC_IP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
if [ -z "$PUBLIC_IP" ]; then
  PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [ -z "$PUBLIC_IP" ]; then
  echo "無法取得公網 IP，中止"; exit 1
fi
echo "公網 IP: $PUBLIC_IP"

# ---------------------------------------------------------------- 2. Java
if ! command -v java >/dev/null 2>&1; then
  echo "安裝 Java..."
  export DEBIAN_FRONTEND=noninteractive
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq openjdk-17-jre-headless curl
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y java-17-openjdk-headless curl
  elif command -v yum >/dev/null 2>&1; then
    yum install -y java-17-openjdk-headless curl
  else
    echo "找不到可用的套件管理器，請手動安裝 JRE 17"; exit 1
  fi
fi

# ---------------------------------------------------------------- 3. JMeter
if [ ! -x "$JMETER_HOME/bin/jmeter" ]; then
  echo "下載 JMeter ${JMETER_VER}..."
  TMP="$(mktemp /tmp/jmeter-XXXXXX.tgz)"
  OK=0
  for URL in \
    "https://dlcdn.apache.org/jmeter/binaries/apache-jmeter-${JMETER_VER}.tgz" \
    "https://mirrors.tuna.tsinghua.edu.cn/apache/jmeter/binaries/apache-jmeter-${JMETER_VER}.tgz" \
    "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VER}.tgz"
  do
    echo "  嘗試 $URL"
    if curl -fsSL --connect-timeout 10 --max-time 900 -o "$TMP" "$URL"; then
      OK=1; break
    fi
  done
  if [ "$OK" != "1" ]; then
    echo "JMeter 下載失敗（三個鏡像都不通）"; rm -f "$TMP"; exit 1
  fi
  mkdir -p /opt
  tar -xzf "$TMP" -C /opt
  rm -f "$TMP"
  rm -rf "$JMETER_HOME"
  mv "/opt/apache-jmeter-${JMETER_VER}" "$JMETER_HOME"
fi

# ---------------------------------------------------------------- 4. RMI 設定
# 必須是節點的對外 IP，否則 master 收到的是內網位址，分散式會卡住
sed -i '/^java\.rmi\.server\.hostname/d' "$JMETER_HOME/bin/system.properties"
echo "java.rmi.server.hostname=${PUBLIC_IP}" >> "$JMETER_HOME/bin/system.properties"
grep -q '^server\.rmi\.ssl\.disable=true' "$JMETER_HOME/bin/jmeter.properties" 2>/dev/null \
  || echo 'server.rmi.ssl.disable=true' >> "$JMETER_HOME/bin/jmeter.properties"

# ---------------------------------------------------------------- 5. 防火牆
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 1099/tcp >/dev/null 2>&1 || true
  ufw allow 4000:4010/tcp >/dev/null 2>&1 || true
  echo "已放行 ufw 1099 / 4000-4010"
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=1099/tcp >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-port=4000-4010/tcp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  echo "已放行 firewalld 1099 / 4000-4010"
fi

# ---------------------------------------------------------------- 6. 系統服務
cat > /etc/systemd/system/jmeter-server.service <<UNIT
[Unit]
Description=Apache JMeter Server (load generator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${JMETER_HOME}/bin
ExecStart=${JMETER_HOME}/bin/jmeter-server -Dserver.rmi.ssl.disable=true -Dserver.rmi.localport=4000 -Djava.rmi.server.hostname=${PUBLIC_IP}
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable jmeter-server >/dev/null 2>&1 || true
systemctl restart jmeter-server
sleep 6
if ! systemctl is-active --quiet jmeter-server; then
  echo "jmeter-server 啟動失敗，最後 20 行日誌："
  journalctl -u jmeter-server -n 20 --no-pager || true
  exit 1
fi
echo "jmeter-server 已啟動"

# ---------------------------------------------------------------- 7. 回報上線
CORES="$(nproc 2>/dev/null || echo 2)"
THREADS=$(( CORES * 150 ))
[ "$THREADS" -gt 600 ] && THREADS=600
[ "$THREADS" -lt 100 ] && THREADS=100

echo "回報壓測平台 ${MASTER_IP}:${MASTER_PORT}（執行緒上限 ${THREADS}）"
curl -fsS -X POST "http://${MASTER_IP}:${MASTER_PORT}/api/hooks/join" \
  -H "X-LT-Token: ${HOOK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"host\":\"${PUBLIC_IP}\",\"name\":\"$(hostname)\",\"max_threads\":${THREADS}}"
echo
echo "=== 完成，節點已上線 ==="
