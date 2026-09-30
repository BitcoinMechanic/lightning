"""Offline live-profile boundaries; no mainnet RPCs or money movements."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import live_pilot as pilot
from deadline_guard import protect
from swap_controller import check_spend, run, save
from swap_invoice import unsigned_invoice
from swap_service import create, publish, renew
import test_deadline_guard


class PilotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = {'profile': pilot.PROFILE, 'btc_cli': ['/btc'], 'xbt_cli': ['/xbt']}
        self.decoded = dict(valid=True, type='bolt11 invoice', currency='xbt',
                            amount_msat=pilot.XBT_MSAT, payment_hash='11'*32,
                            payment_secret='22'*32, payee='receiver',
                            created_at=int(time.time()), expiry=3600, min_final_cltv_expiry=18)
        self.channel = dict(peer_id='receiver', state='CHANNELD_NORMAL',
                            short_channel_id='10x1x0', spendable_msat=100000000,
                            peer_connected=True, feerate={'perkw': 253}, dust_limit_msat=546000)
        self.calls = []
        self.directory = self.root / 'swap'

    def rpc(self, cli, method, *args):
        self.calls.append(method)
        if method == 'getinfo':
            return dict(id=cli[0], network='bitcoin' if cli == ['/btc'] else 'xbt', blockheight=100)
        if method == 'listfunds':
            return {'outputs': [dict(amount_msat=50000000, status='confirmed', reserved=False)]}
        if method == 'listpeerchannels':
            return {'channels': [self.channel]}
        if method == 'listsendpays':
            return {'payments': []}
        if method == 'decode' and cli == ['/xbt']:
            return self.decoded
        if method == 'xbt-register':
            self.assertEqual(json.loads(args[0])['pilot'], pilot.PROFILE)
            self.assertTrue((self.directory / 'quote.json').exists())
            return {'registered': True}
        if method == 'signinvoice':
            self.assertTrue(args[0].startswith('lnbc10000000p1'))
            return {'bolt11': 'signed'}
        if method == 'decode':
            terms = json.loads((self.directory / 'quote.json').read_text())['terms']
            return dict(valid=True, currency='bc', payee='/btc', min_final_cltv_expiry=300,
                        payment_hash=terms['payment_hash'], payment_secret=terms['payment_secret'],
                        amount_msat=pilot.BTC_MSAT)
        raise AssertionError(method)

    def test_live_quote_pins_identity_limits_and_invoice_network(self):
        with patch('swap_service.Lab.rpc', side_effect=self.rpc):
            create(self.config, 'lnxbt-fixture', 1000, self.directory)
            self.assertEqual(publish(self.directory)['btc_sats'], 1000)
        data = json.loads((self.directory / 'quote.json').read_text())
        self.assertEqual(data['controller']['node_ids'], ['/btc', '/xbt'])
        self.assertEqual(data['terms']['min_cltv_delta'], 288)
        self.assertTrue(data['controller']['btc_deadline_guard'])
        self.assertEqual(data['controller']['btc_amount_msat'], pilot.BTC_MSAT)

    def test_oversize_btc_refused_before_rpc(self):
        with patch('swap_service.Lab.rpc') as rpc, self.assertRaises(ValueError):
            create(self.config, 'lnxbt-fixture', 1001, self.directory)
        rpc.assert_not_called()

    def test_wrong_xbt_amount_or_currency_refused(self):
        for key, value in [('amount_msat', 2000001), ('currency', 'xbtrt'), ('currency', 'bc')]:
            original = self.decoded[key]
            self.decoded[key] = value
            with patch('swap_service.Lab.rpc', side_effect=self.rpc), self.assertRaises(ValueError):
                create(self.config, 'lnxbt-fixture', 1000, self.directory)
            self.assertFalse(self.directory.exists())
            self.decoded[key] = original

    def test_live_nodes_require_opt_in(self):
        config = dict(self.config)
        del config['profile']
        with patch('swap_service.Lab.rpc', side_effect=self.rpc), self.assertRaises(ValueError):
            create(config, 'lnxbt-fixture', 1000, self.directory)
        with self.assertRaises(ValueError):
            pilot.is_live({'profile': 'typo'})

    def test_reserves_and_dust(self):
        pilot.require_untrimmed(self.channel, pilot.BTC_MSAT)
        high = dict(self.channel, feerate={'perkw': 1000})
        with self.assertRaises(RuntimeError):
            pilot.require_untrimmed(high, pilot.BTC_MSAT)
        def poor(cli, method, *args):
            if method == 'listfunds':
                return {'outputs': []}
            return self.rpc(cli, method, *args)
        with self.assertRaises(RuntimeError):
            pilot.require_reserves(self.config, poor)

    def test_recovery_rejects_node_substitution_before_payment_calls(self):
        state = dict(self.config, node_ids=['wrong', '/xbt'], phase='outgoing_started',
                     payment_hash='11'*32, quote_gate=True, btc_deadline_guard=True,
                     btc_amount_msat=pilot.BTC_MSAT, xbt_amount_msat=pilot.XBT_MSAT)
        path = self.root / 'state.json'
        save(path, state)
        before = path.read_bytes()
        with patch('swap_controller.Lab.rpc', side_effect=self.rpc), self.assertRaises(RuntimeError):
            run(path)
        self.assertEqual(self.calls, ['getinfo', 'getinfo'])
        self.assertEqual(path.read_bytes(), before)

    def test_untrimmed_committed_binding_required(self):
        state = dict(self.config, btc_binding=['20x1x0', 7], payment_hash='11'*32,
                     route=[dict(id='receiver', channel='10x1x0', delay=40)])
        htlc = dict(id=7, direction='in', payment_hash='11'*32,
                    state='RCVD_ADD_ACK_REVOCATION', amount_msat=pilot.BTC_MSAT)
        incoming = dict(self.channel, short_channel_id='20x1x0', htlcs=[htlc])
        def rpc(cli, method):
            return {'channels': [incoming if cli == ['/btc'] else self.channel]}
        pilot.check_channels(state, rpc)
        for override in ({'local_trimmed': True}, {'state': 'SENT_ADD_HTLC'}, {'amount_msat': 1}):
            incoming['htlcs'] = [dict(htlc, **override)]
            with self.assertRaises(RuntimeError):
                pilot.check_channels(state, rpc)

    def test_live_deadline_boundary(self):
        fixture = test_deadline_guard.DeadlineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.state.update(profile=pilot.PROFILE, node_ids=['btc', 'xbt'],
                             btc_amount_msat=pilot.BTC_MSAT, xbt_amount_msat=pilot.XBT_MSAT)
        height = 127
        def rpc(cli, method, *args):
            if method == 'getinfo':
                return {'network': 'bitcoin' if cli == ['btc'] else 'xbt',
                        'id': cli[0], 'blockheight': height}
            return fixture.rpc(cli, method, *args)
        protect(fixture.path, fixture.state, rpc, save)
        self.assertFalse(any(c[1] == 'close' for c in fixture.calls))
        height = 128  # Incoming expiry 200: 72 blocks remain.
        protect(fixture.path, fixture.state, rpc, save)
        protect(fixture.path, fixture.state, rpc, save)
        self.assertEqual(sum(c[1] == 'close' for c in fixture.calls), 1)

    def test_live_controller_one_send_and_repeat_recovery(self):
        profile = getattr(self, 'controller_profile', pilot.PROFILE)
        btc_amount, xbt_amount = pilot.amounts({'profile': profile})
        self.decoded['amount_msat'] = xbt_amount
        preimage = 'ab' * 32
        payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        self.decoded['payment_hash'] = payment_hash
        state = dict(self.config, profile=profile, btc_channel='20x1x0', node_ids=['/btc', '/xbt'], phase='prepared',
                     quote_gate=True, btc_deadline_guard=True, payment_hash=payment_hash,
                     btc_binding=['20x1x0', 7], btc_amount_msat=btc_amount,
                     xbt_amount_msat=xbt_amount, xbt_invoice='lnxbt-fixture',
                     payment_secret=self.decoded['payment_secret'],
                     route=[dict(id='receiver', channel='10x1x0', delay=40,
                                 amount_msat=xbt_amount)])
        incoming = dict(self.channel, short_channel_id='20x1x0', htlcs=[dict(
            id=7, direction='in', payment_hash=payment_hash, amount_msat=btc_amount,
            state='RCVD_ADD_ACK_REVOCATION')])
        spend = dict(payment_hash=payment_hash, binding=state['btc_binding'],
                     xbt_amount_msat=xbt_amount, btc_amount_msat=btc_amount,
                     pilot=profile, btc_channel='20x1x0', min_cltv_delta=288, max_cltv_delta=2016,
                     cltv_expiry=400, expires_at=int(time.time())+600, xbt_invoice='lnxbt-fixture')
        phase = 'held'
        sent = []
        def rpc(cli, method, *args):
            nonlocal phase
            if method == 'xbt-spend-info':
                return spend
            if method == 'listpeerchannels':
                return {'channels': [incoming if cli == ['/btc'] else self.channel]}
            if method == 'sendpay':
                sent.append(args)
                return {}
            if method == 'waitsendpay':
                return {'status': 'complete'}
            if method == 'listsendpays':
                return {'payments': [dict(payment_hash=payment_hash, status='complete',
                                          amount_msat=xbt_amount, payment_preimage=preimage)]}
            if method == 'xbt-quote-status':
                return dict(payment_hash=payment_hash, binding=state['btc_binding'], phase=phase)
            if method == 'xbt-release':
                self.assertEqual(args, (preimage,))
                phase = 'resolved'
                return {'released': 1}
            return self.rpc(cli, method, *args)
        path = self.root / 'controller.json'
        save(path, state)
        with patch('swap_controller.Lab.rpc', side_effect=rpc):
            self.assertEqual(run(path)['phase'], 'btc_released')
            self.assertEqual(run(path)['phase'], 'btc_released')
        self.assertEqual(len(sent), 1)

    def test_v2_controller_one_send_and_repeat_recovery(self):
        self.controller_profile = pilot.PROFILE_V2
        self.test_live_controller_one_send_and_repeat_recovery()

    def test_gate_requires_opt_in_and_passes_ordinary_invoices(self):
        plugin = self.root / 'gate.py'
        plugin.write_text(Path(__file__).with_name('quote_plugin.py').read_text())
        def execute(options, extra):
            requests = [dict(id=1, method='init', params={
                'configuration': {'network': 'bitcoin'}, 'options': options}), *extra]
            result = subprocess.run([sys.executable, str(plugin)], text=True, capture_output=True,
                                    input='\n\n'.join(map(json.dumps, requests))+'\n\n', timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return {r['id']: r for line in result.stdout.splitlines() if line.strip()
                    for r in [json.loads(line)] if 'id' in r}
        self.assertIn('disable', execute({}, [])[1]['result'])
        terms = dict(payment_hash='11'*32, payment_secret='22'*32,
                     btc_amount_msat=pilot.BTC_MSAT, xbt_amount_msat=pilot.XBT_MSAT,
                     xbt_invoice='lnxbt-fixture', expires_at=int(time.time())+600,
                     min_cltv_delta=288, max_cltv_delta=2016, pilot=pilot.PROFILE)
        def register(i, quote):
            return dict(id=i, method='xbt-register', params=[quote])
        options = {'xbt-live-pilot': pilot.PROFILE}
        replies = execute(options, [register(2, dict(terms, btc_amount_msat=1000001)),
                                    register(3, terms), register(4, terms),
                                    register(5, dict(terms, payment_hash='33'*32)),
                                    dict(id=6, method='htlc_accepted', params={
                                        'htlc': {'payment_hash': '44'*32}, 'onion': {}})])
        self.assertIn('error', replies[2])
        self.assertEqual(replies[3]['result'], {'registered': True})
        self.assertEqual(replies[4]['result'], {'registered': True})
        self.assertIn('error', replies[5])
        self.assertEqual(replies[6]['result'], {'result': 'continue'})
        replies = execute(options, [register(7, dict(terms, payment_hash='55'*32))])
        self.assertIn('error', replies[7])  # One-quote restriction survives restart.


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.base = PilotTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.oldpath = self.base.root / 'old' / 'state.json'
        self.oldpath.parent.mkdir()
        self.old = dict(self.base.config, phase='btc_failed', pre_spend_aborted=True,
                        quote_gate=True, btc_deadline_guard=True, node_ids=['/btc', '/xbt'],
                        payment_hash='aa'*32, btc_binding=['20x1x0', 7],
                        btc_amount_msat=1000000, xbt_amount_msat=2000000)
        save(self.oldpath, self.old)
        self.config = dict(self.base.config, profile=pilot.PROFILE_V2, previous_state=str(self.oldpath))
        self.base.decoded['amount_msat'] = 4000000
        self.incoming = dict(self.base.channel, short_channel_id='20x1x0',
                             receivable_msat=50000000, htlcs=[], feerate={'perkw': 829})
        self.status = dict(payment_hash='aa'*32, binding=['20x1x0', 7], phase='failed')
        self.attempts = []
        self.registered = []

    def rpc(self, cli, method, *args):
        if method == 'xbt-quote-status':
            return self.status
        if method == 'listsendpays':
            return {'payments': self.attempts}
        if method == 'listpeerchannels' and cli == ['/btc']:
            return {'channels': [self.incoming]}
        if method == 'xbt-register':
            self.registered.append(json.loads(args[0]))
            return {'registered': True}
        if method == 'signinvoice':
            self.assertTrue(args[0].startswith('lnbc20000000p1'))
            return {'bolt11': 'signed-v2'}
        if method == 'decode' and cli == ['/btc']:
            terms = self.registered[-1]
            return dict(valid=True, currency='bc', payee='/btc', min_final_cltv_expiry=300,
                        amount_msat=2000000, payment_hash=terms['payment_hash'],
                        payment_secret=terms['payment_secret'])
        return self.base.rpc(cli, method, *args)

    def test_replacement_preserves_old_state_and_binds_channel(self):
        before = self.oldpath.read_bytes()
        with patch('swap_service.Lab.rpc', side_effect=self.rpc):
            create(self.config, 'lnxbt-v2', 2000, self.base.directory)
            result = publish(self.base.directory)
        self.assertEqual(result['btc_sats'], 2000)
        self.assertEqual(result['xbt_msat'], 4000000)
        self.assertEqual(self.registered[0]['replaces'], 'aa'*32)
        self.assertEqual(self.registered[0]['btc_channel'], '20x1x0')
        self.assertEqual(self.oldpath.read_bytes(), before)
        data = json.loads((self.base.directory/'quote.json').read_text())
        self.assertEqual(data['controller']['btc_channel'], '20x1x0')

    def test_btc_fee_preflight_before_quote_creation(self):
        self.incoming['feerate']['perkw'] = 3000
        with patch('swap_service.Lab.rpc', side_effect=self.rpc), self.assertRaises(RuntimeError):
            create(self.config, 'lnxbt-v2', 2000, self.base.directory)
        self.assertFalse(self.base.directory.exists())
        self.assertEqual(self.registered, [])

    def test_fee_change_before_publication_refused(self):
        with patch('swap_service.Lab.rpc', side_effect=self.rpc):
            create(self.config, 'lnxbt-v2', 2000, self.base.directory)
            self.incoming['feerate']['perkw'] = 3000
            with self.assertRaises(RuntimeError):
                publish(self.base.directory)
        self.assertEqual(self.registered, [])

    def test_pending_or_spent_predecessor_refused(self):
        self.attempts = [{'payment_hash': 'aa'*32, 'status': 'failed'}]
        with self.assertRaises(RuntimeError):
            pilot.replacement(self.config, self.rpc)
        self.attempts = []
        self.incoming['htlcs'] = [{'payment_hash': 'aa'*32}]
        with self.assertRaises(RuntimeError):
            pilot.replacement(self.config, self.rpc)
        self.incoming['htlcs'] = []
        self.status['phase'] = 'held'
        with self.assertRaises(RuntimeError):
            pilot.replacement(self.config, self.rpc)

    def test_state_must_prove_pre_spend_cancellation(self):
        for change in ({'phase': 'prepared'}, {'pre_spend_aborted': False},
                       {'preimage': 'bb'*32}, {'btc_amount_msat': 1},
                       {'node_ids': ['wrong', '/xbt']}):
            save(self.oldpath, dict(self.old, **change))
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                pilot.replacement(self.config, self.rpc)

    def test_gate_allows_exactly_one_replacement_and_retains_old(self):
        plugin = self.base.root / 'replacement_gate.py'
        plugin.write_text(Path(__file__).with_name('quote_plugin.py').read_text())
        path = plugin.with_suffix('.quotes.json')
        old = {'terms': {'pilot': pilot.PROFILE}, 'phase': 'failed', 'binding': ['20x1x0', 7]}
        path.write_text(json.dumps({'aa'*32: old}))
        terms = dict(payment_hash='11'*32, payment_secret='22'*32,
                     btc_amount_msat=2000000, xbt_amount_msat=4000000,
                     xbt_invoice='lnxbt-v2', expires_at=int(time.time())+600,
                     min_cltv_delta=288, max_cltv_delta=2016, pilot=pilot.PROFILE_V2,
                     replaces='aa'*32, btc_channel='20x1x0')
        requests = [dict(id=1, method='init', params={
            'configuration': {'network': 'bitcoin'},
            'options': {'xbt-live-pilot': pilot.PROFILE_V2}})]
        for ident, quote in enumerate((dict(terms, btc_channel='wrong'), terms, terms,
                                        dict(terms, payment_hash='33'*32)), 2):
            requests.append(dict(id=ident, method='xbt-register', params=[quote]))
        result = subprocess.run([sys.executable, str(plugin)], text=True, capture_output=True,
                                input='\n\n'.join(map(json.dumps, requests))+'\n\n', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {r['id']: r for line in result.stdout.splitlines() if line.strip()
                   for r in [json.loads(line)] if 'id' in r}
        self.assertIn('error', replies[2])
        self.assertEqual(replies[3]['result'], {'registered': True})
        self.assertEqual(replies[4]['result'], {'registered': True})
        self.assertIn('error', replies[5])
        stored = json.loads(path.read_text())
        self.assertEqual(stored['aa'*32], old)
        self.assertEqual(len(stored), 2)



class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.f = ReplacementTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.directory = self.f.base.directory
        with patch('swap_service.Lab.rpc', side_effect=self.f.rpc):
            create(self.f.config, 'lnxbt-v2', 2000, self.directory)
            publish(self.directory)
        self.path = self.directory / 'quote.json'
        self.original = json.loads(self.path.read_text())
        self.now = self.original['terms']['expires_at'] + 1
        self.gate = dict(payment_hash=self.original['terms']['payment_hash'], phase='quoted')
        self.renewals = []
        self.lose_reply = False

    def rpc(self, cli, method, *args):
        if method == 'xbt-quote-status' and args[0] == self.gate['payment_hash']:
            return self.gate
        if method == 'xbt-renew':
            journal = json.loads(self.path.read_text())['renewal']
            self.assertFalse(journal['complete'])
            self.assertEqual(json.loads(args[0]), self.original['terms'])
            self.renewals.append(args)
            if self.lose_reply:
                self.lose_reply = False
                raise TimeoutError()
            return {'renewed': True}
        return self.f.rpc(cli, method, *args)

    def attempt(self):
        with patch('swap_service.Lab.rpc', side_effect=self.rpc), patch('swap_service.time.time', return_value=self.now):
            return renew(self.directory)

    def test_preserves_terms_and_reprints_without_second_extension(self):
        result = self.attempt()
        data = json.loads(self.path.read_text())
        self.assertEqual(data['renewal']['old_terms'], self.original['terms'])
        self.assertEqual(data['renewal']['old_btc_invoice'], self.original['btc_invoice'])
        self.assertTrue(data['renewal']['complete'])
        self.assertEqual(data['terms'], dict(self.original['terms'], expires_at=self.now+600))
        self.assertEqual(self.attempt(), result)
        self.assertEqual(len(self.renewals), 1)
        self.assertFalse((self.directory/'state.json').exists())

    def test_lost_reply_retries_exact_journal(self):
        self.lose_reply = True
        with self.assertRaises(TimeoutError):
            self.attempt()
        with patch('swap_service.Lab.rpc', side_effect=self.rpc), self.assertRaises(RuntimeError):
            publish(self.directory)
        self.now += 10
        self.attempt()
        self.assertEqual(self.renewals[0], self.renewals[1])

    def test_used_quote_and_state_refused(self):
        before = self.path.read_bytes()
        self.gate['phase'] = 'held'
        with self.assertRaises(RuntimeError):
            self.attempt()
        self.gate['phase'] = 'quoted'
        self.f.attempts = [dict(payment_hash=self.gate['payment_hash'], status='failed')]
        with self.assertRaises(RuntimeError):
            self.attempt()
        self.f.attempts = []
        (self.directory/'state.json').write_text('{}')
        with self.assertRaises(RuntimeError):
            self.attempt()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.renewals, [])

    def test_expired_receiver_and_fee_change_refused(self):
        self.f.incoming['feerate']['perkw'] = 3000
        with self.assertRaises(RuntimeError):
            self.attempt()
        self.f.incoming['feerate']['perkw'] = 829
        self.f.base.decoded['expiry'] = 1
        with self.assertRaises(RuntimeError):
            self.attempt()
        self.assertEqual(self.renewals, [])

    def test_gate_durable_single_extension_and_binding_refusal(self):
        plugin = self.f.base.root / 'renew_gate.py'
        plugin.write_text(Path(__file__).with_name('quote_plugin.py').read_text())
        path = plugin.with_suffix('.quotes.json')
        terms = dict(self.original['terms'], expires_at=int(time.time())-1)
        key = terms['payment_hash']
        path.write_text(json.dumps({key: dict(terms=terms, phase='quoted')}))
        expiry = int(time.time())+500
        def execute(quote, end):
            requests = [dict(id=1, method='init', params={
                'configuration': {'network': 'bitcoin'},
                'options': {'xbt-live-pilot': pilot.PROFILE_V2}}),
                dict(id=2, method='xbt-renew', params=[quote, end])]
            result = subprocess.run([sys.executable, str(plugin)], text=True, capture_output=True,
                input='\n\n'.join(map(json.dumps, requests))+'\n\n', timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return next(json.loads(line) for line in result.stdout.splitlines()
                        if line.strip() and json.loads(line).get('id') == 2)
        self.assertIn('error', execute(dict(terms, btc_amount_msat=1), expiry))
        self.assertIn('error', execute(terms, expiry+1000))
        self.assertEqual(execute(terms, expiry)['result'], {'renewed': True})
        before = path.read_bytes()
        self.assertEqual(execute(terms, expiry)['result'], {'renewed': True})
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('error', execute(terms, expiry+1))
        stored = json.loads(path.read_text())
        stored[key]['binding'] = ['20x1x0', 7]
        path.write_text(json.dumps(stored))
        self.assertIn('error', execute(terms, expiry))


if __name__ == '__main__':
    unittest.main()
