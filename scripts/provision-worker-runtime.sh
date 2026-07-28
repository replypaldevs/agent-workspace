#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${APP_HOME:-$HOME/workerAgents}"
HERMES_WEBUI_HOME="${HERMES_WEBUI_HOME:-$HOME/hermes-webui}"
STATE_DIR="${STATE_DIR:-$HOME/.worker-agents}"
WORKER_ARCHIVE_PATH="${WORKER_ARCHIVE_PATH:-}"
WORKER_APP_SOURCE_DIR="${WORKER_APP_SOURCE_DIR:-}"
WORKER_AGENTS_GIT_URL="${WORKER_AGENTS_GIT_URL:-}"
TUNNEL_CLIENT_PATH="${TUNNEL_CLIENT_PATH:-/tmp/lolgames_tunnel.py}"
HERMES_WEBUI_GIT_URL="${HERMES_WEBUI_GIT_URL:-https://github.com/nesquena/hermes-webui.git}"
APP_PORT="${APP_PORT:-1456}"
INSTALL_CHILD_DEPS="${INSTALL_CHILD_DEPS:-0}"
START_CHILD_AGENTS="${START_CHILD_AGENTS:-0}"
PUBLISH_CHILD_AGENT_PORTS="${PUBLISH_CHILD_AGENT_PORTS:-0}"
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
  local public_name="${TUNNEL_PREFIX}-${name}"
  local log_path="$HOME/${name}-lolgames.log"
  pkill -f "lolgames_tunnel.py client 127.0.0.1:${port} --server 161.153.109.33 --name ${public_name} --same-port" 2>/dev/null || true
  nohup python3 "$TUNNEL_CLIENT_PATH" client "127.0.0.1:${port}" --server 161.153.109.33 --name "$public_name" --same-port > "$log_path" 2>&1 &
  printf 'http://%s.lolgames.net:%s\n' "$public_name" "$port"
}

sync_worker_app() {
  rm -rf "$APP_HOME"
  mkdir -p "$APP_HOME"
  if [[ -n "$WORKER_ARCHIVE_PATH" && -f "$WORKER_ARCHIVE_PATH" ]]; then
    trace "extract workerAgents from archive $WORKER_ARCHIVE_PATH"
    tar -xzf "$WORKER_ARCHIVE_PATH" -C "$HOME"
  elif [[ -n "$WORKER_APP_SOURCE_DIR" && -d "$WORKER_APP_SOURCE_DIR" ]]; then
    trace "copy workerAgents from source dir $WORKER_APP_SOURCE_DIR"
    cp -R "$WORKER_APP_SOURCE_DIR"/. "$APP_HOME"/
  elif [[ -n "$WORKER_AGENTS_GIT_URL" ]]; then
    trace "clone workerAgents from $WORKER_AGENTS_GIT_URL"
    git clone --depth 1 "$WORKER_AGENTS_GIT_URL" "$APP_HOME"
  else
    echo "Need WORKER_ARCHIVE_PATH or WORKER_APP_SOURCE_DIR or WORKER_AGENTS_GIT_URL" >&2
    exit 1
  fi
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
  local codex_url=""
  local opencode_url=""
  local hermes_url=""
  local openclaw_url=""
  worker_agents_url="$(start_tunnel worker-agents "$APP_PORT" || true)"
  if [[ "$PUBLISH_CHILD_AGENT_PORTS" == "1" ]]; then
    python3 - "$status_path" <<'PY' > "$STATE_DIR/ports.env"
import json, sys
agents = {a.get('id'): a for a in json.load(open(sys.argv[1])).get('agents', [])}
for key, env in [('codex-web-local', 'CODEX_PORT'), ('opencode', 'OPENCODE_PORT'), ('hermes-webui', 'HERMES_PORT'), ('openclaw', 'OPENCLAW_PORT')]:
    agent = agents.get(key) or {}
    print(f"{env}={agent.get('port') if agent.get('state') == 'running' else ''}")
PY
    # shellcheck disable=SC1090
    source "$STATE_DIR/ports.env"
    [[ -n "${CODEX_PORT:-}" ]] && codex_url="$(start_tunnel codex-web-local "$CODEX_PORT" || true)"
    [[ -n "${OPENCODE_PORT:-}" ]] && opencode_url="$(start_tunnel opencode "$OPENCODE_PORT" || true)"
    [[ -n "${HERMES_PORT:-}" ]] && hermes_url="$(start_tunnel hermes-webui "$HERMES_PORT" || true)"
    [[ -n "${OPENCLAW_PORT:-}" ]] && openclaw_url="$(start_tunnel openclaw "$OPENCLAW_PORT" || true)"
  fi

  python3 - "$status_path" "$worker_agents_url" "$APP_PORT" "$codex_url" "$opencode_url" "$hermes_url" "$openclaw_url" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
status_path, worker_agents_url, port, codex_url, opencode_url, hermes_url, openclaw_url = sys.argv[1:]
try:
    agents = {a.get('id'): a for a in json.load(open(status_path, encoding='utf-8')).get('agents', [])}
except Exception:
    agents = {}
if worker_agents_url and not openclaw_url and (agents.get('openclaw') or {}).get('state') == 'running':
    parsed = urlsplit(worker_agents_url)
    openclaw_url = urlunsplit((parsed.scheme, parsed.netloc, '/proxy/openclaw/', '', ''))
state = {
    'status': 'running' if worker_agents_url else 'starting',
    'url': worker_agents_url,
    'worker_agents_url': worker_agents_url,
    'port': int(port),
    'lolgames_tunnel_prefix': os.environ.get('LOLGAMES_TUNNEL_PREFIX', ''),
    'ssh_host': os.environ.get('SSH_PUBLIC_HOST', ''),
    'ssh_port': os.environ.get('SSH_PUBLIC_PORT', ''),
    'codex_web_url': codex_url,
    'opencode_url': opencode_url,
    'hermes_webui_url': hermes_url,
    'openclaw_url': openclaw_url,
    'updated_at': datetime.now(timezone.utc).isoformat(),
}
state_dir = os.path.expanduser('~/.worker-agents')
os.makedirs(state_dir, exist_ok=True)
with open(os.path.join(state_dir, 'state.json'), 'w', encoding='utf-8') as f:
    json.dump(state, f)
    f.write('\n')
PY

  echo "__WORKER_AGENTS_DONE__${RUN_TOKEN}"
  echo "PUBLIC_URL=${worker_agents_url}"
  echo "CODEX_URL=${codex_url}"
  echo "OPENCODE_URL=${opencode_url}"
  echo "HERMES_URL=${hermes_url}"
  echo "OPENCLAW_URL=${openclaw_url}"
}

require_cmd npm
require_cmd python3
require_cmd tmux
require_cmd curl
require_cmd timeout
require_cmd tar
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
trace "start Worker Agents tmux"
TMUX='' tmux -L workeragents -f /dev/null kill-server 2>/dev/null || true
TMUX='' tmux -L workeragents -f /dev/null new-session -d -s workeragents "cd \"$APP_HOME\" && PORT=${APP_PORT} AGENT_CONSOLE_HOST=127.0.0.1 HERMES_WEBUI_DIR=\"$HERMES_WEBUI_HOME\" npm start > ~/worker-agents.log 2>&1"
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
