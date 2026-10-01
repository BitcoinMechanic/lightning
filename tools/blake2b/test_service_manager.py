"""Service preparation, secret handling and non-originating recovery tests."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from service_manager import credentials, private_load, units
from service_runtime import tick
from swap_controller import run, save


class ServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = dict(repo='/repo', python='/repo/.venv/bin/python', cli='/repo/cli/lightning-cli',
                             roots=dict(btc='/btc', xbt='/xbt', receiver='/receiver'),
                             btc_cli=['btc'], xbt_cli=['xbt'], receiver_cli=['receiver'],
                             node_ids=['btc-id','xbt-id'], receiver_id='receiver-id', swap_root=str(self.root))
        self.calls = []

    def rpc(self, cli, method, *args):
        self.calls.append((cli, method, args))
        if method == 'getinfo':
            return dict(network='bitcoin' if cli == ['btc'] else 'xbt', id=cli[0]+'-id')
        if method == 'listpeers':
            return {'peers': [{'id':'receiver-id', 'connected':True}]}
        if method == 'xbt-quote-status':
            return dict(payment_hash=args[0], phase='held', binding=['1x1x1', 2])
        raise AssertionError(method)

    def test_capture_roundtrips_special_chars_without_shell(self):
        values = dict(XBT_RPC_HOST='192.168.8.202', XBT_RPC_PORT='8332', XBT_RPC_USER='user',
                      XBT_RPC_PASSWORD='space " quote \\ dollar$ percent%')
        directory = self.root/'private'
        with patch.dict(os.environ, values, clear=True):
            credentials(directory, 'xbt')
            credentials(directory, 'xbt')
        path = directory/'xbt-rpc.json'
        self.assertEqual(private_load(path), values)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            private_load(path)

    def test_missing_vars_never_write_partial_credentials(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            credentials(self.root/'private', 'btc')
        self.assertFalse((self.root/'private').exists())

    def test_generated_units_contain_no_secrets_and_use_venv(self):
        result = units(self.settings, self.root)
        self.assertEqual(len(result), 5)
        for name, text in result.items():
            self.assertNotIn('RPC_PASSWORD', text)
            self.assertNotIn('Environment=', text)
            if name.endswith('.service'):
                self.assertIn('/repo/.venv/bin/python', text)
                self.assertIn('Restart=on-failure', text)
                self.assertIn('UMask=0077', text)

    def quote(self, state=None):
        directory = self.root/'swap-1'
        directory.mkdir()
        save(directory/'quote.json', dict(config=dict(btc_cli=['btc'], xbt_cli=['xbt']),
                                         node_ids=self.settings['node_ids'], terms={'payment_hash':'hash'}, btc_invoice='PRIVATE'))
        if state is not None:
            save(directory/'state.json', dict(btc_cli=['btc'], xbt_cli=['xbt'], node_ids=self.settings['node_ids'],
                                              payment_hash='hash', phase=state))
        return directory

    def test_prepared_is_blocked_inside_controller_lock_without_rpc(self):
        path = self.root/'state.json'
        save(path, {'phase':'prepared'})
        before = path.read_bytes()
        with patch('swap_controller.RPC.call') as rpc:
            result = run(path, recover_only=True)
        rpc.assert_not_called()
        self.assertEqual(result['outcome'], 'needs_manual_start')
        self.assertEqual(before, path.read_bytes())

    def test_monitor_requests_recovery_only_and_omits_preimage(self):
        self.quote('outgoing_started')
        with patch('service_runtime.RPC.call', side_effect=self.rpc), \
                patch('service_runtime.run', return_value={'phase':'btc_released','payment_preimage':'SECRET'}) as recover:
            health = tick(self.settings)
        self.assertTrue(health['nodes_ready'])
        self.assertEqual(recover.call_args.kwargs, {'recover_only':True})
        self.assertNotIn('SECRET', json.dumps(health))

    def test_receiver_outage_does_not_block_operator_recovery(self):
        self.quote('outgoing_started')
        def rpc(cli, method, *args):
            if cli == ['receiver']:
                raise TimeoutError()
            return self.rpc(cli, method, *args)
        with patch('service_runtime.RPC.call', side_effect=rpc), patch('service_runtime.run', return_value={'outcome':'pending'}) as recover:
            health = tick(self.settings)
        recover.assert_called_once()
        self.assertFalse(health['nodes_ready'])
        self.assertTrue(health['operators_ready'])

    def test_accepted_without_state_never_creates_state_or_submits(self):
        directory = self.quote()
        with patch('service_runtime.RPC.call', side_effect=self.rpc), patch('service_runtime.run') as recover:
            health = tick(self.settings)
        recover.assert_not_called()
        self.assertFalse((directory/'state.json').exists())
        self.assertEqual(health['swaps'][0]['outcome'], 'needs_manual_resume')

    def test_other_operator_binding_never_recovered(self):
        directory = self.quote('outgoing_started')
        state = json.loads((directory/'state.json').read_text())
        state['btc_cli'] = ['other']
        save(directory/'state.json', state)
        with patch('service_runtime.RPC.call', side_effect=self.rpc), patch('service_runtime.run') as recover:
            health = tick(self.settings)
        recover.assert_not_called()
        self.assertEqual(health['swaps'][0]['outcome'], 'needs_inspection')


if __name__ == '__main__':
    unittest.main()
