"""Exercise real plugin protocol and disk reload across quote expiry."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.plugin = Path(temporary.name) / 'quote_plugin.py'
        self.plugin.write_text(Path(__file__).with_name('quote_plugin.py').read_text())
        self.disk = self.plugin.with_suffix('.quotes.json')
        self.preimage = '11' * 32
        self.hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.terms = dict(payment_hash=self.hash, payment_secret='22' * 32,
                          btc_amount_msat=100000000, xbt_amount_msat=200000000,
                          xbt_invoice='lnxbtrt-fixture', expires_at=1100,
                          min_cltv_delta=100, max_cltv_delta=2000)
        self.htlc = dict(short_channel_id='100x1x0', id=7, payment_hash=self.hash,
                         amount_msat=100000000, cltv_expiry=250, cltv_expiry_relative=120)
        self.onion = dict(payment_secret='22' * 32, forward_msat=100000000,
                          total_msat=100000000, type='tlv', outgoing_cltv_value=250)

    def request(self, ident, method, params):
        return dict(id=ident, method=method, params=params)

    def hook(self, ident=3, htlc=None, onion=None):
        return self.request(ident, 'htlc_accepted', {
            'htlc': self.htlc if htlc is None else htlc,
            'onion': self.onion if onion is None else onion})

    def run_plugin(self, now, requests):
        init = self.request(1, 'init', {'configuration': {'network': 'regtest'}})
        # Fake wall time only in this subprocess; execute the actual plugin.
        code = ('import runpy,sys,time; time.time=lambda:float(sys.argv[2]); '
                'runpy.run_path(sys.argv[1],run_name="__main__")')
        result = subprocess.run([sys.executable, '-c', code, str(self.plugin), str(now)],
                                input='\n\n'.join(map(json.dumps, [init, *requests]))+'\n\n',
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {r['id']: r for line in result.stdout.splitlines() if line.strip()
                for r in [json.loads(line)] if 'id' in r}

    def accept(self):
        replies = self.run_plugin(1000, [self.request(2, 'xbt-register', [self.terms]),
                                         self.hook()])
        self.assertNotIn(3, replies)  # Hook remains held.
        self.assertEqual(json.loads(self.disk.read_text())[self.hash]['phase'], 'held')

    def test_expired_held_replay_restores_hook_without_disk_change(self):
        self.accept()
        before = self.disk.read_bytes()
        htlc = dict(self.htlc, cltv_expiry_relative=20)
        replies = self.run_plugin(2000, [self.hook(htlc=htlc),
                                         self.request(4, 'xbt-held', [])])
        self.assertNotIn(3, replies)
        self.assertEqual(replies[4]['result']['held'], [htlc])
        self.assertEqual(self.disk.read_bytes(), before)
        replies = self.run_plugin(2001, [self.hook(htlc=htlc),
                                         self.request(5, 'xbt-release', [self.preimage])])
        self.assertEqual(replies[3]['result']['result'], 'resolve')
        self.assertEqual(replies[5]['result'], {'released': 1})

    def test_expired_new_admission_fails(self):
        self.run_plugin(1000, [self.request(2, 'xbt-register', [self.terms])])
        replies = self.run_plugin(2000, [self.hook()])
        self.assertEqual(replies[3]['result']['result'], 'fail')
        self.assertEqual(json.loads(self.disk.read_text())[self.hash]['phase'], 'quoted')

    def test_changed_fields_remain_unresolved_and_not_releasable(self):
        self.accept()
        before = self.disk.read_bytes()
        variants = [(dict(self.htlc, amount_msat=1), self.onion),
                    (dict(self.htlc, cltv_expiry=251), self.onion),
                    (self.htlc, dict(self.onion, payment_secret='33'*32)),
                    (self.htlc, dict(self.onion, outgoing_cltv_value=251)),
                    (self.htlc, {})]
        for htlc, onion in variants:
            with self.subTest(htlc=htlc, onion=onion):
                replies = self.run_plugin(2000, [self.hook(htlc=htlc, onion=onion),
                                                 self.request(4, 'xbt-held', []),
                                                 self.request(5, 'xbt-release', [self.preimage])])
                self.assertNotIn(3, replies)
                self.assertEqual(replies[4]['result']['held'], [])
                self.assertIn('error', replies[5])
                self.assertEqual(self.disk.read_bytes(), before)

    def test_other_binding_cannot_replace_original(self):
        self.accept()
        before = self.disk.read_bytes()
        replies = self.run_plugin(2000, [self.hook(htlc=dict(self.htlc, id=8))])
        self.assertEqual(replies[3]['result']['result'], 'fail')
        self.assertEqual(self.disk.read_bytes(), before)

    def test_legacy_held_checkpoint_is_not_failed(self):
        self.accept()
        data = json.loads(self.disk.read_text())
        del data[self.hash]['accepted']
        self.disk.write_text(json.dumps(data))
        before = self.disk.read_bytes()
        replies = self.run_plugin(2000, [self.hook(), self.request(4, 'xbt-held', [])])
        self.assertNotIn(3, replies)
        self.assertEqual(replies[4]['result']['held'], [])
        self.assertEqual(self.disk.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
