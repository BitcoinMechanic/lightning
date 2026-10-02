"""Candidate routed guard: quote binding, current policy and submission refusal."""
import copy
import unittest
from unittest.mock import Mock, patch

import receive_bounds as bounds
import receive_service as receive
import routed_receive_service as service
import swap_controller as controller
from service_manager import private_load
from swap_controller import save
import test_routed_receive_service as fixtures


class BoundTests(fixtures.ServiceTests):
    def setUp(self):
        super().setUp()
        self.config['profile'] = bounds.PROFILE
        self.outgoing.update(feerate={'perkw':1250}, dust_limit_msat=546000)
        self.funds = 50000000
        self.policy = dict(short_channel_id='2x1x0',source='b',destination='c',
                           direction=0,active=True,htlc_minimum_msat=1000,
                           htlc_maximum_msat=1000000000,base_fee_millisatoshi=5000,
                           fee_per_millionth=0,delay=6)
        self.unknown = False

    def rpc(self, cli, method, *args):
        if method == 'listfunds':
            self.calls.append((cli,method,args))
            return {'outputs':[dict(status='confirmed',reserved=False,amount_msat=self.funds)]}
        if method == 'listchannels':
            self.calls.append((cli,method,args))
            return {'channels':[] if self.unknown else [self.policy]}
        result = super().rpc(cli, method, *args)
        if method == 'decode' and cli == ['btc']:
            result['min_final_cltv_expiry'] = 220
        if method == 'xbt-spend-info':
            result['cltv_expiry'] = 320
        return result

    def held(self):
        super().held()
        self.incoming['htlcs'][0]['expiry'] = 320

    def test_route_timing_bound_to_quote_and_gate(self):
        self.quote()
        q=private_load(self.directory/'quote.json')
        self.assertEqual(q['terms']['min_cltv_delta'],196)
        self.assertEqual(q['terms']['max_cltv_delta'],2016)
        self.assertEqual(q['controller']['xbt_timing'],bounds.timing(46))
        for field in ('minimum_btc_remaining_blocks','proposed_btc_invoice_cltv','expected_btc_blocks_per_xbt_block'):
            changed=copy.deepcopy(q);changed['controller']['xbt_timing'][field]+=1
            with self.assertRaises(ValueError):service.validate_quote(changed)
        changed=copy.deepcopy(q);changed['terms']['min_cltv_delta']-=1
        with self.assertRaises(ValueError):service.validate_quote(changed)
        with self.assertRaises(ValueError):bounds.gate(q['controller'],dict(q['terms'],min_cltv_delta=195))

    def test_current_remote_policy_blocks_worker_before_state(self):
        self.quote();self.held()
        original=copy.deepcopy(self.policy)
        for change in ({'active':False},{'htlc_minimum_msat':100000001},
                       {'htlc_maximum_msat':99999999},{'base_fee_millisatoshi':5001},{'delay':7}):
            self.policy=dict(original,**change)
            send=Mock()
            with self.assertRaises(ValueError):receive.process(self.directory,self.settings,rpc=self.rpc,controller=send,now=lambda:1000)
            send.assert_not_called();self.assertFalse((self.directory/'state.json').exists())
        self.policy=original;self.unknown=True
        with self.assertRaises(ValueError):service.preflight(private_load(self.directory/'quote.json'),self.rpc)

    def test_reserve_and_trimmed_first_hop_block_before_publish(self):
        self.funds=49999999
        with self.assertRaises(RuntimeError):self.quote()
        self.assertFalse((self.directory/'quote.json').exists())
        self.assertNotIn('xbt-register',[m for _,m,_ in self.calls])
        self.funds=50000000;self.outgoing['dust_limit_msat']=100005000
        with self.assertRaises(RuntimeError):service.create(self.config,self.body['xbt_invoice'],None,self.directory)
        self.assertFalse((self.directory/'quote.json').exists())

    def test_fresh_actual_height_boundary_preserves_prepared_checkpoint(self):
        self.quote();self.held()
        def inspect(path, **kw):
            return {'phase':'prepared'}
        receive.process(self.directory,self.settings,rpc=self.rpc,controller=inspect,now=lambda:1000)
        path=self.directory/'state.json';before=path.read_bytes()
        state=private_load(path)
        original=self.rpc
        def rpc(cli,method,*args):
            value=original(cli,method,*args)
            if method=='getinfo' and cli==['btc']:value['blockheight']=self.height
            return value
        self.height=124 # 320 - 124 == minimum 196
        with patch.object(controller.RPC,'call',side_effect=rpc):
            self.assertIsNone(controller.check_spend(copy.deepcopy(state)))
            self.height=125
            self.assertEqual(controller.run(path)['reason'],'insufficient_btc_cltv')
            self.assertEqual(controller.run(path)['reason'],'insufficient_btc_cltv')
        self.assertEqual(path.read_bytes(),before)
        self.assertNotIn('sendpay',[m for _,m,_ in self.calls])

    def test_recovery_does_not_recheck_reserves_or_remote_policy(self):
        self.quote();self.held()
        def start(path,**kw):
            state=private_load(path);state['phase']='outgoing_started';save(path,state)
            return {'outcome':'pending'}
        receive.process(self.directory,self.settings,rpc=self.rpc,controller=start,now=lambda:1000)
        self.funds=0;self.unknown=True
        with patch.object(service,'preflight',side_effect=AssertionError('recovery checked admission')):
            recover=Mock(return_value={'outcome':'pending'})
            receive.process(self.directory,self.settings,rpc=self.rpc,controller=recover,now=lambda:9999)
        recover.assert_called_once_with(self.directory/'state.json',recover_only=True)

    def test_no_live_profile_or_network_accepted(self):
        with self.assertRaises(ValueError):service.validate_config(dict(self.config,profile='bounded-receive-live-v1'))
        self.network='xbt'
        with self.assertRaises(ValueError):self.quote()
        self.assertNotIn('xbt-register',[m for _,m,_ in self.calls])


if __name__=='__main__':unittest.main()
