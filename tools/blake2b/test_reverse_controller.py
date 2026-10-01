"""Reverse recovery invariants; no live nodes or funds."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import reverse_controller as controller
from swap_controller import save


class Crash(BaseException):
    pass


class ReverseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'state.json'
        self.preimage = 'ab'*32
        self.payment_hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.state = dict(profile='reverse-regtest-v1', phase='prepared',
                          xbt_cli=['/xbt'], btc_cli=['/btc'], node_ids=['xbt-node', 'btc-node'],
                          payment_hash=self.payment_hash, xbt_binding=['1x1x0', 8], xbt_expiry=300,
                          xbt_amount_msat=200000000, btc_amount_msat=100000000,
                          btc_invoice='lnbcrt-fixture', btc_secret='22'*32,
                          route=[dict(id='receiver', channel='2x1x0', amount_msat=100000000, delay=40)])
        self.decoded = dict(valid=True, type='bolt11 invoice', currency='bcrt',
                            payment_hash=self.payment_hash, payment_secret='22'*32,
                            payee='receiver', amount_msat=100000000,
                            created_at=int(time.time()), expiry=3600, min_final_cltv_expiry=18)
        self.h = dict(id=8, direction='in', payment_hash=self.payment_hash,
                      amount_msat=200000000, expiry=300, state='RCVD_ADD_ACK_REVOCATION')
        self.held = True
        self.payments = []
        self.calls = []
        self.networks = {'/xbt': 'xbt-regtest', '/btc': 'regtest'}
        self.height = 100
        self.fail_send = self.fail_release = self.fail_fail = False
        self.write()

    def write(self, **updates):
        self.state.update(updates)
        save(self.path, self.state)

    def attempt(self, status='pending', **updates):
        value = dict(payment_hash=self.payment_hash, amount_msat=100000000,
                     amount_sent_msat=100000000, destination='receiver', bolt11='lnbcrt-fixture',
                     groupid=1, partid=0, status=status)
        if status == 'complete':
            value['payment_preimage'] = self.preimage
        value.update(updates)
        return value

    def rpc(self, cli, method, *args):
        self.calls.append(method)
        if method == 'getinfo':
            return dict(id='xbt-node' if cli[0] == '/xbt' else 'btc-node',
                        network=self.networks[cli[0]], blockheight=self.height)
        if method == 'listpeerchannels':
            if cli[0] == '/xbt':
                return {'channels': [dict(short_channel_id='1x1x0', state='CHANNELD_NORMAL',
                                          peer_connected=True, htlcs=[self.h] if self.held else [])]}
            return {'channels': [dict(short_channel_id='2x1x0', peer_id='receiver',
                                      state='CHANNELD_NORMAL', peer_connected=True,
                                      htlcs=[], spendable_msat=900000000)]}
        if method == 'xbt-held':
            return {'held': [dict(short_channel_id='1x1x0', id=8, payment_hash=self.payment_hash,
                                  amount_msat=200000000, cltv_expiry=300)] if self.held else []}
        if method == 'decode':
            return self.decoded
        if method == 'listsendpays':
            return {'payments': self.payments}
        if method == 'sendpay':
            self.assertEqual(json.loads(self.path.read_text())['phase'], 'outgoing_started')
            self.assertFalse(self.payments)
            self.payments = [self.attempt()]
            if self.fail_send:
                raise subprocess.TimeoutExpired('private', 20)
            return {}
        if method == 'waitsendpay':
            self.payments = [self.attempt('complete')]
            return self.payments[0]
        if method in ('xbt-release', 'xbt-fail'):
            self.assertTrue(self.held)
            self.held = False
            disk = json.loads(self.path.read_text())
            if method == 'xbt-release':
                self.assertEqual(disk['phase'], 'btc_paid')
                self.assertEqual(args, (self.preimage,))
                if self.fail_release:
                    raise subprocess.TimeoutExpired('private', 20)
                return {'released': 1}
            self.assertEqual(disk['phase'], 'btc_failed')
            self.assertEqual(args, (self.payment_hash,))
            if self.fail_fail:
                raise subprocess.TimeoutExpired('private', 20)
            return {'failed': 1}
        raise AssertionError(method)

    def run_controller(self, **kwargs):
        with patch('reverse_controller.RPC.call', side_effect=self.rpc):
            return controller.run(self.path, **kwargs)

    def test_pending_recovery_preserves_checkpoint_and_sends_once(self):
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        before = self.path.read_bytes()
        for _ in range(2):
            self.assertEqual(self.run_controller()['outcome'], 'pending')
            self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.calls.count('sendpay'), 1)
        self.assertTrue(self.held)

    def test_complete_recovers_preimage_and_terminal_repeat_is_read_only(self):
        self.run_controller()
        self.payments = [self.attempt('complete')]
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        before = self.path.read_bytes()
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.calls.count('sendpay'), 1)
        self.assertEqual(self.calls.count('xbt-release'), 1)

    def test_definite_failure_releases_only_original_xbt_once(self):
        self.run_controller()
        self.payments = [self.attempt('failed')]
        self.assertEqual(self.run_controller(), {'phase': 'xbt_failed'})
        self.assertEqual(self.run_controller(), {'phase': 'xbt_failed'})
        self.assertEqual(self.calls.count('sendpay'), 1)
        self.assertEqual(self.calls.count('xbt-fail'), 1)

    def test_lost_submission_reply_never_resends(self):
        self.fail_send = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.assertEqual(self.calls.count('sendpay'), 1)

    def test_missing_or_inconsistent_attempt_never_resolves_or_resends(self):
        self.write(phase='outgoing_started')
        cases = [[], [self.attempt(), self.attempt()], [self.attempt('unknown')],
                 [self.attempt('failed', payment_preimage=self.preimage)],
                 [self.attempt('complete', payment_preimage='00'*32)],
                 [self.attempt(amount_msat=1)], [self.attempt(amount_sent_msat=100000001)],
                 [self.attempt(destination='other')], [self.attempt(bolt11='other')]]
        for records in cases:
            self.payments = records
            before = self.path.read_bytes()
            with self.assertRaises(RuntimeError):
                self.run_controller()
            self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(set(self.calls) & {'sendpay', 'xbt-release', 'xbt-fail'})

    def test_wrong_network_or_identity_prevents_mutations(self):
        for key in ('/xbt', '/btc'):
            old = self.networks[key]
            self.networks[key] = 'xbt' if key == '/xbt' else 'bitcoin'
            with self.assertRaises(RuntimeError):
                self.run_controller()
            self.networks[key] = old
        self.write(node_ids=['other', 'btc-node'])
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertNotIn('sendpay', self.calls)

    def test_wrong_binding_blocks_spending_and_failure_resolution(self):
        self.write(xbt_binding=['1x1x0', 9])
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.write(phase='outgoing_started')
        self.payments = [self.attempt('failed')]
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertNotIn('sendpay', self.calls)
        self.assertNotIn('xbt-fail', self.calls)

    def test_stale_margin_and_substituted_invoice_prevent_submission(self):
        self.height = 201
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.height = 100
        self.decoded['payee'] = 'other'
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertNotIn('sendpay', self.calls)

    def test_crash_after_btc_keeps_submission_checkpoint_without_preimage(self):
        with patch('reverse_controller.os._exit', side_effect=Crash) as crash:
            with self.assertRaises(Crash):
                self.run_controller(crash_after_btc=True)
        crash.assert_called_once_with(86)
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk['phase'], 'outgoing_started')
        self.assertNotIn('preimage', disk)
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(self.calls.count('sendpay'), 1)

    def test_lost_release_reply_preserves_checkpoint_and_needs_inspection(self):
        self.run_controller()
        self.payments = [self.attempt('complete')]
        self.fail_release = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        before = self.path.read_bytes()
        self.assertEqual(json.loads(before)['phase'], 'btc_paid')
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.calls.count('xbt-release'), 1)

    def test_lost_failure_reply_preserves_checkpoint_and_needs_inspection(self):
        self.run_controller()
        self.payments = [self.attempt('failed')]
        self.fail_fail = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_controller()
        self.assertEqual(json.loads(self.path.read_text())['phase'], 'btc_failed')
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(self.calls.count('xbt-fail'), 1)

    def test_competing_controller_cannot_read_or_mutate_rpc(self):
        fd = os.open(str(self.path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_controller(), {'outcome': 'busy'})
            self.assertFalse(self.calls)
        finally:
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
