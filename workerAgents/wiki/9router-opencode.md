# 9Router and OpenCode notes

Worker Agents can supervise 9Router as a local provider dashboard and OpenCode as an agent UI.

Default local ports:

- 9Router provider dashboard: `http://127.0.0.1:20127/dashboard/providers`
- 9Router OpenAI-compatible API: `http://127.0.0.1:20127/v1`

## Worker Agents launch notes

- Current 9Router runs from the repo root, not from an old `/opt/9router/.next/standalone` working directory.
- If `.next/standalone/server.js` is missing, build first with `npm run build`.
- Start the current standalone build with:

```bash
node .next/standalone/server.js
```

- On macOS, 9Router listener detection needs an `lsof`/`netstat` fallback; Linux-only `ss` checks can incorrectly report “not running”.
- OpenCode worker preset: starts near port `18924`

Quick probe:

```sh
curl -sS http://127.0.0.1:20127/v1/models
```

If your 9Router binary uses a different command, override it when starting Worker Agents:

```sh
AGENT_CMD_OPENCLAW='openclaw gateway run --port {port}' npm start
```
