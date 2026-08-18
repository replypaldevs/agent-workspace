#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${APP_HOME:-$HOME/workerAgents}"
HERMES_WEBUI_HOME="${HERMES_WEBUI_HOME:-$HOME/hermes-webui}"
STATE_DIR="${STATE_DIR:-$HOME/.worker-agents}"
WORKER_ARCHIVE_PATH="${WORKER_ARCHIVE_PATH:-}"
WORKER_APP_SOURCE_DIR="${WORKER_APP_SOURCE_DIR:-}"
WORKER_AGENTS_GIT_URL="${WORKER_AGENTS_GIT_URL:-https://github.com/replypaldevs/workerAgents.git}"
WORKER_AGENTS_GIT_REF="${WORKER_AGENTS_GIT_REF:-main}"
TUNNEL_CLIENT_PATH="${TUNNEL_CLIENT_PATH:-/tmp/lolgames_tunnel}"
FRP_TUNNEL_CLIENT_PATH="${FRP_TUNNEL_CLIENT_PATH:-/tmp/scripts/frp-tunnel.sh}"
FRP_TOKEN_FILE="${FRP_TOKEN_FILE:-$HOME/.config/frp/token}"
LOLGAMES_TUNNEL_SERVER="${LOLGAMES_TUNNEL_SERVER:-agentsweb.space}"
HERMES_WEBUI_GIT_URL="${HERMES_WEBUI_GIT_URL:-https://github.com/nesquena/hermes-webui.git}"
APP_PORT="${APP_PORT:-1456}"
INSTALL_CHILD_DEPS="${INSTALL_CHILD_DEPS:-0}"
START_CHILD_AGENTS="${START_CHILD_AGENTS:-0}"
PROVISION_TRACE="${PROVISION_TRACE:-1}"
RUN_TOKEN="${RUN_TOKEN:-}"
TUNNEL_PREFIX="${LOLGAMES_TUNNEL_PREFIX:-$(hostname | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9-' )-$(date +%s)}"

trace() {
  [[ "$PROVISION_TRACE" == "1" ]] || return 0
  printf '[remote-trace][%s] %s\n' "$(date -u +%H:%M:%S)" "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

start_tunnel() {
  local name="$1"
  local port="$2"
  local probe_path="${3:-/}"
  local public_name="${TUNNEL_PREFIX}-${name}-${port}"
  local log_path="$HOME/${name}-lolgames.log"
  local public_host="$public_name"
  if [[ "$name" == "worker-agents" && "$port" == "1456" ]]; then
    public_host="${TUNNEL_PREFIX}-${name}"
  fi
  local public_url="https://${public_host}.agentsweb.space"
  if [[ "${IS_WINDOWS:-0}" != "1" && -x "$FRP_TUNNEL_CLIENT_PATH" ]]; then
    pkill -f "[f]rpc.*${public_name}" 2>/dev/null || true
    : > "$log_path"
    nohup "$FRP_TUNNEL_CLIENT_PATH" "127.0.0.1:${port}" "$public_name" >"$log_path" 2>&1 </dev/null &
    for _ in $(seq 1 45); do
      if curl -fsS --max-time 5 "${public_url}${probe_path}" >/dev/null 2>&1; then
        printf '%s\n' "$public_url"
        return 0
      fi
      sleep 1
    done
    echo "frp public probe failed for ${public_url}${probe_path}" >&2
    return 1
  fi
  echo "FRP is required for Worker Agents HTTP publishing on this platform" >&2
  return 1
}

read_boot_marker() {
  if [[ -r /proc/sys/kernel/random/boot_id ]]; then
    tr -d '\n' < /proc/sys/kernel/random/boot_id
  else
    hostname
  fi
}

sync_worker_app() {
  cd "$HOME"
  local target_app_home="$APP_HOME"
  if [[ "$IS_WINDOWS" == "1" ]]; then
    target_app_home="${APP_HOME}-$(date +%s)"
    rm -rf "$target_app_home"
  else
    rm -rf "$target_app_home"
    mkdir -p "$target_app_home"
  fi
  if [[ -n "$WORKER_ARCHIVE_PATH" && -f "$WORKER_ARCHIVE_PATH" ]]; then
    trace "extract workerAgents from archive $WORKER_ARCHIVE_PATH"
    if [[ "$IS_WINDOWS" == "1" ]]; then
      local stage_dir="$HOME/workerAgents-stage-$$"
      rm -rf "$stage_dir"
      mkdir -p "$stage_dir"
      tar -xzf "$WORKER_ARCHIVE_PATH" -C "$stage_dir"
      mv "$stage_dir/workerAgents" "$target_app_home"
      rm -rf "$stage_dir"
    else
      tar -xzf "$WORKER_ARCHIVE_PATH" -C "$HOME"
    fi
  elif [[ -n "$WORKER_APP_SOURCE_DIR" && -d "$WORKER_APP_SOURCE_DIR" ]]; then
    trace "copy workerAgents from source dir $WORKER_APP_SOURCE_DIR"
    mkdir -p "$target_app_home"
    cp -R "$WORKER_APP_SOURCE_DIR"/. "$target_app_home"/
  elif [[ -n "$WORKER_AGENTS_GIT_URL" ]]; then
    trace "clone workerAgents from $WORKER_AGENTS_GIT_URL"
    git clone --depth 1 --branch "$WORKER_AGENTS_GIT_REF" "$WORKER_AGENTS_GIT_URL" "$target_app_home"
  else
    echo "Need WORKER_ARCHIVE_PATH or WORKER_APP_SOURCE_DIR or WORKER_AGENTS_GIT_URL" >&2
    exit 1
  fi
  APP_HOME="$target_app_home"
}

configure_shell_env() {
  mkdir -p "$STATE_DIR" "$HOME/node-http2" "$HOME/.codex"
  python3 - <<'PY'
from pathlib import Path
path = Path.home() / '.codex' / 'config.toml'
existing = path.read_text(encoding='utf-8') if path.exists() else ''
lines = existing.splitlines()
globals_, rest, in_section = [], [], False
for line in lines:
    if line.lstrip().startswith('['):
        in_section = True
    (rest if in_section else globals_).append(line)
def set_global_line(key, value):
    line = f'{key} = {value}'
    for i, current in enumerate(globals_):
        if current.startswith(f'{key} = '):
            globals_[i] = line
            return
    globals_.append(line)

path.write_text('\n'.join([*globals_, *rest]).rstrip() + '\n', encoding='utf-8')
PY
}

write_state_and_emit() {
  local status_path="$STATE_DIR/status.json"
  trace "capture status + start tunnels"
  curl -fsS "http://127.0.0.1:${APP_PORT}/api/status" > "$status_path"
  local worker_agents_url=""
  worker_agents_url="$(start_tunnel worker-agents "$APP_PORT" '/api/status' || true)"

  python3 - "$status_path" "$worker_agents_url" "$APP_PORT" "$(hostname)" "$(read_boot_marker)" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
status_path, worker_agents_url, port, hostname, boot_marker = sys.argv[1:]
try:
    agents = {a.get('id'): a for a in json.load(open(status_path, encoding='utf-8')).get('agents', [])}
except Exception:
    agents = {}
# Child UIs share the Worker Agents hostname; derive each from its own port.
child_defaults = {'codex-web-local': None, 'opencode': None, 'hermes-webui': None, 'openclaw': 18789}
codex_url = opencode_url = hermes_url = openclaw_url = ''
if worker_agents_url:
    parsed = urlsplit(worker_agents_url)
    host = parsed.hostname or ''
    if host:
        for child_id in ('codex-web-local', 'opencode', 'hermes-webui', 'openclaw'):
            agent = agents.get(child_id) or {}
            if agent.get('state') != 'running':
                continue
            child_port = agent.get('port') or child_defaults.get(child_id)
            if child_port:
                suffix = '.agentsweb.space'
                label = host[:-len(suffix)] if host.endswith(suffix) else host
                base = label[:-len('-1456')] if label.endswith('-worker-agents-1456') else label
                child_host = f"{base}-{child_port}{suffix}" if host.endswith(suffix) else host
                child_url = urlunsplit(('https', child_host, '/', '', ''))
                if child_id == 'codex-web-local':
                    codex_url = child_url
                elif child_id == 'opencode':
                    opencode_url = child_url
                elif child_id == 'hermes-webui':
                    hermes_url = child_url
                else:
                    openclaw_url = child_url
state_dir = os.path.expanduser('~/.worker-agents')
state_path = os.path.join(state_dir, 'state.json')
try:
    state = json.load(open(state_path, encoding='utf-8'))
except Exception:
    state = {}
state.update({
    'status': 'running' if worker_agents_url else 'starting',
    'url': worker_agents_url,
    'worker_agents_url': worker_agents_url,
    'port': int(port),
    'lolgames_tunnel_prefix': os.environ.get('LOLGAMES_TUNNEL_PREFIX', ''),
    'ssh_host': os.environ.get('SSH_PUBLIC_HOST', ''),
    'ssh_port': os.environ.get('SSH_PUBLIC_PORT', ''),
    'hostname': hostname,
    'boot_marker': boot_marker,
    'codex_web_url': codex_url,
    'opencode_url': opencode_url,
    'hermes_webui_url': hermes_url,
    'openclaw_url': openclaw_url,
    'updated_at': datetime.now(timezone.utc).isoformat(),
})
os.makedirs(state_dir, exist_ok=True)
with open(state_path, 'w', encoding='utf-8') as f:
    json.dump(state, f)
    f.write('\n')
with open(os.path.join(state_dir, 'child-urls.env'), 'w', encoding='utf-8') as f:
    f.write(f"codex_url={codex_url}\n")
    f.write(f"opencode_url={opencode_url}\n")
    f.write(f"hermes_url={hermes_url}\n")
    f.write(f"openclaw_url={openclaw_url}\n")
PY

  # shellcheck disable=SC1090
  source "$STATE_DIR/child-urls.env" 2>/dev/null || true
  echo "__WORKER_AGENTS_DONE__${RUN_TOKEN}"
  echo "PUBLIC_URL=${worker_agents_url}"
  echo "CODEX_URL=${codex_url}"
  echo "OPENCODE_URL=${opencode_url}"
  echo "HERMES_URL=${hermes_url}"
  echo "OPENCLAW_URL=${openclaw_url}"
}

require_cmd npm
require_cmd python3
require_cmd curl
require_cmd tar
IS_WINDOWS=0
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
esac
if [[ "$IS_WINDOWS" != "1" ]]; then
  require_cmd tmux
fi
if [[ "$INSTALL_CHILD_DEPS" == "1" && "$START_CHILD_AGENTS" == "1" ]]; then
  if ! command -v timeout >/dev/null 2>&1; then
    require_cmd gtimeout
    timeout() { gtimeout "$@"; }
  fi
fi
if [[ "$IS_WINDOWS" == "1" ]]; then
  trace "stop existing Windows node processes before sync"
  taskkill.exe //F //IM node.exe //T >/dev/null 2>&1 || true
fi
trace "configure worker shell env"
configure_shell_env
sync_worker_app
cd "$APP_HOME"
trace "npm install workerAgents"
npm install
if [[ "$INSTALL_CHILD_DEPS" == "1" ]]; then
  trace "clone Hermes WebUI"
  rm -rf "$HERMES_WEBUI_HOME"
  git clone --depth 1 "$HERMES_WEBUI_GIT_URL" "$HERMES_WEBUI_HOME"
  trace "install child CLIs"
  npm install -g codexapp opencode-ai openclaw
fi
if [[ "$INSTALL_CHILD_DEPS" == "1" && "$START_CHILD_AGENTS" == "1" && ! -x "$HOME/.local/bin/hermes" ]]; then
  trace "bootstrap Hermes"
  timeout 180 python3 "$HERMES_WEBUI_HOME/bootstrap.py" --no-browser --foreground --host 127.0.0.1 18935 >/tmp/hermes-bootstrap.log 2>&1 || true
fi
if [[ "$IS_WINDOWS" == "1" ]]; then
  trace "start Worker Agents Windows process"
  START_CMD="$(cygpath -w "$HOME/start-workeragents.cmd")"
  APP_HOME_WIN="$(cygpath -w "$APP_HOME")"
  WORKER_LOG_WIN="$(cygpath -w "$HOME/worker-agents.log")"
  WORKER_ERR_LOG_WIN="$(cygpath -w "$HOME/worker-agents.err.log")"
  cat > "$HOME/start-workeragents.cmd" <<EOF
@echo off
set PATH=C:\Program Files\Git\mingw64\bin;C:\Program Files\Git\usr\bin;C:\Program Files\nodejs;%PATH%
if defined AGENT_AUTO_START_ALL set AGENT_AUTO_START_ALL=%AGENT_AUTO_START_ALL%
set PORT=${APP_PORT}
set AGENT_CONSOLE_HOST=127.0.0.1
set HERMES_WEBUI_DIR=$(cygpath -w "$HERMES_WEBUI_HOME")
cd /d ${APP_HOME_WIN}
npm start > ${WORKER_LOG_WIN} 2> ${WORKER_ERR_LOG_WIN}
EOF
  powershell.exe -NoProfile -Command "Start-Process -FilePath '${START_CMD}' -WindowStyle Hidden" >/dev/null
else
  trace "start Worker Agents tmux"
  TMUX='' tmux -L workeragents -f /dev/null kill-server 2>/dev/null || true
  TMUX='' tmux -L workeragents -f /dev/null new-session -d -s workeragents "cd \"$APP_HOME\" && PORT=${APP_PORT} AGENT_CONSOLE_HOST=127.0.0.1 HERMES_WEBUI_DIR=\"$HERMES_WEBUI_HOME\" FRP_TUNNEL_CLIENT_PATH=\"$FRP_TUNNEL_CLIENT_PATH\" FRP_TOKEN_FILE=\"$FRP_TOKEN_FILE\" FRP_AUTH_TOKEN=\"${FRP_AUTH_TOKEN:-}\" AGENT_TUNNEL_PREFIX=\"${TUNNEL_PREFIX}-worker-agents\"${AGENT_AUTO_START_ALL:+ AGENT_AUTO_START_ALL=\"$AGENT_AUTO_START_ALL\"} npm start > ~/worker-agents.log 2>&1"
fi
trace "wait for Worker Agents API"
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/api/status" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if [[ "$START_CHILD_AGENTS" == "1" ]]; then
  trace "start child agents"
  for agent_id in codex-web-local opencode hermes-webui openclaw; do
    curl -fsS -X POST "http://127.0.0.1:${APP_PORT}/api/agents/${agent_id}/restart" >/dev/null || true
    sleep 8
  done
fi
write_state_and_emit
