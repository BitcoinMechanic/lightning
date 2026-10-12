#!/usr/bin/env python3
"""HTTP boundary and saved pairing tests; synthetic transport responses."""
import copy
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

import bounded_authority as ba
import cross_worker as cw
import customer_https as https
import held_gate as hg
import worker_pairing as pairing
from test_cross_worker import Remote
from test_route_plan import NOW

PEM = '-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n'


def nodes(remote):
    return {k:dict(url='https://'+k+'.invalid',ca_pem=PEM,rune='synthetic_'+k+'_'*32,grant=g)
            for k,g in remote.c['grants'].items()}


class Response(io.BytesIO):
    def __init__(self, value, *, status=201, content_type='application/json'):
        super().__init__(value if type(value) is bytes else json.dumps(value).encode())
        self.status,self.content_type = status,content_type
    def getheader(self,name,default=None): return self.content_type if name=='Content-Type' else default


class Tests(unittest.TestCase):
    def setUp(self):
        self.remote = Remote(); self.calls = []; self.responses = []; self.closed = 0
        self.fake_ctx = object()
        self.addCleanup(patch.stopall)
        patch.object(https,'context',return_value=self.fake_ctx).start()
        outer = self
        class Connection:
            def __init__(self,host,port,**kwargs):
                outer.assertEqual(kwargs,dict(timeout=https.TIMEOUT,context=outer.fake_ctx))
                self.kind = host.split('.')[0]; self.params = None
            def request(self,method,path,body,headers):
                outer.assertEqual(method,'POST'); outer.assertEqual(path,'/v1/swap-customer-call')
                outer.assertEqual(headers['Rune'],nodes(outer.remote)[self.kind]['rune'])
                self.params = json.loads(body); outer.calls.append((self.kind,copy.deepcopy(self.params)))
            def getresponse(self):
                if outer.responses:
                    response = outer.responses.pop(0)
                    if isinstance(response,Exception): raise response
                    return response
                p = self.params; kind = self.kind
                if p['operation'] == 'info': return Response(outer.remote.obs[kind]['info'])
                return Response(outer.remote(kind,p['grant_id'],p['operation'],p['body']))
            def close(self): outer.closed += 1
        patch.object(https.http.client,'HTTPSConnection',Connection).start()

    def test_fixed_method_and_typed_body_with_fresh_grant_identity(self):
        client = https.Client(nodes(self.remote)['incoming'])
        client.call('inspect',dict(amount_msat=2000000))
        self.assertEqual([p['operation'] for _,p in self.calls],['info','inspect'])
        self.assertEqual(self.calls[1][1]['body'],dict(amount_msat=2000000)); self.assertEqual(self.closed,2)

    def test_invalid_endpoints_and_live_or_legacy_grants_never_connect(self):
        for url in ('http://node','https://u:p@node','https://node/v1','https://node?','https://node#',
                    'https://node:0','https://node:65536',' https://node','https://node\n','https://node\\evil',
                    'https://%65vil','https://node:bad','https://-host'):
            with self.subTest(url=url),self.assertRaises(ValueError): https.endpoint(url)
        self.assertEqual(https.endpoint('https://EXAMPLE.com:443/'),'https://example.com')
        self.assertEqual(https.endpoint('https://[::1]:8443'),'https://[::1]:8443')
        for field,value in [('environment','mainnet'),('profile',ba.ci.LEGACY_XBT)]:
            config=copy.deepcopy(nodes(self.remote)['incoming']);config['grant']['chain'][field]=value
            with self.assertRaises(ValueError): https.Client(config)
        self.assertEqual(self.calls,[])

    def test_raw_rpc_other_kind_and_extra_fields_refuse_locally(self):
        client=https.Client(nodes(self.remote)['incoming'])
        for op,body in [('send',{'entry_id':'11'*32}),('withdraw',{}),('getinfo',{}),('info',{'extra':True}),
                        ('inspect',{'amount_msat':True}),('release',{'entry_id':'11'*32,'preimage':'secret'})]:
            with self.subTest(op=op),self.assertRaises(ValueError):client.call(op,body)
        self.assertEqual(self.calls,[])

    def test_changed_grant_stops_before_mutation(self):
        client=https.Client(nodes(self.remote)['outgoing'])
        value=copy.deepcopy(self.remote.obs['outgoing']['info']);value['grant']['chain']['node_id']='03'+'55'*32
        self.responses=[Response(value)]
        with self.assertRaisesRegex(ValueError,'identity'):client.call('send',{'entry_id':'11'*32})
        self.assertEqual(len(self.calls),1)

    def test_redirect_error_malformed_duplicate_oversize_are_opaque_and_never_retry(self):
        bad=[Response(b'secret-url-rune',status=302),Response(b'secret',status=500),
             Response({},content_type='text/html'),Response(b'{"ok":1,"ok":2}'),Response(b'{"x":NaN}'),
             Response(b'x'*(https.LIMIT+1)),Response([]),Response({'error':'secret'}),OSError('private endpoint')]
        client=https.Client(nodes(self.remote)['outgoing'])
        for value in bad:
            self.responses=[Response(self.remote.obs['outgoing']['info']),value];before=len(self.calls)
            with self.assertRaisesRegex(https.HttpsError,'^coordinator_reply_refused_or_unavailable$'):
                client.call('send',{'entry_id':'11'*32})
            self.assertEqual(len(self.calls)-before,2)
        self.assertEqual(self.closed,len(self.calls))

    def test_credentials_and_request_size_are_checked_before_network(self):
        for change in (dict(rune='secret\r\nHeader:x'),dict(ca_pem='private key'),dict(url='https://incoming.invalid/')):
            with self.assertRaises(ValueError):https.Client(dict(nodes(self.remote)['incoming'],**change))
        with patch.object(https,'LIMIT',8),self.assertRaisesRegex(ValueError,'request_too_large'):
            https.Client(nodes(self.remote)['incoming']).call('info')
        self.assertEqual(self.calls,[])

    def test_pairing_commit_requires_both_readonly_identities_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'pair';pairing.initialize_fixture(directory);j=hg.Journal(directory)
            self.addCleanup(j.close);p=pairing.Pairing(j,clock=lambda:NOW)
            value=dict(direction='forward',nodes=nodes(self.remote));before=j.path.read_bytes()
            self.responses=[Response(self.remote.obs['incoming']['info']),OSError('failed second node')]
            with self.assertRaises(https.HttpsError):p.save(value,confirmed=True)
            self.assertEqual(j.path.read_bytes(),before)
            self.assertEqual([v['operation'] for _,v in self.calls],['info','info'])
            p.save(value,confirmed=True);saved=j.path.read_bytes();p.save(value,confirmed=True)
            self.assertEqual(j.path.read_bytes(),saved)
            changed=copy.deepcopy(value);changed['nodes']['incoming']['rune']='changed_'+'a'*32
            before=len(self.calls)
            with self.assertRaisesRegex(ValueError,'immutable'):p.save(changed,confirmed=True)
            self.assertEqual(len(self.calls),before)
            for secret in (value['nodes']['incoming']['url'],value['nodes']['incoming']['rune'],PEM):
                self.assertNotIn(secret,str(p.status()))

    def test_pairing_write_uncertainty_freezes_instance_and_reload_recovers_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'pair';pairing.initialize_fixture(directory);j=hg.Journal(directory)
            p=pairing.Pairing(j,clock=lambda:NOW);real=j.write
            def uncertain(value):real(value);raise OSError('after replace')
            with patch.object(j,'write',uncertain),self.assertRaises(hg.StorageError):
                p.save(dict(direction='forward',nodes=nodes(self.remote)),confirmed=True)
            self.assertTrue(p.status()['blocked']);j.close()
            fresh=hg.Journal(directory)
            try:self.assertTrue(pairing.Pairing(fresh).status()['paired'])
            finally:fresh.close()

    def test_pairing_expiry_is_checked_after_both_identity_replies(self):
        for expired_kind in ('incoming','outgoing'):
            with self.subTest(expired_kind=expired_kind),tempfile.TemporaryDirectory() as temp:
                directory=Path(temp)/'pair';pairing.initialize_fixture(directory);j=hg.Journal(directory)
                try:
                    saved=j.read(); now=[NOW]; seen=[]
                    value=dict(direction='forward',nodes=copy.deepcopy(nodes(self.remote)))
                    value['nodes'][expired_kind]['grant']['expires_at']=NOW+1
                    def factory(node):
                        class Client:
                            def call(client,op):
                                self.assertEqual(op,'info');kind=node['grant']['kind'];seen.append(kind)
                                if len(seen)==2: now[0]=NOW+1
                                return dict(self.remote.obs[kind]['info'],grant=node['grant'])
                        return Client()
                    p=pairing.Pairing(j,factory=factory,clock=lambda:now[0])
                    with self.assertRaisesRegex(ValueError,'https_pairing_grant_unavailable'):
                        p.save(value,confirmed=True)
                    self.assertEqual(seen,['incoming','outgoing']);self.assertEqual(j.read(),saved)
                    self.assertFalse(p.status()['paired'])
                finally:j.close()

    def test_worker_recovery_uses_saved_pairing_and_never_resends(self):
        for direction in ('forward','reverse'):
            with self.subTest(direction=direction),tempfile.TemporaryDirectory() as temp:
                self.remote=Remote(direction);root=Path(temp)
                pairing.initialize_fixture(root/'pair');cw.initialize_fixture(root/'worker')
                j=hg.Journal(root/'pair');w=hg.Journal(root/'worker')
                try:
                    p=pairing.Pairing(j,clock=lambda:NOW);p.save(dict(direction=direction,nodes=nodes(self.remote)),confirmed=True)
                    worker=p.worker(w);cid=worker.prepare(self.remote.c);worker.confirm(cid,cid)
                    for _ in range(3):worker.step(cid)
                    self.remote.hold();self.remote.lost=('outgoing','send')
                    with self.assertRaises(https.HttpsError):worker.step(cid)
                    j.close();w.close();j=hg.Journal(root/'pair');w=hg.Journal(root/'worker')
                    worker=pairing.Pairing(j,clock=lambda:NOW).worker(w);worker.step(cid)
                    self.remote.complete()
                    for kind in cw.KINDS:self.remote.obs[kind]['info']['paused']=True
                    for _ in range(3):worker.step(cid)
                    self.assertEqual(worker.status(cid)['state'],'settled')
                    self.assertEqual(sum(op=='send' for _,op,_ in self.remote.calls),1)
                    count=len(self.calls);worker.status(cid);self.assertEqual(len(self.calls),count)
                finally:w.close();j.close()

    def test_restore_and_changed_contract_block_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);pairing.initialize_fixture(root/'pair');cw.initialize_fixture(root/'worker')
            j=hg.Journal(root/'pair');w=hg.Journal(root/'worker');self.addCleanup(j.close);self.addCleanup(w.close)
            p=pairing.Pairing(j,clock=lambda:NOW);p.save(dict(direction='forward',nodes=nodes(self.remote)),confirmed=True)
            worker=p.worker(w);cid=worker.prepare(self.remote.c);before=len(self.calls)
            other=Remote('reverse').c
            with self.assertRaisesRegex(ValueError,'contract_changed'):worker.prepare(other)
            (root/'pair/restored.json').write_text('{}')
            self.assertTrue(worker.status(cid)['blocked'])
            with self.assertRaisesRegex(ValueError,'restored_pairing'):worker.confirm(cid,cid)
            self.assertEqual(len(self.calls),before)


if __name__=='__main__':unittest.main()
