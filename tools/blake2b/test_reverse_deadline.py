"""Reverse deadline threshold, persisted close target and lost-reply recovery."""
import json
import subprocess
import unittest
from unittest.mock import patch

import reverse_controller as controller
import test_reverse_onchain
from swap_controller import save


class Crash(BaseException):
    pass


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.f = test_reverse_onchain.OnchainTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.base = self.f.f
        self.base.write(xbt_deadline_guard=True)
        self.closes = []
        self.lose_reply = False
        self.crash_before_close = False

    def rpc(self, cli, method, *args):
        if method == 'close':
            disk = json.loads(self.base.path.read_text())
            self.assertEqual(disk['phase'], 'outgoing_started')
            self.assertNotIn('preimage', disk)
            self.assertEqual(disk['xbt_close_intent']['channel'], self.f.pin)
            self.assertEqual(args, (self.f.pin['channel_id'], 1))
            self.assertEqual(cli, ['/xbt'])
            self.closes.append(args)
            self.f.channel_state = 'AWAITING_UNILATERAL'
            if self.lose_reply:
                raise subprocess.TimeoutExpired('private', 20)
            return {'type': 'unilateral', 'txids': ['cc'*32]}
        return self.f.rpc(cli, method, *args)

    def persist(self, path, state):
        save(path, state)
        if self.crash_before_close and 'xbt_close_intent' in state and 'xbt_close_result' not in state:
            raise Crash()

    def run_controller(self):
        with patch('reverse_controller.RPC.call', side_effect=self.rpc), \
                patch('reverse_controller.save', side_effect=self.persist):
            return controller.run(self.base.path)

    def submit(self):
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.base.height = 269  # expiry 300: 31 blocks left

    def test_boundary_and_repeat_do_not_resend_or_close_twice(self):
        self.submit()
        before = self.base.path.read_bytes()
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.assertEqual(self.base.path.read_bytes(), before)
        self.assertFalse(self.closes)
        self.base.height = 270
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        after = self.base.path.read_bytes()
        for state in ('AWAITING_UNILATERAL', 'FUNDING_SPEND_SEEN', 'ONCHAIN'):
            self.f.channel_state = state
            self.assertEqual(self.run_controller()['outcome'], 'pending')
            self.assertEqual(self.base.path.read_bytes(), after)
        self.assertEqual(len(self.closes), 1)
        self.assertEqual(self.f.g.calls.count('sendpay'), 1)
        self.assertNotIn('reverse-release', self.f.g.calls)
        self.assertNotIn('reverse-fail', self.f.g.calls)

    def test_crash_before_rpc_retries_persisted_exact_target(self):
        self.submit()
        self.base.height = 270
        self.crash_before_close = True
        with self.assertRaises(Crash):
            self.run_controller()
        self.assertFalse(self.closes)
        before = json.loads(self.base.path.read_text())['xbt_close_intent']
        self.crash_before_close = False
        self.run_controller()
        self.assertEqual(json.loads(self.base.path.read_text())['xbt_close_intent'], before)
        self.assertEqual(len(self.closes), 1)

    def test_lost_reply_reconciles_without_second_close(self):
        self.submit()
        self.base.height = 270
        self.lose_reply = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        before = self.base.path.read_bytes()
        self.assertNotIn('xbt_close_result', json.loads(before))
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.assertEqual(before, self.base.path.read_bytes())
        self.assertEqual(len(self.closes), 1)

    def test_wrong_pin_hook_identity_or_network_never_closes(self):
        self.submit()
        self.base.height = 270
        for key in self.f.pin:
            old = self.f.pin[key]
            self.f.pin[key] = 9 if key == 'funding_outnum' else 'changed'
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.run_controller()
            self.f.pin[key] = old
        for key, value in (('id', 9), ('payment_hash', '00'*32), ('expiry', 301),
                           ('amount_msat', 199999999), ('direction', 'out')):
            old = self.base.h[key]
            self.base.h[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.run_controller()
            self.base.h[key] = old
        self.f.g.binding = ['other', 8]
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.f.g.binding = ['1x1x0', 8]
        self.base.networks['/xbt'] = 'xbt'
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertFalse(self.closes)

    def test_modified_intent_refused_before_close_retry(self):
        self.submit()
        self.base.height = 270
        self.crash_before_close = True
        with self.assertRaises(Crash):
            self.run_controller()
        state = json.loads(self.base.path.read_text())
        state['xbt_close_intent']['expiry'] += 1
        save(self.base.path, state)
        self.crash_before_close = False
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertFalse(self.closes)

    def test_disabled_does_nothing_and_missing_claim_optin_blocks_submission(self):
        self.base.write(xbt_deadline_guard=False)
        self.submit()
        self.base.height = 270
        before = self.base.path.read_bytes()
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.assertEqual(before, self.base.path.read_bytes())
        self.assertFalse(self.closes)
        self.base.write(xbt_deadline_guard=True, xbt_onchain_claim=False)
        count = self.f.g.calls.count('sendpay')
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(self.f.g.calls.count('sendpay'), count)

    def test_unknown_btc_outcome_does_not_trigger_close(self):
        self.submit()
        self.base.height = 270
        self.base.payments = []
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertFalse(self.closes)


if __name__ == '__main__':
    unittest.main()
