#!/usr/bin/env python3
"""Real local TLS/process boundaries with synthetic coordinator responses.

The separate funded fixture uses the actual CLNrest/rune implementation. These
tests establish client TLS and journal/process behavior, not real settlement.
"""
import contextlib
import copy
import http.server
import io
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bounded_authority as ba
import cross_contract as cc
import cross_worker as cw
import customer_https as https
import held_gate as hg
import test_https_engine as fixture
import worker_pairing as pairing
from test_cross_worker import Remote
from test_route_plan import NOW


class LaunchTests(unittest.TestCase):
    def test_explicit_registration_on_both_prefixes_and_same_restart_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            lab=fixture.HttpsLab(SimpleNamespace(results_parent=Path(temp)))
            self.addCleanup(lab.close)
            launched=[]
            def capture(base,command,log):launched.append(list(map(str,command)))
            with patch.object(fixture.cross.routes.gate.Lab,'start',capture):
                for chain,name in (('btc','btc-source'),('xbt','xbt-recipient')):
                    log=lab.root/name/'console.log'
                    command=[Path('/opt')/chain/'bin/lightningd','--disable-plugin=clnrest','--disable-plugin=pay']
                    lab.start(command,log);first=launched[-1]
                    registration='--important-plugin=/opt/'+chain+'/libexec/c-lightning/plugins/clnrest'
                    self.assertEqual(first.count(registration),1)
                    self.assertNotIn('--disable-plugin=clnrest',first);self.assertIn('--disable-plugin=pay',first)
                    rest=[a for a in first if a.startswith('--clnrest-')]
                    self.assertEqual(len(rest),4)
                    lab.start(lab.commands[name],log);second=launched[-1]
                    self.assertEqual(second.count(registration),1)
                    self.assertEqual([a for a in second if a.startswith('--clnrest-')],rest)

    def test_intermediary_nodes_keep_rest_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            lab=fixture.HttpsLab(SimpleNamespace(results_parent=Path(temp)));self.addCleanup(lab.close)
            launched=[]
            with patch.object(fixture.cross.routes.gate.Lab,'start',lambda base,c,l:launched.append(c)):
                lab.start([Path('/opt/btc/bin/lightningd'),'--disable-plugin=clnrest'],lab.root/'btc-public/console.log')
            self.assertIn('--disable-plugin=clnrest',launched[0])
            self.assertFalse(any(str(a).startswith('--clnrest-') for a in launched[0]))
            self.assertEqual(lab.rest,{})


class ScopeTests(unittest.TestCase):
    def test_core_permission_error_is_required_and_diagnostics_omit_remote_text(self):
        with tempfile.TemporaryDirectory() as temp:
            lab=SimpleNamespace(root=Path(temp))
            expected=dict(code=1502,message='synthetic secret credential and request',data={'private':'hidden'})
            fixture.scope_response(lab,'btc-cln-v1','outgoing','wrong_grant',(401,expected),401,1502)
            for result in ((401,dict(expected,code=19537)),(201,expected),(500,expected),
                           (401,dict(expected,code=None)),(401,{'code':1502}),
                           (401,dict(expected,result={})),(401,dict(expected,code=True))):
                with self.assertRaisesRegex(RuntimeError,'https_scope_response_changed'):
                    fixture.scope_response(lab,'btc-cln-v1','outgoing','wrong_grant',result,401,1502)
            report=(lab.root/'https-scope.jsonl').read_text()
            for private in ('synthetic secret','credential and request','hidden','"message"','"data"'):
                self.assertNotIn(private,report)
            self.assertEqual(len(report.splitlines()),8)

    def test_all_scope_cases_use_their_transport_specific_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);configs=[];topologies={};calls=[]
            for label,profile in (('btc','btc-cln-v1'),('xbt','xbt-privkeyio-v1')):
                clients=[]
                for kind in ('incoming','outgoing'):
                    grant=dict(chain=dict(profile=profile),kind=kind)
                    config=dict(grant=grant,rune='valid_synthetic_credential_'+label+kind,ca_pem=label+kind)
                    configs.append(config)
                    client=SimpleNamespace(node=config,gid='11'*32,credential={'rune':config['rune']},
                        call=lambda op,g=grant:dict(grant=g),mutations=lambda:[])
                    clients.append(client)
                topologies[label]=(None,None,clients)
            lab=SimpleNamespace(root=root,config=lambda node,rune,grant:node)
            def raw(config,method,params,*,rune=None):
                calls.append((method,params,rune))
                if type(params) is list:return 500,dict(code=None,message='parse failure')
                if rune is not None:
                    self.assertEqual(len(rune),len(config['rune']))
                    self.assertNotEqual(rune,config['rune'])
                    return 401,dict(code=1501,message='invalid signature')
                return 401,dict(code=1502,message='not permitted')
            class Client:
                def __init__(self,config):self.config=config
                def call(self,op):
                    if self.config not in configs:raise https.HttpsError('synthetic identity or CA mismatch')
                    return dict(grant=self.config['grant'])
            with patch.object(fixture,'raw',raw),patch.object(fixture.https,'Client',Client), \
                    contextlib.redirect_stdout(io.StringIO()):
                fixture.scope_checks(lab,topologies)
            rows=[json.loads(line) for line in (root/'https-scope.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls),48);self.assertEqual(len(rows),48)
            self.assertEqual(sum(r['rpc_code']==1502 for r in rows),40)
            self.assertEqual(sum(r['rpc_code']==1501 for r in rows),4)
            self.assertEqual(sum(r['http_status']==500 for r in rows),4)


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('openssl'):raise unittest.SkipTest('openssl required for disposable TLS certificates')
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        for label in ('server','untrusted'):
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2',
                '-keyout',str(cls.root/(label+'.key')),'-out',str(cls.root/(label+'.pem')),
                '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost',
                '-addext','basicConstraints=critical,CA:TRUE',
                '-addext','keyUsage=critical,digitalSignature,keyCertSign'],check=True,capture_output=True)
        cls.pem=(cls.root/'server.pem').read_text()

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def setUp(self):
        self.remote=Remote();self.calls=[];self.drop=None;self.redirect=None
        self.servers=[];self.urls={}
        for kind in cw.KINDS:self.server(kind)
        self.addCleanup(self.close)

    def close(self):
        for server,thread in self.servers:server.shutdown();server.server_close();thread.join()

    def server(self,kind):
        outer=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']));p=json.loads(raw)
                outer.calls.append((kind,p['operation']))
                if self.path!='/v1/swap-customer-call' or self.headers.get('Rune')!='synthetic_'+kind+'_'*32:
                    self.send_response(401);self.end_headers();return
                if outer.redirect is not None:
                    self.send_response(302);self.send_header('Location',outer.redirect);self.end_headers();return
                assert p['grant_id']==ba.digest(outer.remote.c['grants'][kind])
                value=outer.remote.obs[kind]['info'] if p['operation']=='info' else outer.remote(kind,
                    p['grant_id'],p['operation'],p['body'])
                if outer.drop==(kind,p['operation']):
                    outer.drop=None;self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                data=json.dumps(value).encode();self.send_response(201)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
                self.end_headers();self.wfile.write(data)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(self.root/'server.pem',self.root/'server.key')
        server.socket=ctx.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True);thread.start()
        self.servers.append((server,thread));self.urls[kind]='https://localhost:'+str(server.server_port)

    def config(self,kind):
        return dict(url=self.urls[kind],ca_pem=self.pem,rune='synthetic_'+kind+'_'*32,grant=self.remote.c['grants'][kind])

    def test_real_TLS_hostname_CA_and_redirect_boundaries(self):
        config=self.config('incoming');ctx=https.context(config['ca_pem'])
        self.assertTrue(ctx.check_hostname);self.assertEqual(ctx.verify_mode,ssl.CERT_REQUIRED)
        self.assertEqual(ctx.minimum_version,ssl.TLSVersion.TLSv1_2)
        self.assertEqual(https.Client(config).call('info')['grant'],config['grant'])
        for change in (dict(ca_pem=(self.root/'untrusted.pem').read_text()),
                       dict(url=config['url'].replace('localhost','127.0.0.1'))):
            before=len(self.calls)
            with self.assertRaisesRegex(https.HttpsError,'coordinator_reply'):https.Client(dict(config,**change)).call('info')
            self.assertEqual(len(self.calls),before)
        self.redirect=self.urls['outgoing']+'/v1/swap-customer-call'
        before=len(self.calls)
        with self.assertRaises(https.HttpsError):https.Client(config).call('info')
        self.assertEqual(self.calls[before:],[('incoming','info')])

    def shifted_remote(self,direction):
        r=Remote(direction);shift=int(time.time())-NOW
        for g in r.c['grants'].values():g['created_at']+=shift;g['expires_at']+=shift
        for p in (r.c['incoming'],r.c['outgoing']):p['expires_at']+=shift
        r.c['outgoing']['created_at']+=shift;r.c['created_at']+=shift;r.c['pay_until']+=shift
        for key in ('fetched_at_ms','ticker_at_ms'):r.c['pricing'][key]+=shift*1000
        r.c['pricing']['expires_at']+=shift
        for k,o in r.obs.items():o['checked_at']+=shift;o['info']['grant']=r.c['grants'][k]
        cc.validate(r.c);self.remote=r

    def test_real_HTTPS_saved_pairing_SIGKILL_and_dropped_send_reply(self):
        for direction in ('forward','reverse'):
            with self.subTest(direction=direction),tempfile.TemporaryDirectory() as temp:
                self.shifted_remote(direction);root=Path(temp)
                lab=SimpleNamespace(root=root,current_stage='synthetic-https',config=lambda node,rune,grant:self.config(node['kind']))
                clients={k:SimpleNamespace(node=dict(kind=k),credential=dict(rune=self.config(k)['rune']),
                    call=lambda op,k=k:self.remote.obs[k]['info']) for k in cw.KINDS}
                directory=root/'cross-worker-tls';cw.initialize_fixture(directory)
                d=fixture.Driver(lab,directory,clients);d.cid=d.run('prepare',contract=self.remote.c)['contract_id']
                self.assertNotIn('clients',d.request('step'));self.assertNotIn('binding',d.request('step'))
                d.run('confirm',confirmation=d.cid)
                for k in cw.KINDS:
                    d.run('step',fault=dict(kind=k,operation='enroll'),crash=True);d.run('step')
                d.run('step',fault=dict(kind='incoming',operation='invoice'),crash=True);d.run('step')
                issued=d.run('invoice');self.remote.hold()
                if direction=='forward':
                    self.drop=('outgoing','send')
                    with self.assertRaises(fixture.cross.DriverError):d.run('step')
                    self.assertIsNone(self.drop)
                else:d.run('step',fault=dict(kind='outgoing',operation='send'),crash=True)
                d.run('step');self.remote.complete();d.run('step')
                d.run('step',fault=dict(kind='incoming',operation='release'),crash=True)
                self.assertEqual(d.run('step')['state'],'settled');self.assertEqual(d.run('invoice'),issued)
                self.assertEqual(sum(op=='send' for _,op,_ in self.remote.calls),1)
                before=len(self.calls);d.run('status');self.assertEqual(len(self.calls),before)
                (directory/'https-pairing/restored.json').write_text('{}')
                self.assertTrue(d.run('status')['blocked'])
                with self.assertRaises(fixture.cross.DriverError):d.run('step')
                self.assertEqual(len(self.calls),before)


if __name__=='__main__':unittest.main()
