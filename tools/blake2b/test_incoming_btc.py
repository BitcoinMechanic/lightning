"""Unbound quotes bind the real BTC HTLC before spending and never reselect."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

import incoming_btc as incoming
import live_pilot as pilot
import market_policy
import receive_selection
from swap_controller import run, save, check_spend
from service_manager import private_load
import test_market_quotes
import test_quote_replay
from quote_refusal import QuoteRefused


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.f = test_market_quotes.MarketTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config['profile'] = pilot.PROFILE_MARKET_ANY
        self.f.config['market'].pop('btc_channel')
        self.f.incoming.update(channel_id='aa'*32, funding_txid='bb'*32, funding_outnum=0)
        self.other = dict(self.f.incoming, short_channel_id='30x1x0', channel_id='cc'*32,
                          funding_txid='dd'*32)
        self.selected = self.f.incoming
        original = self.f.rpc
        def rpc(cli, method, *args):
            if method == 'listpeerchannels' and cli == ['/btc']:
                return dict(channels=[self.f.incoming, self.other])
            result = original(cli, method, *args)
            if method in ('xbt-spend-info', 'xbt-quote-status'):
                result['binding'] = [self.selected['short_channel_id'], 7]
            return result
        self.rpc = rpc
        self.f.rpc = rpc

    def prepare(self, other=False):
        self.f.quote()
        self.selected = self.other if other else self.f.incoming
        q = private_load(self.f.f.directory/'quote.json')
        self.assertNotIn('btc_channel', q['terms'])
        self.assertNotIn('btc_channel', q['controller'])
        self.assertNotIn('btc_channel', q['config']['market'])
        self.selected['htlcs'] = [dict(id=7, direction='in', state='RCVD_ADD_ACK_REVOCATION',
            payment_hash=q['terms']['payment_hash'], amount_msat=q['terms']['btc_amount_msat'], expiry=400)]
        state = incoming.bind(dict(q['controller'], btc_binding=[self.selected['short_channel_id'],7]),self.rpc)
        path = self.f.f.directory/'state.json'; save(path,state)
        return path,state

    def test_quote_has_no_channel_binding_and_second_channel_can_settle(self):
        path,state=self.prepare(other=True)
        self.assertEqual(state['btc_incoming_pin']['funding_txid'],self.other['funding_txid'])
        with patch('swap_controller.RPC.call',side_effect=self.rpc):
            self.assertEqual(run(path)['phase'],'btc_released')
            self.assertEqual(run(path)['phase'],'btc_released')
        self.assertEqual(self.f.sends,1)

    def test_first_channel_can_settle_and_pin_persists_through_pending_recovery(self):
        path,state=self.prepare();self.f.pending=True
        with patch('swap_controller.RPC.call',side_effect=self.rpc):
            with self.assertRaises(subprocess.TimeoutExpired): run(path)
            before=path.read_bytes()
            self.assertEqual(run(path)['outcome'],'pending')
            self.assertEqual(path.read_bytes(),before)
            self.f.complete(); self.assertEqual(run(path)['phase'],'btc_released')
        self.assertEqual(private_load(path)['btc_incoming_pin'],state['btc_incoming_pin'])
        self.assertEqual(self.f.sends,1)

    def test_changed_funding_htlc_or_gate_cannot_spend(self):
        path,state=self.prepare()
        for key,value in (('funding_txid','ee'*32),('funding_outnum',1),('channel_id','ff'*32),('peer_connected',False)):
            old=self.selected[key];self.selected[key]=value
            with patch('swap_controller.RPC.call',side_effect=self.rpc),self.assertRaises(RuntimeError): run(path)
            self.selected[key]=old
        h=self.selected['htlcs'][0]
        for key,value in (('amount_msat',1),('expiry',401),('local_trimmed',True),('direction','out'),('state','RCVD_ADD_HTLC')):
            old=copy.deepcopy(h);h[key]=value
            with self.assertRaises(RuntimeError): incoming.check_spend(state,self.rpc(['/btc'],'xbt-spend-info'),self.rpc)
            h.clear();h.update(old)
        info=self.rpc(['/btc'],'xbt-spend-info')
        for key,value in (('btc_channel_policy',None),('binding',['30x1x0',7]),('cltv_expiry',401),('btc_amount_msat',1)):
            with self.assertRaises(RuntimeError): incoming.check_spend(state,dict(info,**{key:value}),self.rpc)
        self.assertEqual(self.f.sends,0);self.assertEqual(private_load(path),state)

    def test_missing_or_changed_pin_never_originate(self):
        path,state=self.prepare()
        for key in ('btc_incoming_pin','btc_channel_policy'):
            changed=copy.deepcopy(state);changed.pop(key);save(path,changed)
            with patch('swap_controller.RPC.call',side_effect=self.rpc),self.assertRaises((KeyError,RuntimeError)):run(path)
        changed=copy.deepcopy(state);changed['btc_binding']=['30x1x0',7];save(path,changed)
        with patch('swap_controller.RPC.call',side_effect=self.rpc),self.assertRaises(RuntimeError):run(path)
        self.assertEqual(self.f.sends,0)

    def test_deadline_refuses_changed_funding_without_close_or_resend(self):
        from deadline_guard import protect
        path,state=self.prepare();state['phase']='outgoing_started'
        self.selected['funding_txid']='ee'*32
        with self.assertRaises(RuntimeError): protect(path,state,self.rpc,save)
        self.assertNotIn('btc_close_intent',state)
        self.assertEqual(self.f.sends,0)

    def test_deadline_closes_only_pinned_second_channel_once(self):
        from deadline_guard import protect
        path,state=self.prepare(other=True);state['phase']='outgoing_started'
        closes=[]
        def rpc(cli,method,*args):
            if method=='close':
                self.assertEqual(private_load(path)['btc_close_intent']['channel_id'],self.other['channel_id'])
                closes.append(args[0]);self.other['state']='AWAITING_UNILATERAL'
                return dict(type='unilateral')
            result=self.rpc(cli,method,*args)
            if method=='getinfo' and cli==['/btc']:result=dict(result,blockheight=328)
            return result
        protect(path,state,rpc,save)
        protect(path,private_load(path),rpc,save)
        self.assertEqual(closes,[self.other['channel_id']])
        self.assertEqual(self.f.incoming['state'],'CHANNELD_NORMAL')
        self.assertEqual(self.f.sends,0)

    def test_publication_accepts_one_eligible_channel_without_pinning_it(self):
        self.f.incoming.update(peer_connected=False)
        self.f.quote()
        self.assertEqual(len(self.f.registered),1)
        self.other['receivable_msat']=0
        with self.assertRaises(QuoteRefused):incoming.preflight(self.f.config,1500000,self.rpc)

    def test_new_policy_has_no_incoming_channel_and_legacy_still_requires_it(self):
        config=copy.deepcopy(self.f.config)
        config['profile']=receive_selection.PROFILE_ANY
        config['market'].pop('xbt_peer');config['market'].pop('xbt_channel')
        settings=dict(receive_policy=config,btc_cli=config['btc_cli'],xbt_cli=config['xbt_cli'])
        self.assertEqual(receive_selection.configuration(settings),config)
        config['market']['btc_channel']='20x1x0'
        with self.assertRaises(ValueError):receive_selection.configuration(settings)
        bad=copy.deepcopy(self.f.config);bad['profile']=pilot.PROFILE_MARKET
        with self.assertRaises(ValueError):market_policy.policy(bad)


class GateTests(test_quote_replay.ReplayTests):
    def setUp(self):
        super().setUp()
        self.terms['btc_channel_policy']=incoming.POLICY

    def test_other_channel_cannot_replace_accepted_binding(self):
        self.accept();before=self.disk.read_bytes()
        replies=self.run_plugin(1001,[self.hook(htlc=dict(self.htlc,short_channel_id='200x1x0'))])
        self.assertEqual(replies[3]['result']['result'],'fail')
        self.assertEqual(self.disk.read_bytes(),before)

    def test_second_channel_can_be_original_binding_and_replay(self):
        self.htlc['short_channel_id']='200x1x0';self.accept()
        before=self.disk.read_bytes()
        replies=self.run_plugin(2000,[self.hook(),self.request(4,'xbt-spend-info',[self.hash])])
        self.assertEqual(replies[4]['result']['binding'],['200x1x0',7])
        self.assertEqual(replies[4]['result']['btc_channel_policy'],incoming.POLICY)
        self.assertEqual(self.disk.read_bytes(),before)

    def test_unknown_policy_refused(self):
        self.terms['btc_channel_policy']='anything'
        replies=self.run_plugin(1000,[self.request(2,'xbt-register',[self.terms])])
        self.assertIn('error',replies[2]);self.assertFalse(self.disk.exists())


class LiveGateTests(unittest.TestCase):
    def test_v2_registration_requires_explicit_profile_and_no_fixed_channel(self):
        f=test_quote_replay.ReplayTests();f.setUp();self.addCleanup(f.doCleanups)
        terms=dict(f.terms, pilot=pilot.PROFILE_MARKET_ANY, btc_channel_policy=incoming.POLICY,
                   btc_amount_msat=1500000, xbt_invoice='lnxbt-fixture',
                   min_cltv_delta=288,max_cltv_delta=2016,oracle_digest='aa'*32,controller_id='bb'*32)
        def call(profile,quote):
            init=f.request(20,'init',dict(configuration=dict(network='bitcoin'),
                           options={'xbt-live-pilot':profile}))
            return f.run_plugin(1000,[init,f.request(2,'xbt-register',[quote])])
        self.assertIn('error',call(pilot.PROFILE_MARKET,terms)[2])
        self.assertIn('error',call(pilot.PROFILE_MARKET_ANY,dict(terms,btc_channel='1x1x0'))[2])
        self.assertEqual(call(pilot.PROFILE_MARKET_ANY,terms)[2]['result'],dict(registered=True))
        self.assertEqual(json.loads(f.disk.read_text())[f.hash]['terms'],terms)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        import test_receive_selection as fixture
        import receive_service
        self.service=receive_service
        self.f=fixture.SelectionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.config['profile']=receive_selection.PROFILE_ANY
        self.f.config['market'].pop('btc_channel')
        self.c=dict(short_channel_id='9x1x0',channel_id='aa'*32,funding_txid='bb'*32,funding_outnum=0,
                    state='CHANNELD_NORMAL',peer_connected=True,feerate={'perkw':829},dust_limit_msat=546000,
                    htlcs=[dict(id=7,direction='in',payment_hash='1'*64,state='RCVD_ADD_ACK_REVOCATION',
                                amount_msat=1500000,expiry=400)])
        self.info=dict(payment_hash='1'*64,binding=['9x1x0',7],btc_amount_msat=1500000,
                       btc_channel_policy=incoming.POLICY,cltv_expiry=400)
        original_create=self.f.create
        def create(*args):
            original_create(*args)
            path=args[-1]/'quote.json';q=private_load(path)
            q['terms'].pop('btc_channel');q['terms']['btc_channel_policy']=incoming.POLICY
            q['controller'].update(btc_channel_policy=incoming.POLICY,btc_amount_msat=1500000)
            save(path,q)
        with patch('receive_service.service.create',create):
            self.service.ReceiveQuotes(self.f.settings,auto_process=True).quote(self.f.body())

    def rpc(self,cli,method,*args):
        if method=='xbt-quote-status': return dict(payment_hash='1'*64,binding=['9x1x0',7],phase='held')
        if method=='xbt-spend-info':return self.info
        if method=='listsendpays':return dict(payments=[])
        if method=='listpeerchannels' and cli==['/btc']:return dict(channels=[self.c])
        return self.f.rpc(cli,method,*args)

    def test_actual_binding_saved_before_controller_and_reused(self):
        path=self.f.directory()/'state.json'
        def controller(file,**kwargs):
            self.assertEqual(file,path)
            state=private_load(file)
            self.assertEqual(state['btc_binding'],['9x1x0',7])
            self.assertEqual(state['btc_incoming_pin']['funding_txid'],'bb'*32)
            if not kwargs.get('recover_only'):
                state['phase']='outgoing_started';save(file,state)
            return dict(outcome='pending')
        self.assertEqual(self.service.process(self.f.directory(),self.f.settings,rpc=self.rpc,controller=controller),dict(outcome='pending'))
        before=path.read_bytes()
        self.service.process(self.f.directory(),self.f.settings,rpc=self.rpc,controller=controller)
        self.assertEqual(path.read_bytes(),before)

    def test_uncommitted_waits_and_inconsistent_amount_never_creates_state(self):
        from unittest.mock import Mock
        controller=Mock()
        self.c['htlcs'][0]['state']='RCVD_ADD_HTLC'
        self.assertEqual(self.service.process(self.f.directory(),self.f.settings,rpc=self.rpc,controller=controller),dict(outcome='waiting_for_commitment'))
        self.c['htlcs'][0].update(state='RCVD_ADD_ACK_REVOCATION',amount_msat=1)
        with self.assertRaises(RuntimeError):self.service.process(self.f.directory(),self.f.settings,rpc=self.rpc,controller=controller)
        controller.assert_not_called();self.assertFalse((self.f.directory()/'state.json').exists())


if __name__=='__main__':unittest.main()
