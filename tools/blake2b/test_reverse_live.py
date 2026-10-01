"""Dormant live-profile integration with fake RPCs; never contacts live nodes."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import reverse_controller as controller
import reverse_live as live
import reverse_service as service
from reverse_gate import Gate
from reverse_timing import proposal
from swap_controller import save
from swap_invoice import unsigned_invoice

A, B, C, D = ['02'+f'{i:064x}' for i in range(1, 5)]


class LiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root/'reverse-state.json'
        self.preimage = 'ab'*32
        self.hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.route = [dict(id=D, channel='2x1x0', amount_msat=1500000, delay=40)]
        self.policy = dict(source=B, destination=D, max_fee_msat=30000,
                           max_delay=576, max_hops=8, final_cltv=40)
        timing = proposal(40)
        self.terms = dict(profile=live.PROFILE, payment_hash=self.hash,
            payment_secret='22'*32, xbt_amount_msat=350000000, btc_amount_msat=1500000,
            btc_invoice='lnbc-private', xbt_channel='1x1x0', expires_at=int(time.time())+300,
            min_cltv_delta=timing['minimum_xbt_remaining_blocks'], max_cltv_delta=2016,
            node_ids=[A, B], payer_id=C, route=self.route, routing=self.policy,
            timing=timing, allow_signed_private_final=True)
        self.state = dict(profile=live.PROFILE, phase='prepared', xbt_cli=['/xbt'], btc_cli=['/btc'],
            node_ids=[A, B], payment_hash=self.hash, xbt_binding=['1x1x0', 8], xbt_expiry=700,
            xbt_amount_msat=350000000, btc_amount_msat=1500000, btc_invoice='lnbc-private',
            btc_secret='33'*32, route=self.route, routing=self.policy,
            durable_gate=True, xbt_onchain_claim=True, xbt_deadline_guard=True,
            reverse_quote=self.terms, btc_payment_metadata='aAbB')
        self.decoded = dict(valid=True, type='bolt11 invoice', currency='bc',
            payment_hash=self.hash, payment_secret='33'*32, payee=D, amount_msat=1500000,
            created_at=int(time.time()), expiry=3600, min_final_cltv_expiry=18,
            payment_metadata='aAbB', features='024100')
        self.xbt_height = self.btc_height = 100
        self.gate_phase = 'held'
        self.calls = []
        self.payments = []
        self.fail_send = False
        self.fail_release = False
        self.channel_state = 'CHANNELD_NORMAL'
        self.config = dict(btc_cli=['/btc'], xbt_cli=['/xbt'], node_ids=[A, B], payer_id=C)
        self.quote = dict(config=self.config, terms=self.terms, btc_secret='33'*32,
                          btc_payment_metadata='aAbB', xbt_invoice='lnxbt-private')
        self.settings = dict(btc_cli=['/btc'], xbt_cli=['/xbt'], receiver_cli=['/payer'],
                             node_ids=[B, A], receiver_id=C, swap_root=str(self.root))
        save(self.path, self.state)
        save(self.root/'reverse-quote.json', self.quote)

    def channel(self, xbt):
        return dict(short_channel_id='1x1x0' if xbt else '2x1x0',
            channel_id='incoming' if xbt else 'outgoing', funding_txid='aa' if xbt else 'bb',
            funding_outnum=0, peer_id=C if xbt else D,
            state=self.channel_state if xbt else 'CHANNELD_NORMAL', peer_connected=True,
            spendable_msat=400000000, receivable_msat=400000000,
            feerate={'perkw': 1250}, dust_limit_msat=546000,
            htlcs=[dict(id=8, direction='in', payment_hash=self.hash,
                amount_msat=350000000, expiry=700, state='RCVD_ADD_ACK_REVOCATION')]
                if xbt and self.gate_phase == 'held' else
                [dict(id=9, direction='out', payment_hash=self.hash,
                      amount_msat=1500000, expiry=140, state='SENT_ADD_ACK_REVOCATION')]
                if not xbt and self.payments and self.payments[0]['status'] == 'pending' else [])

    def rpc(self, cli, method, *args):
        self.calls.append((method, args))
        xbt = cli[0] == '/xbt'
        if method == 'getinfo':
            return dict(id=A if xbt else C if cli[0] == '/payer' else B,
                network='xbt' if xbt or cli[0] == '/payer' else 'bitcoin',
                blockheight=self.xbt_height if xbt else self.btc_height)
        if method == 'listpeerchannels':
            return {'channels': [self.channel(xbt)]}
        if method == 'xbt-held':
            return {'held': [dict(short_channel_id='1x1x0', id=8, payment_hash=self.hash,
                amount_msat=350000000, cltv_expiry=700)] if self.gate_phase == 'held' else []}
        if method == 'reverse-status':
            return dict(payment_hash=self.hash, terms=self.terms, binding=['1x1x0', 8],
                        cltv_expiry=700, phase=self.gate_phase, hook_ready=self.gate_phase == 'held')
        if method == 'decode':
            return self.decoded
        if method == 'listfunds':
            return {'outputs': [dict(status='confirmed', reserved=False, amount_msat=50000000)]}
        if method == 'listsendpays':
            return {'payments': self.payments}
        if method == 'sendpay':
            disk = json.loads(self.path.read_text())
            self.assertEqual(disk['phase'], 'outgoing_started')
            self.assertEqual(disk['btc_payment_metadata'], 'aAbB')
            self.assertIn('payment_metadata=aAbB', args)
            self.assertIn('incoming_channel', disk)
            self.assertIn('outgoing_channel', disk)
            self.payments = [dict(payment_hash=self.hash, amount_msat=1500000,
                amount_sent_msat=1500000, destination=D, bolt11='lnbc-private', status='pending')]
            if self.fail_send:
                raise subprocess.TimeoutExpired('PRIVATE', 20)
            return {}
        if method in ('reverse-release', 'reverse-fail'):
            self.assertEqual(args[:2], (self.hash, json.dumps(['1x1x0', 8])))
            self.gate_phase = 'resolved' if method == 'reverse-release' else 'failed'
            if self.fail_release:
                raise subprocess.TimeoutExpired('PRIVATE', 20)
            return {'released' if method == 'reverse-release' else 'failed': 1}
        if method == 'close':
            self.assertEqual(args, ('incoming', 1))
            self.channel_state = 'AWAITING_UNILATERAL'
            return {'type': 'unilateral'}
        raise AssertionError(method)

    def run_controller(self, **kwargs):
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True), patch.object(controller.RPC, 'call', side_effect=self.rpc):
            return controller.run(self.path, **kwargs)

    def test_background_recovery_uses_existing_attempt_without_starting_prepared(self):
        def controller_call(path, **kwargs):
            self.assertTrue(kwargs['recover_only'])
            return self.run_controller(**kwargs)
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            result = service.recover_record(self.root, self.settings,
                                            rpc=self.rpc, controller=controller_call)
            self.assertEqual(result['outcome'], 'needs_manual_start')
            self.assertFalse(self.payments)
            self.run_controller()
            self.payments[0].update(status='complete', payment_preimage=self.preimage)
            result = service.recover_record(self.root, self.settings,
                                            rpc=self.rpc, controller=controller_call)
        self.assertEqual(result['phase'], 'xbt_released')
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.calls), 1)

    def test_wrong_recovery_operator_binding_is_rejected(self):
        settings = dict(self.settings, btc_cli=['/wrong'])
        with self.assertRaises(ValueError):
            service.recover_record(self.root, settings, rpc=self.rpc)
        self.assertEqual(self.calls, [])

    def test_quote_outside_monitored_root_refused_before_rpc(self):
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            with self.assertRaises(ValueError):
                service.create(self.settings, 'PRIVATE', self.root/'nested'/'swap', rpc=self.rpc)
        self.assertEqual(self.calls, [])

    def test_live_term_substitution_refused(self):
        for field, value in (('btc_amount_msat', 1500001), ('xbt_amount_msat', 500001000),
                             ('allow_signed_private_final', False), ('max_cltv_delta', 2017),
                             ('min_cltv_delta', 1)):
            terms = dict(self.terms, **{field: value})
            with self.assertRaises(ValueError):
                live.validate_terms(terms)

    def test_quote_is_saved_before_register_and_invoice_verified_before_publication(self):
        self.gate_phase = 'quoted'
        directory = self.root/'new'
        registered = {}
        def rpc(cli, method, *args):
            if method == 'getroutes':
                return {'routes': [dict(amount_msat=1500000, final_cltv=40, path=[
                    dict(short_channel_id_dir='2x1x0/0', node_id_in=B, node_id_out=D,
                         amount_in_msat=1500000, amount_out_msat=1500000,
                         cltv_in=40, cltv_out=40)])]}
            if method == 'reverse-register':
                self.assertTrue((directory/'reverse-quote.json').exists())
                registered.update(json.loads(args[0]))
                return {'registered': True}
            if method == 'signinvoice':
                self.assertTrue(registered)
                self.assertTrue(args[0].startswith('lnxbt'))
                return {'bolt11': 'lnxbt-signed'}
            if method == 'decode' and cli[0] == '/xbt':
                return dict(valid=True, currency='xbt', payment_hash=registered['payment_hash'],
                    payment_secret=registered['payment_secret'], amount_msat=registered['xbt_amount_msat'],
                    payee=A, min_final_cltv_expiry=registered['timing']['proposed_xbt_invoice_cltv'])
            return self.rpc(cli, method, *args)
        summary = dict(route_found=True, btc_sats=1500, reasons=[],
                       btc_outgoing_cltv=40, routing_fee_msat=0,
                       ticker_computed_at_ms=int(time.time()*1000), estimated_xbt_sats=350000)
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            result = service.create(self.settings, 'lnbc-private', directory,
                                    rpc=rpc, inspector=lambda *a, **k: summary)
        self.assertEqual(result['xbt_invoice'], 'lnxbt-signed')
        self.assertFalse((directory/'reverse-state.json').exists())
        self.assertFalse(self.payments)

    def test_abort_unspent_reconciles_original_hook(self):
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True), patch.object(controller.RPC, 'call', side_effect=self.rpc):
            result = service.abort_unspent(self.root, rpc=self.rpc)
        self.assertEqual(result['phase'], 'xbt_failed')
        self.assertEqual(self.gate_phase, 'failed')
        self.assertFalse(self.payments)

    def test_abort_after_submission_refused(self):
        self.run_controller()
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            with self.assertRaises(ValueError):
                service.abort_unspent(self.root, rpc=self.rpc)
        self.assertEqual(self.gate_phase, 'held')

    def test_unspent_abort_lost_reply_reconciles_without_btc_attempt(self):
        self.fail_release = True
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True), patch.object(controller.RPC, 'call', side_effect=self.rpc):
            with self.assertRaises(subprocess.TimeoutExpired):
                service.abort_unspent(self.root, rpc=self.rpc)
        self.assertEqual(self.run_controller(recover_only=True)['phase'], 'xbt_failed')
        self.assertFalse(self.payments)

    def test_signed_private_final_exception_is_exact_and_not_general(self):
        from reverse_policy import inspect_remote_policies
        route = [dict(id=C, channel='2x1x0', amount_msat=1505000, delay=46),
                 dict(id=D, channel='3x1x0', amount_msat=1500000, delay=40)]
        decoded = dict(self.decoded, routes=[[dict(pubkey=C, short_channel_id='3x1x0',
            fee_base_msat=5000, fee_proportional_millionths=0, cltv_expiry_delta=6)]])
        lookup = lambda _: {'channels': []}
        audit = inspect_remote_policies(route, lookup)
        self.assertTrue(live.private_final_allowed(route, decoded, self.policy, audit, lookup))
        changed = copy.deepcopy(decoded)
        changed['routes'][0][0]['pubkey'] = A
        self.assertFalse(live.private_final_allowed(route, changed, self.policy, audit, lookup))
        changed = copy.deepcopy(decoded)
        changed['routes'][0][0]['short_channel_id'] = '9x1x0'
        self.assertFalse(live.private_final_allowed(route, changed, self.policy, audit, lookup))
        self.assertFalse(live.private_final_allowed(route, decoded, self.policy,
            dict(audit, remote_btc_policy_hops_unknown=2), lookup))
        self.assertFalse(live.private_final_allowed(route, decoded, self.policy,
            dict(audit, remote_btc_policy_violations=['disabled']), lookup))

    def test_activation_blocks_before_any_rpc_or_mutation(self):
        with patch.object(controller.RPC, 'call', side_effect=self.rpc):
            with self.assertRaises(RuntimeError):
                controller.run(self.path)
        self.assertEqual(self.calls, [])
        with self.assertRaises(RuntimeError):
            service.create(self.settings, 'PRIVATE', self.root/'new', rpc=self.rpc)
        self.assertFalse((self.root/'new').exists())

    def test_metadata_pending_completion_and_terminal_repeat(self):
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.payments[0].update(status='complete', payment_preimage=self.preimage)
        self.assertEqual(self.run_controller()['phase'], 'xbt_released')
        self.assertEqual(self.run_controller()['phase'], 'xbt_released')
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.calls), 1)
        self.assertEqual(sum(m == 'reverse-release' for m, _ in self.calls), 1)

    def test_definite_failure_returns_original_incoming_once(self):
        self.run_controller()
        self.payments[0]['status'] = 'failed'
        self.assertEqual(self.run_controller()['phase'], 'xbt_failed')
        self.assertEqual(self.run_controller()['phase'], 'xbt_failed')
        self.assertEqual(sum(m == 'reverse-fail' for m, _ in self.calls), 1)

    def test_lost_submission_reply_never_resends(self):
        self.fail_send = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.calls), 1)

    def test_lost_release_reply_reconciles_durable_gate(self):
        self.run_controller()
        self.payments[0].update(status='complete', payment_preimage=self.preimage)
        self.fail_release = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        self.assertEqual(self.run_controller()['phase'], 'xbt_released')
        self.assertEqual(sum(m == 'reverse-release' for m, _ in self.calls), 1)

    def test_stale_margin_never_spends(self):
        self.xbt_height = 511  # 189 remaining, below 40+6+144.
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertFalse(self.payments)

    def test_expired_quote_never_spends(self):
        self.terms['expires_at'] = 1
        save(self.path, self.state)
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertFalse(self.payments)

    def test_unknown_outcome_never_fails_or_resends(self):
        self.run_controller()
        self.payments = []
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(self.gate_phase, 'held')
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.calls), 1)

    def test_margin_breach_closes_only_pinned_incoming_channel_once(self):
        self.run_controller()
        self.xbt_height = 517  # 183 remaining versus 40+144 required.
        self.run_controller()
        self.assertEqual(self.gate_phase, 'held')
        self.run_controller()
        self.assertEqual(sum(m == 'close' for m, _ in self.calls), 1)
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.calls), 1)

    def test_recovery_only_cannot_start_prepared_payment(self):
        self.assertEqual(self.run_controller(recover_only=True)['outcome'], 'needs_manual_start')
        self.assertFalse(self.payments)

    def test_service_recovery_without_state_never_creates_or_sends(self):
        self.path.unlink()
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            result = service.step(self.root, recover_only=True, rpc=self.rpc)
        self.assertEqual(result['outcome'], 'needs_manual_start')
        self.assertFalse(self.path.exists())
        self.assertFalse(self.payments)

    def test_service_state_binding_cannot_be_substituted(self):
        self.state['reverse_quote'] = dict(self.terms, btc_invoice='lnbc-other')
        save(self.path, self.state)
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            with self.assertRaises(ValueError):
                service.step(self.root, rpc=self.rpc)
        self.assertFalse(self.payments)

    def test_gate_and_invoice_activation_default_disabled(self):
        gate = Gate(self.root/'gate.json', live=True)
        response = gate.handle(dict(id=1, method='init', params={'configuration': {'network': 'xbt'}}))
        self.assertIn('disable', response[0]['result'])
        with self.assertRaises(RuntimeError):
            unsigned_invoice(self.hash, '22'*32, currency='xbt', live_reverse=True)

    def test_live_gate_leaves_unrelated_invoices_to_cln(self):
        gate = Gate(self.root/'gate.json', live=True)
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            gate.handle(dict(id=1, method='init', params={'configuration': {'network': 'xbt'}}))
            response = gate.handle(dict(id=2, method='htlc_accepted', params=dict(
                htlc={'payment_hash': '00'*32}, onion={})))
        self.assertEqual(response[0]['result'], {'result': 'continue'})
        self.assertFalse((self.root/'gate.json').exists())

    def test_live_gate_accepts_bound_quote_and_replays_after_expiry(self):
        path = self.root/'gate.json'
        init = dict(id=1, method='init', params={'configuration': {'network': 'xbt'}})
        hook = dict(id=3, method='htlc_accepted', params=dict(
            htlc=dict(payment_hash=self.hash, amount_msat=350000000, short_channel_id='1x1x0',
                      id=8, cltv_expiry=700, cltv_expiry_relative=600),
            onion=dict(payment_secret='22'*32, forward_msat=350000000, total_msat=350000000,
                       type='tlv', outgoing_cltv_value=700)))
        with patch.object(live, 'LIVE_EXECUTION_ENABLED', True):
            gate = Gate(path, live=True)
            gate.handle(init)
            gate.handle(dict(id=2, method='reverse-register', params=[self.terms]))
            self.assertEqual(gate.handle(hook), [])
            before = path.read_bytes()
            restarted = Gate(path, live=True)
            restarted.handle(init)
            with patch('reverse_gate.time.time', return_value=self.terms['expires_at']+1):
                self.assertEqual(restarted.handle(hook), [])
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
