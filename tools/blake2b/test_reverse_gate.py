"""Durable reverse quote admission, replay and terminal intent."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from reverse_gate import Gate
import reverse_controller as controller
from swap_controller import save
import test_reverse_controller


class GateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'quotes.json'
        self.preimage = 'ab'*32
        self.hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.quote = dict(payment_hash=self.hash, payment_secret='22'*32,
                          xbt_amount_msat=200000000, btc_amount_msat=100000000,
                          btc_invoice='lnbcrt-fixture', xbt_channel='1x1x0',
                          expires_at=2000, min_cltv_delta=100, max_cltv_delta=2000)
        self.hook = dict(htlc=dict(payment_hash=self.hash, amount_msat=200000000,
                                   short_channel_id='1x1x0', id=8, cltv_expiry=300, cltv_expiry_relative=200),
                         onion=dict(payment_secret='22'*32, forward_msat=200000000,
                                    total_msat=200000000, type='tlv', outgoing_cltv_value=290))
        self.gate = self.open()

    def call(self, method, params, request_id=10, now=1000):
        with patch('reverse_gate.time.time', return_value=now):
            return self.gate.handle(dict(id=request_id, method=method, params=copy.deepcopy(params)))

    def open(self):
        gate = Gate(self.path)
        gate.handle(dict(id=1, method='init', params={'configuration': {'network': 'xbt-regtest'}}))
        return gate

    def held(self):
        self.call('reverse-register', [self.quote])
        self.assertEqual(self.call('htlc_accepted', self.hook, 2), [])
        self.assertEqual(json.loads(self.path.read_text())[self.hash]['phase'], 'held')

    def test_live_and_wrong_networks_disabled(self):
        for network in ('xbt', 'bitcoin', 'regtest'):
            response = self.gate.handle(dict(id=1, method='init', params={'configuration': {'network': network}}))
            self.assertIn('disable', response[0]['result'])
            with self.assertRaises(ValueError):
                self.call('reverse-register', [self.quote])

    def test_wrong_secret_amount_channel_and_short_onion_refused(self):
        self.call('reverse-register', [self.quote])
        before = self.path.read_bytes()
        for section, key, value in [('onion', 'payment_secret', '33'*32),
                                     ('htlc', 'amount_msat', 1), ('htlc', 'short_channel_id', '2x1x0'),
                                     ('onion', 'outgoing_cltv_value', 199)]:
            hook = copy.deepcopy(self.hook)
            hook[section][key] = value
            self.assertEqual(self.call('htlc_accepted', hook)[0]['result']['result'], 'fail')
            self.assertEqual(self.path.read_bytes(), before)

    def test_expired_new_admission_refused(self):
        self.call('reverse-register', [self.quote])
        self.assertEqual(self.call('htlc_accepted', self.hook, now=2001)[0]['result']['result'], 'fail')

    def test_expired_accepted_replay_restores_hook_without_disk_change(self):
        self.held()
        before = self.path.read_bytes()
        self.gate = self.open()
        self.hook['htlc']['cltv_expiry_relative'] = 99
        self.assertEqual(self.call('htlc_accepted', self.hook, now=3000), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.call('reverse-status', [self.hash])[0]['result']['hook_ready'])

    def test_changed_accepted_replay_remains_unresolved_and_unavailable(self):
        self.held()
        before = self.path.read_bytes()
        self.gate = self.open()
        self.hook['onion']['payment_secret'] = '33'*32
        self.assertEqual(self.call('htlc_accepted', self.hook), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.call('reverse-status', [self.hash])[0]['result']['hook_ready'])
        with self.assertRaises(ValueError):
            self.call('reverse-release', [self.hash, ['1x1x0', 8], self.preimage])

    def test_other_binding_cannot_replace_accepted_htlc(self):
        self.held()
        before = self.path.read_bytes()
        self.hook['htlc']['id'] = 9
        self.assertEqual(self.call('htlc_accepted', self.hook)[0]['result']['result'], 'fail')
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.call('reverse-fail', [self.hash, ['1x1x0', 9]])

    def test_terms_are_immutable_and_active_quotes_serialized(self):
        self.held()
        before = self.path.read_bytes()
        self.call('reverse-register', [self.quote], now=3000)
        with self.assertRaises(ValueError):
            self.call('reverse-register', [dict(self.quote, btc_invoice='lnbcrt-other')])
        with self.assertRaises(ValueError):
            self.call('reverse-register', [dict(self.quote, payment_hash='44'*32)])
        self.assertEqual(self.path.read_bytes(), before)

    def test_release_persisted_before_response_and_replayed_after_restart(self):
        self.held()
        replies = self.call('reverse-release', [self.hash, ['1x1x0', 8], self.preimage])
        disk = json.loads(self.path.read_text())[self.hash]
        self.assertEqual((disk['phase'], disk['preimage']), ('resolved', self.preimage))
        self.assertEqual(replies[0]['result'], {'result': 'resolve', 'payment_key': self.preimage})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.gate = self.open()
        self.assertEqual(self.call('htlc_accepted', self.hook)[0]['result'], replies[0]['result'])
        self.assertEqual(self.call('reverse-release', [self.hash, ['1x1x0', 8], self.preimage])[0]['result'],
                         {'released': 1})
        with self.assertRaises(ValueError):
            self.call('reverse-fail', [self.hash, ['1x1x0', 8]])

    def test_failure_persisted_replayed_and_cannot_be_changed_to_release(self):
        self.held()
        replies = self.call('reverse-fail', [self.hash, ['1x1x0', 8]])
        self.assertEqual(json.loads(self.path.read_text())[self.hash]['phase'], 'failed')
        self.assertNotIn('preimage', json.loads(self.path.read_text())[self.hash])
        self.gate = self.open()
        self.assertEqual(self.call('htlc_accepted', self.hook)[0]['result'], replies[0]['result'])
        self.assertEqual(self.call('reverse-fail', [self.hash, ['1x1x0', 8]])[0]['result'], {'failed': 1})
        with self.assertRaises(ValueError):
            self.call('reverse-release', [self.hash, ['1x1x0', 8], self.preimage])

    def test_wrong_preimage_never_changes_held_checkpoint(self):
        self.held()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.call('reverse-release', [self.hash, ['1x1x0', 8], '00'*32])
        self.assertEqual(self.path.read_bytes(), before)


    def test_plugin_protocol_persists_and_replays_across_processes(self):
        root = self.path.parent
        for name in ('reverse_gate.py', 'quote_plugin.py'):
            (root/name).write_text(Path(__file__).with_name(name).read_text())
        quote = dict(self.quote, expires_at=int(time.time())+3600)
        init = dict(id=1, method='init', params={'configuration': {'network': 'xbt-regtest'}})
        hook = dict(id=3, method='htlc_accepted', params=self.hook)

        def execute(requests):
            result = subprocess.run([sys.executable, str(root/'reverse_gate.py')],
                                    input='\n\n'.join(map(json.dumps, requests))+'\n\n',
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return {item['id']: item for line in result.stdout.splitlines() if line.strip()
                    for item in [json.loads(line)]}

        first = execute([init, dict(id=2, method='reverse-register', params=[quote]), hook,
                         dict(id=4, method='reverse-release', params=[self.hash, ['1x1x0', 8], self.preimage])])
        self.assertEqual(first[2]['result'], {'registered': True})
        self.assertEqual(first[4]['result'], {'released': 1})
        second = execute([init, hook])
        self.assertEqual(second[3]['result'], first[3]['result'])
        self.assertEqual(second[3]['result']['result'], 'resolve')


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.f = test_reverse_controller.ReverseTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.quote = dict(payment_hash=self.f.payment_hash, payment_secret='22'*32,
                          xbt_amount_msat=200000000, btc_amount_msat=100000000,
                          btc_invoice='lnbcrt-fixture', xbt_channel='1x1x0',
                          expires_at=9999999999, min_cltv_delta=100, max_cltv_delta=2000)
        self.f.write(durable_gate=True, reverse_quote=self.quote)
        self.phase = 'held'
        self.binding = ['1x1x0', 8]
        self.calls = []

    def rpc(self, cli, method, *args):
        self.calls.append(method)
        if method == 'reverse-status':
            return dict(payment_hash=self.f.payment_hash, terms=self.quote, binding=self.binding,
                        phase=self.phase, cltv_expiry=300, hook_ready=self.f.held)
        if method in ('reverse-release', 'reverse-fail'):
            self.assertEqual(args[:2], (self.f.payment_hash, json.dumps(self.binding)))
            self.phase = 'resolved' if method == 'reverse-release' else 'failed'
            # Simulate gate persisting terminal intent and then losing its reply.
            self.f.held = False
            raise subprocess.TimeoutExpired('private', 20)
        return self.f.rpc(cli, method, *args)

    def run_controller(self):
        with patch('reverse_controller.RPC.call', side_effect=self.rpc):
            return controller.run(self.f.path)

    def test_lost_release_reply_reconciles_without_resend_or_second_release(self):
        self.run_controller()
        self.f.payments = [self.f.attempt('complete')]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        self.assertEqual(json.loads(self.f.path.read_text())['phase'], 'btc_paid')
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(self.calls.count('reverse-release'), 1)
        self.assertEqual(self.calls.count('sendpay'), 1)

    def test_lost_failure_reply_reconciles_without_second_failure(self):
        self.run_controller()
        self.f.payments = [self.f.attempt('failed')]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        self.assertEqual(self.run_controller(), {'phase': 'xbt_failed'})
        self.assertEqual(self.calls.count('reverse-fail'), 1)
        self.assertEqual(self.calls.count('sendpay'), 1)

    def test_terminal_gate_wrong_binding_never_checkpoints(self):
        self.f.write(phase='btc_paid', preimage=self.f.preimage)
        self.phase = 'resolved'
        self.binding = ['other', 8]
        before = self.f.path.read_bytes()
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(self.f.path.read_bytes(), before)

    def test_expired_quote_blocks_new_spend_but_not_paid_recovery(self):
        self.quote['expires_at'] = 1
        self.f.write(reverse_quote=self.quote)
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertNotIn('sendpay', self.calls)
        self.f.write(phase='btc_paid', preimage=self.f.preimage)
        self.phase = 'resolved'
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})


if __name__ == '__main__':
    unittest.main()
