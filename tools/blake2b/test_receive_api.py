"""Forward API identity, restart, quote authorization and submission boundaries."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import receive_service as receiving
from quote_refusal import QuoteRefused
from service_manager import private_load
from swap_controller import save
from test_reverse_quote_api import ApiTests


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = dict(profile='live-market-v1', btc_cli=['/btc'], xbt_cli=['/xbt'], market=dict(
            btc_channel='1x1x1', xbt_channel='2x2x2', xbt_peer='customer', max_btc_sats=3000,
            max_xbt_sats=400000, margin_bps=100))
        self.settings = dict(swap_root=str(self.root), btc_cli=['/btc'], xbt_cli=['/xbt'],
                             node_ids=['btc-id', 'xbt-id'], receiver_id='customer', receive_config=self.config)
        self.body = dict(request_id='ab'*16, xbt_invoice='lnxbt-original', max_btc_sats=1480)
        self.created = self.published = self.sent = 0
        self.gate = 'quoted'
        self.committed = True
        self.api = receiving.ReceiveQuotes(self.settings, auto_process=True)
        for name, replacement in (('create', self.create), ('publish', self.publish),
                                   ('identities', lambda c: self.settings['node_ids'])):
            patcher = patch('receive_service.service.'+name, replacement)
            patcher.start(); self.addCleanup(patcher.stop)

    def create(self, config, invoice, price, directory):
        self.created += 1
        self.assertIsNone(price)
        self.assertEqual(config['market']['max_btc_sats'], 1480)
        directory.mkdir(mode=0o700)
        template = dict(config, phase='prepared', payment_hash='hash', xbt_invoice=invoice)
        save(directory/'quote.json', dict(config=config, node_ids=self.settings['node_ids'],
            controller=template, terms=dict(xbt_invoice=invoice, btc_amount_msat=1473000,
                xbt_amount_msat=325000000, expires_at=2000000000, payment_hash='hash', btc_channel='1x1x1')))

    def publish(self, directory):
        self.published += 1
        q = private_load(directory/'quote.json'); q['btc_invoice'] = 'lnbc-offer'
        save(directory/'quote.json', q)

    def directory(self):
        return self.root/('receive-api-'+self.body['request_id'])

    def rpc(self, cli, method, *args):
        self.assertIn(cli, (['/btc'], ['/xbt']))
        if method == 'xbt-quote-status':
            return dict(phase=self.gate, payment_hash='hash', binding=['1x1x1', 7])
        if method == 'listpeerchannels':
            return {'channels': [dict(short_channel_id='1x1x1', htlcs=[dict(id=7, direction='in',
                payment_hash='hash', state='RCVD_ADD_ACK_REVOCATION' if self.committed else 'pending')])]}
        if method == 'listsendpays': return {'payments': []}
        self.fail('Unexpected RPC')

    def controller(self, path, recover_only=False):
        state = private_load(path)
        if state['phase'] == 'prepared':
            self.assertFalse(recover_only)
            self.sent += 1
            state['phase'] = 'outgoing_started'; save(path, state)
        else:
            self.assertTrue(recover_only)
        return dict(phase=state['phase'], outcome='pending', preimage='PRIVATE')

    def step(self):
        return receiving.process(self.directory(), self.settings, rpc=self.rpc, controller=self.controller)

    def test_repeat_and_lost_publication_reply_preserve_original_offer(self):
        def lost(directory):
            self.publish(directory)
            raise TimeoutError('PRIVATE')
        with patch('receive_service.service.publish', lost), self.assertRaises(TimeoutError):
            self.api.quote(self.body)
        first = self.api.quote(self.body)
        self.assertEqual(self.api.quote(self.body), first)
        self.assertEqual((self.created, self.published, self.sent), (1, 1, 0))
        self.assertEqual(set(first), receiving.FIELDS)

    def test_uncertain_creation_never_retried(self):
        with patch('receive_service.service.create', side_effect=TimeoutError), self.assertRaises(TimeoutError):
            self.api.quote(self.body)
        with self.assertRaises(ValueError):
            self.api.quote(dict(self.body, retry_refused=True))
        self.assertEqual(self.created, 0)

    def test_explicit_refusal_retry_retains_request(self):
        with patch('receive_service.service.create', side_effect=ValueError('oracle BTC amount exceeds operator cap')):
            with self.assertRaises(QuoteRefused) as caught: self.api.quote(self.body)
            self.assertEqual(caught.exception.reason, 'btc_price_cap')
        with self.assertRaises(QuoteRefused): self.api.quote(self.body)
        self.api.quote(dict(self.body, retry_refused=True))
        self.assertEqual(self.created, 1)

    def test_reused_id_changed_customer_and_second_id_same_invoice_refused(self):
        self.api.quote(self.body)
        for changed in (dict(self.body, max_btc_sats=1470), dict(self.body, xbt_invoice='lnxbt-other'),
                        dict(self.body, request_id='cd'*16)):
            with self.assertRaises(ValueError): self.api.quote(changed)
        changed = copy.deepcopy(self.settings); changed['node_ids'][0] = 'different'
        with self.assertRaises(ValueError): receiving.ReceiveQuotes(changed).quote(self.body)
        changed['receiver_id'] = 'other'
        with self.assertRaises(ValueError): receiving.ReceiveQuotes(changed)
        self.assertEqual(self.created, 1)

    def test_api_request_bounds_before_creation(self):
        for key, value in (('request_id', '../bad'), ('max_btc_sats', True), ('max_btc_sats', 10001),
                           ('xbt_invoice', 'lnbc-wrong'), ('retry_refused', 1)):
            with self.assertRaises(ValueError): self.api.quote(dict(self.body, **{key:value}))
        self.assertEqual(self.created, 0)

    def test_worker_waits_then_submits_once_and_recovers_without_permit(self):
        self.api.quote(self.body)
        self.assertEqual(self.step()['outcome'], 'waiting_for_btc')
        self.assertFalse((self.directory()/'state.json').exists())
        self.gate = 'held'; self.committed = False
        self.assertEqual(self.step()['outcome'], 'waiting_for_commitment')
        self.committed = True
        self.assertNotIn('preimage', self.step())
        (self.directory()/'receive-authorization.json').unlink()
        self.step(); self.step()
        self.assertEqual(self.sent, 1)

    def test_old_quote_not_upgraded_to_automatic(self):
        self.api = receiving.ReceiveQuotes(self.settings, auto_process=False)
        self.api.quote(self.body)
        receiving.ReceiveQuotes(self.settings, auto_process=True).quote(self.body)
        self.assertFalse((self.directory()/'receive-authorization.json').exists())
        self.assertEqual(self.step()['outcome'], 'needs_manual_resume')
        self.assertEqual(self.sent, 0)

    def test_modified_quote_permit_and_expiry_never_submit(self):
        self.api.quote(self.body); self.gate = 'held'
        permit = self.directory()/'receive-authorization.json'
        original = private_load(permit)
        save(permit, dict(original, quote_sha256='bad'))
        with self.assertRaises(ValueError): self.step()
        save(permit, original)
        result = receiving.process(self.directory(), self.settings, rpc=self.rpc,
                                    controller=self.controller, now=lambda:2000000000)
        self.assertEqual(result['outcome'], 'authorization_expired')
        quote = self.directory()/'quote.json'; changed = private_load(quote)
        changed['controller']['xbt_invoice'] = 'lnxbt-other'; save(quote, changed)
        with self.assertRaises(ValueError): self.step()
        self.assertEqual(self.sent, 0)

    def test_lock_prevents_competing_worker_rpc(self):
        self.api.quote(self.body)
        with receiving.lock(self.directory()/'service.lock'), self.assertRaises(BlockingIOError):
            self.step()
        self.assertEqual(self.sent, 0)

    def test_worker_tick_dispatches_authorized_forward_quote(self):
        from service_runtime import tick
        self.api.quote(self.body)
        settings = dict(self.settings, deployment='operator-pair-v1')
        def rpc(cli, method, *args):
            if method == 'getinfo': return dict(network='bitcoin' if cli == ['/btc'] else 'xbt',
                                               id='btc-id' if cli == ['/btc'] else 'xbt-id')
            if method == 'listpeers': return {'peers': []}
            self.fail(method)
        with patch('service_runtime.RPC.call', rpc), patch('receive_service.process', return_value={'outcome':'waiting_for_btc'}) as process:
            health = tick(settings)
        process.assert_called_once_with(self.directory(), settings)
        self.assertEqual(health['swaps'][0]['outcome'], 'waiting_for_btc')


class ControllerIntegrationTests(unittest.TestCase):
    def test_actual_market_quote_controller_and_recovery_only_one_send(self):
        import subprocess
        from test_market_quotes import MarketTests
        f=MarketTests(); f.setUp(); self.addCleanup(f.doCleanups)
        settings=dict(swap_root=str(f.f.root), btc_cli=['/btc'], xbt_cli=['/xbt'],
                      node_ids=['/btc','/xbt'],receiver_id='receiver',receive_config=f.config)
        api=receiving.ReceiveQuotes(settings,auto_process=True)
        body=dict(request_id='12'*16,xbt_invoice='lnxbt-market',max_btc_sats=2000)
        with patch('market_policy.fetch',side_effect=[f.ticker,f.book]), patch('swap_service.RPC.call',side_effect=f.rpc):
            offer=api.quote(body)
        self.assertEqual(offer['btc_sats'],1591)
        directory=f.f.root/('receive-api-'+body['request_id'])
        q=private_load(directory/'quote.json')
        f.incoming['htlcs']=[dict(id=7,direction='in',payment_hash=q['terms']['payment_hash'],
            state='RCVD_ADD_ACK_REVOCATION',amount_msat=q['terms']['btc_amount_msat'])]
        f.pending=True
        with patch('swap_controller.RPC.call',side_effect=f.rpc), patch('market_policy.fetch',side_effect=AssertionError('reprice')):
            with self.assertRaises(subprocess.TimeoutExpired): receiving.process(directory,settings,rpc=f.rpc)
            self.assertEqual(receiving.process(directory,settings,rpc=f.rpc)['outcome'],'pending')
            f.complete()
            self.assertEqual(receiving.process(directory,settings,rpc=f.rpc)['phase'],'btc_released')
            self.assertEqual(receiving.process(directory,settings,rpc=f.rpc)['phase'],'btc_released')
        self.assertEqual(f.sends,1)

    def test_setup_is_explicit_idempotent_and_preserves_other_settings(self):
        from receive_setup import setup
        from test_market_quotes import MarketTests
        f=MarketTests(); f.setUp(); self.addCleanup(f.doCleanups)
        settings=dict(btc_cli=['/btc'],xbt_cli=['/xbt'],node_ids=['/btc','/xbt'],
                      receiver_id='receiver',unrelated={'keep':True})
        sp=f.f.root/'settings.json'; cp=f.f.root/'config.json'
        save(sp,settings);save(cp,f.config)
        with patch('swap_service.RPC.call',side_effect=f.rpc):
            self.assertTrue(setup(sp,cp)['receiving_enabled'])
            first=sp.read_bytes(); setup(sp,cp);self.assertEqual(sp.read_bytes(),first)
            changed=copy.deepcopy(f.config);changed['market']['max_btc_sats']=2999;save(cp,changed)
            with self.assertRaises(ValueError): setup(sp,cp)
        self.assertEqual(private_load(sp),dict(settings,receive_config=f.config))


class TransportTests(unittest.TestCase):
    def test_receive_endpoint_uses_existing_auth_and_customer_binding(self):
        import http.client
        f = ApiTests(); f.setUp(); self.addCleanup(f.doCleanups)
        class Handler:
            def quote(self, request): return {'received':request}
        f.api.receive = Handler()
        server = f.start_server()
        for token, expected in (('wrong',401), (f.credential['token'],200)):
            c = http.client.HTTPConnection('127.0.0.1',server.server_port)
            c.request('POST','/v1/receive',json.dumps({'test':1}),headers={
                'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            r = c.getresponse(); self.assertEqual(r.status,expected); r.read(); c.close()


if __name__ == '__main__': unittest.main()
