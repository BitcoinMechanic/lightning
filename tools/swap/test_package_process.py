"""Sealed runtime CLI and external fixture-reader protocol; synthetic TLS, no funds."""
import copy
import http.server
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time

import held_gate as hg
import service
from test_cross_worker import Remote
from test_route_plan import NOW
from test_package_engine import Driver
import package_runtime

class Lab:
    def __init__(self,runtime):self.procs=[];self.runtime=runtime
    def wait(self,fn,proc=None):
        until=time.monotonic()+15
        while time.monotonic()<until:
            assert proc is None or proc.poll() is None,proc.communicate()
            try:
                value=fn()
                if value:return value
            except BlockingIOError:pass
            time.sleep(.03)
        raise RuntimeError('synthetic HTTPS service timeout')


def run(direction,failed,swap,controller):
    import test_cross_worker
    remote=Remote(direction);now=int(time.time());test_cross_worker.NOW=now
    for g in remote.c['grants'].values():g.update(created_at=now-10,expires_at=now+3600)
    remote.c['outgoing'].update(created_at=now,expires_at=now+1800,invoice='lnbcrt1'+'b'*150)
    for k in ('incoming','outgoing'):remote.obs[k]['info']['grant']=remote.c['grants'][k]
    lock=threading.Lock()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);runtime=root/"a";runtime.mkdir();alternate=root/"b";alternate.mkdir()
        for target,g in ((runtime,"a"),(alternate,"b")):
            package_runtime.stage(swap,controller,target/"controller","controller",g)
        lab=Lab(runtime);servers=[];threads=[]
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost',
            '-addext','subjectAltName=IP:127.0.0.1','-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem')],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(root/'cert.pem',root/'key.pem')
        def handler(kind):
            class H(http.server.BaseHTTPRequestHandler):
                def log_message(self,*a):pass
                def do_POST(self):
                    with lock:
                        p=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        assert self.path=='/v1/swap-customer-call'
                        op=p['operation'];remote.obs[kind]['checked_at']=int(time.time())
                        if op=='info':value=copy.deepcopy(remote.obs[kind]['info'])
                        elif op=='plan':value=copy.deepcopy(remote.c['outgoing'])
                        else:value=remote(kind,p['grant_id'],op,p['body'])
                        payload=json.dumps(value).encode()
                    self.send_response(201);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)))
                    self.end_headers();self.wfile.write(payload)
            return H
        nodes={}
        for kind in ('incoming','outgoing'):
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler(kind));server.socket=ctx.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();servers.append(server);threads.append(thread)
            nodes[kind]=dict(url='https://127.0.0.1:'+str(server.server_port),ca_pem=(root/'cert.pem').read_text(),
                rune='synthetic_'+kind+'_'*32,grant=remote.c['grants'][kind])
        try:
            d=Driver(lab,root/'service-worker-test');d.action('pair',binding=dict(direction=direction,nodes=nodes),confirmed=True)
            v=d.action('prepare',direction=direction,invoice=remote.c['outgoing']['invoice']);d.cid=v['confirmation']
            with lock:remote.c=d.record()['contract']
            d.start()
            assert not remote.entries
            d.action('confirm',cid=d.cid,confirmation=d.cid,confirmed=True)
            d.wait(lambda:d.status()['state']=='waiting_for_payment')
            invoice=d.action('invoice',cid=d.cid)
            with lock:remote.hold()
            d.wait(lambda:d.status()['state']=='outgoing_pending');saved=d.record();d.stop(crash=True)
            lab.runtime=alternate
            d=Driver(lab,d.directory);d.cid=next(iter(service.validate(json.loads((d.directory/'state.json').read_text()))['jobs']))
            assert d.record()==saved and d.action('invoice',cid=d.cid)==invoice
            d.action('configure',paused=True,markup_bps=100)
            d.start()
            with lock:remote.complete(failed=failed)
            d.wait(lambda:d.status()['terminal']);d.stop()
            op='fail' if failed else 'release'
            assert d.status()['state']==('failed' if failed else 'settled')
            assert d.action('invoice',cid=d.cid)==invoice
            assert sum(op=='send' for _,op,_ in remote.calls)==1
            print('PASS synthetic packaged TLS subprocess service:',direction,op,flush=True)
        finally:
            for p in lab.procs:
                if p.poll() is None:p.kill();p.communicate(timeout=5)
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join()
