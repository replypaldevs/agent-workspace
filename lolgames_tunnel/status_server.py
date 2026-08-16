import asyncio
import html
import json
import os
import time
from datetime import datetime, timezone

BUCKET_SECONDS = 60
BUCKET_COUNT = 90


def parse_bind(value):
    if not value:
        return None
    if ':' not in value:
        return ('127.0.0.1', int(value))
    host, port = value.rsplit(':', 1)
    return (host or '127.0.0.1', int(port))


def read_status(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            status = json.load(f)
    except FileNotFoundError:
        status = {'ok': False, 'state': 'missing', 'last_error': f'status file not found: {path}'}
    except Exception as exc:
        status = {'ok': False, 'state': 'error', 'last_error': str(exc)}
    status['statusFile'] = path
    return add_derived_fields(status)


def default_history_file(status_file):
    base, ext = os.path.splitext(status_file)
    return f'{base}-history.jsonl' if ext else f'{status_file}-history.jsonl'


def read_history(path, limit=2000):
    events = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return events[-limit:]


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def seconds_since(value):
    dt = parse_iso(value)
    if not dt:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def human_duration(seconds):
    if seconds is None:
        return 'unknown'
    seconds = int(seconds)
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


def display_time(value):
    dt = parse_iso(value)
    if not dt:
        return 'unknown time'
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC')


def iso_from_ts(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts))


def add_derived_fields(status):
    uptime = seconds_since(status.get('started_at'))
    updated_age = seconds_since(status.get('updated_at'))
    ping_age = seconds_since(status.get('last_ping_at'))
    pong_age = seconds_since(status.get('last_pong_at'))
    status['uptimeSeconds'] = uptime
    status['uptimeText'] = human_duration(uptime)
    status['updatedAgeSeconds'] = updated_age
    status['updatedAgeText'] = human_duration(updated_age)
    status['lastPingAgeSeconds'] = ping_age
    status['lastPingAgeText'] = human_duration(ping_age)
    status['lastPongAgeSeconds'] = pong_age
    status['lastPongAgeText'] = human_duration(pong_age)
    return status


def history_path_for(status):
    return status.get('history_file') or default_history_file(status.get('statusFile') or '/tmp/lolgames-tunnel-status.json')


def build_history(status):
    history_file = history_path_for(status)
    events = read_history(history_file)
    now = int(time.time())
    end = now - (now % BUCKET_SECONDS) + BUCKET_SECONDS
    start = end - (BUCKET_COUNT * BUCKET_SECONDS)
    buckets = []
    for i in range(BUCKET_COUNT):
        bucket_start = start + (i * BUCKET_SECONDS)
        buckets.append({
            'start': iso_from_ts(bucket_start),
            'end': iso_from_ts(bucket_start + BUCKET_SECONDS),
            'level': 'empty',
            'state': 'unknown',
            'events': [],
            'active_connections': 0,
            'total_connections': 0,
            'reconnects': int(status.get('reconnects') or 0),
        })

    started = parse_iso(status.get('started_at'))
    started_ts = int(started.timestamp()) if started else None
    for bucket in buckets:
        bucket_ts = int(parse_iso(bucket['start']).timestamp())
        if started_ts is not None and bucket_ts >= started_ts and status.get('ok'):
            bucket['level'] = 'ok'
            bucket['state'] = status.get('state') or 'registered'

    for event in events:
        dt = parse_iso(event.get('time'))
        if not dt:
            continue
        event_ts = int(dt.timestamp())
        if event_ts < start or event_ts >= end:
            continue
        index = min(BUCKET_COUNT - 1, max(0, (event_ts - start) // BUCKET_SECONDS))
        bucket = buckets[index]
        bucket['events'].append(event)
        bucket['active_connections'] = max(int(bucket.get('active_connections') or 0), int(event.get('active_connections') or 0))
        bucket['total_connections'] = max(int(bucket.get('total_connections') or 0), int(event.get('total_connections') or 0))
        bucket['reconnects'] = max(int(bucket.get('reconnects') or 0), int(event.get('reconnects') or 0))
        bucket['state'] = event.get('state') or bucket.get('state') or 'unknown'
        if event.get('level') == 'error':
            bucket['level'] = 'error'
        elif event.get('level') == 'warn' and bucket.get('level') != 'error':
            bucket['level'] = 'warn'
        elif bucket.get('level') == 'empty':
            bucket['level'] = 'ok'

    observed = [b for b in buckets if b['level'] != 'empty']
    healthy = [b for b in observed if b['level'] == 'ok']
    uptime_percent = (len(healthy) / len(observed) * 100) if observed else (100.0 if status.get('ok') else 0.0)
    return {
        'historyFile': history_file,
        'bucketSeconds': BUCKET_SECONDS,
        'bucketCount': BUCKET_COUNT,
        'windowStart': buckets[0]['start'],
        'windowEnd': buckets[-1]['end'],
        'uptimePercent': round(uptime_percent, 2),
        'buckets': buckets,
        'events': events[-200:],
    }


def response(status, content_type, body):
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


def status_label(status):
    if status.get('ok') and status.get('state') == 'registered':
        return 'All systems operational'
    if status.get('state') == 'connecting':
        return 'Tunnel connecting'
    return 'Tunnel needs attention'


def status_class(status):
    if status.get('ok') and status.get('state') == 'registered':
        return 'ok'
    if status.get('state') == 'connecting':
        return 'warn'
    return 'bad'


def history_bars(history):
    buckets = history.get('buckets') or []
    bars = []
    for bucket in buckets:
        level = bucket.get('level') or 'empty'
        cls = 'bar-ok'
        height = 76
        if level == 'empty':
            tip = f'{display_time(bucket.get("start"))}\nNo status events in this minute'
            bars.append(f'<span class="bar bar-empty" style="height:38%" title="{html.escape(tip)}" data-tip="{html.escape(tip)}"></span>')
            continue
        if level == 'warn':
            cls = 'bar-warn'
            height = 45
        elif level == 'error':
            cls = 'bar-bad'
            height = 62
        active = int(bucket.get('active_connections') or 0)
        if active:
            height = 92
        events = bucket.get('events') or []
        messages = '\n'.join(f'- {event.get("type", "event")}: {event.get("message", "")}' for event in events[:5])
        if len(events) > 5:
            messages += f'\n- ... {len(events) - 5} more'
        if not messages:
            messages = f'{level} minute with no detailed event'
        tip = (
            f'{display_time(bucket.get("start"))} - {display_time(bucket.get("end"))}\n'
            f'{messages}\n'
            f'state: {bucket.get("state") or "unknown"}\n'
            f'connections: {active} / {int(bucket.get("total_connections") or 0)}\n'
            f'reconnects: {int(bucket.get("reconnects") or 0)}'
        )
        safe_tip = html.escape(tip)
        bars.append(f'<span class="bar {cls}" style="height:{height}%" title="{safe_tip}" data-tip="{safe_tip}"></span>')
    return ''.join(bars)


def html_page(status):
    history = build_history(status)
    esc = lambda value: html.escape(str(value if value is not None else ''))
    cls = status_class(status)
    title = status_label(status)
    url = status.get('url') or ''
    target = status.get('target') or ''
    updated = status.get('updatedAgeText') or 'unknown'
    uptime = status.get('uptimeText') or 'unknown'
    uptime_pct = f'{float(history.get("uptimePercent") or 0):.2f}%'
    active = int(status.get('active_connections') or 0)
    total = int(status.get('total_connections') or 0)
    reconnects = int(status.get('reconnects') or 0)
    dropped = int(status.get('dropped_connections') or 0)
    last_error = status.get('last_error') or 'None'
    body = f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>Agentsweb Tunnel Status</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #05070d;
      --panel: #080b13;
      --line: #1b2434;
      --text: #f7f8fb;
      --muted: #9aa3b2;
      --ok: #11c5a5;
      --ok2: #0d7563;
      --warn: #f6ce4c;
      --bad: #c75a7a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 32px auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
    }}
    .hero {{
      min-height: 180px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .mark {{
      width: 58px;
      height: 58px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: rgba(17, 197, 165, .14);
      color: var(--ok);
      font-size: 34px;
      font-weight: 800;
    }}
    .warn .mark {{ background: rgba(246, 206, 76, .14); color: var(--warn); }}
    .bad .mark {{ background: rgba(199, 90, 122, .14); color: var(--bad); }}
    h1 {{ margin: 0; font-size: clamp(30px, 5vw, 48px); letter-spacing: 0; }}
    .content {{ padding: 36px 46px 44px; }}
    .row-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 12px;
    }}
    .service {{
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
      color: #d8dde7;
      font-size: clamp(22px, 3vw, 32px);
      font-weight: 750;
    }}
    .chev {{
      width: 32px;
      height: 32px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: #e7edf7;
      color: #070a10;
      font-weight: 900;
    }}
    .uptime {{
      color: var(--ok);
      font-size: clamp(22px, 3vw, 32px);
      font-weight: 800;
      white-space: nowrap;
    }}
    .bars {{
      height: 76px;
      display: grid;
      grid-template-columns: repeat(90, 1fr);
      gap: 2px;
      align-items: end;
      margin-top: 12px;
    }}
    .bar {{
      display: block;
      min-width: 3px;
      border-radius: 6px 6px 0 0;
      background: var(--ok2);
      position: relative;
    }}
    .bar-ok {{ background: var(--ok2); }}
    .bar-warn {{ background: var(--warn); }}
    .bar-bad {{ background: var(--bad); }}
    .bar-empty {{ background: #152033; opacity: .62; }}
    .bar:hover::after {{
      content: attr(data-tip);
      white-space: pre;
      position: absolute;
      left: 50%;
      bottom: calc(100% + 12px);
      transform: translateX(-50%);
      z-index: 5;
      width: max-content;
      max-width: min(360px, 80vw);
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #02040a;
      color: var(--text);
      box-shadow: 0 18px 50px rgba(0, 0, 0, .42);
      font-size: 13px;
      line-height: 1.45;
      font-weight: 650;
      pointer-events: none;
    }}
    .bar:hover::before {{
      content: "";
      position: absolute;
      left: 50%;
      bottom: calc(100% + 5px);
      transform: translateX(-50%);
      border: 7px solid transparent;
      border-top-color: #02040a;
      z-index: 6;
      pointer-events: none;
    }}
    .axis {{
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: clamp(17px, 2.4vw, 24px);
      font-weight: 650;
      margin: 10px 0 34px;
    }}
    .details {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-top: 1px solid var(--line);
    }}
    .metric {{
      padding: 22px 18px;
      border-right: 1px solid var(--line);
    }}
    .metric:last-child {{ border-right: 0; }}
    .metric.wide {{ grid-column: span 2; }}
    .label {{ color: var(--muted); font-size: 13px; text-transform: uppercase; letter-spacing: 0; }}
    .value {{ margin-top: 8px; font-size: 22px; font-weight: 760; overflow-wrap: anywhere; }}
    .incident {{
      margin-top: 28px;
      padding: 20px 0 0;
      border-top: 1px solid var(--line);
      display: flex;
      align-items: center;
      gap: 14px;
      color: #e6e9ef;
      font-size: 20px;
      font-weight: 700;
    }}
    .bang {{
      width: 32px;
      height: 32px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: var(--warn);
      color: #090b10;
      font-weight: 900;
    }}
    a {{ color: inherit; }}
    @media (max-width: 760px) {{
      main {{ width: 100%; min-height: 100vh; margin: 0; border: 0; border-radius: 0; }}
      .hero {{ min-height: 150px; padding: 24px; }}
      .content {{ padding: 24px 18px 32px; }}
      .row-head {{ align-items: flex-start; flex-direction: column; gap: 10px; }}
      .bars {{ gap: 1px; height: 64px; }}
      .details {{ grid-template-columns: 1fr 1fr; }}
      .metric:nth-child(2) {{ border-right: 0; }}
      .metric {{ border-bottom: 1px solid var(--line); }}
    }}
  </style>
</head>
<body>
  <main class="{cls}">
    <section class="hero">
      <div class="mark">✓</div>
      <h1>{esc(title)}</h1>
    </section>
    <section class="content">
      <div class="row-head">
        <div class="service"><span class="chev">›</span><span>{esc(status.get('subdomain') or 'Agentsweb Tunnel')}</span></div>
        <div class="uptime">{esc(uptime_pct)} uptime</div>
      </div>
      <div class="bars" aria-label="recent tunnel health">{history_bars(history)}</div>
      <div class="axis"><span>90 MINUTES AGO</span><span>TODAY</span></div>
      <div class="details">
        <div class="metric"><div class="label">Uptime</div><div class="value">{esc(uptime)}</div></div>
        <div class="metric"><div class="label">Last update</div><div class="value">{esc(updated)} ago</div></div>
        <div class="metric"><div class="label">Connections</div><div class="value">{active} / {total}</div></div>
        <div class="metric"><div class="label">Reconnects</div><div class="value">{reconnects}</div></div>
      </div>
      <div class="details">
        <div class="metric"><div class="label">Public URL</div><div class="value"><a href="{esc(url)}">{esc(url or 'unknown')}</a></div></div>
        <div class="metric"><div class="label">Target</div><div class="value">{esc(target or 'unknown')}</div></div>
        <div class="metric"><div class="label">Broker</div><div class="value">{esc(status.get('server') or 'unknown')}</div></div>
        <div class="metric"><div class="label">Dropped</div><div class="value">{dropped}</div></div>
      </div>
      <div class="incident"><span class="bang">!</span><span>{esc(last_error)}</span></div>
    </section>
  </main>
</body>
</html>'''
    return body


async def handle_status_http(reader, writer, status_file):
    try:
        head = await asyncio.wait_for(reader.read(4096), timeout=5)
        first = head.decode('iso-8859-1', 'ignore').splitlines()[0] if head else ''
        path = first.split()[1] if len(first.split()) >= 2 else '/'
        status = read_status(status_file)
        if path == '/api/status':
            payload = json.dumps(status, separators=(',', ':')) + '\n'
            writer.write(response(200, 'application/json; charset=utf-8', payload))
        elif path == '/api/history':
            payload = json.dumps(build_history(status), separators=(',', ':')) + '\n'
            writer.write(response(200, 'application/json; charset=utf-8', payload))
        elif path == '/healthz':
            writer.write(response(200, 'text/plain; charset=utf-8', 'ok\n' if status.get('ok') else 'not ok\n'))
        elif path == '/':
            writer.write(response(200, 'text/html; charset=utf-8', html_page(status)))
        else:
            writer.write(response(404, 'text/plain; charset=utf-8', 'not found\n'))
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_status_server(bind, status_file):
    host, port = parse_bind(bind)
    server = await asyncio.start_server(lambda r, w: handle_status_http(r, w, status_file), host, port)
    print(f'http://{host}:{port}', flush=True)
    async with server:
        await server.serve_forever()
