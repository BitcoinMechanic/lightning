#!/usr/bin/env python3
"""Native CLNrest init/TLS/EOF/restart preflight; no daemon RPC or wallet.

Feeds the real plugin protocol through pipes. An HTTPS request with no rune
must fail before any daemon RPC. This complements the funded daemon matrix;
it does not establish rune enforcement or payment behavior.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import tempfile
import threading
import time

import customer_https as https

HASHES=dict(btc='16e3ff3e0285eb915b04e35f868c8b93a876427c37983d3a49b5c6bda446417d',
            xbt='ad9a2ba9bf14df2dfb6360eef1b48e9852c3c3226e18e9cc4da677ac6feaf1b5')
CERTS=('ca.pem','ca-key.pem','server.pem','server-key.pem','client.pem','client-key.pem')


def need(ok,reason):
    if not ok:raise RuntimeError(reason)


class Plugin:
    def __init__(self,path,directory):
        self.messages=queue.Queue();self.counter=0
        self.stderr=(directory/'plugin-stderr.log').open('a')
        self.proc=subprocess.Popen([str(path)],cwd=directory,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=self.stderr,text=True,env=dict(os.environ,LIGHTNINGD_PLUGIN='1',TOKIO_WORKER_THREADS='2'))
        def drain():
            try:
                for line in self.proc.stdout:
                    if line.strip():self.messages.put(json.loads(line))
            finally:self.messages.put(None)
        self.thread=threading.Thread(target=drain,daemon=True);self.thread.start()

    def call(self,method,params):
        self.counter+=1
        self.proc.stdin.write(json.dumps(dict(jsonrpc='2.0',id=self.counter,method=method,params=params))+'\n\n')
        self.proc.stdin.flush();end=time.monotonic()+15
        while time.monotonic()<end:
            try:value=self.messages.get(timeout=max(.01,end-time.monotonic()))
            except queue.Empty:break
            need(value is not None,'native_rest_plugin_exited')
            if value.get('id')==self.counter:
                need('result' in value and 'error' not in value,'native_rest_protocol_refused')
                return value['result']
        raise RuntimeError('native_rest_protocol_timeout')

    def close(self):
        self.proc.stdin.close()
        try:self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait();raise RuntimeError('native_rest_EOF_did_not_exit')
        finally:self.thread.join(timeout=1);self.proc.stdout.close();self.stderr.close()
        need(self.proc.returncode==0,'native_rest_plugin_exit_failed')


def run(paths):
    for chain,path in paths.items():
        need(path.is_file() and not path.is_symlink() and os.access(path,os.X_OK)
             and hashlib.sha256(path.read_bytes()).hexdigest()==HASHES[chain],'native_rest_binary_changed')
    for chain,path in paths.items():
        with tempfile.TemporaryDirectory(prefix='native-clnrest-') as temp:
            directory=Path(temp);certs=directory/'certs'
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            saved=None
            for restart in (False,True):
                plugin=Plugin(path,directory)
                try:
                    manifest=plugin.call('getmanifest',{})
                    need({'clnrest-host','clnrest-port','clnrest-protocol','clnrest-certs'} <=
                         {o['name'] for o in manifest['options']},'native_rest_options_missing')
                    # lightningd sends declared defaults, including an array
                    # for multi-valued options, in its init request.
                    options={o['name']:([o['default']] if o.get('multi') else o['default'])
                             for o in manifest['options'] if 'default' in o}
                    options.update({'clnrest-host':'127.0.0.1','clnrest-port':port,
                        'clnrest-protocol':'https','clnrest-certs':str(certs)})
                    reply=plugin.call('init',dict(options=options,configuration={
                        'lightning-dir':str(directory),'rpc-file':str(directory/'absent-rpc'),
                        'network':'regtest','startup':True,'feature_set':{}}))
                    need(reply=={},'native_rest_init_disabled')
                    end=time.monotonic()+20;ready=False
                    while time.monotonic()<end:
                        need(plugin.proc.poll() is None,'native_rest_server_exited')
                        conn=None
                        try:
                            conn=http.client.HTTPSConnection('127.0.0.1',port,timeout=1,
                                context=https.context((certs/'ca.pem').read_text()))
                            conn.request('POST','/v1/getinfo',body=b'{}',headers={'Content-Type':'application/json'})
                            response=conn.getresponse();value=json.loads(response.read(65536))
                            need(response.status==403 and value.get('code')==1501,'native_rest_no_rune_not_refused')
                            ready=True;break
                        except (OSError,http.client.HTTPException):time.sleep(.05)
                        finally:
                            if conn:conn.close()
                    need(ready,'native_rest_TLS_timeout')
                    current={name:hashlib.sha256((certs/name).read_bytes()).hexdigest() for name in CERTS}
                    if restart:need(current==saved,'native_rest_certificates_changed_after_restart')
                    else:saved=current
                    need(not (directory/'absent-rpc').exists(),'native_rest_unexpected_rpc_socket')
                finally:plugin.close()
            print('PASS: '+chain+' native CLNrest manifest, init, verified HTTPS, missing-rune refusal and same-certificate restart',flush=True)
    return dict(schema=1,fixture='native-clnrest-startup-0082b',binaries=HASHES,
        native_init=True,verified_https=True,missing_rune_refused=True,same_certificates_after_restart=True,
        clean_EOF_exit=True,daemon_rpc=False,funded=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for chain in HASHES:parser.add_argument('--'+chain+'-plugin',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True);args=parser.parse_args();os.umask(0o077)
    result=run({chain:getattr(args,chain+'_plugin').resolve() for chain in HASHES})
    with args.receipt.open('x') as stream:json.dump(result,stream,indent=2);stream.write('\n')
