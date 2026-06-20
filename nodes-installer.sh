#!/usr/bin/env bash
# machine-nodes · nodes-installer — 在【节点机】上一键装:握手 agent + 随机命名人格 + 加入 center 握手。
# 零依赖(python3 stdlib)。只需对 center 出站 HTTP(不要求 center 能 ssh 进来)。幂等可重跑。
# 用法(在节点机上跑):
#   curl -fsSL <raw>/nodes-installer.sh | CENTER=http://<center-ip>:8770 bash
#   或:  CENTER=http://192.168.50.50:8770 ./nodes-installer.sh
# 可选 env: NODE=<节点名,默认hostname>  PERSONA=<指定人格,默认随机>  HS_INTERVAL=60  HS_DURATION=43200
set -euo pipefail
CENTER="${CENTER:-}"; [ -n "$CENTER" ] || { echo "需要 CENTER 环境变量,如 CENTER=http://192.168.50.50:8770"; exit 2; }
CENTER="${CENTER%/}"
NODE="${NODE:-$(hostname | cut -d. -f1)}"
INTERVAL="${HS_INTERVAL:-60}"; DURATION="${HS_DURATION:-43200}"
RAW="${MN_RAW:-https://raw.githubusercontent.com/GeojoL/machine-nodes/main}"
BIN="$HOME/.local/bin"; NHOME="$HOME/machine-nodes-node"; mkdir -p "$BIN" "$NHOME"
say(){ printf '\033[35m[nodes-installer]\033[0m %s\n' "$*"; }
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }

# 1) 取 node-agent.py(本地仓库优先,否则远程)
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
if [ -f "$SRC_DIR/node/node-agent.py" ]; then cp "$SRC_DIR/node/node-agent.py" "$BIN/mn-node-agent.py"; say "用本地 node-agent.py"
else say "远程拉 node-agent.py"; curl -fsSL "$RAW/node/node-agent.py" -o "$BIN/mn-node-agent.py"; fi
chmod +x "$BIN/mn-node-agent.py"

# 2) 探活 center
curl -fsS -m6 "$CENTER/hs/status" >/dev/null 2>&1 || { echo "连不上 center $CENTER —— 查中心是否在跑、端口/防火墙"; exit 1; }
say "center 可达: $CENTER"

# 3) 随机命名人格(带唯一性:撞 center 已有名就重摇)
PERSONA="${PERSONA:-}"
if [ -z "$PERSONA" ]; then
  ADJ=(swift calm bright bold quiet keen lucid amber jade onyx coral slate vivid noble brisk); NOUN=(otter lynx heron raven ibis koi crane fox marten egret tern vole stoat finch wren)
  EXIST="$(curl -fsS -m6 "$CENTER/hs/status" 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ".join(v.get("persona","") for v in d.values()))' 2>/dev/null || echo)"
  for _ in 1 2 3 4 5 6 7 8; do
    r=$(( (RANDOM) % ${#ADJ[@]} )); s=$(( (RANDOM) % ${#NOUN[@]} )); h=$(printf '%02x' $((RANDOM%256)))
    cand="${ADJ[$r]}-${NOUN[$s]}-$h"
    case " $EXIST " in *" $cand "*) continue;; *) PERSONA="$cand"; break;; esac
  done
  PERSONA="${PERSONA:-node-$(date +%s | tail -c5)}"
fi
say "人格(随机命名)= $PERSONA   节点 = $NODE"
# 人格身份文件(供后续起 claude 会话用;本 installer 只建身份+登记,起活会话是可选的)
cat > "$NHOME/persona.txt" <<EOF
persona=$PERSONA
node=$NODE
center=$CENTER
machine=$(uname -s) $(uname -m)
created=$(date '+%Y-%m-%dT%H:%M:%S')
role=machine-nodes 节点级人格(随机命名);职责=本机节点 agent + 与 center 读确认握手
EOF

# 4) 起 node-agent 服务(systemd / launchd / nohup)
OS="$(uname -s)"; PY="$(command -v python3)"
start_systemd(){
  local unit="$HOME/.config/systemd/user/machine-nodes-node.service"; mkdir -p "$(dirname "$unit")"
  cat > "$unit" <<EOF
[Unit]
Description=machine-nodes node-agent ($PERSONA)
After=network.target
[Service]
Environment=CENTER=$CENTER
Environment=NODE=$NODE
Environment=PERSONA=$PERSONA
Environment=HS_INTERVAL=$INTERVAL
Environment=HS_DURATION=$DURATION
ExecStart=$PY $BIN/mn-node-agent.py
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload; systemctl --user enable --now machine-nodes-node.service
  say "systemd user service machine-nodes-node 已起"
}
start_launchd(){
  local pl="$HOME/Library/LaunchAgents/com.geojol.machine-nodes-node.plist"; mkdir -p "$(dirname "$pl")"
  cat > "$pl" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>Label</key><string>com.geojol.machine-nodes-node</string>
 <key>ProgramArguments</key><array><string>$PY</string><string>$BIN/mn-node-agent.py</string></array>
 <key>EnvironmentVariables</key><dict>
   <key>CENTER</key><string>$CENTER</string><key>NODE</key><string>$NODE</string>
   <key>PERSONA</key><string>$PERSONA</string><key>HS_INTERVAL</key><string>$INTERVAL</string><key>HS_DURATION</key><string>$DURATION</string></dict>
 <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
EOF
  launchctl unload "$pl" 2>/dev/null || true; launchctl load "$pl"
  say "launchd agent machine-nodes-node 已起(macOS)"
}
start_nohup(){
  pkill -f "mn-node-agent.py" 2>/dev/null || true; sleep 0.5
  CENTER=$CENTER NODE=$NODE PERSONA=$PERSONA HS_INTERVAL=$INTERVAL HS_DURATION=$DURATION \
    setsid nohup "$PY" "$BIN/mn-node-agent.py" >> "$NHOME/node.out" 2>&1 < /dev/null & disown 2>/dev/null || true
  say "nohup 兜底起"
}
case "$OS" in
  Linux)  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then start_systemd; else start_nohup; fi ;;
  Darwin) start_launchd ;;
  *)      start_nohup ;;
esac

# 5) 验证已登记 + 开始握手
sleep 4
if curl -fsS -m6 "$CENTER/hs/status" 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);exit(0 if '$NODE' in d else 1)" 2>/dev/null; then
  say "✅ 节点 $NODE(人格 $PERSONA)已登记 + 握手已开始。"
  say "本机看: cat $NHOME/handshake.stat   |  中心看: curl -s $CENTER/ "
else
  echo "[nodes-installer] ⚠ 起后未在 center 看到登记,查 $NHOME/handshake.log"; exit 1
fi
