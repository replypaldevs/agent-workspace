# Worker Agents

A generic local control plane for launching and supervising agent UIs and worker processes.

Worker Agents was forked from the Hermes-on-Android console and stripped down to the reusable Node.js worker supervisor. It is no longer an Android project and does not include APK, Gradle, fastlane, Play Store, rootfs, Shizuku, or device-build workflows.

## What it does

- Starts named agent/workers from one web dashboard.
- Shows status, PID, port, URL, errors, and recent logs.
- Restarts workers without remembering long shell commands.
- Keeps built-in presets for Codex Web Local, OpenCode, OpenClaw, 9Router, and Hermes WebUI when those tools are installed.
- Lets you add arbitrary workers with environment variables or `workers.json`.

## Quick start

```bash
npm install
npm start
```

Open the console at:

```text
http://127.0.0.1:1456
```

Override the console port if needed:

```bash
PORT=3000 npm start
```

## Add your own workers

Create `workers.json` in the project root:

```json
[
  {
    "id": "my-agent",
    "name": "My Agent",
    "basePort": 19050,
    "path": "/",
    "command": "my-agent-web --host 127.0.0.1 --port {port}",
    "readyPatterns": ["listening", "http://127.0.0.1:"]
  }
]
```

Then restart the console. `{port}` is replaced with an available port starting at `basePort`.

You can also override built-in commands with environment variables:

```bash
AGENT_CMD_OPENCODE='opencode web --hostname 127.0.0.1 --port {port}' npm start
AGENT_CMD_OPENCLAW='openclaw gateway run --port {port}' npm start
AGENT_CMD_CODEX_WEB_LOCAL='codexui --port {port} --no-password --no-tunnel' npm start
AGENT_CMD_HERMES_WEBUI='hermes-webui --host 0.0.0.0 {port}' npm start
```

OpenClaw is exposed through the console proxy at `/proxy/openclaw/` so the browser talks to the Worker Agents origin while the upstream gateway sees `localhost`.

## Project structure

```text
.
├── src/              # Node.js control plane and worker supervisor
├── public/           # Browser UI
├── scripts/          # Small local helper scripts
├── wiki/             # Operational notes
├── workers.json      # Optional local worker definitions, ignored by git
└── README.md
```

## Notes

This repository is intentionally generic. If a worker command exists on the machine, Worker Agents can supervise it. Android-specific project files and Android build instructions were removed from this fork.
