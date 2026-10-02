"""Routed outgoing XBT bounds and durable single-submission recovery."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import outgoing_xbt as routed
from swap_controller import run, save


class RoutedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.json'
        self.proof='ab'*32;self.ph=hashlib.sha256(bytes.fromhex(self.proof)).hexdigest()
        self.policy=dict(source='a',destination='c',max_fee_msat=10000,max_delay=80,max_hops=4,final_cltv=40)
        self.route=[dict(id='b',channel='1x1x0',amount_msat=100005000,delay=46),
                    dict(id='c',channel='2x1x0',amount_msat=100000000,delay=40)]
        self.state=dict(phase='prepared',quote_gate=True,xbt_routing=routed.MODE,
            payment_hash=self.ph,payment_secret='22'*32,xbt_invoice='lnxbtrt-fixture',
            xbt_amount_msat=100000000,btc_binding=['3x1x0',0],btc_node_id='btc',
            btc_cli=['btc'],xbt_cli=['xbt'],route=self.route,xbt_route_policy=self.policy)
        self.decoded=dict(valid=True,type='bolt11 invoice',currency='xbtrt',amount_msat=100000000,
                          payee='c',payment_hash=self.ph,payment_secret='22'*32,created_at=900,expiry=1000,
                          min_final_cltv_expiry=18)
        self.channel=dict(short_channel_id='1x1x0',peer_id='b',channel_id='11'*32,
                          funding_txid='33'*32,funding_outnum=0,state='CHANNELD_NORMAL',
                          peer_connected=True,htlcs=[],spendable_msat=100005000)
        self.info=dict(payment_hash=self.ph,binding=self.state['btc_binding'],xbt_amount_msat=100000000,
                       cltv_expiry=220,min_cltv_delta=100,max_cltv_delta=2000,expires_at=2000,
                       xbt_invoice=self.state['xbt_invoice'])
        self.calls=[];self.rows=[];self.gate='held';self.lost=False;self.network='xbt-regtest'
        save(self.path,self.state)

    def rpc(self,cli,method,*args):
        self.calls.append((cli,method,args))
        if method=='getinfo':
            return dict(network='regtest' if cli==['btc'] else self.network,
                        id='btc' if cli==['btc'] else 'a',blockheight=100)
        if method=='xbt-spend-info':return self.info
        if method=='decode':return self.decoded
        if method=='listpeerchannels':return {'channels':[self.channel]}
        if method=='sendpay':
            persisted=json.loads(self.path.read_text())
            self.assertEqual(persisted['phase'],'outgoing_started')
            self.assertEqual(persisted['xbt_first_hop']['funding_txid'],self.channel['funding_txid'])
            self.assertIn('bolt11=lnxbtrt-fixture',args)
            self.rows=[dict(payment_hash=self.ph,amount_msat=100000000,amount_sent_msat=100005000,
                            destination='c',bolt11='lnxbtrt-fixture',status='pending')]
            if self.lost:raise subprocess.TimeoutExpired('private',1)
            return {}
        if method=='listsendpays':return {'payments':self.rows}
        if method=='xbt-quote-status':return dict(payment_hash=self.ph,binding=self.state['btc_binding'],phase=self.gate)
        if method=='xbt-release':self.gate='resolved';return {'released':1}
        if method=='xbt-fail':
            self.assertEqual(json.loads(args[1]),self.state['btc_binding'])
            self.gate='failed';return {'failed':1}
        raise AssertionError(method)

    def invoke(self,**kw):
        with patch('swap_controller.RPC.call',side_effect=self.rpc),patch('swap_controller.time.time',return_value=1000):
            return run(self.path,**kw)

    def submit(self):
        with patch('swap_controller.os._exit',side_effect=SystemExit) as crash,self.assertRaises(SystemExit):
            self.invoke(crash_after_sendpay=True)
        crash.assert_called_once_with(88)

    def test_exact_fee_liquidity_and_margin_boundaries(self):
        routed.preflight(self.state,self.decoded,106,self.rpc)
        for remaining in (105,):
            with self.assertRaises(ValueError):routed.preflight(self.state,self.decoded,remaining,self.rpc)
        self.channel['spendable_msat']-=1
        with self.assertRaises(ValueError):routed.preflight(self.state,self.decoded,106,self.rpc)
        self.channel['spendable_msat']+=1
        self.policy['max_fee_msat']=5000
        routed.preflight(self.state,self.decoded,106,self.rpc)
        self.policy['max_fee_msat']=4999
        with self.assertRaises(ValueError):routed.preflight(self.state,self.decoded,106,self.rpc)

    def test_first_hop_readiness_and_funding_pin(self):
        routed.preflight(self.state,self.decoded,120,self.rpc)
        original=copy.deepcopy(self.channel)
        for changes in ({'peer_connected':False},{'state':'ONCHAIN'},{'htlcs':[{}]},
                        {'peer_id':'other'},{'funding_txid':'44'*32}):
            self.channel=dict(original,**changes)
            with self.assertRaises(ValueError):routed.preflight(self.state,self.decoded,120,self.rpc)

    def test_route_substitution_and_loops_refused(self):
        for idx,key,value in ((0,'id','a'),(1,'id','other'),(1,'amount_msat',1),
                               (0,'delay',81),(1,'delay',39),(1,'channel','1x1x0')):
            state=copy.deepcopy(self.state);state['route'][idx][key]=value
            with self.assertRaises(ValueError):routed.preflight(state,self.decoded,120,self.rpc)
        with self.assertRaises(ValueError):routed.preflight(self.state,dict(self.decoded,payee='other'),120,self.rpc)

    def test_wrong_network_and_live_optin_never_spend(self):
        for network in ('xbt','bitcoin'):
            self.network=network
            with self.assertRaises(ValueError):self.invoke()
        for profile in ('live-market-v1','unknown'):
            save(self.path,dict(self.state,profile=profile))
            with self.assertRaises(ValueError):self.invoke()
        self.assertNotIn('sendpay',[m for c,m,a in self.calls])

    def test_fee_refusal_preserves_file(self):
        self.state['xbt_route_policy']['max_fee_msat']=4999;save(self.path,self.state)
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.calls,[])

    def test_pending_recovery_does_not_replan_or_send(self):
        self.submit();before=self.path.read_bytes();self.calls=[]
        for _ in range(2):self.assertEqual(self.invoke()['outcome'],'pending')
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual({m for c,m,a in self.calls},{'getinfo','listsendpays'})

    def test_success_accounts_for_fee_and_releases_once(self):
        self.submit();self.rows[0].update(status='complete',payment_preimage=self.proof)
        for _ in range(2):self.assertEqual(self.invoke()['phase'],'btc_released')
        self.assertEqual(sum(m=='sendpay' for c,m,a in self.calls),1)
        self.assertEqual(sum(m=='xbt-release' for c,m,a in self.calls),1)

    def test_failure_only_fails_original_binding(self):
        self.submit();self.rows[0]['status']='failed'
        for _ in range(2):self.assertEqual(self.invoke()['phase'],'btc_failed')
        self.assertEqual(sum(m=='xbt-fail' for c,m,a in self.calls),1)
        self.assertNotIn('xbt-release',[m for c,m,a in self.calls])

    def test_lost_submission_reply_is_read_only_on_recovery(self):
        self.lost=True
        with self.assertRaises(subprocess.TimeoutExpired):self.invoke()
        self.assertEqual(self.invoke()['outcome'],'pending')
        self.rows=[]
        with self.assertRaises(RuntimeError):self.invoke()
        self.assertEqual(sum(m=='sendpay' for c,m,a in self.calls),1)

    def test_changed_attempt_never_releases_or_refunds(self):
        self.submit();original=copy.deepcopy(self.rows[0]);before=self.path.read_bytes()
        for key,value in (('amount_sent_msat',100006000),('destination','other'),('bolt11','other')):
            self.rows=[dict(original,**{key:value},status='failed')]
            with self.assertRaises(RuntimeError):self.invoke()
            self.assertEqual(self.path.read_bytes(),before)
        self.assertNotIn('xbt-fail',[m for c,m,a in self.calls])

    def test_invoice_network_metadata_and_features_rejected(self):
        for changes in ({'currency':'bcrt'},{'payment_metadata':''},{'features':'1'},
                        {'amount_msat':200000000},{'min_final_cltv_expiry':41}):
            with self.assertRaises(ValueError):routed.invoice(dict(self.decoded,**changes))

    def test_planner_uses_bounded_read_only_query(self):
        result={'routes':[dict(amount_msat=100000000,final_cltv=40,path=[
            dict(short_channel_id_dir='1x1x0/0',node_id_in='a',node_id_out='b',
                 amount_in_msat=100005000,amount_out_msat=100005000,cltv_in=46,cltv_out=46),
            dict(short_channel_id_dir='2x1x0/0',node_id_in='b',node_id_out='c',
                 amount_in_msat=100005000,amount_out_msat=100000000,cltv_in=46,cltv_out=40)])]}
        rpc=Mock(return_value=result)
        route,policy=routed.plan(['xbt'],self.decoded,'a',rpc)
        self.assertEqual(route,self.route);self.assertEqual(policy,self.policy)
        self.assertEqual(rpc.call_count,1)
        self.assertEqual(rpc.call_args.args[1],'getroutes')
        for arg in ('maxfee_msat=10000','maxdelay=80','maxparts=1'):self.assertIn(arg,rpc.call_args.args)



class RestartReadyTests(unittest.TestCase):
    def test_waits_for_height_and_warning_clearance(self):
        from routed_receive_regtest import wait_for_ready
        ready=dict(id='original',network='xbt-regtest',blockheight=150)
        responses=[dict(ready,warning_lightningd_sync='private'),
                   dict(ready,blockheight=149),
                   dict(ready,warning_bitcoind_sync='private'), ready]
        lab=Mock()
        lab.rpc.side_effect=[150,*responses]
        node=dict(cli=['xbt'],proc=None)
        def poll(fn,proc,timeout):
            self.assertEqual(timeout,90)
            self.assertEqual([fn() for _ in responses],[False,False,False,True])
        with patch('routed_receive_regtest.wait_until',side_effect=poll):
            wait_for_ready(lab,node,dict(cli=['backend']),'xbt-regtest','original')
        self.assertEqual({c.args[1] for c in lab.rpc.call_args_list},{'getinfo','getblockcount'})

    def test_wrong_identity_or_network_is_not_waited_away(self):
        from routed_receive_regtest import wait_for_ready
        for changes in ({'id':'other'},{'network':'xbt'}):
            lab=Mock()
            lab.rpc.side_effect=[150,dict(id='original',network='xbt-regtest',blockheight=150) | changes]
            with patch('routed_receive_regtest.wait_until',side_effect=lambda fn,*a,**kw:fn()), self.assertRaises(AssertionError):
                wait_for_ready(lab,dict(cli=['xbt'],proc=None),dict(cli=['backend']),'xbt-regtest','original')

    def test_warning_still_blocks_controller_before_spend(self):
        f=RoutedTests();f.setUp();self.addCleanup(f.doCleanups)
        def rpc(cli,method,*args):
            value=f.rpc(cli,method,*args)
            if method=='getinfo': value['warning_lightningd_sync']='private'
            return value
        with patch('swap_controller.RPC.call',side_effect=rpc), self.assertRaisesRegex(ValueError,'readiness warning'):
            run(f.path)
        self.assertEqual(json.loads(f.path.read_text())['phase'],'prepared')
        self.assertNotIn('sendpay',[m for c,m,a in f.calls])

if __name__=='__main__':unittest.main()
