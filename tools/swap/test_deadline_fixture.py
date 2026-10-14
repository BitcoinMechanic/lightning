#!/usr/bin/env python3
"""Fixture preflight checks: ledger attribution, versions, scoped requests."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import chain_identity as ci
import customer_remote as remote
import test_deadline_engine as fixture
import bounded_authority as ba
import fixture_https_worker
import held_gate as hg
from test_cross_worker import Remote
from test_customer_https import nodes


# Only the coordinator response is synthetic. The real child entry point,
# private-stdin requests, path checks, pairing, journals and worker are used.
CHILD = '''
import io,json,sys,time
from pathlib import Path
from test_route_plan import NOW
time.time=lambda:NOW
import customer_https as https
import fixture_https_worker as entry
from test_cross_worker import Remote
request=json.loads(sys.stdin.read())
directory=Path(request['directory'])
saved=directory/'unit-remote.json'
state=json.loads(saved.read_text())
remote=Remote(state['contract']['direction'])
remote.c=state['contract'];remote.obs=state['observations'];remote.entries=state['entries']
https.context=lambda pem:object()
def call(client,operation,body):
    kind=client.grant['kind']
    value=remote.obs[kind]['info'] if operation=='info' else remote(kind,client.gid,operation,body)
    saved.write_text(json.dumps(dict(state,entries=remote.entries)))
    return value
https.Client._request=call
sys.stdin=io.StringIO(json.dumps(request))
raise SystemExit(entry.main())
'''


class WorkerSetupTests(unittest.TestCase):
    def test_six_scenarios_pair_prepare_confirm_invoice_and_reopen_real_entrypoint(self):
        original=fixture.cross.subprocess.Popen
        def child(command,**kwargs):
            self.assertEqual(Path(command[1]).name,'fixture_https_worker.py')
            return original([command[0],'-c',CHILD,*command[2:]],**kwargs)
        with tempfile.TemporaryDirectory() as temp,patch.object(fixture.cross.subprocess,'Popen',side_effect=child):
            lab=SimpleNamespace(root=Path(temp),current_stage='unit-deadline-setup')
            for direction in ('forward','reverse'):
                for name in ('release','timeout','restart-held'):
                    with self.subTest(direction=direction,scenario=name):
                        r=Remote(direction);g=r.c['grants']['incoming']
                        g['limits'].update(close_before_blocks=72,min_cltv_delta=350)
                        g['format']=ba.PROTECTED_FORMAT
                        r.obs['incoming']['info']['grant']=copy.deepcopy(g)
                        configs=nodes(r)
                        clients={k:SimpleNamespace(node=k,credential={'rune':v['rune']},
                            call=lambda op,k=k:copy.deepcopy(r.obs[k]['info'])) for k,v in configs.items()}
                        lab.config=lambda node,rune,grant:copy.deepcopy(configs[node])
                        initialize=fixture.cw.initialize_fixture
                        def initialize_remote(directory):
                            initialize(directory)
                            (directory/'unit-remote.json').write_text(json.dumps(dict(
                                contract=r.c,observations=r.obs,entries={})))
                        with patch.object(fixture.cw,'initialize_fixture',side_effect=initialize_remote):
                            driver=fixture.start_worker(lab,direction,name,clients,r.c)
                        for _ in range(3):driver.run('step')
                        invoice=driver.run('invoice');saved=driver.record()
                        self.assertEqual(driver.run('status')['state'],'waiting_for_payment')
                        self.assertNotIn('send',saved)
                        fresh=fixture.transport.Driver(lab,driver.directory,clients);fresh.cid=driver.cid
                        self.assertEqual(fresh.run('invoice'),invoice)
                        self.assertEqual(fresh.record(),saved)
                        self.assertEqual(fresh.saved_pairing,driver.saved_pairing)

    def test_wrong_prefix_and_relative_paths_still_refuse_before_journal_access(self):
        for path in ('/tmp/worker-forward-release','cross-worker-relative'):
            with patch.object(hg,'Journal') as journal,self.assertRaisesRegex(ValueError,'fixture_worker_path_refused'):
                fixture_https_worker.execute(dict(directory=path))
            journal.assert_not_called()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.pin=dict(funding_txid='a'*64,funding_outnum=0)
        self.nodes=['hint','coordinator']
        def o(txid,amount):return dict(txid=txid,output=0,amount_msat=amount*1000,status='confirmed')
        self.before=[{('b'*64,0):o('b'*64,100000)},{('c'*64,0):o('c'*64,100000)}]
        self.returned=[dict(self.before[0],**{}),dict(self.before[1],**{})]
        self.returned[0][('d'*64,0)]=o('d'*64,290000)
        self.returned[1][('d'*64,1)]=dict(o('d'*64,8670),output=1)
        tx=dict(txid='d'*64,vin=[dict(txid='a'*64,vout=0)],vout=[
            dict(n=i,value=amount/100000000,scriptPubKey=dict(hex='00')) for i,amount in enumerate((290000,8670,330))])
        self.block=dict(tx=[tx]);self.proof=dict(commitment='d'*64,timeout='d'*64)
    def rpc(self,cli,method,*args):
        if method=='getblockcount':return 102
        if method=='getblockhash':return 'block102'
        if method=='getblock':return self.block
        if method=='listfunds':return dict(outputs=list(self.returned[self.nodes.index(cli)].values()))
        raise AssertionError(method)
    def run_ledger(self):
        lab=SimpleNamespace(rpc=self.rpc)
        with patch.object(fixture,'wallet',side_effect=lambda lab,n:self.returned[self.nodes.index(n)]):
            return fixture.reconcile_chain(lab,dict(cli='backend'),self.nodes,self.pin,self.before,101,self.proof)
    def test_exact_wallet_returns_anchor_and_fee(self):
        r=self.run_ledger();self.assertEqual(r['fee_sats'],1000)
        self.assertEqual(r['wallet_delta_sats'],[290000,8670]);self.assertEqual(r['unspent_anchor_sats'],330)
    def test_unknown_input_missing_wallet_and_unknown_residue_refuse(self):
        original=copy.deepcopy(self.block)
        self.block['tx'][0]['vin'].append(dict(txid='e'*64,vout=1))
        with self.assertRaisesRegex(RuntimeError,'unknown_external'):self.run_ledger()
        self.block=original;self.returned[1][('d'*64,1)]['amount_msat']+=1000
        with self.assertRaisesRegex(RuntimeError,'wallet_recovery_ledger_mismatch'):self.run_ledger()
        self.returned[1].pop(('d'*64,1))
        with self.assertRaisesRegex(RuntimeError,'unrecovered_nonanchor'):self.run_ledger()
    def test_distinct_versions_and_old_grant_operations(self):
        for p in (ci.BTC,ci.PRIVKEYIO_XBT):self.assertNotEqual(fixture.VERSIONS[p],fixture.routes.VERSIONS[p])
        self.assertNotIn('arm',remote.OPERATIONS['incoming'])

if __name__=='__main__':unittest.main()
