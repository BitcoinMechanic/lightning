"""Offline controller recovery checks with RPC calls recorded and constrained."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swap_controller import run, save


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.preimage = 'ab' * 32
        self.payment_hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.state = {'phase': 'xbt_paid', 'preimage': self.preimage,
                      'payment_hash': self.payment_hash, 'quote_gate': True,
                      'btc_binding': ['104x1x0', 0], 'btc_cli': ['btc'], 'xbt_cli': ['xbt']}
        save(self.path, self.state)
        self.status = {'payment_hash': self.payment_hash, 'phase': 'resolved',
                       'binding': ['104x1x0', 0]}

    def test_resolved_skips_release_and_outgoing_rpcs(self):
        with patch('swap_controller.RPC.call', return_value=self.status) as rpc:
            self.assertEqual(run(self.path)['phase'], 'btc_released')
            rpc.assert_called_once_with(['btc'], 'xbt-quote-status', self.payment_hash)
        with patch('swap_controller.RPC.call', side_effect=AssertionError('unexpected RPC')):
            self.assertEqual(run(self.path)['phase'], 'btc_released')

    def test_held_releases_once(self):
        with patch('swap_controller.RPC.call', side_effect=[dict(self.status, phase='held'),
                                                         {'released': 1}]) as rpc:
            run(self.path)
            self.assertEqual(rpc.call_count, 2)
            self.assertEqual(rpc.call_args.args, (['btc'], 'xbt-release', self.preimage))

    def test_wrong_identity_or_phase_does_not_release(self):
        for override in ({'binding': ['104x1x0', 1]}, {'payment_hash': '00' * 32},
                         {'phase': 'quoted'}):
            with self.subTest(override=override):
                with patch('swap_controller.RPC.call', return_value=dict(self.status, **override)) as rpc:
                    with self.assertRaises(RuntimeError):
                        run(self.path)
                    self.assertEqual(rpc.call_count, 1)
                self.assertEqual(json.loads(self.path.read_text()), self.state)

    def test_unknown_status_does_not_checkpoint(self):
        with patch('swap_controller.RPC.call', side_effect=RuntimeError('transport failure')):
            with self.assertRaises(RuntimeError):
                run(self.path)
        self.assertEqual(json.loads(self.path.read_text()), self.state)

    def test_crash_keeps_xbt_paid_checkpoint(self):
        with patch('swap_controller.RPC.call', side_effect=[dict(self.status, phase='held'),
                                                         {'released': 1}]), \
                patch('swap_controller.os._exit', side_effect=SystemExit) as crash:
            with self.assertRaises(SystemExit):
                run(self.path, crash_after_btc=True)
            crash.assert_called_once_with(87)
        self.assertEqual(json.loads(self.path.read_text()), self.state)

    def test_pending_only_queries_outgoing_and_preserves_checkpoint(self):
        state = dict(self.state, phase='outgoing_started', xbt_amount_msat=200000000)
        del state['preimage']
        save(self.path, state)
        before = self.path.read_bytes()
        payment = {'payment_hash': self.payment_hash, 'status': 'pending',
                   'amount_msat': 200000000}
        for attempt in range(2):
            with patch('swap_controller.RPC.call', return_value={'payments': [payment]}) as rpc:
                self.assertEqual(run(self.path), {'phase': 'outgoing_started', 'outcome': 'pending'})
                rpc.assert_called_once_with(['xbt'], 'listsendpays')
            self.assertEqual(self.path.read_bytes(), before)

    def test_unresolved_or_inconsistent_outgoing_never_releases_btc(self):
        state = dict(self.state, phase='outgoing_started', xbt_amount_msat=200000000)
        del state['preimage']
        save(self.path, state)
        before = self.path.read_bytes()
        payment = {'payment_hash': self.payment_hash, 'status': 'pending',
                   'amount_msat': 200000000}
        for payments in ([], [payment, payment],
                         [dict(payment, status='failed', payment_preimage=self.preimage)],
                         [dict(payment, amount_msat=1)],
                         [dict(payment, payment_preimage=self.preimage)]):
            with self.subTest(payments=payments):
                with patch('swap_controller.RPC.call', return_value={'payments': payments}) as rpc:
                    with self.assertRaises(RuntimeError):
                        run(self.path)
                    rpc.assert_called_once_with(['xbt'], 'listsendpays')
                self.assertEqual(self.path.read_bytes(), before)

    def test_definite_failure_fails_only_original_btc_binding(self):
        state = dict(self.state, phase='outgoing_started', xbt_amount_msat=200000000)
        del state['preimage']
        save(self.path, state)
        payment = {'payment_hash': self.payment_hash, 'status': 'failed',
                   'amount_msat': 200000000}
        with patch('swap_controller.RPC.call', side_effect=[{'payments': [payment]},
                   dict(self.status, phase='held'), {'failed': 1}]) as rpc:
            self.assertEqual(run(self.path), {'phase': 'btc_failed', 'outcome': 'failed'})
            self.assertEqual([call.args[1] for call in rpc.call_args_list],
                             ['listsendpays', 'xbt-quote-status', 'xbt-fail'])
            self.assertEqual(rpc.call_args.args,
                             (['btc'], 'xbt-fail', self.payment_hash, json.dumps(state['btc_binding'])))
        with patch('swap_controller.RPC.call', side_effect=AssertionError('unexpected RPC')):
            self.assertEqual(run(self.path), {'phase': 'btc_failed', 'outcome': 'failed'})

    def test_failure_checkpoint_reconciles_already_failed_hook(self):
        state = dict(self.state, phase='xbt_failed')
        del state['preimage']
        save(self.path, state)
        with patch('swap_controller.RPC.call', return_value=dict(self.status, phase='failed')) as rpc:
            self.assertEqual(run(self.path)['phase'], 'btc_failed')
            rpc.assert_called_once_with(['btc'], 'xbt-quote-status', self.payment_hash)

    def test_failure_refuses_resolved_or_different_btc_binding(self):
        state = dict(self.state, phase='xbt_failed')
        del state['preimage']
        save(self.path, state)
        for status in (self.status, dict(self.status, phase='held', binding=['104x1x0', 1])):
            with patch('swap_controller.RPC.call', return_value=status) as rpc:
                with self.assertRaises(RuntimeError):
                    run(self.path)
                rpc.assert_called_once_with(['btc'], 'xbt-quote-status', self.payment_hash)
            self.assertEqual(json.loads(self.path.read_text()), state)


if __name__ == '__main__':
    unittest.main()
