#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: frp-tunnel.sh host:port name}"
NAME="${2:?usage: frp-tunnel.sh host:port name}"
FRP_VERSION="${FRP_VERSION:-0.70.1}"
FRP_SERVER="${FRP_SERVER:-agentsweb.space}"
FRP_SERVER_PORT="${FRP_SERVER_PORT:-17000}"
TOKEN_FILE="${FRP_TOKEN_FILE:-$HOME/.config/frp/token}"
STATE_DIR="${FRP_STATE_DIR:-$HOME/.local/share/agentsweb-frp}"

if [[ ! "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  echo "Invalid tunnel name: $NAME" >&2
  exit 2
fi
if [[ "$TARGET" == *:* ]]; then
  TARGET_HOST="${TARGET%:*}"
  TARGET_PORT="${TARGET##*:}"
else
  TARGET_HOST="127.0.0.1"
  TARGET_PORT="$TARGET"
fi
TOKEN="${FRP_AUTH_TOKEN:-}"
if [[ -z "$TOKEN" && -r "$TOKEN_FILE" ]]; then
  TOKEN="$(<"$TOKEN_FILE")"
fi
if [[ -z "$TOKEN" ]]; then
  echo "FRP_AUTH_TOKEN is unset and token file is missing: $TOKEN_FILE" >&2
  exit 1
fi

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) asset="linux_amd64" ;;
  Linux-aarch64|Linux-arm64) asset="linux_arm64" ;;
  Darwin-x86_64) asset="darwin_amd64" ;;
  Darwin-arm64) asset="darwin_arm64" ;;
  *) echo "Unsupported frpc platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

install_dir="$STATE_DIR/$FRP_VERSION-$asset"
binary="$install_dir/frpc"
mkdir -p "$install_dir" "$STATE_DIR/config" "$STATE_DIR/logs"
if [[ ! -x "$binary" ]]; then
  archive="$install_dir/frp.tar.gz"
  curl -fL --retry 3 -o "$archive" "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_${asset}.tar.gz"
  tar -xzf "$archive" -C "$install_dir" --strip-components=1
fi

config="$STATE_DIR/config/$NAME.toml"
log="$STATE_DIR/logs/$NAME.log"
custom_domains="\"$NAME.agentsweb.space\""
if [[ "$NAME" =~ ^(.+-worker-agents)-1456$ ]]; then
  custom_domains+=" , \"${BASH_REMATCH[1]}.agentsweb.space\""
fi
cat >"$config" <<EOF
serverAddr = "$FRP_SERVER"
serverPort = $FRP_SERVER_PORT
auth.method = "token"
auth.token = "$TOKEN"
transport.tls.enable = true
transport.tcpMux = true

[[proxies]]
name = "$NAME"
type = "http"
localIP = "$TARGET_HOST"
localPort = $TARGET_PORT
customDomains = [$custom_domains]
transport.useEncryption = false
transport.useCompression = false
EOF
chmod 600 "$config"
exec "$binary" -c "$config" >>"$log" 2>&1
