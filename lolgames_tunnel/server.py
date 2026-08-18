import argparse
import asyncio
import calendar
import html
import json
import os
import random
import string
import sys
import time
import threading
import urllib.error
import urllib.request

from .common import (
    BROKER_PORT,
    LOCAL_REGISTRY_PORT,
    CONTROL_PORT,
    DOMAIN,
    b64,
    original_dst,
    parse_host,
    raise_buffer_limits,
    rand_name,
    recv,
    send,
    ub64,
)

REG = {}
CTRL_BY_KEY = {}
META_BY_KEY = {}


def known_domains():
    """Primary tunnel domain plus optional legacy domains (TUNNEL_DOMAINS=comma list)."""
    extra = [d.strip() for d in os.environ.get('TUNNEL_DOMAINS', '').split(',') if d.strip()]
    return [DOMAIN] + [d for d in extra if d != DOMAIN]


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def peer_is_loopback(writer):
    """Registry pages are only served to the local Caddy proxy, never to direct
    public connections that forge a broker.agentsweb.space Host header."""
    try:
        peer = writer.get_extra_info('peername')
    except Exception:
        return False
    if not peer:
        return False
    return str(peer[0]).startswith('127.') or peer[0] == '::1'


BROKER_API_TOKEN = os.environ.get('BROKER_API_TOKEN', '').strip()
REGISTRY_HOSTS = {f'broker.{DOMAIN}', f'status.{DOMAIN}', f'registry.{DOMAIN}'}
CONN_QUEUE_MAX = int(os.environ.get('LOLGAMES_TUNNEL_CONN_QUEUE', '512') or '512')
MAX_CONNS_PER_REGISTRATION = int(os.environ.get('LOLGAMES_TUNNEL_MAX_CONNS', '128') or '128')
CONN_STALL_TIMEOUT = float(os.environ.get('LOLGAMES_TUNNEL_STALL_TIMEOUT', '15') or '15')
FLOW_PAUSE_AT = max(1, CONN_QUEUE_MAX // 2)
FLOW_RESUME_AT = max(0, CONN_QUEUE_MAX // 8)
HISTORY_FILE = os.environ.get('LOLGAMES_TUNNEL_HISTORY_FILE', '/var/log/lolgames-tunnel/history.jsonl')
PROBE_INTERVAL = float(os.environ.get('LOLGAMES_TUNNEL_PROBE_INTERVAL', '30') or '30')
PROBE_TIMEOUT = float(os.environ.get('LOLGAMES_TUNNEL_PROBE_TIMEOUT', '8') or '8')
HISTORY_LOCK = threading.Lock()


def history_event(event_type, level='info', **fields):
    event = {'time': utc_now(), 'type': event_type, 'level': level, **fields}
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE) or '.', exist_ok=True)
        line = json.dumps(event, separators=(',', ':')) + '\n'
        with HISTORY_LOCK:
            with open(HISTORY_FILE, 'a', encoding='utf-8') as stream:
                stream.write(line)
                stream.flush()
    except Exception as exc:
        print(f'WARNING: unable to append broker history: {exc}', file=sys.stderr, flush=True)


def probe_url(url):
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'agentsweb-broker-probe/2'})
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            response.read(256)
            return {
                'ok': 200 <= response.status < 400,
                'status': response.status,
                'error': '',
                'latency_ms': round((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        return {
            'ok': False,
            'status': exc.code,
            'error': str(exc.reason),
            'latency_ms': round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            'ok': False,
            'status': 0,
            'error': str(exc)[:240],
            'latency_ms': round((time.monotonic() - started) * 1000),
        }


async def probe_registrations():
    """Persist HTTPS health evidence for the FRP service paired with each SSH registration."""
    while True:
        await asyncio.sleep(PROBE_INTERVAL)
        for (subdomain, public_port), _meta in list(META_BY_KEY.items()):
            url, _ssh_url = registration_urls(subdomain, public_port)
            if not url.startswith('https://'):
                history_event(
                    'http_probe_skip',
                    level='info',
                    reason='no_paired_https_service',
                    subdomain=subdomain,
                    public_port=public_port,
                )
                continue
            status_url = url + '/api/status'
            result = await asyncio.to_thread(probe_url, status_url)
            history_event(
                'http_probe',
                level='ok' if result['ok'] else 'error',
                scheme='https',
                url=status_url,
                subdomain=subdomain,
                public_port=public_port,
                **result,
            )


def bearer_token(initial):
    """Return the Bearer token from an HTTP request head, or ''."""
    try:
        for raw in initial.decode('iso-8859-1', 'ignore').splitlines():
            if raw.lower().startswith('authorization:'):
                parts = raw.split(None, 2)
                if len(parts) >= 2 and parts[1].lower() == 'bearer':
                    return (parts[2] if len(parts) >= 3 else '').strip()
    except Exception:
        pass
    return ''


def seconds_since(value):
    if not value:
        return 0
    try:
        parsed = time.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return max(0, int(time.time() - calendar.timegm(parsed)))
    except Exception:
        return 0


def human_duration(seconds):
    seconds = int(seconds or 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m {seconds}s'
    return f'{seconds}s'


def registration_urls(subdomain, public_port):
    """Return the human web link and raw SSH link for a broker registration.

    The Python broker carries SSH only. Worker HTTP is published separately by
    FRP, but runner registrations have a stable paired Worker Agents hostname
    that is useful from the registry UI.
    """
    ssh_url = f'ssh://{subdomain}.{DOMAIN}:{public_port}'
    if subdomain.endswith('-ssh'):
        worker_prefix = subdomain[:-4]
        return f'https://{worker_prefix}-worker-agents.{DOMAIN}', ssh_url
    return ssh_url, ssh_url


def parse_request(buf):
    try:
        first = buf.decode('iso-8859-1', 'ignore').splitlines()[0]
        parts = first.split()
        if len(parts) >= 2 and parts[0].isalpha():
            return parts[0].upper(), parts[1].split('?', 1)[0]
    except Exception:
        pass
    return '', ''


def registration_snapshot():
    rows = []
    for (subdomain, public_port), meta in sorted(META_BY_KEY.items()):
        started_at = meta.get('started_at', '')
        uptime_seconds = seconds_since(started_at)
        url, ssh_url = registration_urls(subdomain, public_port)
        rows.append({
            **meta,
            'subdomain': subdomain,
            'public_port': public_port,
            'uptime_seconds': uptime_seconds,
            'uptime_text': human_duration(uptime_seconds),
            'url': url,
            'ssh_url': ssh_url,
        })
    return {
        'ok': True,
        'updated_at': utc_now(),
        'domain': DOMAIN,
        'count': len(rows),
        'registrations': rows,
    }


def uptime_bars(meta, count=90):
    uptime_seconds = seconds_since(meta.get('started_at'))
    live_bars = max(1, min(count, int((uptime_seconds + 59) // 60))) if uptime_seconds else 1
    bars = []
    for index in range(count):
        active = index >= count - live_bars
        cls = 'bar-ok' if active else 'bar-empty'
        label = 'registered' if active else 'before current registration'
        bars.append(f'<span class="bar {cls}" title="{html.escape(label)}"></span>')
    return ''.join(bars)


def http_response(status, content_type, body):
    data = body.encode('utf-8')
    reason = 'OK' if status == 200 else 'Not Found'
    return (
        f'HTTP/1.1 {status} {reason}\r\n'
        f'content-type: {content_type}\r\n'
        'cache-control: no-store, max-age=0\r\n'
        f'content-length: {len(data)}\r\n'
        'connection: close\r\n'
        '\r\n'
    ).encode('utf-8') + data


def registry_html():
    esc = lambda value: html.escape(str(value if value is not None else ''))
    snapshot = registration_snapshot()
    rows = []
    for meta in snapshot['registrations']:
        active = int(meta.get('active_connections') or 0)
        total = int(meta.get('total_connections') or 0)
        rows.append(f'''
        <article class="tunnel">
          <div class="row">
            <div>
              <a class="name" href="{esc(meta.get('url'))}">{esc(meta.get('subdomain'))}</a>
              <div class="url"><a href="{esc(meta.get('url'))}">{esc(meta.get('url'))}</a></div>
              <div class="ssh-url">SSH: {esc(meta.get('ssh_url'))}</div>
              <div class="target">{esc(meta.get('target') or 'target unknown')}</div>
            </div>
            <div class="uptime">{esc(meta.get('uptime_text'))}</div>
          </div>
          <div class="bars" aria-label="current registration uptime">{uptime_bars(meta)}</div>
          <div class="meta">
            <span>public {esc(meta.get('public_port')) or 'unassigned'}</span>
            <span>display {esc(meta.get('display_port') or '')}</span>
            <span>connections {active} / {total}</span>
            <span>peak {esc(meta.get('peak_connections') or 0)}</span>
            <span>last ping {esc(human_duration(seconds_since(meta.get('last_ping_at'))))} ago</span>
          </div>
        </article>''')
    if not rows:
        rows.append('<div class="empty">No active tunnel registrations.</div>')
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>Agentsweb Broker Registry</title>
  <style>
    :root {{ color-scheme: dark; --bg:#05070d; --panel:#080b13; --line:#1b2434; --text:#f7f8fb; --muted:#9aa3b2; --ok:#11c5a5; --ok2:#0d7563; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1180px, calc(100vw - 32px)); margin:28px auto 40px; }}
    header {{ display:flex; align-items:end; justify-content:space-between; gap:24px; padding:8px 0 18px; border-bottom:1px solid var(--line); }}
    h1 {{ margin:0; font-size:34px; letter-spacing:0; }}
    .count {{ color:var(--ok); font-size:24px; font-weight:800; white-space:nowrap; }}
    .list {{ display:grid; gap:12px; margin-top:18px; }}
    .tunnel, .empty {{ border:1px solid var(--line); border-radius:8px; background:var(--panel); padding:18px; }}
    .row, .meta {{ display:flex; align-items:center; justify-content:space-between; gap:18px; }}
    .name {{ color:var(--text); font-size:22px; font-weight:800; text-decoration:none; overflow-wrap:anywhere; }}
    .url, .ssh-url, .target, .meta {{ color:var(--muted); overflow-wrap:anywhere; }}
    .url a {{ color:var(--ok); }}
    .ssh-url {{ font-size:13px; margin-top:3px; }}
    .uptime {{ color:var(--ok); font-size:22px; font-weight:850; white-space:nowrap; }}
    .bars {{ height:54px; display:grid; grid-template-columns:repeat(90, 1fr); gap:2px; align-items:end; margin:16px 0 12px; }}
    .bar {{ display:block; min-width:3px; height:82%; border-radius:5px 5px 0 0; }}
    .bar-ok {{ background:var(--ok2); }}
    .bar-empty {{ background:#152033; opacity:.62; height:36%; }}
    .meta {{ justify-content:flex-start; flex-wrap:wrap; font-size:13px; }}
    @media (max-width: 700px) {{ main {{ width:100%; margin:0; padding:16px; }} header, .row {{ align-items:flex-start; flex-direction:column; gap:8px; }} .bars {{ gap:1px; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Agentsweb Broker Registry</h1>
      <div class="count">{snapshot['count']} registered</div>
    </header>
    <section class="list">{''.join(rows)}</section>
  </main>
</body>
</html>'''


async def write_registry(writer, path):
    if path == '/api/registrations':
        body = json.dumps(registration_snapshot(), separators=(',', ':')) + '\n'
        writer.write(http_response(200, 'application/json; charset=utf-8', body))
    else:
        writer.write(http_response(200, 'text/html; charset=utf-8', registry_html()))
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def handle_control(reader, writer):
    try:
        hello = await recv(reader)
    except (ValueError, UnicodeDecodeError):
        # Garbage on the control port (scanners, stray clients). Close quietly
        # instead of letting an unhandled exception churn the handler task.
        hello = None
    if not hello or hello.get('type') != 'register':
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    subdomain = hello.get('subdomain') or rand_name()
    for d in known_domains():
        suffix = '.' + d
        if subdomain.endswith(suffix):
            subdomain = subdomain[: -len(suffix)]
            break
    subdomain = subdomain.lower()
    port = int(hello.get('public_port', 0))
    target_port = int(hello.get('display_port') or 0)
    if target_port not in (22, 2222) or port <= 0:
        history_event('register_rejected', level='error', subdomain=subdomain,
                      public_port=port, display_port=target_port,
                      reason='Python broker registrations are SSH/raw-TCP only')
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return
    key = (subdomain, port)
    lock = asyncio.Lock()
    session_conns = set()
    session = (reader, writer, lock, session_conns)
    prev = CTRL_BY_KEY.get(key)
    if prev:
        print(f'WARNING: replacing existing tunnel registration for key={key!r}', file=sys.stderr, flush=True)
    CTRL_BY_KEY[key] = session
    display_port = int(hello.get('display_port') or port)
    url = f'ssh://{subdomain}.{DOMAIN}:{port}'
    META_BY_KEY[key] = {
        'started_at': utc_now(),
        'expires_at': str(hello.get('expires_at') or '').strip(),
        'last_ping_at': '',
        'display_port': display_port,
        'run_id': str(hello.get('run_id') or '').strip(),
        'target': hello.get('target') or '',
        'active_connections': 0,
        'total_connections': 0,
        'peak_connections': 0,
    }
    history_event('register', subdomain=subdomain, public_port=port, display_port=display_port,
                  run_id=META_BY_KEY[key]['run_id'], target=META_BY_KEY[key]['target'], expires_at=META_BY_KEY[key]['expires_at'])
    await send(writer, {'type': 'registered', 'url': url, 'subdomain': subdomain, 'port': port}, lock)
    # The control socket carries relayed payload for every connection on this
    # registration; without a bigger transport buffer the broker pays one RTT
    # per frame and the whole tunnel becomes stop-and-wait.
    raise_buffer_limits(writer)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(recv(reader), timeout=45)
            except asyncio.TimeoutError:
                break
            except (ValueError, UnicodeDecodeError):
                break
            if msg is None:
                break
            if msg.get('type') == 'ping':
                if key in META_BY_KEY:
                    META_BY_KEY[key]['last_ping_at'] = utc_now()
                history_event('control_ping', subdomain=subdomain, public_port=port)
                await send(writer, {'type': 'pong'}, lock)
                continue
            entry = REG.get(('conn', msg.get('id')))
            if entry:
                to_pub, state = entry
                if state['dead']:
                    continue
                if msg.get('type') == 'data' and not state['flow_paused'] and to_pub.qsize() >= FLOW_PAUSE_AT:
                    state['flow_paused'] = True
                    await send(writer, {'type': 'flow', 'id': msg.get('id'), 'paused': True}, lock)
                try:
                    to_pub.put_nowait(msg)
                except asyncio.QueueFull:
                    # Pause was sent with half the queue still available, so
                    # this is only the bounded in-flight tail. Preserve data
                    # while TCP and the per-connection flow signal push
                    # backpressure to the local producer.
                    await to_pub.put(msg)
    finally:
        history_event('disconnect', level='error', subdomain=subdomain, public_port=port, reason='control session ended')
        if CTRL_BY_KEY.get(key) is session:
            CTRL_BY_KEY.pop(key, None)
            META_BY_KEY.pop(key, None)
        for conn_id in list(session_conns):
            entry = REG.get(('conn', conn_id))
            if entry:
                try:
                    entry[0].put_nowait({'type': 'close'})
                except asyncio.QueueFull:
                    pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def handle_public_local(reader, writer):
    """Localhost-only registry endpoint (127.0.0.1:LOCAL_REGISTRY_PORT) with
    auth disabled. Remote packets to this port are NAT-redirected to the
    public listener, so it is not reachable off-box."""
    sock = writer.get_extra_info('socket')
    _ip, port = original_dst(sock) if sock else ('', 0)
    try:
        initial = await asyncio.wait_for(reader.read(4096), timeout=2)
    except Exception:
        initial = b''
    host = parse_host(initial)
    _method, path = parse_request(initial)
    # Caddy protects the human broker page with Basic auth, but the automation
    # registry hostname must still present BROKER_API_TOKEN. Treating every
    # loopback proxy request as trusted made the public registry API readable
    # without a token.
    require_token = host == f'registry.{DOMAIN}'
    if await serve_registry_if_allowed(writer, initial, host, path, port, require_token=require_token):
        return
    await write_no_tunnel(writer, initial, host, port)


async def serve_registry_if_allowed(writer, initial, host, path, port, require_token):
    if host in REGISTRY_HOSTS and path in {'', '/', '/api/registrations'}:
        # Caddy terminates the human-facing broker site on loopback and
        # protects it with HTTP basic auth.  It does not (and should not)
        # mint the broker API Bearer token, so allow that one host when the
        # request really came from loopback.  Keep the automation endpoint
        # token-gated even through Caddy; otherwise a public reverse proxy
        # would turn loopback into an auth bypass for registry.agentsweb.space.
        loopback_broker_page = host == f'broker.{DOMAIN}' and peer_is_loopback(writer)
        token_ok = BROKER_API_TOKEN and bearer_token(initial) == BROKER_API_TOKEN
        if not require_token or loopback_broker_page or token_ok:
            await write_registry(writer, path or '/')
        else:
            await write_no_tunnel(writer, initial, host, port)
        return True
    return False


async def handle_public(reader, writer):
    sock = writer.get_extra_info('socket')
    _ip, port = original_dst(sock) if sock else ('', 0)
    try:
        initial = await asyncio.wait_for(reader.read(4096), timeout=2)
    except Exception:
        initial = b''
    host = parse_host(initial)
    _method, path = parse_request(initial)
    if _method:
        # The Python broker is raw SSH/TCP only. HTTP services use the FRP
        # encoded-hostname path and must never be reachable on public ports.
        await write_bytes(writer, b'')
        return
    if await serve_registry_if_allowed(writer, initial, host, path, port, require_token=True):
        return
    matches = [session for (_sub, public_port), session in CTRL_BY_KEY.items() if public_port == port]
    ctrl = matches[0] if len(matches) == 1 else None

    if not ctrl:
        # Public high ports are retained for explicitly registered raw tunnels
        # such as SSH. An unmatched port is not an HTTP endpoint: close it
        # without emitting the broker's legacy 502 page.
        await write_bytes(writer, b'')
        return

    control_reader, control_writer, lock, session_conns = ctrl
    routed_key = None
    for meta_key, session in CTRL_BY_KEY.items():
        if session is ctrl:
            routed_key = meta_key
            break
    if routed_key in META_BY_KEY:
        active = int(META_BY_KEY[routed_key].get('active_connections') or 0)
        if active >= MAX_CONNS_PER_REGISTRATION:
            await write_busy(writer, initial, host, port, active)
            return
        META_BY_KEY[routed_key]['active_connections'] = active + 1
        META_BY_KEY[routed_key]['total_connections'] = int(META_BY_KEY[routed_key].get('total_connections') or 0) + 1
        peak = int(META_BY_KEY[routed_key].get('peak_connections') or 0)
        if active + 1 > peak:
            META_BY_KEY[routed_key]['peak_connections'] = active + 1
    conn_id = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12))
    to_client = asyncio.Queue(maxsize=CONN_QUEUE_MAX)
    to_pub = asyncio.Queue(maxsize=CONN_QUEUE_MAX)
    state = {'dead': False, 'flow_paused': False}
    REG[('conn', conn_id)] = (to_pub, state)
    session_conns.add(conn_id)
    try:
        await send(control_writer, {'type': 'open', 'id': conn_id, 'port': port, 'host': '', 'initial': b64(initial)}, lock)
    except Exception:
        session_conns.discard(conn_id)
        REG.pop(('conn', conn_id), None)
        if routed_key in META_BY_KEY:
            META_BY_KEY[routed_key]['active_connections'] = max(0, int(META_BY_KEY[routed_key].get('active_connections') or 0) - 1)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    async def queue_to_client():
        """Drains the bounded to_client queue onto the shared control socket.
        Blocking here only ever stalls this one connection: if the client stops
        reading, to_client fills, pub_to_client pauses and times out, and this
        connection is dropped instead of stalling the other ports."""
        try:
            while True:
                msg = await to_client.get()
                if msg is None:
                    break
                if msg.get('type') == 'data':
                    await send(control_writer, {'type': 'data', 'id': conn_id, 'data': msg['data']}, lock)
                else:
                    await send(control_writer, {'type': 'close', 'id': conn_id, 'error': msg.get('error', '')}, lock)
                    break
        except Exception:
            pass

    async def pub_to_client():
        paused = False
        pending = b''
        full_since = None
        loop = asyncio.get_running_loop()
        try:
            while True:
                if not pending:
                    data = await reader.read(32768)
                    if not data:
                        break
                    pending = data
                try:
                    to_client.put_nowait({'type': 'data', 'data': b64(pending)})
                    pending = b''
                    if paused:
                        paused = False
                        reader.resume_reading()
                except asyncio.QueueFull:
                    if not paused:
                        paused = True
                        full_since = loop.time()
                        reader.pause_reading()
                    elif full_since is not None and loop.time() - full_since >= CONN_STALL_TIMEOUT:
                        break
                    await asyncio.sleep(0.05)
        finally:
            try:
                to_client.put_nowait({'type': 'close'})
            except asyncio.QueueFull:
                pass

    async def client_to_pub():
        wrote_data = False
        raise_buffer_limits(writer)
        total = 0
        batches = 0
        reason = 'worker close frame'
        try:
            while True:
                if state['dead']:
                    reason = 'connection marked dead'
                    break
                try:
                    msg = await asyncio.wait_for(to_pub.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                msgs = [msg]
                try:
                    while len(msgs) < CONN_QUEUE_MAX:
                        msgs.append(to_pub.get_nowait())
                except asyncio.QueueEmpty:
                    pass
                close_msg = None
                for m in msgs:
                    if m.get('type') == 'close':
                        close_msg = m
                        break
                    data = ub64(m['data'])
                    if data:
                        wrote_data = True
                    writer.write(data)
                    total += len(data)
                if close_msg is not None:
                    if not wrote_data and close_msg.get('error') and parse_request(initial)[0]:
                        await write_target_error(writer, initial, host, port, close_msg.get('error', ''))
                    reason = 'close frame'
                    break
                batches += 1
                await writer.drain()
                if state['flow_paused'] and to_pub.qsize() <= FLOW_RESUME_AT:
                    state['flow_paused'] = False
                    await send(control_writer, {'type': 'flow', 'id': conn_id, 'paused': False}, lock)
        finally:
            print(f'conn {conn_id} public close reason={reason} bytes={total} batches={batches} queued={to_pub.qsize()}', file=sys.stderr, flush=True)
            writer.close()

    t1 = asyncio.create_task(pub_to_client())
    t2 = asyncio.create_task(client_to_pub())
    t3 = asyncio.create_task(queue_to_client())
    await asyncio.wait({t1, t2, t3}, return_when=asyncio.FIRST_COMPLETED)
    t1.cancel()
    t2.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
    REG.pop(('conn', conn_id), None)
    state['dead'] = True
    session_conns.discard(conn_id)
    if routed_key in META_BY_KEY:
        META_BY_KEY[routed_key]['active_connections'] = max(0, int(META_BY_KEY[routed_key].get('active_connections') or 0) - 1)
    if not t3.done():
        t3.cancel()
        try:
            await t3
        except asyncio.CancelledError:
            pass
    try:
        await send(control_writer, {'type': 'close', 'id': conn_id}, lock)
    except Exception:
        pass
    try:
        await writer.wait_closed()
    except Exception:
        pass


def error_page(status, reason, headline, lines, refresh_seconds):
    lines_text = ''.join(html.escape(line) + '\n' for line in lines)
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>agentsweb tunnel broker</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #111827;
      color: #e5e7eb;
    }}
    main {{
      max-width: 720px;
      padding: 24px;
      margin: 24px;
      border: 1px solid #374151;
      border-radius: 12px;
      background: #0b1220;
      box-shadow: 0 10px 30px rgba(0,0,0,0.35);
    }}
    h1 {{ margin-top: 0; font-size: 1.2rem; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      padding: 16px;
      border-radius: 8px;
      background: #111827;
      border: 1px solid #1f2937;
    }}
  </style>
</head>
<body>
  <main>
    <h1>agentsweb tunnel broker</h1>
    <pre>{html.escape(headline)}

{lines_text}</pre>
  </main>
</body>
</html>
""".encode()
    headers = (
        f'HTTP/1.1 {status} {reason}\r\n'.encode()
        + b'content-type: text/html; charset=utf-8\r\n'
        + b'content-length: ' + str(len(body)).encode() + b'\r\n'
        + b'cache-control: no-store, max-age=0\r\n\r\n'
    )
    return headers + body


async def write_bytes(writer, payload):
    if not payload:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return
    try:
        writer.write(payload)
        await writer.drain()
    except Exception:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def write_no_tunnel(writer, initial, host, port):
    host_text = host or 'None'
    payload = error_page(
        502,
        'Bad Gateway',
        f'no active tunnel for host={host_text} port={port}',
        [],
        3,
    )
    await write_bytes(writer, payload)


async def write_target_error(writer, initial, host, port, error):
    host_text = host or 'None'
    port_text = port or 'any'
    payload = error_page(
        502,
        'Bad Gateway',
        f'tunnel for host={host_text} port={port_text} is up, but the runner could not reach its local target port.',
        [
            error or 'unknown error',
            'The agent on the runner may still be starting, or the port may be closed. Retrying in 5s.',
        ],
        5,
    )
    await write_bytes(writer, payload)


async def write_busy(writer, initial, host, port, active):
    host_text = host or 'None'
    port_text = port or 'any'
    payload = error_page(
        429,
        'Too Many Requests',
        f'tunnel for host={host_text} port={port_text} is at capacity ({active} active connections).',
        ['Retrying in 3s.'],
        3,
    )
    await write_bytes(writer, payload)


async def server():
    public_server = await asyncio.start_server(handle_public, '0.0.0.0', BROKER_PORT)
    local_registry_server = await asyncio.start_server(handle_public_local, '127.0.0.1', LOCAL_REGISTRY_PORT)
    control_server = await asyncio.start_server(handle_control, '0.0.0.0', CONTROL_PORT)
    print(f'broker public:{BROKER_PORT} local-registry:{LOCAL_REGISTRY_PORT} control:{CONTROL_PORT}', flush=True)
    async with public_server, local_registry_server, control_server:
        probe_task = asyncio.create_task(probe_registrations())
        try:
            await asyncio.gather(public_server.serve_forever(), local_registry_server.serve_forever(), control_server.serve_forever())
        finally:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)


def main():
    argparse.ArgumentParser(description='Run agentsweb tunnel broker').parse_args()
    asyncio.run(server())


if __name__ == '__main__':
    main()
