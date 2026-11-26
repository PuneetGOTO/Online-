#!/usr/bin/env bash
# 從主服務器批次部署所有節點（透過 SSH）
# 用法：./deploy-nodes.sh [--all] [--dry-run]
#   --all       連同 nodes.json 中 enabled=false 的節點一起部署
#   --dry-run   只顯示將要執行的動作，不實際連線
# 前置：主服務器已能用金鑰免密登入各節點（ssh-copy-id）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
NODES_FILE="${PROJECT_DIR}/config/nodes.json"
SSH_USER="${SSH_USER:-root}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o BatchMode=yes)
INCLUDE_DISABLED=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --all) INCLUDE_DISABLED=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "未知參數: $arg"; exit 1 ;;
  esac
done

c_ok()   { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m[ .. ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*"; }

[ -f "$NODES_FILE" ] || { c_err "找不到 $NODES_FILE"; exit 1; }
[ -f "$SCRIPT_DIR/setup-node.sh" ] || { c_err "找不到 setup-node.sh"; exit 1; }

MASTER_IP="${MASTER_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
[ -n "$MASTER_IP" ] || { c_err "無法判斷本機 IP，請指定：MASTER_IP=10.0.1.10 ./deploy-nodes.sh"; exit 1; }
echo "==============================================="
echo " 批次部署節點"
echo " 主服務器 IP : ${MASTER_IP}"
echo " SSH 使用者  : ${SSH_USER}"
echo " 節點清單    : ${NODES_FILE}"
echo "==============================================="

mapfile -t ENTRIES < <(python3 - "$NODES_FILE" "$INCLUDE_DISABLED" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
include_disabled = sys.argv[2] == "1"
for key, cfg in data.get("nodes", {}).items():
    if not include_disabled and not cfg.get("enabled", True):
        continue
    print(f'{key}\t{cfg["host"]}\t{cfg.get("enabled", True)}')
PY
)

if [ "${#ENTRIES[@]}" -eq 0 ]; then
  c_warn "nodes.json 中沒有符合條件的節點"
  exit 0
fi

OK=0; FAIL=0; SKIP=0
for entry in "${ENTRIES[@]}"; do
  IFS=$'\t' read -r NAME HOST ENABLED <<< "$entry"
  echo ""
  echo "-----------------------------------------------"
  echo " 節點 ${NAME} (${HOST})"
  echo "-----------------------------------------------"

  if [ "$DRY_RUN" -eq 1 ]; then
    c_info "[dry-run] 將執行：scp setup-node.sh → ssh ${SSH_USER}@${HOST} 'bash setup-node.sh ${MASTER_IP} ${HOST} ${NAME}'"
    continue
  fi

  if ! ssh "${SSH_OPTS[@]}" "${SSH_USER}@${HOST}" true 2>/dev/null; then
    c_err "SSH 連線失敗（檢查金鑰、防火牆、sshd）"
    FAIL=$((FAIL + 1))
    continue
  fi

  if ! scp "${SSH_OPTS[@]}" -q "${SCRIPT_DIR}/setup-node.sh" \
        "${SSH_USER}@${HOST}:/tmp/setup-node.sh"; then
    c_err "複製腳本失敗"
    FAIL=$((FAIL + 1))
    continue
  fi

  if ssh "${SSH_OPTS[@]}" -t "${SSH_USER}@${HOST}" \
       "chmod +x /tmp/setup-node.sh && sudo bash /tmp/setup-node.sh ${MASTER_IP} ${HOST} ${NAME}"; then
    c_ok "節點 ${NAME} 部署完成"
    OK=$((OK + 1))
  else
    c_err "節點 ${NAME} 部署失敗"
    FAIL=$((FAIL + 1))
  fi
done

echo ""
echo "==============================================="
echo " 結果：成功 ${OK} · 失敗 ${FAIL}"
echo "==============================================="
[ "$FAIL" -eq 0 ] || exit 1

if [ "$DRY_RUN" -eq 0 ]; then
  cat <<EOF

 全部節點就緒。接著在主服務器驗證連線：

   sudo systemctl restart loadtest-bot
   Discord 執行 /nodes

 若有節點顯示離線，常見原因：
   1. 節點防火牆未放行本機 IP 的 1099 / 4000 埠
   2. 節點的 java.rmi.server.hostname 設定成 127.0.0.1
   3. 兩端 JMeter 版本不一致
EOF
fi
