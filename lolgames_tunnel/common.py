import base64
import json
import os
import random
import socket
import string
import struct
import sys
import time

BROKER_PORT = 10080
LOCAL_REGISTRY_PORT = 10081
CONTROL_PORT = 20222
DOMAIN = 'agentsweb.space'
WRITE_BUF_HIGH = int(os.environ.get('LOLGAMES_TUNNEL_WRITE_BUF', str(1024 * 1024)) or str(1024 * 1024))
WRITE_BUF_LOW = WRITE_BUF_HIGH // 4
SOCK_BUF = int(os.environ.get('LOLGAMES_TUNNEL_SOCK_BUF', str(4 * 1024 * 1024)) or str(4 * 1024 * 1024))


def b64(data):
    return base64.b64encode(data).decode()


def ub64(text):
    return base64.b64decode(text.encode())


async def send(writer, payload, lock=None):
    data = (json.dumps(payload, separators=(',', ':')) + '\n').encode()
    if lock:
        async with lock:
            writer.write(data)
            await writer.drain()
    else:
        writer.write(data)
        await writer.drain()


def raise_buffer_limits(writer):
    """Let the transport buffer MBs of frames so a long RTT does not reduce
    throughput to one frame per round trip (stop-and-wait)."""
    try:
        writer.transport.set_write_buffer_limits(high=WRITE_BUF_HIGH, low=WRITE_BUF_LOW)
    except Exception:
        pass
    sock = writer.get_extra_info('socket')
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
    except Exception:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
    except Exception:
        pass
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


async def recv(reader):
    line = await reader.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        # A client that sends a non-JSON line (or an empty line) must not take
        # down the control handler; treat it as a closed connection.
        return None


def original_dst(sock):
    try:
        raw = sock.getsockopt(socket.SOL_IP, 80, 16)  # SO_ORIGINAL_DST
        _family, port, a, b, c, d = struct.unpack('!HHBBBBxxxxxxxx', raw)
        return socket.inet_ntoa(bytes([a, b, c, d])), port
    except Exception:
        try:
            return sock.getsockname()
        except Exception:
            return ('0.0.0.0', 0)


def parse_host(buf):
    try:
        head = buf.decode('iso-8859-1', 'ignore')
        for line in head.split('\r\n'):
            if line.lower().startswith('host:'):
                host = line.split(':', 1)[1].strip().split()[0]
                if ':' in host:
                    host = host.rsplit(':', 1)[0]
                return host.lower()
    except Exception:
        pass
    return None


def rand_name():
    words = ['blue', 'red', 'green', 'fast', 'tiny', 'mega', 'nova', 'pixel',
             'fuzzy', 'lucky', 'orbit', 'mango', 'tiger', 'fox', 'panda',
             'rocket']
    return random.choice(words) + '-' + random.choice(words) + '-' + ''.join(
        random.choice(string.digits) for _ in range(4)
    )


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def write_status(path, payload):
    if not path:
        return
    data = {**payload, 'updated_at': now_iso()}
    tmp = f'{path}.tmp'
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, separators=(',', ':'))
            f.write('\n')
        os.replace(tmp, path)
    except Exception as exc:
        print(f'failed to write status file {path}: {exc}', file=sys.stderr, flush=True)
