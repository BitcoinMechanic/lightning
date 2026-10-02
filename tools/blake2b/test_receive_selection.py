"""Invoice-selected receiving and recovery remain bound to the original receiver."""
import copy
from email.message import Message
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import receive_selection as select
import receive_service as service
from reverse_quote_api import Quotes, Server, Handler
from service_manager import private_load
from swap_controller import save
from quote_refusal import QuoteRefused
import test_customer_receive as customer_fixture

BTC = '02'+'01'*32
XBT = '03'+'02'*32
A = '02'+'03'*32
B = '03'+'04'*32


class SelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = dict(swap_root=str(self.root), btc_cli=['/btc'], xbt_cli=['/xbt'], node_ids=[BTC,XBT])
        self.config = dict(profile=select.PROFILE, btc_cli=['/btc'], xbt_cli=['/xbt'], market=dict(
            btc_channel='1x1x1', max_btc_sats=3000, max_xbt_sats=400000, margin_bps=100))
        self.settings['receive_policy'] = self.config
        self.channels = [dict(peer_id=peer, state='CHANNELD_NORMAL', peer_connected=True,
            short_channel_id=str(n)+'x1x1', channel_id=str(n)*64, funding_txid=str(n+2)*64,
            funding_outnum=0, spendable_msat=400000000, htlcs=[]) for n,peer in ((2,A),(3,B))]
        self.decoded = {invoice:dict(valid=True,type='bolt11 invoice',currency='xbt',amount_msat=200000000,
            payee=peer,payment_hash=str(n)*64,payment_secret='ab'*32,created_at=1000,expiry=2000000000)
            for n,peer,invoice in ((1,A,'lnxbt-a'),(2,B,'lnxbt-b'))}
        self.created = 0
        self.submitted = 0
        self.selected = 0
        self.gate_phase = 'quoted'
        self.original_check = select.check_channel
        original = select.select
        def selector(*args):
            self.selected += 1
            return original(*args, rpc=self.rpc)
        for name, replacement in (('receive_service.selection.select', selector),
                                   ('receive_service.selection.check_channel', lambda pin, rpc=None: self.check_pin(pin)),
                                   ('receive_service.service.identities', lambda config:[BTC,XBT]),
                                   ('receive_service.service.create', self.create),
                                   ('receive_service.service.publish', self.publish)):
            p=patch(name,replacement); p.start(); self.addCleanup(p.stop)
        self.api = service.ReceiveQuotes(self.settings, auto_process=True)

    def rpc(self, cli, method, *args):
        self.assertIn(cli, (['/btc'], ['/xbt']))
        if method=='getinfo': return dict(id=BTC if cli==['/btc'] else XBT,network='bitcoin' if cli==['/btc'] else 'xbt')
        if method=='decode': return copy.deepcopy(self.decoded[args[0]])
        if method=='listpeerchannels': return dict(channels=copy.deepcopy(self.channels))
        self.fail('Unexpected RPC '+method)

    def check_pin(self, pin):
        return self.original_check(pin, rpc=self.rpc)

    def body(self, n=1):
        return dict(request_id=str(n)*32,xbt_invoice='lnxbt-a' if n==1 else 'lnxbt-b',max_btc_sats=1500)

    def directory(self,n=1): return self.root/('receive-api-'+str(n)*32)

    def create(self,config,invoice,price,directory):
        self.created+=1; directory.mkdir(mode=0o700)
        ph=self.decoded[invoice]['payment_hash']
        save(directory/'quote.json',dict(config=config,node_ids=[BTC,XBT],controller=dict(
            config,phase='prepared',payment_hash=ph,xbt_invoice=invoice),terms=dict(
            payment_hash=ph,xbt_invoice=invoice,btc_amount_msat=1500000,xbt_amount_msat=200000000,
            btc_channel='1x1x1',expires_at=2000000000)))

    def publish(self,directory):
        q=private_load(directory/'quote.json');q['btc_invoice']='lnbc-'+q['terms']['payment_hash']
        save(directory/'quote.json',q)

    def test_two_receivers_without_customer_setting_and_stable_replay(self):
        before=copy.deepcopy(self.settings)
        offers=[self.api.quote(self.body(n)) for n in (1,2)]
        pins=[private_load(self.directory(n)/'quote.json')['receive_selection'] for n in (1,2)]
        self.assertEqual([p['payee'] for p in pins],[A,B])
        self.assertNotEqual(pins[0]['funding_txid'],pins[1]['funding_txid'])
        self.channels=[]
        fresh=service.ReceiveQuotes(self.settings,auto_process=True)
        for n in (1,2): self.assertEqual(fresh.quote(self.body(n)),offers[n-1])
        self.assertEqual((self.created,self.selected),(2,2))
        self.assertEqual(self.settings,before)

    def test_selection_saved_before_creation_and_no_reselection_after_refusal(self):
        def refused(config,invoice,price,directory):
            journal=private_load(self.root/'receive-requests'/('1'*32+'.json'))
            self.assertEqual(journal['selection']['config'],config)
            raise QuoteRefused('btc_peer_disconnected')
        with patch('receive_service.service.create',refused),self.assertRaises(QuoteRefused): self.api.quote(self.body())
        self.api.quote(dict(self.body(),retry_refused=True))
        self.assertEqual(self.selected,1)

    def test_changed_funding_prevents_worker_before_controller(self):
        self.api.quote(self.body())
        self.channels[0]['funding_txid']='ff'*32
        controller=MagicMock()
        with self.assertRaises(ValueError):
            service.process(self.directory(),self.settings,rpc=self.rpc,controller=controller)
        controller.assert_not_called()

    def test_changed_policy_refuses_new_spend_but_started_recovery_continues(self):
        self.api.quote(self.body())
        changed=copy.deepcopy(self.settings); changed['receive_policy']['market']['max_xbt_sats']=300000
        controller=MagicMock(return_value=dict(outcome='pending'))
        with self.assertRaises(ValueError): service.process(self.directory(),changed,rpc=self.rpc,controller=controller)
        controller.assert_not_called()
        quote=private_load(self.directory()/'quote.json')
        save(self.directory()/'state.json',dict(quote['controller'],phase='outgoing_started'))
        self.assertEqual(service.process(self.directory(),changed,rpc=self.rpc,controller=controller),dict(outcome='pending'))
        self.assertTrue(controller.call_args.kwargs['recover_only'])

    def test_same_hash_in_another_invoice_cannot_create_second_quote(self):
        self.api.quote(self.body())
        self.decoded['lnxbt-b']['payment_hash']=self.decoded['lnxbt-a']['payment_hash']
        with self.assertRaises(ValueError): self.api.quote(self.body(2))
        self.assertEqual(self.created,1)

    def test_modified_request_selection_and_authorization_refused(self):
        self.api.quote(self.body())
        with self.assertRaises(ValueError): self.api.quote(dict(self.body(),xbt_invoice='lnxbt-b'))
        q=private_load(self.directory()/'quote.json')
        q['receive_selection']['payee']=B;save(self.directory()/'quote.json',q)
        controller=MagicMock()
        with self.assertRaises(ValueError): service.process(self.directory(),self.settings,rpc=self.rpc,controller=controller)
        controller.assert_not_called()

    def test_invalid_invoice_ambiguous_channels_and_low_balance_refused(self):
        for key,value in (('valid',False),('currency','bc'),('payee',XBT),('amount_msat',500000000)):
            original=self.decoded['lnxbt-a'][key]; self.decoded['lnxbt-a'][key]=value
            with self.assertRaises(ValueError): select.select(self.config,'lnxbt-a',1500,[BTC,XBT])
            self.decoded['lnxbt-a'][key]=original
        self.channels.append(copy.deepcopy(self.channels[0]))
        with self.assertRaises(QuoteRefused): select.select(self.config,'lnxbt-a',1500,[BTC,XBT])
        self.channels.pop();self.channels[0]['spendable_msat']=1000
        with self.assertRaises(QuoteRefused): select.select(self.config,'lnxbt-a',1500,[BTC,XBT])

    def test_receive_only_credential_never_authorizes_reverse_endpoint(self):
        api=Quotes(self.settings,auto_process=True)
        with patch('reverse_quote_api.HTTPServer.__init__'):
            server=Server(19840,api,dict(scope='receive',token='ab'*32))
        handler=MagicMock();handler.server=server;handler.path='/v1/quote'
        handler.headers=Message();handler.headers['Authorization']='Bearer '+'ab'*32
        Handler.do_POST(handler)
        handler.reply.assert_called_once_with(403,{'error':'request_rejected'})
        handler.rfile.read.assert_not_called()


class CustomerTests(unittest.TestCase):
    setUp=customer_fixture.CustomerTests.setUp
    rpc=customer_fixture.CustomerTests.rpc
    send=customer_fixture.CustomerTests.send

    def test_receive_scope_credential_has_no_customer_id(self):
        import customer_receive
        credentials=dict(token='ab'*32,scope='receive')
        value=customer_receive.workflow(['customer'],self.directory,credentials,
            'http://127.0.0.1:19840',325000,1480,rpc=self.rpc,send=self.send,now=lambda:1000)
        self.assertEqual(value['outcome'],'awaiting_btc')
        state=private_load(self.directory/'receive.json')
        self.assertEqual(state['customer_id'],'customer')
        self.assertNotIn('payer_id',credentials)


if __name__=='__main__': unittest.main()
