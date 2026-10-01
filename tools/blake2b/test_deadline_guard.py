"""Deadline boundaries, exact channel targeting, and interrupted-close recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swap_controller import run, save


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'state.json'
        self.state = {'phase': 'outgoing_started', 'quote_gate': True,
                      'btc_deadline_guard': True, 'payment_hash': 'ab' * 32,
                      'btc_binding': ['100x1x0', 7], 'xbt_amount_msat': 200000000,
                      'btc_cli': ['btc'], 'xbt_cli': ['xbt']}
        self.status = {'payment_hash': self.state['payment_hash'], 'phase': 'held',
                       'binding': self.state['btc_binding']}
        self.spend = dict(self.status, cltv_expiry=200)
        self.info = {'network': 'regtest', 'blockheight': 170}
        self.channel = {'channel_id': 'cd' * 32, 'short_channel_id': '100x1x0',
                        'state': 'CHANNELD_NORMAL', 'htlcs': [
                            {'id': 7, 'direction': 'in', 'payment_hash': self.state['payment_hash'],
                             'expiry': 200}]}
        self.calls = []
        self.interrupt = False
        save(self.path, self.state)

    def rpc(self, cli, method, *args):
        self.calls.append((cli, method, args))
        if method == 'listsendpays':
            return {'payments': [{'payment_hash': self.state['payment_hash'],
                                  'amount_msat': 200000000, 'status': 'pending'}]}
        if method == 'xbt-quote-status':
            return self.status
        if method == 'getinfo':
            return self.info
        if method == 'listpeerchannels':
            # Another channel to the same node must not affect target selection.
            return {'channels': [dict(self.channel, channel_id='ef' * 32,
                                      short_channel_id='99x1x0'), self.channel]}
        if method == 'xbt-spend-info':
            return self.spend
        if method == 'close':
            self.assertEqual(args, (self.channel['channel_id'], 1))
            checkpoint = json.loads(self.path.read_text())
            self.assertEqual(checkpoint['btc_close_intent']['channel_id'], args[0])
            self.assertNotIn('preimage', checkpoint)
            self.channel['state'] = 'AWAITING_UNILATERAL'
            if self.interrupt:
                raise RuntimeError('lost close response')
            return {'type': 'unilateral', 'txids': ['12' * 32]}
        raise AssertionError('unexpected RPC: ' + method)

    def reconcile(self):
        with patch('swap_controller.RPC.call', side_effect=self.rpc):
            return run(self.path)

    def test_boundary_and_repeated_recovery(self):
        self.info['blockheight'] = 169
        before = self.path.read_bytes()
        self.assertEqual(self.reconcile()['outcome'], 'pending')
        self.assertEqual(self.path.read_bytes(), before)
        self.info['blockheight'] = 170
        self.reconcile()
        after = self.path.read_bytes()
        self.reconcile()
        self.assertEqual(self.path.read_bytes(), after)
        self.assertEqual(sum(c[1] == 'close' for c in self.calls), 1)

    def test_lost_reply_reconciles_channel_without_second_close(self):
        self.interrupt = True
        with self.assertRaisesRegex(RuntimeError, 'lost close response'):
            self.reconcile()
        self.assertNotIn('btc_close_result', json.loads(self.path.read_text()))
        for phase in ('AWAITING_UNILATERAL', 'FUNDING_SPEND_SEEN', 'ONCHAIN'):
            self.channel['state'] = phase
            self.assertEqual(self.reconcile()['outcome'], 'pending')
        self.assertEqual(sum(c[1] == 'close' for c in self.calls), 1)

    def test_crash_before_close_retries_exact_persisted_channel(self):
        self.state['btc_close_intent'] = {'channel_id': self.channel['channel_id'],
                                        'binding': self.state['btc_binding'],
                                        'payment_hash': self.state['payment_hash'], 'expiry': 200}
        save(self.path, self.state)
        self.reconcile()
        self.assertEqual(sum(c[1] == 'close' for c in self.calls), 1)

    def test_wrong_binding_or_network_never_closes(self):
        for target, field, bad in ((self.status, 'binding', ['100x1x0', 8]),
                                   (self.status, 'phase', 'failed'),
                                   (self.info, 'network', 'bitcoin'),
                                   (self.spend, 'binding', ['100x1x0', 8])):
            with self.subTest(field=field, bad=bad):
                old = copy.deepcopy(target[field])
                target[field] = bad
                with self.assertRaises(RuntimeError):
                    self.reconcile()
                target[field] = old
        self.assertFalse(any(c[1] == 'close' for c in self.calls))
        self.assertEqual(json.loads(self.path.read_text()), self.state)

    def test_wrong_htlc_never_closes(self):
        for field, bad in (('id', 8), ('direction', 'out'), ('payment_hash', '00' * 32), ('expiry', 201)):
            with self.subTest(field=field):
                htlc = self.channel['htlcs'][0]
                old = htlc[field]
                htlc[field] = bad
                with self.assertRaises(RuntimeError):
                    self.reconcile()
                htlc[field] = old
        self.assertFalse(any(c[1] == 'close' for c in self.calls))

    def test_disabled_preserves_existing_behavior(self):
        del self.state['btc_deadline_guard']
        save(self.path, self.state)
        self.reconcile()
        self.assertEqual([c[1] for c in self.calls], ['listsendpays'])


if __name__ == '__main__':
    unittest.main()
