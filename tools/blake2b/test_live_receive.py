"""Offline live producer/controller integration: no real nodes or live funds."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import live_receive as live
import receive_service as service
import swap_controller as controller
from service_manager import private_load

A,B,C,D=['02'+str(i)*64 for i in range(1,5)]


class LiveTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.config=dict(profile=live.PROFILE,btc_cli=['btc'],xbt_cli=['xbt'],node_ids=[A,B],
                         market=dict(max_btc_sats=2000,max_xbt_sats=500000,margin_bps=100),
                         max_xbt_routing_fee_msat=10000,max_delay=288)
        self.settings=dict(receive_policy=self.config,btc_cli=['btc'],xbt_cli=['xbt'],node_ids=[A,B],swap_root=str(self.root))
        self.preimage='11'*32;self.ph=hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.inv=dict(valid=True,type='bolt11 invoice',currency='xbt',amount_msat=325000000,
                      payment_hash=self.ph,payment_secret='22'*32,payee=D,created_at=900,expiry=1000,min_final_cltv_expiry=18)
        self.body=dict(request_id='ab'*16,xbt_invoice='lnxbt-original',max_btc_sats=2000)
        self.directory=self.root/('receive-api-'+self.body['request_id'])
        self.incoming=dict(short_channel_id='3x1x0',peer_id=A,channel_id='33'*32,funding_txid='44'*32,funding_outnum=0,
                           state='CHANNELD_NORMAL',peer_connected=True,htlcs=[],receivable_msat=100000000,
                           feerate={'perkw':1250},dust_limit_msat=546000)
        self.outgoing=dict(short_channel_id='1x1x0',peer_id=C,channel_id='55'*32,funding_txid='66'*32,funding_outnum=0,
                           state='CHANNELD_NORMAL',peer_connected=True,htlcs=[],spendable_msat=400000000,
                           feerate={'perkw':1250},dust_limit_msat=546000)
        self.route=dict(routes=[dict(amount_msat=325000000,final_cltv=40,path=[
            dict(short_channel_id_dir='1x1x0/0',node_id_in=B,node_id_out=C,
                 amount_in_msat=325005000,amount_out_msat=325005000,cltv_in=46,cltv_out=46),
            dict(short_channel_id_dir='2x1x0/0',node_id_in=C,node_id_out=D,
                 amount_in_msat=325005000,amount_out_msat=325000000,cltv_in=46,cltv_out=40)])])
        self.remote=dict(short_channel_id='2x1x0',source=C,destination=D,direction=0,active=True,
                         htlc_minimum_msat=1000,htlc_maximum_msat=1000000000,
                         base_fee_millisatoshi=5000,fee_per_millionth=0,delay=6)
        self.now=1000;self.height=100;self.funds=50000000;self.network='xbt';self.gate_profile=live.PROFILE
        self.node=B;self.unknown=False;self.phase='quoted';self.terms=None;self.calls=[];self.payments=[]
        self.lost_send=False;self.lost_release=False;self.close_count=0;self.lost_close=False
        for target,kwargs in (('time.time',dict(side_effect=lambda:self.now)),
                               ('live_receive.RPC.call',dict(side_effect=self.rpc)),
                               ('live_receive.oracle.fetch',dict(side_effect=self.market))):
            p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)

    def market(self,kind):
        base=dict(success=True,pair='BTCB2_BTC')
        if kind=='ticker':return dict(base,ticker=dict(bestBid='0.0045',bestAsk='0.0045',computedAt=self.now*1000))
        return dict(base,asks=[dict(price='0.0045',quantity='1',isAmm=False)])

    def rpc(self,cli,method,*args):
        self.assertIn(cli,(['btc'],['xbt'],['xbt','-k']))
        self.calls.append((cli,method,args))
        if method=='getinfo':return dict(id=A if cli==['btc'] else self.node,
                                        network='bitcoin' if cli==['btc'] else self.network,blockheight=self.height)
        if method=='xbt-pilot-info':return {'profile':self.gate_profile}
        if method=='decode':
            if cli==['xbt']:return copy.deepcopy(self.inv)
            t=self.terms
            return dict(valid=True,currency='bc',payee=A,payment_hash=t['payment_hash'],payment_secret=t['payment_secret'],
                        amount_msat=t['btc_amount_msat'],min_final_cltv_expiry=t['xbt_route_delay']+174)
        if method=='listpeers':return {'peers':[dict(id=C,connected=True)]}
        if method=='getroutes':return copy.deepcopy(self.route)
        if method=='listpeerchannels':return {'channels':[self.incoming if cli==['btc'] else self.outgoing]}
        if method=='listchannels':return {'channels':[] if self.unknown else [self.remote]}
        if method=='listfunds':return {'outputs':[dict(status='confirmed',reserved=False,amount_msat=self.funds)]}
        if method=='listsendpays':return {'payments':copy.deepcopy(self.payments)}
        if method=='xbt-register':
            self.assertTrue((self.directory/'quote.json').exists());self.terms=json.loads(args[0]);return {'registered':True}
        if method=='signinvoice':self.assertTrue(args[0].startswith('lnbc'));return {'bolt11':'lnbc-signed'}
        if method=='xbt-quote-status':return dict(payment_hash=self.ph,phase=self.phase,binding=['3x1x0',7] if self.phase!='quoted' else None)
        if method=='xbt-spend-info':return dict(self.terms,binding=['3x1x0',7],cltv_expiry=320)
        if method=='sendpay':
            state=private_load(self.directory/'state.json');self.assertEqual(state['phase'],'outgoing_started')
            self.assertEqual(state['btc_incoming_pin']['funding_txid'],self.incoming['funding_txid'])
            values=dict(x.split('=',1) for x in args);self.assertEqual(json.loads(values['route']),state['route'])
            self.assertEqual(values['bolt11'],state['xbt_invoice'])
            self.assertFalse(self.payments)
            self.payments=[dict(id=1,status='pending',payment_hash=self.ph,amount_msat=325000000,
                                amount_sent_msat=325005000,destination=D,bolt11=state['xbt_invoice'])]
            if self.lost_send:raise TimeoutError('lost submission reply')
            return {'status':'pending'}
        if method=='waitsendpay':raise TimeoutError('held fixture')
        if method=='xbt-release':
            self.assertEqual(args,(self.preimage,));self.phase='resolved'
            if self.lost_release:raise TimeoutError('lost release reply')
            return {'released':1}
        if method=='xbt-fail':self.phase='failed';return {'failed':1}
        if method=='close':
            self.assertEqual(args,(self.incoming['channel_id'],1));self.close_count+=1
            self.assertEqual(private_load(self.directory/'state.json')['btc_close_intent']['channel_id'],args[0])
            self.incoming['state']='AWAITING_UNILATERAL'
            if self.lost_close:raise TimeoutError('lost close reply')
            return {'type':'unilateral','txids':['77'*32]}
        raise AssertionError(method)

    def quote(self):return service.ReceiveQuotes(self.settings,auto_process=True).quote(self.body)

    def held(self):
        self.phase='held';self.incoming['htlcs']=[dict(id=7,direction='in',payment_hash=self.ph,
            amount_msat=self.terms['btc_amount_msat'],expiry=320,state='RCVD_ADD_ACK_REVOCATION')]

    def step(self):return service.process(self.directory,self.settings,rpc=self.rpc,now=lambda:self.now)

    def submitted(self):
        self.quote();self.held()
        with self.assertRaises(TimeoutError):self.step()
        self.assertEqual(private_load(self.directory/'state.json')['phase'],'outgoing_started')

    def test_api_quote_prices_full_allowance_and_pins_native_live_policy(self):
        offer=self.quote();q=private_load(self.directory/'quote.json')
        self.assertEqual((offer['btc_sats'],offer['xbt_sats']),(1478,325000))
        self.assertEqual(q['oracle']['xbt_sats'],325010)
        self.assertEqual(q['terms']['pilot'],live.PROFILE)
        self.assertEqual(q['terms']['min_cltv_delta'],196)
        self.assertEqual(q['controller']['btc_close_blocks'],72)
        self.assertEqual(self.step()['outcome'],'waiting_for_btc')
        self.assertFalse((self.directory/'state.json').exists())
        self.calls=[]
        with patch.object(live.oracle,'fetch',side_effect=AssertionError('reprice')):self.assertEqual(self.quote(),offer)
        self.assertEqual(self.calls,[])

    def test_gate_optin_and_network_identity_refused_before_registration(self):
        for field,bad in (('gate_profile','live-market-v2'),('network','xbt-regtest'),('node',C)):
            old=getattr(self,field);setattr(self,field,bad)
            with self.assertRaises(ValueError):live.create(self.config,'lnxbt-original',None,self.directory)
            setattr(self,field,old)
        self.assertFalse(self.directory.exists());self.assertNotIn('xbt-register',[m for _,m,_ in self.calls])

    def test_caps_fee_and_metadata_refused_without_spend(self):
        for field,value in (('max_btc_sats',1477),('max_xbt_sats',325009)):
            config=copy.deepcopy(self.config);config['market'][field]=value
            with self.assertRaises(ValueError):live.create(config,'lnxbt-original',None,self.directory)
        for field,value in (('max_delay',1843),('max_xbt_routing_fee_msat',100001)):
            with self.assertRaises(ValueError):live.validate_config(dict(self.config,**{field:value}))
        self.inv['payment_metadata']='ab'
        with self.assertRaises(ValueError):live.create(self.config,'lnxbt-original',None,self.directory)
        self.assertFalse(self.directory.exists());self.assertFalse(self.payments)

    def test_current_remote_limits_and_reserves_block_unspent_worker(self):
        self.quote();self.held()
        for field,value in (('unknown',True),('funds',49999999)):
            original=getattr(self,field);setattr(self,field,value)
            with self.assertRaises((ValueError,RuntimeError)):self.step()
            setattr(self,field,original)
        self.remote['base_fee_millisatoshi']=5001
        with self.assertRaises(ValueError):self.step()
        self.assertFalse((self.directory/'state.json').exists());self.assertFalse(self.payments)

    def test_changed_outgoing_funding_or_invoice_blocks_submission(self):
        self.quote();self.held()
        self.outgoing['funding_txid']='99'*32
        with self.assertRaises(ValueError):self.step()
        self.outgoing['funding_txid']='66'*32;self.inv['payment_secret']='88'*32
        with self.assertRaises(ValueError):self.step()
        self.assertFalse(self.payments)

    def test_stale_incoming_margin_does_not_send(self):
        self.quote();self.held();self.height=125
        self.assertEqual(self.step()['outcome'],'refused')
        before=(self.directory/'state.json').read_bytes();self.step()
        self.assertEqual((self.directory/'state.json').read_bytes(),before);self.assertFalse(self.payments)

    def test_lost_submission_recovers_without_resend_reprice_or_replan(self):
        self.lost_send=True;self.submitted()
        before=(self.directory/'state.json').read_bytes()
        for _ in range(2):self.assertEqual(self.step()['outcome'],'pending')
        self.assertEqual((self.directory/'state.json').read_bytes(),before)
        self.payments[0].update(status='complete',payment_preimage=self.preimage)
        self.now=2000;self.funds=0;self.unknown=True;self.gate_profile='live-market-v2'
        with patch.object(live.oracle,'fetch',side_effect=AssertionError('reprice')):
            self.assertEqual(self.step()['phase'],'btc_released')
            self.assertEqual(self.step()['phase'],'btc_released')
        self.assertEqual(sum(m=='sendpay' for _,m,_ in self.calls),1)
        self.assertEqual(sum(m=='getroutes' for _,m,_ in self.calls),1)

    def test_definite_failure_releases_only_original_binding(self):
        self.submitted();self.payments[0]['status']='failed'
        self.assertEqual(self.step()['phase'],'btc_failed');self.step()
        failed=[args for _,m,args in self.calls if m=='xbt-fail']
        self.assertEqual(failed,[(self.ph,json.dumps(['3x1x0',7]))])

    def test_lost_btc_release_reply_reconciles_durable_gate(self):
        self.submitted();self.payments[0].update(status='complete',payment_preimage=self.preimage)
        self.lost_release=True
        with self.assertRaises(TimeoutError):self.step()
        self.assertEqual(private_load(self.directory/'state.json')['phase'],'xbt_paid')
        self.assertEqual(self.step()['phase'],'btc_released')
        self.assertEqual(sum(m=='xbt-release' for _,m,_ in self.calls),1)

    def test_missing_or_inconsistent_outgoing_record_never_closes_or_releases(self):
        self.submitted();original=copy.deepcopy(self.payments);self.height=248
        for rows in ([],[dict(original[0],amount_sent_msat=325005001)],original*2):
            self.payments=rows
            with self.assertRaises(RuntimeError):self.step()
        self.assertEqual(self.close_count,0);self.assertEqual(self.phase,'held')
        self.assertEqual(sum(m=='sendpay' for _,m,_ in self.calls),1)

    def test_live_deadline_73_72_lost_reply_and_onchain_release(self):
        self.submitted();self.height=247;self.step();self.assertEqual(self.close_count,0)
        self.height=248;self.lost_close=True
        with self.assertRaises(TimeoutError):self.step()
        self.incoming['state']='ONCHAIN';self.step();self.assertEqual(self.close_count,1)
        self.payments[0].update(status='complete',payment_preimage=self.preimage)
        self.assertEqual(self.step()['phase'],'btc_released')
        self.assertEqual(sum(m=='sendpay' for _,m,_ in self.calls),1)

    def test_authenticated_http_and_background_worker_use_only_operator_rpcs(self):
        import threading
        import socket
        from reverse_quote_api import Quotes, Server
        from reverse_request import transport
        from service_runtime import tick as runtime_tick
        original_process=service.process
        def tick(settings):
            with patch.object(service,'process',side_effect=lambda *a,**kw:original_process(*a,**kw,rpc=self.rpc,now=lambda:self.now)):
                return runtime_tick(settings)
        self.settings['deployment']='operator-pair-v1'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=Server(port,Quotes(self.settings,auto_process=True),dict(token='ab'*32,scope='receive'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_address[1])
            offer=transport(url,'ab'*32,self.body,'/v1/receive')
            self.assertEqual(transport(url,'ab'*32,self.body,'/v1/receive'),offer)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
        health=tick(self.settings)
        self.assertTrue(health['operators_ready'])
        self.assertEqual(health['swaps'][0]['outcome'],'waiting_for_btc')
        self.held();tick(self.settings) # waitsendpay timeout: reconcile next step.
        self.assertEqual(tick(self.settings)['swaps'][0]['outcome'],'pending')
        self.payments[0].update(status='complete',payment_preimage=self.preimage)
        self.assertEqual(tick(self.settings)['swaps'],[])
        self.assertEqual(tick(self.settings)['swaps'],[])
        self.assertEqual(sum(m=='sendpay' for _,m,_ in self.calls),1)

    def test_no_authorization_never_originates_payment(self):
        service.ReceiveQuotes(self.settings,auto_process=False).quote(self.body);self.held()
        self.assertEqual(self.step()['outcome'],'needs_manual_resume')
        self.assertFalse((self.directory/'state.json').exists());self.assertFalse(self.payments)

    def test_wrong_incoming_funding_or_route_gate_commitment_blocks_spend(self):
        self.quote();self.held()
        service.process(self.directory,self.settings,rpc=self.rpc,now=lambda:self.now,
                        controller=Mock(return_value={'phase':'prepared'}))
        path=self.directory/'state.json'
        self.terms['xbt_route_digest']='99'*32
        with self.assertRaises(ValueError):controller.run(path)
        self.terms=private_load(self.directory/'quote.json')['terms']
        self.incoming['funding_txid']='99'*32
        with self.assertRaises(RuntimeError):controller.run(path)
        self.assertFalse(self.payments)

    def test_attempt_appearing_after_preparation_blocks_submission(self):
        self.quote();self.held()
        service.process(self.directory,self.settings,rpc=self.rpc,now=lambda:self.now,
                        controller=Mock(return_value={'phase':'prepared'}))
        path=self.directory/'state.json';before=path.read_bytes()
        state=private_load(path)
        self.payments.append({'payment_hash':state['payment_hash'],'status':'pending'})
        with self.assertRaises(ValueError):controller.run(path)
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(any(m=='sendpay' for _,m,_ in self.calls))

    def test_quote_tampering_and_guard_removal_refused(self):
        self.quote();q=private_load(self.directory/'quote.json')
        for section,key,value in (('terms','xbt_route_digest','99'*32),('controller','btc_deadline_guard',False),
                                  ('controller','btc_close_blocks',30),('oracle','btc_sats',1)):
            changed=copy.deepcopy(q);changed[section][key]=value
            with self.assertRaises(ValueError):live.validate_quote(changed)
        self.assertFalse(self.payments)


if __name__=='__main__':unittest.main()
