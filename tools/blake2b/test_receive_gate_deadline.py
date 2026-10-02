"""Real standalone gate protocol; offline bounded close/recovery boundaries."""
import copy
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import quote_plugin
import receive_bounds as bounds
import receive_service
import swap_controller
from service_manager import private_load
import test_quote_replay as replay
import test_receive_bounds as fixtures


class GateTests(replay.ReplayTests):
    def setUp(self):
        super().setUp()
        self.terms.update(pilot=quote_plugin.ROUTED_PROFILE,btc_amount_msat=1500000,
                          xbt_amount_msat=325000000,xbt_invoice='lnxbt-fixture',
                          oracle_digest='33'*32,controller_id='44'*32,
                          btc_channel_policy='any-normal-v1',xbt_route_delay=40,
                          xbt_route_digest='55'*32,min_cltv_delta=190,max_cltv_delta=2016)
        self.htlc.update(amount_msat=1500000,cltv_expiry=350,cltv_expiry_relative=220)
        self.onion.update(forward_msat=1500000,total_msat=1500000,outgoing_cltv_value=350)
        self.profile=quote_plugin.ROUTED_PROFILE
        self.network='bitcoin'

    def run_plugin(self, now, requests):
        init=self.request(1,'init',dict(configuration={'network':self.network},
                                      options={'xbt-live-pilot':self.profile}))
        code=('import runpy,sys,time; time.time=lambda:float(sys.argv[2]); '
              'runpy.run_path(sys.argv[1],run_name="__main__")')
        result=subprocess.run([sys.executable,'-c',code,str(self.plugin),str(now)],
                              input='\n\n'.join(map(json.dumps,[init,*requests]))+'\n\n',
                              text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        return {r['id']:r for line in result.stdout.splitlines() if line.strip()
                for r in [json.loads(line)] if 'id' in r}

    def test_profile_optin_and_existing_live_profile_cannot_register_routed(self):
        self.profile='disabled'
        self.assertIn('disable',self.run_plugin(1000,[])[1]['result'])
        self.profile='live-market-v2'
        self.assertIn('error',self.run_plugin(1000,[self.request(2,'xbt-register',[self.terms])])[2])
        self.assertFalse(self.disk.exists())

    def test_amount_timing_and_digest_caps_refuse_before_journal(self):
        for field,bad in (('btc_amount_msat',10000001),('xbt_amount_msat',500001000),
                          ('xbt_route_delay',0),('xbt_route_delay',1843),
                          ('min_cltv_delta',189),('min_cltv_delta',190.0),
                          ('max_cltv_delta',2017),('xbt_route_digest','AA'*32),
                          ('btc_channel_policy','other'),('expires_at',1121)):
            terms=dict(self.terms,**{field:bad})
            self.assertIn('error',self.run_plugin(1000,[self.request(2,'xbt-register',[terms])])[2])
            self.assertFalse(self.disk.exists())

    def test_route_commitment_reported_and_cannot_be_replaced(self):
        self.accept()
        replies=self.run_plugin(1000,[self.hook(),self.request(4,'xbt-spend-info',[self.hash])])
        self.assertEqual(replies[4]['result']['xbt_route_digest'],self.terms['xbt_route_digest'])
        self.assertEqual(replies[4]['result']['xbt_route_delay'],40)
        before=self.disk.read_bytes()
        bad=dict(self.terms,xbt_route_digest='66'*32)
        self.assertIn('error',self.run_plugin(1000,[self.request(2,'xbt-register',[bad])])[2])
        self.assertEqual(self.disk.read_bytes(),before)

    def test_regtest_exercises_same_gate_fields_without_live_profile(self):
        self.network='regtest';self.profile='disabled'
        for k in ('pilot','oracle_digest','controller_id'):self.terms.pop(k)
        self.terms['xbt_invoice']='lnxbtrt-fixture'
        self.accept()


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.BoundTests();f.setUp();self.addCleanup(f.doCleanups);self.f=f
        f.quote();f.held()
        def start(path,**kwargs):
            state=private_load(path);state['phase']='outgoing_started';swap_controller.save(path,state)
            return {'outcome':'pending'}
        receive_service.process(f.directory,f.settings,rpc=f.rpc,controller=start,now=lambda:1000)
        self.path=f.directory/'state.json';self.state=private_load(self.path)
        self.height=247 # 320 - 247 == 73
        self.lost=False;self.missing=False;self.closes=0;self.sends=0

    def rpc(self,cli,method,*args):
        if method=='listsendpays':
            s=self.state
            return {'payments':[] if self.missing else [dict(payment_hash=s['payment_hash'],
                    amount_msat=s['xbt_amount_msat'],amount_sent_msat=s['route'][0]['amount_msat'],
                    destination=s['xbt_route_policy']['destination'],bolt11=s['xbt_invoice'],status='pending')]}
        if method=='close':
            self.closes+=1
            self.assertEqual(args,(self.f.incoming['channel_id'],1))
            intent=private_load(self.path)['btc_close_intent']
            self.assertEqual(intent['channel_id'],self.state['btc_incoming_pin']['channel_id'])
            self.f.incoming['state']='AWAITING_UNILATERAL'
            if self.lost:raise RuntimeError('lost close reply')
            return {'type':'unilateral','txids':['88'*32]}
        if method=='sendpay':self.sends+=1;raise AssertionError('unexpected resend')
        result=self.f.rpc(cli,method,*args)
        if method=='getinfo' and cli==['btc']:result['blockheight']=self.height
        return result

    def run_step(self):
        with patch.object(swap_controller.RPC,'call',side_effect=self.rpc):
            return swap_controller.run(self.path,recover_only=True)

    def test_73_open_72_close_and_repeated_recovery(self):
        before=self.path.read_bytes();self.run_step()
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.closes,0)
        self.height=248;self.run_step();after=self.path.read_bytes();self.run_step()
        self.assertEqual(self.closes,1);self.assertEqual(self.sends,0)
        self.assertEqual(self.path.read_bytes(),after)

    def test_lost_close_reply_reconciles_without_another_close(self):
        self.height=248;self.lost=True
        with self.assertRaises(RuntimeError):self.run_step()
        self.assertNotIn('btc_close_result',private_load(self.path))
        for state in ('AWAITING_UNILATERAL','FUNDING_SPEND_SEEN','ONCHAIN'):
            self.f.incoming['state']=state;self.run_step()
        self.assertEqual(self.closes,1);self.assertEqual(self.sends,0)

    def test_crash_before_close_retries_persisted_exact_target(self):
        self.height=248
        original=self.rpc
        def crash(cli,method,*args):
            if method=='close':raise RuntimeError('crash before close')
            return original(cli,method,*args)
        with patch.object(swap_controller.RPC,'call',side_effect=crash):
            with self.assertRaises(RuntimeError):swap_controller.run(self.path,recover_only=True)
        self.assertIn('btc_close_intent',private_load(self.path))
        self.assertEqual(self.f.incoming['state'],'CHANNELD_NORMAL')
        self.run_step();self.assertEqual(self.closes,1)

    def test_changed_gate_route_commitment_never_closes(self):
        self.height=248;self.f.terms['xbt_route_digest']='99'*32
        with self.assertRaises(ValueError):self.run_step()
        self.assertEqual(self.closes,0)
        self.assertNotIn('btc_close_intent',private_load(self.path))

    def test_missing_attempt_never_closes_or_resends(self):
        self.height=248;self.missing=True
        with self.assertRaises(RuntimeError):self.run_step()
        self.assertEqual(self.closes,0);self.assertEqual(self.sends,0)

    def test_guard_or_funding_changes_never_close(self):
        self.height=248
        for key,value in (('btc_deadline_guard',False),('btc_close_blocks',30)):
            changed=dict(self.state,**{key:value});swap_controller.save(self.path,changed)
            with self.assertRaises(ValueError):self.run_step()
        swap_controller.save(self.path,self.state)
        self.f.incoming['funding_txid']='99'*32
        with self.assertRaises(RuntimeError):self.run_step()
        self.assertEqual(self.closes,0)


if __name__=='__main__':unittest.main()
