"""Unbound reverse admission and exact-channel recovery; no live activation."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import incoming_xbt as incoming
import reverse_controller as controller
import reverse_service as service
import reverse_live as live
from reverse_gate import Gate
from service_manager import private_load
from swap_controller import save
import test_reverse_live as fixtures
import test_reverse_check as check_fixture


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LiveTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.path.unlink()
        self.f.terms.update(profile=live.SERVICE_REGTEST,btc_invoice='lnbcrt-private',incoming_policy=incoming.POLICY)
        self.f.terms.pop('payer_id');self.f.terms.pop('xbt_channel')
        self.f.decoded['currency']='bcrt'
        self.f.settings.pop('receiver_id');self.f.settings.pop('receiver_cli')
        self.f.settings.update(reverse_profile=live.SERVICE_REGTEST,reverse_incoming_policy=incoming.POLICY,deployment='operator-pair-v1')
        self.f.config=service.binding(self.f.settings)
        self.f.quote.update(config=self.f.config,terms=self.f.terms)
        save(self.f.root/'reverse-quote.json',self.f.quote)
        self.scid='9x1x0';self.peer='03'+'44'*32
        self.funding='bb'*32;self.cid='aa'*32
        self.closes=[]

    def rpc(self,cli,method,*args):
        self.assertNotEqual(cli[0],'/payer')
        if method in ('reverse-release','reverse-fail'):
            self.f.calls.append((method,args))
            self.assertEqual(args[:2],(self.f.hash,json.dumps([self.scid,8])))
            self.f.gate_phase='resolved' if method=='reverse-release' else 'failed'
            return {'released' if method=='reverse-release' else 'failed':1}
        if method=='close':
            self.closes.append(args[0]);self.f.channel_state='AWAITING_UNILATERAL'
            return dict(type='unilateral')
        result=self.f.rpc(cli,method,*args)
        if method=='getinfo':result['network']='xbt-regtest' if cli[0]=='/xbt' else 'regtest'
        if method=='listpeerchannels' and cli[0]=='/xbt':
            c=result['channels'][0]
            c.update(short_channel_id=self.scid,peer_id=self.peer,channel_id=self.cid,funding_txid=self.funding)
            result['channels'].append(dict(c,short_channel_id='8x1x0',channel_id='cc'*32,funding_txid='dd'*32,htlcs=[]))
        if method=='reverse-status':result['binding']=[self.scid,8]
        if method=='xbt-held':
            for h in result['held']:h['short_channel_id']=self.scid
        if method=='sendpay':
            self.f.payments[0]['bolt11']='lnbcrt-private'
            self.assertEqual(private_load(self.f.path)['incoming_channel']['funding_txid'],self.funding)
        return result

    def step(self,**kwargs):
        with patch.object(controller.RPC,'call',side_effect=self.rpc):
            return service.step(self.f.root,rpc=self.rpc,**kwargs)

    def test_unknown_payer_channel_pinned_before_send_and_complete_recovery(self):
        self.assertEqual(self.step()['outcome'],'pending')
        before=private_load(self.f.path)['incoming_channel']
        self.assertEqual(before['peer_id'],self.peer)
        self.assertNotIn('payer_id',self.f.quote['config'])
        self.assertEqual(self.step(recover_only=True)['outcome'],'pending')
        self.f.payments[0].update(status='complete',payment_preimage=self.f.preimage)
        self.assertEqual(self.step(recover_only=True)['phase'],'xbt_released')
        self.assertEqual(self.step(recover_only=True)['phase'],'xbt_released')
        self.assertEqual(private_load(self.f.path)['incoming_channel'],before)
        self.assertEqual(sum(m=='sendpay' for m,a in self.f.calls),1)

    def test_first_channel_also_accepted_and_definite_failure_resolves_original(self):
        self.scid='1x1x0';self.step()
        self.f.payments[0]['status']='failed'
        self.assertEqual(self.step(recover_only=True)['phase'],'xbt_failed')
        self.assertEqual(self.step(recover_only=True)['phase'],'xbt_failed')
        self.assertEqual(sum(m=='sendpay' for m,a in self.f.calls),1)
        self.assertEqual(sum(m=='reverse-fail' for m,a in self.f.calls),1)

    def test_recovery_only_does_not_create_state(self):
        self.assertEqual(self.step(recover_only=True),{'outcome':'needs_manual_start'})
        self.assertFalse(self.f.path.exists())

    def test_prepared_funding_change_refused_without_send(self):
        self.step(controller=lambda path,**kw:dict(phase='prepared'))
        before=self.f.path.read_bytes();self.funding='ee'*32
        with self.assertRaises(RuntimeError):self.step()
        self.assertEqual(self.f.path.read_bytes(),before)
        self.assertFalse(any(m=='sendpay' for m,a in self.f.calls))

    def test_pending_changed_funding_prevents_deadline_close(self):
        self.step();self.f.xbt_height=560;self.funding='ee'*32
        with self.assertRaises(RuntimeError):self.step(recover_only=True)
        self.assertEqual(self.closes,[])
        self.assertEqual(sum(m=='sendpay' for m,a in self.f.calls),1)

    def test_deadline_closes_only_actual_channel_once(self):
        self.step();self.f.xbt_height=560
        self.step(recover_only=True);self.step(recover_only=True)
        self.assertEqual(self.closes,[self.cid])

    def test_missing_pin_never_sends(self):
        self.step(controller=lambda path,**kw:dict(phase='prepared'))
        state=private_load(self.f.path);state.pop('incoming_channel');save(self.f.path,state)
        with self.assertRaises(RuntimeError):self.step()
        self.assertFalse(any(m=='sendpay' for m,a in self.f.calls))

    def test_changed_binding_or_expiry_refused(self):
        self.step(controller=lambda path,**kw:dict(phase='prepared'))
        state=private_load(self.f.path)
        for key,value in (('xbt_binding',['8x1x0',8]),('xbt_expiry',701)):
            save(self.f.path,dict(state,**{key:value}))
            with self.assertRaises(RuntimeError):self.step()
        self.assertFalse(any(m=='sendpay' for m,a in self.f.calls))

    def test_onchain_completion_still_uses_original_pin(self):
        self.step()
        self.f.channel_state='ONCHAIN'
        self.f.payments[0].update(status='complete',payment_preimage=self.f.preimage)
        self.assertEqual(self.step(recover_only=True)['phase'],'xbt_released')
        self.assertEqual(private_load(self.f.path)['incoming_channel']['funding_txid'],self.funding)

    def test_quote_creation_has_no_payer_or_channel_binding(self):
        self.f.gate_phase='quoted';directory=self.f.root/'new';registered={}
        def rpc(cli,method,*args):
            if method=='getroutes':
                return dict(routes=[dict(amount_msat=1500000,final_cltv=40,path=[dict(
                    short_channel_id_dir='2x1x0/0',node_id_in=fixtures.B,node_id_out=fixtures.D,
                    amount_in_msat=1500000,amount_out_msat=1500000,cltv_in=40,cltv_out=40)])])
            if method=='reverse-register':
                self.assertTrue((directory/'reverse-quote.json').exists())
                registered.update(json.loads(args[0]));live.validate_terms(registered)
                return dict(registered=True)
            if method=='signinvoice':return dict(bolt11='lnxbtrt-signed')
            if method=='decode' and cli[0]=='/xbt':
                return dict(valid=True,currency='xbtrt',payee=fixtures.A,
                    payment_hash=registered['payment_hash'],payment_secret=registered['payment_secret'],
                    amount_msat=registered['xbt_amount_msat'],
                    min_final_cltv_expiry=registered['timing']['proposed_xbt_invoice_cltv'])
            return self.rpc(cli,method,*args)
        def inspector(invoice,clis,**kwargs):
            self.assertNotIn('payer',clis);self.assertNotIn('payer_id',kwargs)
            self.assertEqual(kwargs['incoming_policy'],incoming.POLICY)
            return dict(route_found=True,btc_sats=1500,reasons=[],btc_outgoing_cltv=40,
                routing_fee_msat=0,ticker_computed_at_ms=int(time.time()*1000),estimated_xbt_sats=350000)
        quote=service.create(self.f.settings,'lnbcrt-private',directory,rpc=rpc,inspector=inspector)
        self.assertNotIn('payer_id',quote['terms']);self.assertNotIn('xbt_channel',quote['terms'])
        self.assertFalse((directory/'reverse-state.json').exists());self.assertFalse(self.f.payments)

    def test_unbound_mode_cannot_enable_live_or_fixed_payer_terms(self):
        with self.assertRaises(ValueError):live.validate_terms(dict(self.f.terms,profile=live.PROFILE))
        with self.assertRaises(ValueError):live.validate_terms(dict(self.f.terms,payer_id=self.peer))
        with self.assertRaises(ValueError):service.binding(dict(self.f.settings,reverse_profile=live.PROFILE))
        with self.assertRaises(ValueError):service.binding(dict(self.f.settings,reverse_incoming_policy='anything'))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.flow=FlowTests();self.flow.setUp();self.addCleanup(self.flow.doCleanups)
        self.terms=self.flow.f.terms
        self.path=self.flow.f.root/'unbound-gate.json'
        self.gate=self.open()
        self.hook=dict(htlc=dict(short_channel_id='9x1x0',id=8,payment_hash=self.terms['payment_hash'],
            amount_msat=self.terms['xbt_amount_msat'],cltv_expiry=700,cltv_expiry_relative=600),
            onion=dict(payment_secret=self.terms['payment_secret'],forward_msat=self.terms['xbt_amount_msat'],
                total_msat=self.terms['xbt_amount_msat'],type='tlv',outgoing_cltv_value=700))

    def open(self):
        gate=Gate(self.path,service_regtest=True)
        gate.handle(dict(id=1,method='init',params=dict(configuration=dict(network='xbt-regtest'))))
        return gate

    def call(self,method,params):return self.gate.handle(dict(id=2,method=method,params=copy.deepcopy(params)))

    def held(self):
        self.call('reverse-register',[self.terms])
        self.assertEqual(self.call('htlc_accepted',self.hook),[])

    def test_other_channel_cannot_replace_original_after_restart(self):
        self.held();before=self.path.read_bytes();self.gate=self.open()
        other=copy.deepcopy(self.hook);other['htlc']['short_channel_id']='8x1x0'
        self.assertEqual(self.call('htlc_accepted',other)[0]['result']['result'],'fail')
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.call('htlc_accepted',self.hook),[])
        status=self.call('reverse-status',[self.terms['payment_hash']])[0]['result']
        self.assertTrue(status['hook_ready']);self.assertEqual(status['binding'],['9x1x0',8])

    def test_expired_exact_replay_restores_original_hook(self):
        self.held();before=self.path.read_bytes();self.gate=self.open()
        with patch('reverse_gate.time.time',return_value=self.terms['expires_at']+10):
            self.assertEqual(self.call('htlc_accepted',self.hook),[])
        self.assertEqual(self.path.read_bytes(),before)

    def test_wrong_secret_amount_and_short_cltv_fail_before_binding(self):
        self.call('reverse-register',[self.terms]);before=self.path.read_bytes()
        for section,key,value in (('onion','payment_secret','ff'*32),('htlc','amount_msat',1),('onion','outgoing_cltv_value',101)):
            bad=copy.deepcopy(self.hook);bad[section][key]=value
            self.assertEqual(self.call('htlc_accepted',bad)[0]['result']['result'],'fail')
            self.assertEqual(self.path.read_bytes(),before)


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.f=check_fixture.CheckTests();self.f.setUp()

    def check(self):
        from reverse_check import check
        f=self.f
        def rpc(cli,method,*args):
            self.assertNotEqual(cli[0],'/payer')
            value=copy.deepcopy(f.rpc(cli,method,*args))
            if method=='getinfo':value['network']='regtest' if cli[0]=='/btc' else 'xbt-regtest'
            if method=='decode':value['currency']='bcrt'
            return value
        return check('lnbc-private-invoice',{k:v for k,v in f.clis.items() if k!='payer'},rpc=rpc,
            incoming_policy=incoming.POLICY,_service_regtest=True,now=lambda:1000.001,
            market_fetch=lambda kind:f.ticker if kind=='ticker' else f.book)

    def test_two_eligible_channels_without_customer_rpc(self):
        c=self.f.channels['operator'][0]
        self.f.channels['operator'].append(dict(c,peer_id=fixtures.D,short_channel_id='9x1x0'))
        result=self.check()
        self.assertTrue(result['feasible']);self.assertFalse(result['payer_rpc_checked'])
        self.assertIsNone(result['xbt_payer_spendable_sats'])

    def test_disconnected_candidate_does_not_hide_ready_channel(self):
        c=self.f.channels['operator'][0]
        self.f.channels['operator'].append(dict(c,short_channel_id='9x1x0'))
        c['peer_connected']=False
        self.assertTrue(self.check()['feasible'])

    def test_no_eligible_channel_refused(self):
        self.f.channels['operator'][0]['receivable_msat']=0
        from reverse_check import CheckError
        with self.assertRaises(CheckError) as error:self.check()
        self.assertEqual(error.exception.public_reason,'insufficient_xbt_liquidity')


class CredentialTests(unittest.TestCase):
    def test_reverse_scope_has_no_customer_id_and_refuses_receive(self):
        from reverse_quote_api import Quotes, Server, Handler
        from unittest.mock import MagicMock
        from email.message import Message
        f=FlowTests();f.setUp();self.addCleanup(f.doCleanups)
        with patch('reverse_quote_api.HTTPServer.__init__'):
            server=Server(19840,Quotes(f.f.settings),dict(scope='reverse',token='ab'*32))
        self.assertTrue(server.reverse_only);self.assertFalse(server.receive_only)
        handler=MagicMock();handler.server=server;handler.path='/v1/receive'
        handler.headers=Message();handler.headers['Authorization']='Bearer '+'ab'*32
        Handler.do_POST(handler)
        handler.reply.assert_called_once_with(403,{'error':'request_rejected'})
        handler.rfile.read.assert_not_called()


if __name__=='__main__':unittest.main()
