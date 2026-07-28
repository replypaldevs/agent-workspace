import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { spawn } from 'node:child_process';
import { defaultPath } from './config.js';

const ROUTER_GIT_URL = process.env.ROUTER_GIT_URL || 'https://github.com/decolua/9router.git';
const ROUTER_HOME = process.env.WORKER_AGENTS_9ROUTER_DIR || path.join(process.env.HOME || '/tmp', '9router');
const ROUTER_LOG_PATH = '/tmp/9router.log';
const ROUTER_PORT = Number.parseInt(process.env.WORKER_AGENTS_9ROUTER_PORT || '20127', 10);
const ROUTER_API_KEY = process.env.WORKER_AGENTS_9ROUTER_API_KEY || 'local-dev-key';
const ROUTER_MODEL = process.env.WORKER_AGENTS_9ROUTER_MODEL || 'openai/gpt-5.4-mini';
const HEALTH_TIMEOUT_MS = Number.parseInt(process.env.ROUTER_HEALTH_TIMEOUT_MS || '120000', 10);
const HEALTH_POLL_MS = 2000;

function execText(command) {
  try {
    return execSync(command, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 60000 });
  } catch {
    return '';
  }
}

function findListenerForPort(port) {
  const portPattern = new RegExp(`(?:127\\.0\\.0\\.1|0\\.0\\.0\\.0|\\*|localhost|\\[::\\]|::|:::|\\.)[:.]${port}(?:\\b|\\s)`);
  const listenerRows = execText('ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null || true').split('\n').filter(Boolean);
  for (const line of listenerRows) {
    if (!portPattern.test(line)) continue;
    const match = line.match(/pid=(\d+)/) || line.match(/\s(\d+)\/\S+/);
    return match ? Number.parseInt(match[1], 10) : -1;
  }
  const lsofRows = execText(`lsof -nP -iTCP:${port} -sTCP:LISTEN 2>/dev/null || true`).split('\n').filter(Boolean);
  if (lsofRows.length > 1) {
    const match = lsofRows[1].match(/^\S+\s+(\d+)\s/);
    return match ? Number.parseInt(match[1], 10) : null;
  }
  return null;
}

function killExistingListeners() {
  const pid = findListenerForPort(ROUTER_PORT);
  if (pid && pid > 0) {
    execText(`kill ${pid} 2>/dev/null || true`);
    execText(`sleep 1`);
  }
}

async function ensureRepo(log) {
  if (fs.existsSync(path.join(ROUTER_HOME, 'package.json'))) {
    if (log) log('[9router] Repo already exists');
    return false;
  }
  if (log) log(`[9router] Cloning ${ROUTER_GIT_URL}...`);
  execSync(`rm -rf "${ROUTER_HOME}" && git clone --depth 1 "${ROUTER_GIT_URL}" "${ROUTER_HOME}"`, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 120000,
    env: { ...process.env, PATH: defaultPath },
  });
  if (log) log('[9router] Clone complete');
  return true;
}

async function ensureBuilt(log) {
  const standaloneServer = path.join(ROUTER_HOME, '.next', 'standalone', 'server.js');
  if (fs.existsSync(standaloneServer)) {
    if (log) log('[9router] Already built');
    return false;
  }
  if (log) log('[9router] Building...');
  const buildCmd = `cd "${ROUTER_HOME}" && npm install && npm run build`;
  execSync(buildCmd, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 300000,
    env: { ...process.env, PATH: defaultPath },
  });
  if (log) log('[9router] Build complete');
  return true;
}

function prepareStandalone() {
  const standaloneDir = path.join(ROUTER_HOME, '.next', 'standalone');
  const staticSrc = path.join(ROUTER_HOME, '.next', 'static');
  const staticDst = path.join(standaloneDir, '.next', 'static');
  const publicSrc = path.join(ROUTER_HOME, 'public');
  const publicDst = path.join(standaloneDir, 'public');
  execText(`rm -rf "${staticDst}" "${publicDst}"`);
  if (fs.existsSync(staticSrc)) execText(`cp -R "${staticSrc}" "${staticDst}"`);
  if (fs.existsSync(publicSrc)) execText(`cp -R "${publicSrc}" "${publicDst}"`);
}

function buildLaunchCommand(port = ROUTER_PORT) {
  const dataDir = path.join(process.env.HOME || '/tmp', '.9router', 'data');
  return [
    `export PATH="${defaultPath}"`,
    'export NODE_ENV=production',
    `export PORT=${port}`,
    'export HOSTNAME=127.0.0.1',
    `export NEXT_PUBLIC_BASE_URL=http://127.0.0.1:${port}`,
    `export BASE_URL=http://127.0.0.1:${port}`,
    `export DATA_DIR="${dataDir}"`,
    'mkdir -p "$DATA_DIR"',
    `cd "${path.join(ROUTER_HOME, '.next', 'standalone')}"`,
    'exec node server.js',
  ].join('; ');
}

function writeHermesConfig(port = ROUTER_PORT) {
  const hermesConfigPath = path.join(process.env.HOME || '/tmp', '.hermes', 'config.yaml');
  const current = (() => {
    try { return fs.readFileSync(hermesConfigPath, 'utf8'); } catch { return ''; }
  })();
  const apiLine = `api: http://127.0.0.1:${port}/v1`;
  if (current.includes(apiLine)) return false;
  const next = current
    ? current.replace(/api:\s*http:\/\/127\.0\.0\.1:\d+\/v1/, apiLine)
    : [
        'model:',
        '  provider: custom:9router',
        '  default: opencode/big-pickle',
        'providers:',
        '  9router:',
        '    name: 9Router',
        `    api: ${apiLine}`,
        '    default_model: opencode/big-pickle',
        '    transport: chat_completions',
        `    api_key: ${ROUTER_API_KEY}`,
        '',
      ].join('\n');
  fs.mkdirSync(path.dirname(hermesConfigPath), { recursive: true });
  fs.writeFileSync(hermesConfigPath, next, { mode: 0o600 });
  return true;
}

async function waitForHealth(timeoutMs = HEALTH_TIMEOUT_MS) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const result = execSync(
        `curl -fsS --max-time 5 http://127.0.0.1:${ROUTER_PORT}/api/health`,
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 10000 }
      );
      if (result) return true;
    } catch {
      // not ready yet
    }
    await new Promise((resolve) => setTimeout(resolve, HEALTH_POLL_MS));
  }
  return false;
}

export async function start(log) {
  await ensureRepo(log);
  await ensureBuilt(log);
  prepareStandalone();
  killExistingListeners();
  if (log) log(`[9router] Starting on port ${ROUTER_PORT}...`);
  const cmd = buildLaunchCommand();
  const logFd = fs.openSync(ROUTER_LOG_PATH, 'w');
  const child = spawn('sh', ['-lc', cmd], {
    detached: true,
    stdio: ['ignore', logFd, logFd],
    env: { ...process.env, PATH: defaultPath },
  });
  child.unref();
  fs.closeSync(logFd);
  if (log) log(`[9router] Process started (pid ${child.pid})`);
  const healthy = await waitForHealth();
  if (healthy) {
    if (log) log(`[9router] Health check passed on port ${ROUTER_PORT}`);
    writeHermesConfig();
  } else {
    if (log) log(`[9router] Health check failed after ${HEALTH_TIMEOUT_MS / 1000}s`);
  }
  const status = getStatus();
  return {
    ...status,
    healthy,
    port: ROUTER_PORT,
    pid: status.pid || child.pid,
  };
}

export function getStatus() {
  const listenerPid = findListenerForPort(ROUTER_PORT);
  const running = Boolean(listenerPid);
  const pid = listenerPid && listenerPid > 0 ? listenerPid : null;
  let logs = [];
  try {
    logs = fs.readFileSync(ROUTER_LOG_PATH, 'utf8').trimEnd().split('\n').slice(-120);
  } catch { /* no log yet */ }
  return {
    configuredPort: ROUTER_PORT,
    livePort: running ? ROUTER_PORT : null,
    state: running ? 'running' : 'error',
    error: running ? '' : `9Router is not listening on port ${ROUTER_PORT}.`,
    pid,
    logs,
    url: `http://127.0.0.1:${ROUTER_PORT}/dashboard/providers`,
    agent: {
      id: '__9router__',
      name: '9Router',
      state: running ? 'running' : 'error',
      port: ROUTER_PORT,
      pid,
      url: `http://127.0.0.1:${ROUTER_PORT}/dashboard/providers`,
      error: running ? '' : `9Router is not listening on port ${ROUTER_PORT}.`,
      startedAt: '',
      command: '9router',
      logs,
    },
  };
}

export async function restart(log) {
  killExistingListeners();
  return start(log);
}

export { ROUTER_PORT, ROUTER_API_KEY, ROUTER_MODEL };
