#!/usr/bin/env python3
import argparse, asyncio, base64, json, os, random, socket, string, struct, sys

BROKER_PORT=10080
CONTROL_PORT=20222
DOMAIN='lolgames.net'
REG={}
CTRL_BY_KEY={}

def b64(b): return base64.b64encode(b).decode()
def ub64(s): return base64.b64decode(s.encode())
async def send(w,obj,lock=None):
    data=(json.dumps(obj,separators=(',',':'))+'\n').encode()
    if lock:
        async with lock:
            w.write(data); await w.drain()
    else:
        w.write(data); await w.drain()
async def recv(r):
    line=await r.readline()
    if not line: return None
    return json.loads(line)

def original_dst(sock):
    try:
        od=sock.getsockopt(socket.SOL_IP, 80, 16)  # SO_ORIGINAL_DST
        _family, port, a,b,c,d = struct.unpack('!HHBBBBxxxxxxxx', od)
        return socket.inet_ntoa(bytes([a,b,c,d])), port
    except Exception:
        try: return sock.getsockname()
        except Exception: return ('0.0.0.0',0)

def parse_host(buf):
    try:
        head=buf.decode('iso-8859-1','ignore')
        for line in head.split('\r\n'):
            if line.lower().startswith('host:'):
                h=line.split(':',1)[1].strip().split()[0]
                if ':' in h: h=h.rsplit(':',1)[0]
                return h.lower()
    except Exception: pass
    return None

def rand_name():
    words=['blue','red','green','fast','tiny','mega','nova','pixel','fuzzy','lucky','orbit','mango','tiger','fox','panda','rocket']
    return random.choice(words)+'-'+random.choice(words)+'-'+''.join(random.choice(string.digits) for _ in range(4))

async def handle_control(r,w):
    hello=await recv(r)
    if not hello or hello.get('type')!='register':
        w.close()
        try: await w.wait_closed()
        except Exception: pass
        return
    sub=hello.get('subdomain') or rand_name()
    sub=sub.replace('.'+DOMAIN,'').lower()
    port=int(hello.get('public_port', 0))
    key=(sub,port)
    lock=asyncio.Lock()
    CTRL_BY_KEY[key]=(r,w,lock)
    display_port = int(hello.get('display_port') or port)
    url = f'http://{sub}.{DOMAIN}:{display_port}' if display_port else f'http://{sub}.{DOMAIN}'
    await send(w, {'type':'registered','url':url,'subdomain':sub,'port':port}, lock)
    try:
        while True:
            msg=await recv(r)
            if msg is None: break
            if msg.get('type') == 'ping':
                await send(w, {'type':'pong'}, lock)
                continue
            q=REG.get(('conn', msg.get('id')))
            if q: await q.put(msg)
    finally:
        CTRL_BY_KEY.pop(key,None)
        w.close()
        try: await w.wait_closed()
        except Exception: pass

async def handle_public(r,w):
    sock=w.get_extra_info('socket')
    _ip, port = original_dst(sock) if sock else ('',0)
    try:
        # Read the first bytes once. For HTTP this usually contains Host; for
        # raw TCP it preserves the first payload instead of losing partial data
        # through a cancelled readuntil().
        initial=await asyncio.wait_for(r.read(4096), timeout=2)
    except Exception:
        initial=b''
    host=parse_host(initial)
    sub=None
    if host and host.endswith('.'+DOMAIN): sub=host[:-(len(DOMAIN)+1)]
    ctrl=CTRL_BY_KEY.get((sub,port)) if sub else None
    if not ctrl and sub:
        ctrl=CTRL_BY_KEY.get((sub,0))
    if not ctrl and not sub:
        # Raw TCP has no hostname. Permit unique-port mode when exactly one
        # tunnel is registered for the original destination port.
        matches=[v for (s,p),v in CTRL_BY_KEY.items() if p == port]
        if len(matches) == 1:
            ctrl=matches[0]
    if not ctrl:
        body=(f'lolgames tunnel broker\nno active tunnel for host={host} port={port}\n').encode()
        w.write(b'HTTP/1.1 502 Bad Gateway\r\ncontent-type: text/plain\r\ncontent-length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        await w.drain(); w.close(); await w.wait_closed(); return
    cr,cw,lock=ctrl; cid=''.join(random.choice(string.ascii_letters+string.digits) for _ in range(12)); q=asyncio.Queue(); REG[('conn',cid)]=q
    try:
        await send(cw, {'type':'open','id':cid,'port':port,'host':host,'initial':b64(initial)}, lock)
    except Exception:
        REG.pop(('conn',cid),None)
        w.close()
        try: await w.wait_closed()
        except Exception: pass
        return
    async def pub_to_cli():
        try:
            while True:
                data=await r.read(32768)
                if not data: break
                await send(cw, {'type':'data','id':cid,'data':b64(data)}, lock)
        finally:
            await q.put({'type':'close'})
    async def cli_to_pub():
        try:
            while True:
                msg=await q.get()
                if msg.get('type')=='data': w.write(ub64(msg['data'])); await w.drain()
                elif msg.get('type')=='close': break
        finally:
            w.close()
    t1=asyncio.create_task(pub_to_cli())
    t2=asyncio.create_task(cli_to_pub())
    await asyncio.wait({t1,t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in (t1,t2): t.cancel()
    await asyncio.gather(t1,t2, return_exceptions=True)
    REG.pop(('conn',cid),None)
    try: await w.wait_closed()
    except Exception: pass

async def server():
    s1=await asyncio.start_server(handle_public,'0.0.0.0',BROKER_PORT)
    s2=await asyncio.start_server(handle_control,'0.0.0.0',CONTROL_PORT)
    print(f'broker public:{BROKER_PORT} control:{CONTROL_PORT}', flush=True)
    async with s1,s2: await asyncio.gather(s1.serve_forever(), s2.serve_forever())

async def client_once(args):
    target=args.target
    if target.startswith('localhost:'): host='127.0.0.1'; tport=int(target.split(':',1)[1])
    elif ':' in target: host,t=target.rsplit(':',1); tport=int(t)
    else: raise SystemExit('target must be host:port, e.g. localhost:3000')
    public_port=0 if args.same_port else (args.public_port or tport)
    r,w=await asyncio.open_connection(args.server, CONTROL_PORT)
    lock=asyncio.Lock(); conns={}
    await send(w, {'type':'register','subdomain':args.name,'public_port':public_port,'display_port':tport}, lock)
    msg=await recv(r); print(msg['url'], flush=True)
    async def keepalive():
        while True:
            await asyncio.sleep(args.keepalive_interval)
            await send(w, {'type':'ping'}, lock)
    async def pump_local(cid, lr):
        try:
            while True:
                data=await lr.read(32768)
                if not data: break
                await send(w, {'type':'data','id':cid,'data':b64(data)}, lock)
        finally:
            await send(w, {'type':'close','id':cid}, lock)
    ka=asyncio.create_task(keepalive())
    try:
        while True:
            msg=await recv(r)
            if msg is None: break
            typ=msg.get('type'); cid=msg.get('id')
            if typ=='pong':
                continue
            if typ=='open':
                connect_port = int(msg.get('port') or tport) if args.same_port else tport
                try:
                    lr,lw=await asyncio.open_connection(host,connect_port)
                except Exception as exc:
                    await send(w, {'type':'close','id':cid,'error':str(exc)}, lock)
                    continue
                conns[cid]=lw
                init=ub64(msg.get('initial',''))
                if init: lw.write(init); await lw.drain()
                asyncio.create_task(pump_local(cid,lr))
            elif typ=='data' and cid in conns:
                conns[cid].write(ub64(msg['data'])); await conns[cid].drain()
            elif typ=='close' and cid in conns:
                conns.pop(cid).close()
    finally:
        ka.cancel()
        await asyncio.gather(ka, return_exceptions=True)
        w.close()
        try: await w.wait_closed()
        except Exception: pass

async def client(args):
    while True:
        try:
            await client_once(args)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:
            print(f'control connection lost: {exc}; reconnecting in {args.reconnect_delay}s', file=sys.stderr, flush=True)
        await asyncio.sleep(args.reconnect_delay)

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('server')
    c=sub.add_parser('client'); c.add_argument('target'); c.add_argument('--server',default='lolgames.net'); c.add_argument('--name'); c.add_argument('--public-port',type=int); c.add_argument('--same-port', action='store_true', help='route any public port on this hostname to the same port on the target host'); c.add_argument('--reconnect-delay', type=float, default=2.0, help='seconds to wait before reconnecting the control session after a broker reset'); c.add_argument('--keepalive-interval', type=float, default=10.0, help='seconds between control-session pings')
    a=p.parse_args()
    asyncio.run(server() if a.cmd=='server' else client(a))
if __name__=='__main__': main()
