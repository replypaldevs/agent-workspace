import argparse
import asyncio
import json
import os
import sys

from .common import CONTROL_PORT, b64, now_iso, raise_buffer_limits, recv, send, ub64, write_status

CONN_QUEUE_MAX = int(os.environ.get('LOLGAMES_TUNNEL_CONN_QUEUE', '64') or '64')
OUT_QUEUE_MAX = int(os.environ.get('LOLGAMES_TUNNEL_OUT_QUEUE', '128') or '128')


def default_history_file(status_file):
    base, ext = os.path.splitext(status_file)
    return f'{base}-history.jsonl' if ext else f'{status_file}-history.jsonl'


def add_event(args, status, event_type, message, level='ok', **fields):
    event = {
        'time': now_iso(),
        'level': level,
        'type': event_type,
        'message': message,
        'state': status.get('state', ''),
        'active_connections': int(status.get('active_connections') or 0),
        'total_connections': int(status.get('total_connections') or 0),
        'reconnects': int(status.get('reconnects') or 0),
        **fields,
    }
    try:
        os.makedirs(os.path.dirname(args.history_file) or '.', exist_ok=True)
        with open(args.history_file, 'a', encoding='utf-8') as f:
            json.dump(event, f, separators=(',', ':'))
            f.write('\n')
    except Exception as exc:
        print(f'failed to write history file {args.history_file}: {exc}', file=sys.stderr, flush=True)


async def client_once(args):
    target = args.target
    if target.startswith('localhost:'):
        host = '127.0.0.1'
        target_port = int(target.split(':', 1)[1])
    elif ':' in target:
        host, port_text = target.rsplit(':', 1)
        target_port = int(port_text)
    else:
        raise SystemExit('target must be an SSH listener, e.g. 127.0.0.1:22 or 127.0.0.1:2222')

    if target_port not in (22, 2222):
        raise SystemExit('the Python broker is SSH/raw-TCP only; use FRP for HTTP services')
    public_port = args.public_port or target_port
    status_base = {
        'ok': False,
        'state': 'connecting',
        'server': args.server,
        'control_port': CONTROL_PORT,
        'target': target,
        'target_host': host,
        'target_port': target_port,
        'public_port': public_port,
        'subdomain': args.name or '',
        'pid': os.getpid(),
        'started_at': now_iso(),
        'last_error': '',
        'last_ping_at': '',
        'last_pong_at': '',
        'last_open_at': '',
        'last_close_at': '',
        'active_connections': 0,
        'total_connections': 0,
        'dropped_connections': int(getattr(args, '_dropped_connections', 0)),
        'reconnects': getattr(args, '_reconnects', 0),
        'history_file': args.history_file,
    }
    add_event(args, status_base, 'connect', f'connecting to broker {args.server}:{CONTROL_PORT}', level='info')
    write_status(args.status_file, status_base)

    reader, writer = await asyncio.open_connection(args.server, CONTROL_PORT)
    raise_buffer_limits(writer)
    lock = asyncio.Lock()
    conns = {}
    out_queue = asyncio.Queue(maxsize=OUT_QUEUE_MAX)

    async def control_out():
        """Single writer for the shared control socket. Every broker-bound
        message goes through the bounded out_queue, so a slow broker or a
        congested network applies bounded backpressure instead of growing
        memory without limit."""
        try:
            while True:
                msg = await out_queue.get()
                if msg is None:
                    break
                await send(writer, msg, lock)
        except asyncio.CancelledError:
            pass

    out_task = asyncio.create_task(control_out())
    try:
        registration = {
            'type': 'register',
            'subdomain': args.name,
            'public_port': public_port,
            'display_port': target_port,
        }
        run_id = (os.environ.get('LOLGAMES_TUNNEL_RUN_ID') or os.environ.get('GITHUB_RUN_ID') or '').strip()
        if run_id:
            registration['run_id'] = run_id
        expires_at = os.environ.get('LOLGAMES_TUNNEL_EXPIRES_AT', '').strip()
        if expires_at:
            registration['expires_at'] = expires_at
        try:
            out_queue.put_nowait(registration)
        except asyncio.QueueFull:
            raise ConnectionError('control socket backed up before registration')
        try:
            msg = await asyncio.wait_for(recv(reader), timeout=45)
        except asyncio.TimeoutError as exc:
            # A broker reboot can orphan the control connection before
            # registration; fail fast instead of hanging in state 'connecting'.
            raise ConnectionError('broker did not answer registration within 45s') from exc
        if not msg:
            raise ConnectionError('broker closed control connection before registration')
    except BaseException:
        out_task.cancel()
        writer.close()
        raise
    print(msg['url'], flush=True)
    status = {
        **status_base,
        'ok': True,
        'state': 'registered',
        'url': msg.get('url', ''),
        'subdomain': msg.get('subdomain') or args.name or '',
        'public_port': int(msg.get('port') or public_port),
    }
    add_event(args, status, 'register', f'registered {status.get("url", "")} -> {target}', url=status.get('url', ''), target=target)
    write_status(args.status_file, status)

    async def keepalive():
        while True:
            await asyncio.sleep(args.keepalive_interval)
            status['last_ping_at'] = now_iso()
            add_event(args, status, 'ping', 'sent keepalive ping')
            write_status(args.status_file, status)
            try:
                out_queue.put_nowait({'type': 'ping'})
            except asyncio.QueueFull:
                pass

    async def close_conn(conn_id, entry, error='', notify_broker=True):
        """Tear down one connection and its relay task. Safe to call once;
        later calls from the local pump or broker close are no-ops."""
        if conns.pop(conn_id, None) is None:
            return
        if not entry['closed']:
            entry['closed'] = True
            status['last_close_at'] = now_iso()
            status['active_connections'] = max(0, int(status.get('active_connections') or 0) - 1)
            add_event(args, status, 'close', f'closed connection {conn_id}' + (f': {error}' if error else ''), connection_id=conn_id, error=error or '')
            write_status(args.status_file, status)
            if notify_broker:
                try:
                    out_queue.put_nowait({'type': 'close', 'id': conn_id, 'error': error})
                except asyncio.QueueFull:
                    pass
        task = entry.get('task')
        if task:
            task.cancel()
        try:
            entry['writer'].close()
        except Exception:
            pass

    async def pump_local(conn_id, local_reader):
        try:
            while True:
                entry = conns.get(conn_id)
                if entry is None or entry['closed']:
                    break
                await entry['send_allowed'].wait()
                data = await local_reader.read(32768)
                if not data:
                    break
                # A full control queue is normal when the local target can
                # produce data faster than the tunnel uplink.  Backpressure
                # this connection's producer until the single control writer
                # catches up.  Dropping here truncated every sustained
                # download once the small queue filled (roughly a few MiB).
                await out_queue.put({'type': 'data', 'id': conn_id, 'data': b64(data)})
        finally:
            entry = conns.get(conn_id)
            if entry is not None and not entry['closed']:
                await close_conn(conn_id, entry)

    async def relay_to_local(conn_id, entry):
        """Broker -> local pump for one connection. Reads from the connection's
        bounded queue so a slow local target blocks only its own connection."""
        try:
            while True:
                msg = await entry['queue'].get()
                if msg is None:
                    break
                data = ub64(msg.get('data') or '')
                if data:
                    entry['writer'].write(data)
                    await entry['writer'].drain()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                entry['writer'].close()
            except Exception:
                pass

    keepalive_task = asyncio.create_task(keepalive())
    try:
        while True:
            try:
                msg = await asyncio.wait_for(recv(reader), timeout=45)
            except asyncio.TimeoutError:
                print('control session idle timeout; reconnecting', file=sys.stderr, flush=True)
                status['last_error'] = 'control session idle timeout'
                add_event(args, status, 'reconnect', 'control session idle timeout; reconnecting', level='warn', error=status['last_error'])
                write_status(args.status_file, status)
                break
            if msg is None:
                break
            typ = msg.get('type')
            conn_id = msg.get('id')
            if typ == 'pong':
                status['last_pong_at'] = now_iso()
                add_event(args, status, 'pong', 'received keepalive pong')
                write_status(args.status_file, status)
                continue
            if typ == 'open':
                status['last_open_at'] = now_iso()
                status['active_connections'] = int(status.get('active_connections') or 0) + 1
                status['total_connections'] = int(status.get('total_connections') or 0) + 1
                public_port = int(msg.get('port') or target_port)
                connect_port = target_port
                add_event(args, status, 'open', f'opened connection {conn_id} on port {public_port}', connection_id=conn_id, public_port=public_port, target_port=connect_port)
                write_status(args.status_file, status)
                try:
                    local_reader, local_writer = await asyncio.open_connection(host, connect_port)
                except Exception as exc:
                    status['last_error'] = str(exc)
                    status['active_connections'] = max(0, int(status.get('active_connections') or 0) - 1)
                    add_event(args, status, 'error', f'target connection failed: {exc}', level='error', connection_id=conn_id, target_port=connect_port, error=str(exc))
                    write_status(args.status_file, status)
                    try:
                        out_queue.put_nowait({'type': 'close', 'id': conn_id, 'error': str(exc)})
                    except asyncio.QueueFull:
                        pass
                    continue
                entry = {
                    'writer': local_writer,
                    'queue': asyncio.Queue(maxsize=CONN_QUEUE_MAX),
                    'task': None,
                    'closed': False,
                    'send_allowed': asyncio.Event(),
                }
                entry['send_allowed'].set()
                conns[conn_id] = entry
                initial = ub64(msg.get('initial', ''))
                if initial:
                    try:
                        entry['queue'].put_nowait({'type': 'data', 'data': b64(initial)})
                    except asyncio.QueueFull:
                        status['dropped_connections'] = int(status.get('dropped_connections') or 0) + 1
                        add_event(args, status, 'drop', f'dropped slow connection {conn_id}', level='warn', connection_id=conn_id)
                        write_status(args.status_file, status)
                        await close_conn(conn_id, entry, error='local consumer too slow')
                        continue
                entry['task'] = asyncio.create_task(relay_to_local(conn_id, entry))
                task = asyncio.create_task(pump_local(conn_id, local_reader))
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            elif typ == 'flow' and conn_id in conns:
                if msg.get('paused'):
                    conns[conn_id]['send_allowed'].clear()
                else:
                    conns[conn_id]['send_allowed'].set()
            elif typ == 'data' and conn_id in conns:
                entry = conns[conn_id]
                try:
                    entry['queue'].put_nowait(msg)
                except asyncio.QueueFull:
                    status['dropped_connections'] = int(status.get('dropped_connections') or 0) + 1
                    add_event(args, status, 'drop', f'dropped slow connection {conn_id}', level='warn', connection_id=conn_id)
                    write_status(args.status_file, status)
                    await close_conn(conn_id, entry, error='local consumer too slow')
            elif typ == 'close' and conn_id in conns:
                await close_conn(conn_id, conns[conn_id], notify_broker=False)
    finally:
        args._dropped_connections = int(status.get('dropped_connections') or 0)
        status['ok'] = False
        status['state'] = 'disconnected'
        add_event(args, status, 'reconnect', 'control connection disconnected', level='warn')
        write_status(args.status_file, status)
        keepalive_task.cancel()
        await asyncio.gather(keepalive_task, return_exceptions=True)
        for conn_id in list(conns):
            await close_conn(conn_id, conns[conn_id], error='control session lost', notify_broker=False)
        try:
            out_queue.put_nowait(None)
        except asyncio.QueueFull:
            out_task.cancel()
        try:
            await asyncio.wait_for(out_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            out_task.cancel()
            await asyncio.gather(out_task, return_exceptions=True)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def client(args):
    if not args.history_file:
        args.history_file = default_history_file(args.status_file)
    args._reconnects = 0
    while True:
        try:
            await client_once(args)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:
            status = {
                'ok': False,
                'state': 'error',
                'server': args.server,
                'control_port': CONTROL_PORT,
                'target': args.target,
                'subdomain': args.name or '',
                'pid': os.getpid(),
                'reconnects': args._reconnects,
                'dropped_connections': int(getattr(args, '_dropped_connections', 0)),
                'last_error': str(exc),
                'history_file': args.history_file,
            }
            add_event(args, status, 'error', f'control connection lost: {exc}', level='error', error=str(exc))
            write_status(args.status_file, status)
            print(f'control connection lost: {exc}; reconnecting in {args.reconnect_delay}s', file=sys.stderr, flush=True)
        args._reconnects += 1
        await asyncio.sleep(args.reconnect_delay)


def add_client_args(parser):
    parser.add_argument('target')
    parser.add_argument('--server', default='agentsweb.space')
    parser.add_argument('--name')
    parser.add_argument('--public-port', type=int, help='public SSH port; never an HTTP listener')
    parser.add_argument('--reconnect-delay', type=float, default=2.0, help='seconds to wait before reconnecting the control session after a broker reset')
    parser.add_argument('--keepalive-interval', type=float, default=10.0, help='seconds between control-session pings')
    parser.add_argument('--status-file', default=os.environ.get('LOLGAMES_TUNNEL_STATUS_FILE', '/tmp/lolgames-tunnel-status.json'), help='JSON status file written by the client')
    parser.add_argument('--history-file', default=os.environ.get('LOLGAMES_TUNNEL_HISTORY_FILE', ''), help='JSONL history file written by the client')
    return parser


def main():
    parser = add_client_args(argparse.ArgumentParser(description='Run agentsweb tunnel client'))
    args = parser.parse_args()
    if not args.history_file:
        args.history_file = default_history_file(args.status_file)
    asyncio.run(client(args))


if __name__ == '__main__':
    main()
