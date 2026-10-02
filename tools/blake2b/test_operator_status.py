import copy
import json
from pathlib import Path
import tempfile
import unittest

from operator_status import summarize
from swap_controller import save


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = dict(btc_cli=['btc'], xbt_cli=['xbt'], node_ids=['private-btc','private-xbt'],
                             receiver_id='private-customer', swap_root=str(self.root))
        self.channel = dict(state='CHANNELD_NORMAL', peer_connected=True, htlcs=[],
                            spendable_msat=400000000, receivable_msat=41178000,
                            peer_id='private-customer', channel_id='PRIVATE-ID')
        self.channels = [self.channel]
        self.calls = []
        save(self.root/'health.json', dict(checked_at=99, swaps=[dict(
            directory='PRIVATE-PATH', outcome='needs_inspection')]))
        (self.root/'api-requests').mkdir()
        save(self.root/'api-requests/private-key.json', dict(phase='creating', token='PRIVATE-TOKEN'))

    def rpc(self, cli, method):
        self.calls.append((cli[0], method))
        if method == 'getinfo':
            return dict(id='private-'+cli[0], network='bitcoin' if cli[0]=='btc' else 'xbt')
        if method == 'listpeerchannels':
            return dict(channels=self.channels)
        self.fail('Mutating or unexpected RPC')

    def run_status(self, **kwargs):
        return summarize(self.settings, self.root, rpc=kwargs.get('rpc', self.rpc),
                         now=lambda: 100, services=lambda: {},
                         probe=lambda port: dict(reachable=True, unauthenticated_request_rejected=True))

    def test_read_only_capacities_and_no_private_values(self):
        before = (self.root/'health.json').read_bytes()
        result = self.run_status()
        self.assertEqual(result['nodes']['xbt']['bound_customer_channel']['clear_channel_receivable_sats'], 41178)
        self.assertEqual(result['quote_requests']['creating_or_uncertain'], 1)
        self.assertEqual(result['recovery_snapshot']['attention_reports'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('private-', json.dumps(result))
        self.assertEqual(before, (self.root/'health.json').read_bytes())
        self.assertEqual(len(self.calls), 4)

    def test_pending_disconnected_and_historical_excluded_from_clear_capacity(self):
        self.channels = [dict(self.channel, htlcs=[{}]),
                         dict(self.channel, peer_connected=False), dict(self.channel, state='ONCHAIN')]
        result = self.run_status()['nodes']['xbt']
        self.assertEqual(result['clear_channel_spendable_sats'], 0)
        self.assertEqual(result['pending_htlcs'], 1)
        self.assertEqual(result['other_channels'], 1)

    def test_other_peer_not_counted_as_customer(self):
        self.channels = [dict(self.channel, peer_id='someone-else')]
        self.assertEqual(self.run_status()['nodes']['xbt']['bound_customer_channel']['normal_channels'], 0)

    def test_bad_identity_does_not_query_channels(self):
        self.settings['node_ids'][0] = 'different'
        result = self.run_status()
        self.assertFalse(result['nodes']['btc']['rpc_ready'])
        self.assertTrue(result['nodes']['xbt']['rpc_ready'])
        self.assertNotIn(('btc','listpeerchannels'), self.calls)

    def test_stale_missing_and_malformed_records(self):
        save(self.root/'health.json', dict(checked_at=1, swaps=[]))
        path = self.root/'api-requests/bad.json'
        path.write_text('garbage'); path.chmod(0o600)
        result = self.run_status()
        self.assertTrue(result['recovery_snapshot']['stale'])
        self.assertEqual(result['quote_requests']['unreadable'], 1)
        (self.root/'health.json').unlink()
        self.assertFalse(self.run_status()['recovery_snapshot']['available'])

    def test_unknown_capacity_not_reported_as_zero(self):
        del self.channel['receivable_msat']
        self.assertFalse(self.run_status()['nodes']['xbt']['rpc_ready'])


if __name__ == '__main__':
    unittest.main()
