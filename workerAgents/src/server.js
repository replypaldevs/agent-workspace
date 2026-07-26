import net from 'node:net';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { URL } from 'node:url';
import { execSync } from 'node:child_process';
import { config, defaultPath } from './config.js';
import { createLoginUrl, exchangeCodeForTokens, getAuthStatus, logout } from './auth.js';
import { supervisor } from './agents.js';
import { ensureSshd, runSetup, getSetupStatus, onSetupEvent, syncSkills } from './setup.js';

const publicDir = path.join(config.projectRoot, 'public');
const contentTypes = new Map([
  ['.html', 'text/html; charset=utf-8'],
  ['.css', 'text/css; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8']
]);
const ANSI = /\u001B\[[0-9;?]*[ -/]*[@-~]/g;
const HERMES_CONFIG_PATH = path.join(process.env.HOME || '/tmp', '.hermes', 'config.yaml');
const ROUTER_LOG_PATH = '/tmp/9router.log';
const ROUTER_PORTS = [20127, 20128, 20129, 20130, 20131, 20132];
const ROUTER_CANDIDATE_DIRS = [
  process.env.WORKER_AGENTS_9ROUTER_DIR,
  path.join(process.env.HOME || '/tmp', '9router'),
  path.join(process.env.HOME || '/tmp', '.worker-agents', '9router'),
  '/Users/igor/Git-projects/9router',
  '/opt/9router',
].filter(Boolean);
const consoleLogs = [];
const MAX_CONSOLE_LOGS = 500;

function resolve9RouterDir() {
  return ROUTER_CANDIDATE_DIRS.find((dir) => fs.existsSync(path.join(dir, 'package.json'))) || ROUTER_CANDIDATE_DIRS[0] || '/opt/9router';
}

function build9RouterLaunchCommand(port = 20127) {
  const routerDir = resolve9RouterDir();
  return [
    `export PATH="${defaultPath}"`,
    'export NODE_ENV=production',
    `export PORT=${port}`,
    'export HOSTNAME=127.0.0.1',
    `export NEXT_PUBLIC_BASE_URL=http://127.0.0.1:${port}`,
    `export BASE_URL=http://127.0.0.1:${port}`,
    `export DATA_DIR="${path.join(process.env.HOME || '/tmp', '.9router', 'data')}"`,
    'mkdir -p "$DATA_DIR"',
    `cd "${routerDir}"`,
    'if [ ! -f .next/standalone/server.js ]; then npm run build; fi',
    'mkdir -p .next/standalone/.next',
    'rm -rf .next/standalone/.next/static .next/standalone/public',
    'if [ -d .next/static ]; then cp -R .next/static .next/standalone/.next/static; fi',
    'if [ -d public ]; then cp -R public .next/standalone/public; fi',
    'cd .next/standalone',
    'exec node server.js'
  ].join('; ');
}

function captureConsoleLog(level, args) {
  const raw = [].map.call(args, String).join(' ');
  const clean = raw.replace(ANSI, '').trimEnd();
  if (!clean) return;
  consoleLogs.push(`${new Date().toLocaleTimeString()} [${level}] ${clean}`);
  if (consoleLogs.length > MAX_CONSOLE_LOGS) consoleLogs.shift();
}

const _origLog = console.log;
const _origWarn = console.warn;
const _origError = console.error;
console.log = function(...a) { captureConsoleLog('LOG', a); return _origLog.apply(console, a); };
console.warn = function(...a) { captureConsoleLog('WARN', a); return _origWarn.apply(console, a); };
console.error = function(...a) { captureConsoleLog('ERROR', a); return _origError.apply(console, a); };

function sendJson(res, status, payload) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

function sendHtml(res, status, body) {
  res.writeHead(status, { 'content-type': 'text/html; charset=utf-8' });
  res.end(body);
}

function readFileSafe(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf8');
  } catch {
    return '';
  }
}

function readLastLines(filePath, limit = 120) {
  const text = readFileSafe(filePath);
  if (!text) return [];
  return text.trimEnd().split('\n').slice(-limit);
}

function execText(command) {
  try {
    return execSync(command, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    return '';
  }
}

function parseHermes9RouterPort() {
  const text = readFileSafe(HERMES_CONFIG_PATH);
  const match = text.match(/api:\s*http:\/\/127\.0\.0\.1:(\d+)\/v1/);
  return match ? Number.parseInt(match[1], 10) : 20127;
}

function writeHermes9RouterPort(port) {
  const target = Number.parseInt(String(port), 10);
  if (!Number.isFinite(target)) return false;
  const current = readFileSafe(HERMES_CONFIG_PATH);
  const next = current
    ? current.replace(/api:\s*http:\/\/127\.0\.0\.1:\d+\/v1/, `api: http://127.0.0.1:${target}/v1`)
    : [
        'model:',
        '  provider: custom:9router',
        '  default: opencode/big-pickle',
        'providers:',
        '  9router:',
        '    name: 9Router',
        `    api: http://127.0.0.1:${target}/v1`,
        '    default_model: opencode/big-pickle',
        '    transport: chat_completions',
        '    api_key: ${WORKER_AGENTS_9ROUTER_API_KEY:-local-dev-key}',
        ''
      ].join('\n');
  if (next === current) return false;
  fs.mkdirSync(path.dirname(HERMES_CONFIG_PATH), { recursive: true });
  fs.writeFileSync(HERMES_CONFIG_PATH, next, { mode: 0o600 });
  return true;
}

function getListenerRows() {
  const rows = execText('ss -tlnp 2>/dev/null || true').split('\n').filter(Boolean);
  if (rows.length) return rows;
  return execText('netstat -anv -p tcp 2>/dev/null || true').split('\n').filter(Boolean);
}

function findListenerForPort(port) {
  const portPattern = new RegExp(`(?:127\\.0\\.0\\.1|0\\.0\\.0\\.0|\\*|localhost|\\[::\\]|::|:::|\\.)[:.]${port}(?:\\b|\\s)`);
  for (const line of getListenerRows()) {
    if (!portPattern.test(line)) continue;
    const pidMatch = line.match(/pid=(\d+)/);
    return {
      line,
      pid: pidMatch ? Number.parseInt(pidMatch[1], 10) : null
    };
  }
  const lsofRows = execText(`lsof -nP -iTCP:${port} -sTCP:LISTEN 2>/dev/null || true`).split('\n').filter(Boolean);
  for (const line of lsofRows.slice(1)) {
    const pidMatch = line.match(/^\S+\s+(\d+)\s/);
    return {
      line,
      pid: pidMatch ? Number.parseInt(pidMatch[1], 10) : null
    };
  }
  return null;
}

function kill9RouterListeners() {
  const seen = new Set();
  for (const port of ROUTER_PORTS) {
    const listener = findListenerForPort(port);
    if (listener?.pid) seen.add(listener.pid);
  }
  for (const pid of seen) {
    execText(`kill ${pid} 2>/dev/null || true`);
  }
  execText(`sh -lc "for port in ${ROUTER_PORTS.join(' ')}; do pids=$(lsof -tiTCP:$port -sTCP:LISTEN 2>/dev/null || true); [ -z \"$pids\" ] || kill $pids 2>/dev/null || true; done"`);
  execText(`sh -lc "for port in ${ROUTER_PORTS.join(' ')}; do hex=$(printf '%04X' "$port"); for inode in $(awk -v hex="$hex" '$2 ~ (":" hex "$") {print $10}' /proc/net/tcp /proc/net/tcp6 2>/dev/null | sort -u); do for fd in /proc/[0-9]*/fd/[0-9]*; do link=$(readlink "$fd" 2>/dev/null || true); [ "$link" = "socket:[$inode]" ] || continue; pid=\${fd#/proc/}; pid=\${pid%%/*}; kill "$pid" 2>/dev/null || true; done; done; done"`);
  execText(`sh -lc "pkill -f '/Users/igor/Git-projects/9router' || true"`);
  execText(`sh -lc "pkill -f '/opt/9router' || true"`);
  execText(`sh -lc "pkill -f 'node custom-server.js' || true"`);
  execText(`sh -lc "pkill -f 'next start' || true"`);
  execText(`sh -lc "sleep 1"`);
}

function get9RouterStatus(origin) {
  const configuredPort = parseHermes9RouterPort();
  const listener = ROUTER_PORTS.map((port) => ({ port, row: findListenerForPort(port) })).find((item) => item.row);
  const livePort = listener?.port || null;
  const pid = listener?.row?.pid || null;
  const logs = readLastLines(ROUTER_LOG_PATH);
  const running = Boolean(livePort);
  let state = running ? 'running' : 'error';
  let error = '';

  if (!running) {
    error = `9Router is not listening on ${ROUTER_PORTS[0]}-${ROUTER_PORTS.at(-1)}.`;
  } else if (configuredPort !== livePort) {
    state = 'error';
    error = `Hermes points to 127.0.0.1:${configuredPort}, but 9Router is live on 127.0.0.1:${livePort}. Restart 9Router from the console to repoint Hermes WebUI.`;
  }

  const url = livePort ? `http://127.0.0.1:${livePort}/dashboard/providers` : `http://127.0.0.1:${configuredPort}/dashboard/providers`;
  let routerUrl = url;
  try {
    const publicOrigin = new URL(origin);
    if (livePort) {
      publicOrigin.port = String(livePort);
      publicOrigin.pathname = '/dashboard/providers';
      publicOrigin.search = '';
      publicOrigin.hash = '';
      routerUrl = publicOrigin.toString();
    }
  } catch {}

  return {
    configuredPort,
    livePort,
    state,
    error,
    url: routerUrl,
    pid,
    logs,
    agent: {
      id: '__9router__',
      name: '9Router',
      state,
      port: livePort || configuredPort,
      pid,
      url: routerUrl,
      error,
      startedAt: '',
      command: '9router',
      logs
    }
  };
}

async function restartHermesWebUiIfRunning() {
  const snapshot = supervisor.snapshot().find((agent) => agent.id === 'hermes-webui');
  if (snapshot?.state === 'running') {
    try {
      await supervisor.restart('hermes-webui');
    } catch (error) {
      console.warn('[9router] Hermes WebUI restart failed:', error.message);
    }
  }
}

async function handle9RouterAction(action) {
  if (!['start', 'restart'].includes(action)) {
    throw new Error(`Unsupported 9Router action: ${action}`);
  }
  if (action === 'restart') {
    kill9RouterListeners();
  }
  execText(`sh -lc ": > ${ROUTER_LOG_PATH}; ${build9RouterLaunchCommand()} >> ${ROUTER_LOG_PATH} 2>&1 &"`);
  const started = Date.now();
  let status = get9RouterStatus('http://127.0.0.1:1400');
  while (!status.livePort && Date.now() - started < 15000) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    status = get9RouterStatus('http://127.0.0.1:1400');
  }
  if (status.livePort) {
    const changed = writeHermes9RouterPort(status.livePort);
    if (changed || action === 'restart') await restartHermesWebUiIfRunning();
  }
  return status;
}

function redirect(res, location) {
  res.writeHead(302, { location });
  res.end();
}

function requestOrigin(req) {
  const proto = req.headers['x-forwarded-proto'] || 'http';
  const host = req.headers['x-forwarded-host'] || req.headers.host || `${config.host}:${config.port}`;
  return `${proto}://${host}`;
}

function proxiedAgentUrl(agent, origin) {
  if (agent?.id === 'openclaw' && agent?.port) {
    try {
      const publicOrigin = new URL(origin);
      publicOrigin.pathname = `/proxy/openclaw/`;
      publicOrigin.search = '';
      publicOrigin.hash = '';
      return publicOrigin.toString();
    } catch {}
  }
  return agent?.url || '';
}

function publicAgent(agent, origin) {
  if (!agent?.url) return agent;
  try {
    if (agent.id === 'openclaw') {
      return { ...agent, url: proxiedAgentUrl(agent, origin) };
    }
    const publicOrigin = new URL(origin);
    const rebased = new URL(agent.url);
    rebased.protocol = publicOrigin.protocol;
    rebased.hostname = publicOrigin.hostname;
    return { ...agent, url: rebased.toString() };
  } catch {
    return agent;
  }
}

function getOpenClawProxyTarget() {
  const snapshot = supervisor.snapshot().find((agent) => agent.id === 'openclaw');
  if (!snapshot?.port || snapshot.state !== 'running') return null;
  return {
    port: snapshot.port,
    origin: `http://127.0.0.1:${snapshot.port}`
  };
}

function filterProxyHeaders(headers, upstreamOrigin) {
  const next = { ...headers };
  delete next.host;
  delete next.connection;
  delete next['content-length'];
  delete next['transfer-encoding'];
  delete next['accept-encoding'];
  next.origin = upstreamOrigin;
  next.host = upstreamOrigin.replace(/^https?:\/\//, '');
  next['x-forwarded-proto'] = 'http';
  next['x-forwarded-host'] = next.host;
  return next;
}

function handleOpenClawProxy(req, res, url) {
  const target = getOpenClawProxyTarget();
  if (!target) {
    sendJson(res, 503, { error: 'OpenClaw is not running' });
    return true;
  }
  const upstreamPath = url.pathname.replace(/^\/proxy\/openclaw/, '') || '/';
  const upstreamReq = http.request({
    host: '127.0.0.1',
    port: target.port,
    method: req.method,
    path: `${upstreamPath}${url.search}`,
    headers: filterProxyHeaders(req.headers, target.origin)
  }, (upstreamRes) => {
    const headers = { ...upstreamRes.headers };
    delete headers['content-security-policy'];
    delete headers['content-length'];
    delete headers['transfer-encoding'];
    if (headers.location && typeof headers.location === 'string') {
      try {
        const resolved = new URL(headers.location, target.origin);
        if (resolved.origin === target.origin) {
          headers.location = `/proxy/openclaw${resolved.pathname}${resolved.search}${resolved.hash}`;
        }
      } catch {}
    }
    res.writeHead(upstreamRes.statusCode || 502, headers);
    upstreamRes.pipe(res);
  });
  upstreamReq.on('error', (error) => {
    if (!res.headersSent) sendJson(res, 502, { error: `OpenClaw proxy failed: ${error.message}` });
    else res.destroy(error);
  });
  req.pipe(upstreamReq);
  return true;
}

function handleOpenClawUpgrade(req, socket) {
  const target = getOpenClawProxyTarget();
  if (!target) {
    socket.end('HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n');
    return;
  }
  const upstream = net.connect(target.port, '127.0.0.1', () => {
    const lines = [
      `GET ${req.url.replace(/^\/proxy\/openclaw/, '') || '/'} HTTP/1.1`,
      `Host: 127.0.0.1:${target.port}`,
      'Connection: Upgrade',
      'Upgrade: websocket',
      `Origin: ${target.origin}`
    ];
    for (const [key, value] of Object.entries(req.headers)) {
      if (!value) continue;
      const lower = key.toLowerCase();
      if (['host', 'connection', 'upgrade', 'origin'].includes(lower)) continue;
      if (Array.isArray(value)) {
        for (const item of value) lines.push(`${key}: ${item}`);
      } else {
        lines.push(`${key}: ${value}`);
      }
    }
    lines.push('\r\n');
    upstream.write(lines.join('\r\n'));
    socket.pipe(upstream).pipe(socket);
  });
  upstream.on('error', () => {
    socket.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n');
  });
}

async function findAvailablePort(basePort, maxRange) {
  for (let offset = 0; offset < maxRange; offset++) {
    const port = basePort + offset;
    const available = await new Promise((resolve) => {
      const tester = net.createServer();
      tester.once('error', () => resolve(false));
      tester.once('listening', () => {
        tester.close();
        resolve(true);
      });
      tester.listen(port, config.host);
    });
    if (available) return port;
  }
  return basePort;
}

function statusPayload(req) {
  const origin = requestOrigin(req);
  const router = get9RouterStatus(origin);
  const agents = supervisor.snapshot().map((agent) => publicAgent(agent, origin));
  const filtered = config.launch
    ? agents.filter((a) => a.id === config.launch)
    : agents;
  return {
    version: buildVersion,
    auth: getAuthStatus(),
    router,
    agents: [
      router.agent,
      ...filtered,
      {
        id: '__console__',
        name: 'Agent Console',
        state: 'running',
        port: config.port,
        pid: process.pid,
        url: '',
        error: '',
        startedAt: '',
        command: '',
        logs: [...consoleLogs]
      }
    ],
    setup: getSetupStatus()
  };
}

function serveStatic(res, pathname, headOnly = false) {
  const normalized = pathname === '/' ? '/index.html' : pathname;
  const filePath = path.normalize(path.join(publicDir, normalized));
  if (!filePath.startsWith(publicDir)) {
    sendJson(res, 403, { error: 'Forbidden' });
    return;
  }
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    sendJson(res, 404, { error: 'Not found' });
    return;
  }
  const ext = path.extname(filePath);
  res.writeHead(200, { 'content-type': contentTypes.get(ext) || 'application/octet-stream' });
  if (headOnly) {
    res.end();
    return;
  }
  fs.createReadStream(filePath).pipe(res);
}
function serveLaunchPage(res) {
  const filePath = path.join(publicDir, 'launch.html');
  if (!fs.existsSync(filePath)) {
    sendJson(res, 404, { error: 'launch.html not found' });
    return;
  }
  let html = fs.readFileSync(filePath, 'utf8');
  html = html.replace('{{LAUNCH_AGENT}}', escapeHtml(config.launch));
  html = html.replace('{{LAUNCH_AGENT_JSON}}', JSON.stringify(config.launch));
  sendHtml(res, 200, html);
}


async function handleAgentAction(req, res, pathname) {
  const match = pathname.match(/^\/api\/agents\/([^/]+)\/(start|stop|restart)$/);
  if (!match || req.method !== 'POST') return false;
  const [, id, action] = match;
  if (id === '__9router__') {
    try {
      const result = await handle9RouterAction(action);
      sendJson(res, 200, { ok: true, agent: result.agent, router: result });
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error.message });
    }
    return true;
  }
  try {
    const result = publicAgent(await supervisor[action](id), requestOrigin(req));
    sendJson(res, 200, { ok: true, agent: result });
  } catch (error) {
    sendJson(res, 400, { ok: false, error: error.message });
  }
  return true;
}

function handleEvents(req, res) {
  res.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache, no-transform',
    connection: 'keep-alive'
  });

  const send = (event, data) => {
    res.write(`event: ${event}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };

  send('status', statusPayload(req));
  const listener = (event) => send('status', { ...statusPayload(req), event });
  supervisor.on('change', listener);
  const unsubSetup = onSetupEvent(() => send('status', statusPayload(req)));
  const interval = setInterval(() => send('status', statusPayload(req)), 5000);

  req.on('close', () => {
    clearInterval(interval);
    supervisor.off('change', listener);
    unsubSetup();
  });
}

async function handleRequest(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || `${config.host}:${config.port}`}`);

  if (url.pathname === '/api/status' && req.method === 'GET') {
    sendJson(res, 200, statusPayload(req));
    return;
  }

  if (url.pathname === '/api/events' && req.method === 'GET') {
    handleEvents(req, res);
    return;
  }

  if (url.pathname === '/api/auth/login' && req.method === 'GET') {
    redirect(res, createLoginUrl());
    return;
  }

  if (url.pathname === '/api/auth/logout' && req.method === 'POST') {
    logout();
    sendJson(res, 200, { ok: true, auth: getAuthStatus() });
    return;
  }

  if (url.pathname === '/auth/callback' && req.method === 'GET') {
    const code = url.searchParams.get('code');
    const state = url.searchParams.get('state');
    try {
      if (!code) throw new Error('Missing OAuth code.');
      await exchangeCodeForTokens(code, state);
      // Restart Hermes WebUI so legacy auth callbacks still refresh the
      // on-disk default config, which now stays pinned to 9Router.
      try { await supervisor.restart('hermes-webui'); } catch {}
      sendHtml(res, 200, '<!doctype html><meta charset="utf-8"><title>Signed in</title><script>location.href="/?dashboard=1"</script><p>Signed in. Returning to the console.</p>');
    } catch (error) {
      sendHtml(res, 500, `<!doctype html><meta charset="utf-8"><title>Login failed</title><p>Login failed: ${escapeHtml(error.message)}</p><p><a href="/">Return to console</a></p>`);
    }
    return;
  }

  if (url.pathname === '/api/skills/update' && req.method === 'POST') {
    try {
      const result = await syncSkills();
      sendJson(res, 200, { ok: result.ok, error: result.err || null, changed: result.changed || false, summary: result.summary || '' });
    } catch (error) {
      sendJson(res, 500, { ok: false, error: error.message });
    }
    return;
  }

  if (await handleAgentAction(req, res, url.pathname)) return;
  if (url.pathname === '/proxy/openclaw' || url.pathname.startsWith('/proxy/openclaw/')) {
    handleOpenClawProxy(req, res, url);
    return;
  }
  // Launch mode: serve launch.html at / (unless ?dashboard=1)
  if (config.launch && url.pathname === '/' && req.method === 'GET') {
    if (url.searchParams.get('dashboard') !== '1') {
      serveLaunchPage(res);
      return;
    }
  }


  if (req.method === 'GET' || req.method === 'HEAD') {
    serveStatic(res, url.pathname, req.method === 'HEAD');
    return;
  }

  sendJson(res, 405, { error: 'Method not allowed' });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

const server = http.createServer((req, res) => {
  handleRequest(req, res).catch((error) => {
    sendJson(res, 500, { error: error.message });
  });
});

server.on('upgrade', (req, socket) => {
  if (req.url === '/proxy/openclaw' || req.url?.startsWith('/proxy/openclaw/')) {
    handleOpenClawUpgrade(req, socket);
    return;
  }
  socket.end('HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n');
});

process.on('SIGINT', async () => {
  await supervisor.stopAll();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  await supervisor.stopAll();
  process.exit(0);
});

try {
  ensureSshd();
} catch (error) {
  console.error('[sshd] Startup error:', error.message);
}

(async () => {
  // Kill any stale process on the default port before acquiring
  try {
    const pids = execSync(`lsof -ti :${config.port} 2>/dev/null`, { encoding: 'utf8' }).trim();
    if (pids) {
      for (const pid of pids.split('\n').filter(Boolean)) {
        try { process.kill(parseInt(pid, 10), 'SIGKILL'); } catch { /* already gone */ }
      }
    }
  } catch {
    // lsof not available — try fuser as fallback
    try { execSync(`fuser -k ${config.port}/tcp 2>/dev/null`, { stdio: 'ignore' }); } catch {}
  }
  const resolvedPort = await findAvailablePort(config.port, config.portScanRange);
  if (resolvedPort !== config.port) {
    console.log(`Port ${config.port} in use, using port ${resolvedPort} instead`);
  }
  server.listen(resolvedPort, config.host, () => {
  console.log(`Agent console listening at http://${config.host}:${resolvedPort}`);

  // Idempotent filesystem preflight (non-fatal)
  runSetup().catch((error) => {
    console.error('[setup] Preflight error:', error.message);
  }).then(async () => {
  try {
    const routerStatus = await handle9RouterAction('restart');
    if (!routerStatus.livePort) {
      console.warn('[9router] Startup did not produce a live listener');
    }
  } catch (error) {
    console.error('[9router] Startup error:', error.message);
  }
  const router = get9RouterStatus(`http://${config.host}:${resolvedPort}`);
  if (router.livePort) {
    writeHermes9RouterPort(router.livePort);
  }
  if (config.launch) {
    if (supervisor.agents.has(config.launch)) {
      console.log(`Launch mode: auto-starting agent "${config.launch}"...`);
      supervisor.start(config.launch).catch((error) => {
        console.error(`Launch mode: failed to start "${config.launch}":`, error.message);
      });
    } else {
      console.error(`Launch mode: unknown agent "${config.launch}". Available: ${Array.from(supervisor.agents.keys()).join(', ')}`);
    }
  }
  });
  });
})();
const buildVersion = (() => {
  // Optional packaged build metadata.
  try {
    const apkVersionPath = path.join(process.cwd(), '.apk_version');
    const versionCode = parseInt(fs.readFileSync(apkVersionPath, 'utf-8').trim(), 10);
    if (!isNaN(versionCode) && versionCode > 0) {
      return { versionCode, versionName: '0.1.0' };
    }
  } catch { /* fall through */ }

  // On Mac dev: compute from git
  try {
    const cwd = config.projectRoot;
    const count = execSync('git rev-list --count HEAD', { encoding: 'utf-8', cwd }).trim();
    return { versionCode: parseInt(count, 10) + 578, versionName: '0.1.0' };
  } catch {
    return { versionCode: 0, versionName: 'dev' };
  }
})();
