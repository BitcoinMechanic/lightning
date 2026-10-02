"""API quote fee pricing, route binding and worker submission boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import routed_receive_service as service
import receive_service as receive
from swap_controller import save
from service_manager import private_load


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = dict(profile=service.PROFILE, btc_cli=['btc'], xbt_cli=['xbt'],
                           market=dict(max_btc_sats=2000,max_xbt_sats=100010,margin_bps=0),
                           max_xbt_routing_fee_msat=10000)
        self.settings = dict(receive_policy=self.config, btc_cli=['btc'], xbt_cli=['xbt'],
                             node_ids=['btc','a'], swap_root=str(self.root))
        self.decoded = dict(valid=True,type='bolt11 invoice',currency='xbtrt',amount_msat=100000000,
                            payment_hash='11'*32,payment_secret='22'*32,payee='c',
                            created_at=900,expiry=1000,min_final_cltv_expiry=18)
        self.outgoing = dict(short_channel_id='1x1x0',peer_id='b',channel_id='33'*32,
                             funding_txid='44'*32,funding_outnum=0,state='CHANNELD_NORMAL',
                             peer_connected=True,htlcs=[],spendable_msat=100005000)
        self.incoming = dict(short_channel_id='3x1x0',peer_id='payer',channel_id='55'*32,
                             funding_txid='66'*32,funding_outnum=0,state='CHANNELD_NORMAL',
                             peer_connected=True,htlcs=[],receivable_msat=100000000,
                             feerate={'perkw':1250},dust_limit_msat=546000)
        self.route_result = dict(routes=[dict(amount_msat=100000000,final_cltv=40,path=[
            dict(short_channel_id_dir='1x1x0/0',node_id_in='a',node_id_out='b',
                 amount_in_msat=100005000,amount_out_msat=100005000,cltv_in=46,cltv_out=46),
            dict(short_channel_id_dir='2x1x0/0',node_id_in='b',node_id_out='c',
                 amount_in_msat=100005000,amount_out_msat=100000000,cltv_in=46,cltv_out=40)])])
        self.body = dict(request_id='ab'*16,xbt_invoice='lnxbtrt-original',max_btc_sats=2000)
        self.directory = self.root/('receive-api-'+self.body['request_id'])
        self.calls=[];self.terms=None;self.network='xbt-regtest';self.gate='quoted'
        self.ticker=dict(success=True,pair='BTCB2_BTC',ticker=dict(bestBid='0.015',bestAsk='0.015',computedAt=1000000))
        self.book=dict(success=True,pair='BTCB2_BTC',asks=[dict(price='0.015',quantity='1',isAmm=False)])
        for target, kw in (('routed_receive_service.RPC.call',dict(side_effect=self.rpc)),
                            ('routed_receive_service.oracle.fetch',dict(side_effect=lambda k:self.ticker if k=='ticker' else self.book)),
                            ('time.time',dict(return_value=1000))):
            p=patch(target,**kw);p.start();self.addCleanup(p.stop)

    def rpc(self,cli,method,*args):
        self.assertIn(cli,(['btc'],['xbt'],['xbt','-k']))
        self.calls.append((cli,method,args))
        if method=='getinfo':
            return dict(id='btc' if cli==['btc'] else 'a',network='regtest' if cli==['btc'] else self.network,blockheight=100)
        if method=='decode':
            if cli==['xbt']:return copy.deepcopy(self.decoded)
            return dict(valid=True,currency='bcrt',payee='btc',payment_hash=self.terms['payment_hash'],
                        payment_secret=self.terms['payment_secret'],amount_msat=self.terms['btc_amount_msat'],
                        min_final_cltv_expiry=160)
        if method=='getroutes':return copy.deepcopy(self.route_result)
        if method=='listpeerchannels':return {'channels':[self.incoming if cli==['btc'] else self.outgoing]}
        if method=='listsendpays':return {'payments':[]}
        if method=='xbt-register':
            self.assertTrue((self.directory/'quote.json').exists())
            self.assertFalse((self.directory/'state.json').exists())
            self.terms=json.loads(args[0]);return {'registered':True}
        if method=='signinvoice':return {'bolt11':'lnbcrt-signed'}
        if method=='xbt-quote-status':return dict(payment_hash=self.decoded['payment_hash'],phase=self.gate,binding=['3x1x0',7])
        if method=='xbt-spend-info':
            return dict(self.terms,binding=['3x1x0',7],cltv_expiry=260)
        raise AssertionError(method)

    def quote(self,auto=True):
        return receive.ReceiveQuotes(self.settings,auto_process=auto).quote(self.body)

    def held(self):
        self.gate='held'
        self.incoming['htlcs']=[dict(id=7,direction='in',payment_hash=self.decoded['payment_hash'],
                                    amount_msat=self.terms['btc_amount_msat'],expiry=260,state='RCVD_ADD_ACK_REVOCATION')]

    def test_full_fee_allowance_priced_and_receiver_amount_unchanged(self):
        offer=self.quote();q=private_load(self.directory/'quote.json')
        self.assertEqual((offer['btc_sats'],offer['xbt_sats']),(1501,100000))
        self.assertEqual(q['oracle']['xbt_sats'],100010)
        self.assertEqual(q['controller']['route'][0]['amount_msat'],100005000)
        self.assertEqual(q['controller']['xbt_first_hop']['funding_txid'],self.outgoing['funding_txid'])
        self.assertNotIn('btc_channel',q['terms'])
        self.assertNotIn('receiver_id',q['config'])
        zero=copy.deepcopy(self.config);zero['max_xbt_routing_fee_msat']=0
        self.assertEqual(service.pricing(zero,self.ticker,self.book,1000000)['btc_sats'],1500)
        tiny=copy.deepcopy(self.config);tiny['max_xbt_routing_fee_msat']=1
        self.assertEqual(service.pricing(tiny,self.ticker,self.book,1000000)['xbt_sats'],100001)

    def test_authenticated_http_returns_fee_priced_offer_without_customer_rpc(self):
        import socket
        import threading
        from reverse_quote_api import Quotes, Server
        from reverse_request import transport
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=Server(port,Quotes(self.settings,auto_process=True),dict(token='ab'*32,scope='receive'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(port)
            offer=transport(url,'ab'*32,self.body,'/v1/receive')
            self.assertEqual(offer['btc_sats'],1501)
            self.assertEqual(transport(url,'ab'*32,self.body,'/v1/receive'),offer)
            self.assertFalse((self.directory/'state.json').exists())
            self.assertEqual(sum(m=='xbt-register' for _,m,_ in self.calls),1)
            self.assertNotIn('sendpay',[m for _,m,_ in self.calls])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_repeat_returns_identical_quote_without_rpc_or_reprice(self):
        offer=self.quote();before=(self.directory/'quote.json').read_bytes();self.calls=[]
        with patch.object(service.oracle,'fetch',side_effect=AssertionError('repriced')):
            self.assertEqual(self.quote(),offer)
        self.assertEqual(self.calls,[]);self.assertEqual((self.directory/'quote.json').read_bytes(),before)

    def test_price_and_total_xbt_caps_refuse_before_registration(self):
        for key,value in (('max_btc_sats',1500),('max_xbt_sats',100009)):
            config=copy.deepcopy(self.config);config['market'][key]=value
            with self.assertRaises(ValueError):service.create(config,self.body['xbt_invoice'],None,self.directory)
            self.assertFalse(self.directory.exists())
        self.assertNotIn('xbt-register',[m for _,m,_ in self.calls])

    def test_wrong_network_and_unsupported_profile_never_mutate(self):
        for network in ('xbt','bitcoin'):
            self.network=network
            with self.assertRaises(ValueError):service.create(self.config,self.body['xbt_invoice'],None,self.directory)
        config=dict(self.config,profile='live-market-v1')
        with self.assertRaises(ValueError):service.identities(config)
        self.assertFalse(self.directory.exists())
        self.assertTrue(all(m=='getinfo' for _,m,_ in self.calls))

    def test_overbudget_route_and_stale_market_never_register(self):
        self.config['max_xbt_routing_fee_msat']=4999
        with self.assertRaises(ValueError):service.create(self.config,self.body['xbt_invoice'],None,self.directory)
        self.config['max_xbt_routing_fee_msat']=10000
        self.ticker['ticker']['computedAt']=0
        with self.assertRaises(ValueError):service.create(self.config,self.body['xbt_invoice'],None,self.directory)
        self.assertFalse(self.directory.exists());self.assertNotIn('xbt-register',[m for _,m,_ in self.calls])

    def test_saved_quote_tampering_fails_before_worker_calls(self):
        self.quote();original=private_load(self.directory/'quote.json')
        for section,key,value in (('oracle','xbt_sats',100000),('terms','btc_amount_msat',1),
                                  ('controller','xbt_invoice','other'),('controller','btc_node_id','other')):
            changed=copy.deepcopy(original);changed[section][key]=value
            with self.assertRaises(ValueError):service.validate_quote(changed)
            save(self.directory/'quote.json',changed)
            controller=Mock(side_effect=AssertionError('must not send'))
            with self.assertRaises(ValueError):receive.process(self.directory,self.settings,rpc=self.rpc,controller=controller,now=lambda:1000)
            controller.assert_not_called()
        save(self.directory/'quote.json',original)

    def test_changed_funding_pin_or_disconnected_first_hop_blocks_worker(self):
        self.quote();self.held();original=copy.deepcopy(self.outgoing)
        for change in ({'funding_txid':'77'*32},{'peer_connected':False},{'spendable_msat':100004999}):
            self.outgoing=dict(original,**change)
            controller=Mock(side_effect=AssertionError('must not send'))
            with self.assertRaises(ValueError):receive.process(self.directory,self.settings,rpc=self.rpc,controller=controller,now=lambda:1000)
            self.assertFalse((self.directory/'state.json').exists());controller.assert_not_called()

    def test_no_state_before_committed_btc_and_pin_before_worker_submission(self):
        self.quote();controller=Mock()
        self.assertEqual(receive.process(self.directory,self.settings,rpc=self.rpc,controller=controller,now=lambda:1000)['outcome'],'waiting_for_btc')
        controller.assert_not_called();self.assertFalse((self.directory/'state.json').exists())
        self.held()
        def submit(path,**kw):
            state=private_load(path)
            self.assertEqual(state['btc_incoming_pin']['funding_txid'],self.incoming['funding_txid'])
            self.assertEqual(state['xbt_first_hop']['funding_txid'],self.outgoing['funding_txid'])
            self.assertEqual(state['btc_binding'],['3x1x0',7])
            self.assertEqual(state['route'][0]['amount_msat'],100005000)
            state['phase']='outgoing_started';save(path,state)
            return {'outcome':'pending','phase':'outgoing_started'}
        self.assertEqual(receive.process(self.directory,self.settings,rpc=self.rpc,controller=submit,now=lambda:1000)['outcome'],'pending')
        self.calls=[]
        recover=Mock(return_value={'outcome':'pending'})
        with patch.object(service.oracle,'fetch',side_effect=AssertionError('recovery repriced')):
            receive.process(self.directory,self.settings,rpc=self.rpc,controller=recover,now=lambda:1000)
        recover.assert_called_once_with(self.directory/'state.json',recover_only=True)
        self.assertEqual(self.calls,[])

    def test_manual_or_expired_permit_does_not_create_state(self):
        self.quote(auto=False);self.held();controller=Mock()
        self.assertEqual(receive.process(self.directory,self.settings,rpc=self.rpc,controller=controller,now=lambda:1000)['outcome'],'needs_manual_resume')
        q=private_load(self.directory/'quote.json')
        save(self.directory/'receive-authorization.json',dict(format='receive-authorization-v1',quote_sha256=receive.digest(q),expires_at=q['terms']['expires_at']))
        self.assertEqual(receive.process(self.directory,self.settings,rpc=self.rpc,controller=controller,now=lambda:2000)['outcome'],'authorization_expired')
        controller.assert_not_called();self.assertFalse((self.directory/'state.json').exists())


if __name__=='__main__':unittest.main()
