"""Opt-in reverse on-chain release binding; no live funds or daemon RPCs."""
import json
import subprocess
import unittest
from unittest.mock import patch

import reverse_controller as controller
import test_reverse_gate


class OnchainTests(unittest.TestCase):
    def setUp(self):
        self.g = test_reverse_gate.ReconcileTests()
        self.g.setUp()
        self.addCleanup(self.g.doCleanups)
        self.f = self.g.f
        self.f.write(xbt_onchain_claim=True)
        self.channel_state = 'CHANNELD_NORMAL'
        self.connected = True
        self.pin = dict(channel_id='aa'*32, funding_txid='bb'*32, funding_outnum=0,
                        peer_id='payer', short_channel_id='1x1x0')

    def rpc(self, cli, method, *args):
        result = self.g.rpc(cli, method, *args)
        if method == 'listpeerchannels' and cli[0] == '/xbt':
            result['channels'][0].update(self.pin, state=self.channel_state, peer_connected=self.connected)
        return result

    def run_controller(self):
        with patch('reverse_controller.RPC.call', side_effect=self.rpc):
            return controller.run(self.f.path)

    def submitted(self):
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.assertEqual(json.loads(self.f.path.read_text())['incoming_channel'], self.pin)
        self.channel_state = 'ONCHAIN'
        self.connected = False

    def test_pins_before_send_and_pending_onchain_is_read_only(self):
        self.submitted()
        before = self.f.path.read_bytes()
        for _ in range(2):
            self.assertEqual(self.run_controller()['outcome'], 'pending')
            self.assertEqual(before, self.f.path.read_bytes())
        self.assertEqual(self.g.calls.count('sendpay'), 1)
        self.assertNotIn('reverse-release', self.g.calls)
        self.assertNotIn('reverse-fail', self.g.calls)
        self.assertNotIn('close', self.g.calls)

    def test_complete_releases_onchain_and_lost_reply_reconciles(self):
        self.submitted()
        self.f.payments = [self.f.attempt('complete')]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        self.assertEqual(json.loads(self.f.path.read_text())['phase'], 'btc_paid')
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(self.g.calls.count('reverse-release'), 1)
        self.assertEqual(self.g.calls.count('sendpay'), 1)

    def test_wrong_channel_or_funding_pin_never_releases(self):
        self.submitted()
        self.f.payments = [self.f.attempt('complete')]
        for key in self.pin:
            previous = self.pin[key]
            self.pin[key] = 1 if key == 'funding_outnum' else 'changed'
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.run_controller()
            self.pin[key] = previous
        self.assertNotIn('reverse-release', self.g.calls)

    def test_missing_optin_or_pin_never_releases(self):
        self.submitted()
        self.f.payments = [self.f.attempt('complete')]
        original = json.loads(self.f.path.read_text())
        for key in ('incoming_channel', 'xbt_onchain_claim'):
            state = dict(original)
            del state[key]
            self.f.path.write_text(json.dumps(state))
            with self.assertRaises(RuntimeError):
                self.run_controller()
        self.assertNotIn('reverse-release', self.g.calls)

    def test_wrong_htlc_or_hook_never_releases(self):
        self.submitted()
        self.f.payments = [self.f.attempt('complete')]
        for key, value in (('expiry', 301), ('amount_msat', 199999999),
                           ('payment_hash', '00'*32), ('id', 9)):
            previous = self.f.h[key]
            self.f.h[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.run_controller()
            self.f.h[key] = previous
        self.f.held = False
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertNotIn('reverse-release', self.g.calls)

    def test_no_new_spend_or_failure_resolution_onchain(self):
        self.channel_state = 'ONCHAIN'
        before = self.f.path.read_bytes()
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(before, self.f.path.read_bytes())
        self.assertNotIn('sendpay', self.g.calls)
        self.channel_state = 'CHANNELD_NORMAL'
        self.submitted()
        self.f.payments = [self.f.attempt('failed')]
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertNotIn('reverse-fail', self.g.calls)

    def test_transition_states_are_not_confirmed_onchain(self):
        self.submitted()
        self.f.payments = [self.f.attempt('complete')]
        for state in ('AWAITING_UNILATERAL', 'FUNDING_SPEND_SEEN', 'CHANNELD_SHUTTING_DOWN'):
            self.channel_state = state
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                self.run_controller()
        self.assertNotIn('reverse-release', self.g.calls)


if __name__ == '__main__':
    unittest.main()
