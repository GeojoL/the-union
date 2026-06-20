#!/usr/bin/env bash
# machine-nodes · center-installer — 把本机装成集群【中心】(握手对端 + 节点登记 + ok/fail 聚合)。
# 零依赖(python3 stdlib)。幂等可重跑。支持 systemd(Linux)/ launchd(macOS)/ nohup 兜底。
# 用法:
#   ./center-installer.sh                 # 用仓库里的 center/center-agent.py
#   CENTER_PORT=8770 ./center-installer.sh
#   curl -fsSL <raw>/center-installer.sh | bash    # 远程(会先拉 center-agent.py)
set -euo pipefail
PORT="${CENTER_PORT:-8770}"
HOME_DIR="${CENTER_HOME:-$HOME/machine-nodes-center}"
RAW="${MN_RAW:-https://raw.githubusercontent.com/GeojoL/machine-nodes/main}"
BIN="$HOME/.local/bin"; mkdir -p "$BIN" "$HOME_DIR"
say(){ printf '\033[36m[center-installer]\033[0m %s\n' "$*"; }

command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }

# 1) 取 center-agent.py(本地仓库优先,否则远程拉)
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
if [ -f "$SRC_DIR/center/center-agent.py" ]; then
  cp "$SRC_DIR/center/center-agent.py" "$BIN/mn-center-agent.py"; say "用本地 center-agent.py"
else
  say "远程拉 center-agent.py"; curl -fsSL "$RAW/center/center-agent.py" -o "$BIN/mn-center-agent.py"
fi
chmod +x "$BIN/mn-center-agent.py"

OS="$(uname -s)"
# 2) 起服务(三选一,按平台)
start_systemd(){
  local unit="$HOME/.config/systemd/user/machine-nodes-center.service"
  mkdir -p "$(dirname "$unit")"
  cat > "$unit" <<EOF
[Unit]
Description=machine-nodes center-agent (handshake hub)
After=network.target
[Service]
Environment=CENTER_PORT=$PORT
Environment=CENTER_HOME=$HOME_DIR
ExecStart=$(command -v python3) $BIN/mn-center-agent.py
Restart=always
RestartSec=3
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now machine-nodes-center.service
  say "systemd user service machine-nodes-center 已起(Restart=always)"
}
start_launchd(){
  local pl="$HOME/Library/LaunchAgents/com.geojol.machine-nodes-center.plist"
  mkdir -p "$(dirname "$pl")"
  cat > "$pl" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.geojol.machine-nodes-center</string>
  <key>ProgramArguments</key><array><string>$(command -v python3)</string><string>$BIN/mn-center-agent.py</string></array>
  <key>EnvironmentVariables</key><dict><key>CENTER_PORT</key><string>$PORT</string><key>CENTER_HOME</key><string>$HOME_DIR</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
EOF
  launchctl unload "$pl" 2>/dev/null || true
  launchctl load "$pl"
  say "launchd agent machine-nodes-center 已起(KeepAlive)"
}
start_nohup(){
  pkill -f "mn-center-agent.py" 2>/dev/null || true; sleep 0.5
  CENTER_PORT=$PORT CENTER_HOME=$HOME_DIR setsid nohup python3 "$BIN/mn-center-agent.py" \
    >> "$HOME_DIR/center.out" 2>&1 < /dev/null & disown 2>/dev/null || true
  say "nohup 兜底起(无 systemd/launchd 持久化)"
}
case "$OS" in
  Linux)  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then start_systemd; else start_nohup; fi ;;
  Darwin) start_launchd ;;
  *)      start_nohup ;;
esac

# 3) 验证
sleep 2
if curl -fsS -m5 "http://127.0.0.1:$PORT/hs/status" >/dev/null 2>&1; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; [ -z "${IP:-}" ] && IP="$(ipconfig getifaddr en0 2>/dev/null || hostname)"
  say "✅ center 上线:http://$IP:$PORT  (节点装机用 CENTER=http://$IP:$PORT)"
  say "看健康: curl -s http://127.0.0.1:$PORT/  |  停: 见 docs/INSTALL.md"
else
  echo "[center-installer] ⚠ 起后 /hs/status 不通,查 $HOME_DIR/center.out"; exit 1
fi
