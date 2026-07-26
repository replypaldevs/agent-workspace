import EventEmitter from 'node:events';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { config, defaultPath, nowIso } from './config.js';
import { importCodexAuthForHermes, refreshTokenIfNeeded } from './auth.js';

const ANSI_ESCAPE = /\u001B\[[0-9;?]*[ -/]*[@-~]/g;
const browserHost = process.env.AGENT_BROWSER_HOST || '127.0.0.1';

function stripAnsi(line) {
  return String(line).replace(ANSI_ESCAPE, '');
}

function agentLogFileFor(agentId) {
  const template = process.env.AGENT_CONSOLE_AGENT_LOG;
  if (template) {
    return template.includes('{agentId}')
      ? template.replaceAll('{agentId}', agentId)
      : template;
  }
  return `/tmp/agent-console-agent-${agentId}.log`;
}

function commandFromEnv(envName, fallback) {
  return process.env[envName] || fallback;
}

function sh(command, options = {}) {
  return spawn('/bin/sh', ['-lc', command], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PATH: defaultPath, ...(options.env || {}) },
    ...options
  });
}

async function runCommand(command, options = {}) {
  const { onData, ...spawnOptions } = options;
  return new Promise((resolve, reject) => {
    const child = sh(command, spawnOptions);
    let stdout = '';
    let stderr = '';
    child.stdout?.on('data', (chunk) => { const s = typeof chunk === 'string' ? chunk : chunk.toString('utf8'); stdout += s; if (onData) onData(s); });
    child.stderr?.on('data', (chunk) => { const s = typeof chunk === 'string' ? chunk : chunk.toString('utf8'); stderr += s; if (onData) onData(s); });
    child.once('error', reject);
    child.once('exit', (code) => {
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        reject(new Error((stderr || stdout || `command exited ${code}`).trim()));
      }
    });
  });
}

function commandExists(command) {
  try {
    execSync(`command -v ${command}`, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], env: { ...process.env, PATH: defaultPath } });
    return true;
  } catch {
    return false;
  }
}

function applyPortTemplate(template, port) {
  return template.replaceAll('{port}', String(port));
}

function routerPort() {
  const value = Number.parseInt(process.env.WORKER_AGENTS_9ROUTER_PORT || '20127', 10);
  return Number.isFinite(value) ? value : 20127;
}

function routerBaseUrl() {
  return `http://127.0.0.1:${routerPort()}/v1`;
}

function routerApiKey() {
  return process.env.WORKER_AGENTS_9ROUTER_API_KEY || 'local-dev-key';
}

function routerDefaultModel() {
  return process.env.WORKER_AGENTS_9ROUTER_MODEL || 'openai/gpt-5.4-mini';
}

async function ensureGlobalPackage(commandName, packageName, log) {
  if (commandExists(commandName)) return false;
  await runCommand(`npm install -g ${packageName}`, { onData: log });
  return true;
}

async function ensureHermesWebUiRepo(log) {
  const hermesWebUiDir = process.env.HERMES_WEBUI_DIR || path.join(os.homedir(), 'hermes-webui');
  const repo = process.env.HERMES_WEBUI_GIT_URL || 'https://github.com/nesquena/hermes-webui.git';
  if (fs.existsSync(path.join(hermesWebUiDir, 'bootstrap.py'))) return { changed: false, dir: hermesWebUiDir };
  await runCommand(`rm -rf "${hermesWebUiDir}" && git clone --depth 1 "${repo}" "${hermesWebUiDir}"`, { onData: log });
  return { changed: true, dir: hermesWebUiDir };
}

async function ensureHermesInstalled(port, log) {
  const { changed, dir } = await ensureHermesWebUiRepo(log);
  const hasBootstrap = fs.existsSync(path.join(dir, 'bootstrap.py'));
  if (commandExists('hermes') && (commandExists('hermes-webui') || hasBootstrap)) {
    return changed;
  }
  if (hasBootstrap) {
    try {
      await runCommand('curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup --skip-browser --non-interactive', { onData: log });
    } catch {
      // bootstrap.py from the cloned repo is still a viable fallback launch path
    }
    return true;
  }
  throw new Error('Hermes WebUI repo is missing bootstrap.py');
}

function defaultHermesWebUiCommand(port) {
  const hermesWebUiDir = process.env.HERMES_WEBUI_DIR || path.join(os.homedir(), 'hermes-webui');
  if (fs.existsSync(path.join(hermesWebUiDir, 'bootstrap.py'))) {
    return `/bin/sh -lc 'cd "${hermesWebUiDir}" && exec python3 bootstrap.py --skip-agent-install --no-browser --foreground --host 0.0.0.0 ${port}'`;
  }
  const webui = `exec /usr/local/bin/hermes-webui --skip-agent-install --no-browser --foreground --host 0.0.0.0 ${port}`;
  const gatewayCheck = 'if [ -x /usr/local/lib/hermes-agent/venv/bin/python ]; then /usr/local/lib/hermes-agent/venv/bin/python -c "import sys; import gateway.status as s; sys.exit(0 if s.get_running_pid(cleanup_stale=False) else 1)"; else exit 0; fi';
  return [
    '/bin/sh -lc ',
    '\'',
    'if [ "${HERMES_WEBUI_START_GATEWAY:-1}" = "1" ] || [ "${HERMES_WEBUI_START_GATEWAY:-1}" = "true" ] || [ "${HERMES_WEBUI_START_GATEWAY:-1}" = "yes" ]; then ',
    `if ! ${gatewayCheck} >/dev/null 2>&1; then `,
    'gateway_log="${HERMES_WEBUI_GATEWAY_LOG:-${HERMES_HOME:-$HOME/.hermes}/gateway.log}"; ',
    'mkdir -p "$(dirname "$gateway_log")"; ',
    '/usr/local/bin/hermes gateway run >> "$gateway_log" 2>&1 & ',
    'fi; ',
    'fi; ',
    webui,
    '\''
  ].join('');
}


function normalizeReadyPatterns(patterns = []) {
  return patterns.map((pattern) => pattern instanceof RegExp ? pattern : new RegExp(String(pattern), 'i'));
}

function loadCustomWorkerDefinitions() {
  const filePath = process.env.WORKER_AGENTS_CONFIG || path.join(config.projectRoot, 'workers.json');
  if (!fs.existsSync(filePath)) return [];
  const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  if (!Array.isArray(parsed)) throw new Error(`${filePath} must contain a JSON array`);
  return parsed.map((worker) => {
    if (!worker?.id || !worker?.command) throw new Error('Each worker needs id and command');
    const basePort = Number.parseInt(worker.basePort ?? worker.port ?? 19000, 10);
    return {
      id: String(worker.id),
      name: String(worker.name || worker.id),
      basePort: Number.isFinite(basePort) ? basePort : 19000,
      path: worker.path || '/',
      readyPath: worker.readyPath,
      command: (port) => applyPortTemplate(String(worker.command), port),
      readyPatterns: normalizeReadyPatterns(worker.readyPatterns || ['listening', 'http://127.0.0.1:', 'http://localhost:']),
      env: () => buildBaseEnv(worker.env || {})
    };
  });
}

function readOpenClawToken() {
  try {
    const configPath = path.join(config.openClawHome, 'openclaw.json');
    const json = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    return json?.gateway?.auth?.token || '';
  } catch {
    return '';
  }
}

function readWorkerAgentsPublicUrl() {
  try {
    const statePath = path.join(os.homedir(), '.worker-agents', 'state.json');
    const json = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    return String(json?.worker_agents_url || json?.url || '').trim();
  } catch {
    return '';
  }
}

function deriveOpenClawAllowedOrigins(port) {
  const origins = [`http://127.0.0.1:${port}`, `http://localhost:${port}`];
  // If AGENT_CONSOLE_PUBLIC_URL is set, derive the lolgames origin from it
  const publicUrl = process.env.AGENT_CONSOLE_PUBLIC_URL || process.env.WORKER_AGENTS_URL || '';
  if (publicUrl) {
    try {
      const u = new URL(publicUrl);
      u.port = String(port);
      origins.push(u.origin);
    } catch {}
  }
  return origins;
}

function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, { mode: 0o600 });
}

function ensureCodexWebUi9RouterConfig() {
  fs.mkdirSync(config.codexHome, { recursive: true });
  const statePath = path.join(config.codexHome, 'webui-custom-providers.json');
  const state = {
    enabled: true,
    provider: 'custom',
    model: routerDefaultModel(),
    customBaseUrl: routerBaseUrl(),
    apiKey: routerApiKey(),
    customKey: true,
    wireApi: 'responses',
    providerKeys: {}
  };
  writeJson(statePath, state);
}

function ensureOpenClawConfig(port = 18789) {
  const configPath = path.join(config.openClawHome, 'openclaw.json');
  const existing = (() => {
    try {
      return JSON.parse(fs.readFileSync(configPath, 'utf8'));
    } catch {
      return {};
    }
  })();

  const routerProviderId = '9router';
  const routerModel = routerDefaultModel();
  const routerQualifiedModel = `${routerProviderId}/${routerModel}`;
  existing.models ||= {};
  existing.models.mode ||= 'merge';
  existing.models.providers ||= {};
  existing.models.providers[routerProviderId] = {
    ...(existing.models.providers[routerProviderId] || {}),
    baseUrl: routerBaseUrl(),
    apiKey: routerApiKey(),
    api: 'openai-responses',
    authHeader: true,
    models: [
      {
        id: routerModel,
        name: routerModel,
        api: 'openai-responses'
      }
    ]
  };
  existing.agents ||= {};
  existing.agents.defaults ||= {};
  existing.agents.defaults.model = { primary: routerQualifiedModel };
  existing.agents.defaults.models = { [routerQualifiedModel]: {} };
  existing.gateway ||= {};
  existing.gateway.mode ||= 'local';
  existing.gateway.trustedProxies = Array.from(new Set([
    ...(Array.isArray(existing.gateway.trustedProxies) ? existing.gateway.trustedProxies : []),
    '127.0.0.1/32',
    '::1/128'
  ]));
  existing.gateway.auth ||= {};
  existing.gateway.auth.mode ||= 'token';
  existing.gateway.auth.token ||= cryptoToken();
  existing.gateway.controlUi ||= {};
  existing.gateway.controlUi.allowedOrigins = Array.from(new Set([
    ...(Array.isArray(existing.gateway.controlUi.allowedOrigins) ? existing.gateway.controlUi.allowedOrigins : []),
    ...deriveOpenClawAllowedOrigins(port)
  ]));
  existing.gateway.controlUi.allowInsecureAuth = true;
  existing.gateway.controlUi.dangerouslyDisableDeviceAuth = true;
  existing.gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback = true;
  existing.update ||= {};
  existing.update.checkOnStart = false;
  writeJson(configPath, existing);
}

async function ensureOpenClawBaseline(log) {
  const workspaceDir = path.join(config.openClawHome, 'workspace');
  const sessionsDir = path.join(config.openClawHome, 'agents', 'main', 'sessions');
  if (fs.existsSync(path.join(config.openClawHome, 'openclaw.json')) && fs.existsSync(workspaceDir) && fs.existsSync(sessionsDir)) {
    return false;
  }
  await runCommand(
    `openclaw setup --baseline --non-interactive --accept-risk --skip-channels --skip-skills --skip-ui --skip-health --workspace ${JSON.stringify(workspaceDir)}`, { onData: log }
  );
  return true;
}
function ensureOpenClawPatch() {
  const targetPath = openClawPatchPath();
  if (fs.existsSync(targetPath)) return;
  const content = [
    'const __req = typeof require === "function"',
    '  ? require',
    '  : ((globalThis.process && typeof globalThis.process.getBuiltinModule === "function")',
    '      ? (id) => globalThis.process.getBuiltinModule(id)',
    '      : null);',
    'const os = __req ? __req("os") : null;',
    'const _ni = os && typeof os.networkInterfaces === "function" ? os.networkInterfaces : null;',
    'if (_ni) {',
    '  os.networkInterfaces = function() {',
    '    try { return _ni.call(this); } catch(e) {',
    '      return {',
    '        lo: [{',
    '          address: "127.0.0.1",',
    '          netmask: "255.0.0.0",',
    '          family: "IPv4",',
    '          mac: "00:00:00:00:00:00",',
    '          internal: true,',
    '          cidr: "127.0.0.1/8"',
    '        }]',
    '      };',
    '    }',
    '  };',
    '}',
    ''
  ].join('\n');
  fs.writeFileSync(targetPath, content, { mode: 0o644 });
}

function openClawPatchPath() {
  return path.join(os.homedir(), '.openclaw-patch.js');
}

function cryptoToken() {
  return crypto.randomBytes(24).toString('hex');
}

async function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => server.close(() => resolve(true)));
    server.listen(port, '127.0.0.1');
  });
}

async function findAvailablePort(basePort) {
  for (let offset = 0; offset < config.portScanRange; offset += 1) {
    const port = basePort + offset;
    if (await isPortFree(port)) return port;
  }
  return basePort;
}

async function waitForPort(port, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const connected = await new Promise((resolve) => {
      const socket = net.createConnection({ host: '127.0.0.1', port });
      socket.setTimeout(750);
      socket.once('connect', () => {
        socket.end();
        resolve(true);
      });
      socket.once('timeout', () => {
        socket.destroy();
        resolve(false);
      });
      socket.once('error', () => resolve(false));
    });
    if (connected) return true;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

async function waitForHttpReady(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3000);
      const res = await fetch(url, { signal: controller.signal });
      clearTimeout(timeout);
      if (res.status < 500) return true;
    } catch {
      // Connection error — keep polling
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return false;
}

async function isHttpReady(url) {
  let timeout;
  try {
    const controller = new AbortController();
    timeout = setTimeout(() => controller.abort(), 3000);
    const res = await fetch(url, { signal: controller.signal });
    return res.status < 500;
  } catch {
    return false;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}


function buildBaseEnv(extra = {}) {
  return {
    ...process.env,
    HOME: os.homedir(),
    PATH: defaultPath,
    CODEX_HOME: config.codexHome,
    OPENCLAW_HOME: config.openClawHome,
    HERMES_HOME: config.hermesHome,
    NODE_PATH: '/usr/local/lib/node_modules',
    LANG: process.env.LANG || 'C.UTF-8',
    ...extra
  };
}

const builtInDefinitions = [
  {
    id: 'codex-web-local',
    name: 'Codex Web Local',
    basePort: 18923,
    path: '/',
    command: (port) => applyPortTemplate(
      commandFromEnv(
        'AGENT_CMD_CODEX_WEB_LOCAL',
        'codexapp --port {port} --no-password --no-tunnel'
      ),
      port
    ),
    readyPatterns: [/http:\/\/(localhost|127\.0\.0\.1):/i, /listening/i],
    beforeStart: async (_port, log) => {
      await refreshTokenIfNeeded();
      await ensureGlobalPackage('codexapp', 'codexapp', log);
      ensureCodexWebUi9RouterConfig();
      ensureOpenClawPatch();
    },
    env: () => buildBaseEnv({
      CUSTOM_ENDPOINT_API_KEY: routerApiKey(),
      NODE_OPTIONS: `--require ${openClawPatchPath()} --unhandled-rejections=warn`
    })
  },
  {
    id: 'opencode',
    name: 'OpenCode',
    basePort: 18924,
    path: '/Lw/session',
    command: (port) => applyPortTemplate(
      commandFromEnv('AGENT_CMD_OPENCODE', 'opencode web --port {port} --hostname 127.0.0.1'),
      port
    ),
    readyPatterns: [/http:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0):/i, /listening/i],
    beforeStart: async (_port, log) => {
      await ensureGlobalPackage('opencode', 'opencode-ai', log);
    },
    env: () => buildBaseEnv({
      OPENAI_BASE_URL: routerBaseUrl(),
      OPENAI_API_KEY: routerApiKey()
    })
  },
  {
    id: 'hermes-webui',
    name: 'Hermes WebUI',
    basePort: 18935,
    path: '/',
    readyPath: '/health',
    command: (port) => applyPortTemplate(
      commandFromEnv(
        'AGENT_CMD_HERMES_WEBUI',
        defaultHermesWebUiCommand(port)
      ),
      port
    ),
    readyPatterns: [/\/health/i, /HTTP server/i, /http:\/\/(127\.0\.0\.1|0\.0\.0\.0):/i],
    beforeStart: async (_port, log) => {
      await refreshTokenIfNeeded();
      await ensureHermesInstalled(18935, log);
      importCodexAuthForHermes();
    },
    env: (port) => buildBaseEnv({
      HERMES_WEBUI_HOST: '0.0.0.0',
      HERMES_WEBUI_PORT: String(port),
      HERMES_WEBUI_SKIP_ONBOARDING: '1',
      HERMES_WEBUI_PRESERVE_ENV: '1',
      UV_LINK_MODE: 'copy'
    })
  },
  {
    id: 'openclaw',
    name: 'OpenClaw Gateway',
    basePort: 18789,
    path: '/',
    command: (port) => applyPortTemplate(
      commandFromEnv('AGENT_CMD_OPENCLAW', 'openclaw gateway run --port {port} --allow-unconfigured'),
      port
    ),
    readyPatterns: [/listening on/i, /gateway is ready/i],
    beforeStart: async (port, log) => {
      await refreshTokenIfNeeded();
      await ensureGlobalPackage('openclaw', 'openclaw', log);
      ensureOpenClawConfig(port);
      await ensureOpenClawBaseline(log);
      ensureOpenClawPatch();
    },
    env: () => buildBaseEnv({
      UV_USE_IO_URING: '0',
      PLAYWRIGHT_BROWSERS_PATH: '/root/.cache/ms-playwright',
      NODE_OPTIONS: `--require ${openClawPatchPath()}`,
      OPENAI_BASE_URL: routerBaseUrl(),
      OPENAI_API_KEY: routerApiKey(),
      OPENROUTER_API_KEY: process.env.OPENROUTER_API_KEY || '',
      ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY || '',
      BRAVE_API_KEY: process.env.BRAVE_API_KEY || ''
    }),
    url: (port) => {
      const token = readOpenClawToken();
      const suffix = token ? `?token=${encodeURIComponent(token)}` : '';
      return `http://${browserHost}:${port}/${suffix}`;
    }
  }
];

const definitions = [...builtInDefinitions, ...loadCustomWorkerDefinitions()];

class AgentRuntime {
  constructor(definition, notify) {
    this.definition = definition;
    this.notify = notify;
    this.state = 'stopped';
    this.logs = [];
    this.process = null;
    this.port = definition.basePort;
    this.pid = null;
    this.error = '';
    this.startedAt = '';
    this.command = '';
  }

  snapshot(includeLogs = true) {
    const url = this.definition.url
      ? this.definition.url(this.port)
      : `http://${browserHost}:${this.port}${this.definition.path}`;
    return {
      id: this.definition.id,
      name: this.definition.name,
      state: this.state,
      port: this.port,
      pid: this.pid,
      url,
      error: this.error,
      startedAt: this.startedAt,
      command: this.command,
      logs: includeLogs ? this.logs : undefined
    };
  }

  log(line) {
    const clean = stripAnsi(line).trimEnd();
    if (!clean) return;
    const formatted = `${new Date().toLocaleTimeString()} [${this.definition.id}] ${clean}`;
    this.logs.push(formatted);
    if (this.logs.length > config.logLimit) this.logs = this.logs.slice(-config.logLimit);
    try {
      fs.appendFileSync(agentLogFileFor(this.definition.id), `${formatted}\n`);
    } catch {
      // Keep the live UI working even if the diagnostic file cannot be written.
    }
    this.notify({ type: 'log', agentId: this.definition.id });
  }

  markRunning() {
    if (this.state !== 'starting' && this.state !== 'error') return;
    this.state = 'running';
    this.error = '';
    this.startedAt ||= nowIso();
    this.notify({ type: 'state', agentId: this.definition.id });
  }

  async waitForReady(child) {
    const portReady = await waitForPort(this.port, config.readyTimeoutMs);
    if (this.process !== child || this.state !== 'starting') return;
    if (!portReady) {
      this.state = 'error';
      this.error = `Timed out waiting for port ${this.port}`;
      this.log(this.error);
      this.notify({ type: 'state', agentId: this.definition.id });
      const path = this.definition.readyPath ?? this.definition.path ?? '/';
      this.recoverWhenReady(child, `http://127.0.0.1:${this.port}${path}`);
      return;
    }

    const path = this.definition.readyPath ?? this.definition.path ?? '/';
    const readyUrl = `http://127.0.0.1:${this.port}${path}`;
    this.log(`Waiting for HTTP readiness: ${readyUrl}`);
    const httpReady = await waitForHttpReady(readyUrl, config.readyTimeoutMs);
    if (this.process !== child || this.state !== 'starting') return;
    if (httpReady) {
      this.markRunning();
    } else {
      this.state = 'error';
      this.error = `Timed out waiting for HTTP readiness at ${readyUrl}`;
      this.log(this.error);
      this.notify({ type: 'state', agentId: this.definition.id });
      this.recoverWhenReady(child, readyUrl);
    }
  }

  async start() {
    if (this.state === 'running' || this.state === 'starting' || this.state === 'installing') return this.snapshot();
    this.state = 'installing';
    this.error = '';
    this.logs = [];
    this.startedAt = '';
    this.notify({ type: 'state', agentId: this.definition.id });

    try {
      this.port = await findAvailablePort(this.definition.basePort);
      await this.definition.beforeStart?.(this.port, (chunk) => {
        const lines = String(chunk).split(/\r?\n/);
        for (const line of lines) {
          if (line) this.log(line);
        }
      });
      this.state = 'starting';
      this.notify({ type: 'state', agentId: this.definition.id });
      this.command = this.definition.command(this.port);
      this.log(`Starting: ${this.command}`);
      const child = spawn(this.command, {
        shell: true,
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: this.definition.env?.(this.port) || buildBaseEnv()
      });
      this.process = child;
      this.pid = child.pid;

      this.pipeOutput(child, child.stdout);
      this.pipeOutput(child, child.stderr);

      child.once('error', (error) => {
        this.error = error.message;
        this.state = 'error';
        this.log(`Error: ${error.message}`);
        this.notify({ type: 'state', agentId: this.definition.id });
      });

      child.once('exit', (code, signal) => {
        const wasStopping = this.state === 'stopping';
        this.process = null;
        this.pid = null;
        this.state = wasStopping ? 'stopped' : code === 0 ? 'stopped' : 'error';
        this.error = this.state === 'error' ? `Process exited with code ${code ?? 'null'} signal ${signal ?? 'null'}` : '';
        this.log(`Process exited with code ${code ?? 'null'} signal ${signal ?? 'null'}`);
        this.notify({ type: 'state', agentId: this.definition.id });
      });

      this.waitForReady(child);
    } catch (error) {
      this.state = 'error';
      this.error = error.message;
      this.log(`Error: ${error.message}`);
      this.notify({ type: 'state', agentId: this.definition.id });
    }
    return this.snapshot();
  }

  pipeOutput(child, stream) {
    let buffer = '';
    stream.setEncoding('utf8');
    stream.on('data', (chunk) => {
      buffer += chunk;
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || '';
      lines.forEach((line) => {
        this.log(line);
        this.recoverIfReadyFromOutput(child, line);
      });
    });
    stream.on('end', () => {
      if (buffer) {
        this.log(buffer);
        this.recoverIfReadyFromOutput(child, buffer);
      }
    });
  }

  recoverIfReadyFromOutput(child, line) {
    if (this.process !== child || this.state !== 'error') return;
    const clean = stripAnsi(line);
    const hasReadyOutput = this.definition.readyPatterns?.some((pattern) => pattern.test(clean));
    if (!hasReadyOutput) return;
    const path = this.definition.readyPath ?? this.definition.path ?? '/';
    const readyUrl = `http://127.0.0.1:${this.port}${path}`;
    this.recoverWhenReady(child, readyUrl);
  }

  async recoverWhenReady(child, readyUrl) {
    const started = Date.now();
    while (this.process === child && this.state === 'error' && Date.now() - started < config.readyTimeoutMs) {
      if (await isHttpReady(readyUrl)) {
        if (this.process === child && this.state === 'error') {
          this.log(`Recovered after readiness check passed: ${readyUrl}`);
          this.markRunning();
        }
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }

  async stop() {
    if (!this.process || this.state === 'stopped') {
      this.state = 'stopped';
      this.notify({ type: 'state', agentId: this.definition.id });
      return this.snapshot();
    }
    const child = this.process;
    this.state = 'stopping';
    this.log('Stopping...');
    this.notify({ type: 'state', agentId: this.definition.id });

    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch {
      try {
        child.kill('SIGTERM');
      } catch {
        // Process may have already exited.
      }
    }

    await new Promise((resolve) => setTimeout(resolve, 1500));
    if (this.process === child) {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch {
        try {
          child.kill('SIGKILL');
        } catch {
          // Process may have already exited.
        }
      }
    }
    this.process = null;
    this.pid = null;
    this.state = 'stopped';
    this.notify({ type: 'state', agentId: this.definition.id });
    return this.snapshot();
  }
}

class AgentSupervisor extends EventEmitter {
  constructor() {
    super();
    this.agents = new Map(definitions.map((definition) => [
      definition.id,
      new AgentRuntime(definition, (event) => this.emit('change', event))
    ]));
  }

  snapshot() {
    return Array.from(this.agents.values()).map((agent) => agent.snapshot());
  }

  get(id) {
    const agent = this.agents.get(id);
    if (!agent) throw new Error(`Unknown agent: ${id}`);
    return agent;
  }

  async start(id) {
    return this.get(id).start();
  }

  async stop(id) {
    return this.get(id).stop();
  }

  async restart(id) {
    await this.stop(id);
    return this.start(id);
  }

  async stopAll() {
    await Promise.all(Array.from(this.agents.values()).map((agent) => agent.stop()));
  }
}

export const supervisor = new AgentSupervisor();
