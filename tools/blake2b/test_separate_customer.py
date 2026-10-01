"""Deployment separation, interrupted migration and operator-only recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from separate_customer import separate
from service_manager import private_load, units
from service_runtime import node_command, tick
from reverse_activation import configured, record
from reverse_service import binding
from swap_controller import save

A, B, C = ['02'+f'{i:064x}' for i in range(1, 4)]


class SeparateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root/'settings'
        self.unitdir = self.root/'units'
        self.directory.mkdir(mode=0o700)
        self.unitdir.mkdir()
        self.settings = dict(repo='/repo', python='/venv/python', bitcoin_cli='/bitcoin-cli',
            cli='/repo/cli/lightning-cli', roots=dict(btc='/btc', xbt='/xbt', receiver='/customer'),
            btc_cli=['btc'], xbt_cli=['xbt'], receiver_cli=['customer'], node_ids=[A, B],
            receiver_id=C, swap_root=str(self.root/'swaps'))
        Path(self.settings['swap_root']).mkdir()
        self.settings['reverse_live'] = record(self.settings)
        save(self.directory/'settings.json', self.settings)
        self.credentials = {'XBT_RPC_'+k: 'PRIVATE' for k in ('HOST','PORT','USER','PASSWORD')}
        save(self.directory/'xbt-rpc.json', self.credentials)
        for name, value in units(self.settings, self.directory).items():
            (self.unitdir/name).write_text(value)

    def migrate(self):
        result = separate(self.directory, self.unitdir)
        self.assertFalse(result['services_restarted'])
        return private_load(self.directory/'settings.json')

    def test_exact_roles_preserve_wallets_activation_and_swap_binding(self):
        updated = self.migrate()
        self.assertNotIn('receiver_cli', updated)
        self.assertNotIn('receiver', updated['roots'])
        self.assertNotIn('payer_cli', updated['reverse_live'])
        self.assertTrue(configured(updated))
        self.assertEqual(binding(updated), binding(self.settings))
        customer = private_load(self.directory/'customer-wallet/settings.json')
        self.assertEqual(customer['roots']['receiver'], self.settings['roots']['receiver'])
        self.assertNotIn('btc_cli', customer)
        self.assertEqual(private_load(self.directory/'customer-wallet/xbt-rpc.json'), self.credentials)
        self.assertEqual(private_load(self.directory/'settings.before-customer-separation.json'), self.settings)
        generated = units(updated, self.directory)
        self.assertEqual(len(generated), 4)
        self.assertNotIn('cln-xbt-receiver', generated['cln-swaps.target'])
        self.assertNotIn('cln-xbt-receiver', generated['cln-swap-recovery.service'])
        receiver = (self.unitdir/'cln-xbt-receiver.service').read_text()
        self.assertNotIn('PartOf=', receiver)
        self.assertIn('WantedBy=default.target', receiver)
        self.assertIn('customer-wallet', receiver)
        self.assertIn('--lightning-dir=/customer', receiver)

    def test_repeat_is_idempotent(self):
        self.migrate()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.migrate()
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_unrelated_unit_change_refused_before_settings_write(self):
        path = self.unitdir/'cln-xbt-receiver.service'
        path.write_text(path.read_text()+'# customized\n')
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertEqual(private_load(self.directory/'settings.json'), self.settings)
        self.assertFalse((self.directory/'customer-wallet').exists())

    def test_interrupted_unit_update_can_resume(self):
        import separate_customer
        original = separate_customer.write_text
        count = 0
        def interrupted(path, text):
            nonlocal count
            count += 1
            original(path, text)
            if count == 1:
                raise OSError('simulated interruption')
        with patch('separate_customer.write_text', side_effect=interrupted), self.assertRaises(OSError):
            self.migrate()
        updated = self.migrate()
        self.assertTrue(configured(updated))
        self.assertNotIn('receiver_cli', updated)

    def test_operator_health_never_calls_or_reconnects_customer(self):
        updated = self.migrate()
        calls = []
        def rpc(cli, method, *args):
            calls.append((cli, method))
            self.assertIn(cli, (['btc'], ['xbt']))
            if method == 'getinfo':
                return dict(network='bitcoin' if cli == ['btc'] else 'xbt', id=A if cli == ['btc'] else B)
            if method == 'listpeers':
                return {'peers': []}
            raise AssertionError('unexpected RPC: '+method)
        swap = Path(updated['swap_root'])/'existing'
        swap.mkdir()
        save(swap/'reverse-quote.json', {})
        with patch('service_runtime.RPC.call', side_effect=rpc), \
                patch('reverse_service.recover_record', return_value={'phase': 'xbt_released'}) as recover:
            result = tick(updated)
        recover.assert_called_once_with(swap, updated)
        self.assertTrue(result['operators_ready'])
        self.assertTrue(result['nodes_ready'])
        self.assertFalse(result['customer_wallet_managed'])
        self.assertFalse(result['xbt_connected'])
        self.assertNotIn('error', result)
        self.assertNotIn('connect', [method for _, method in calls])

    def test_independent_customer_launcher_has_no_operator_configuration(self):
        self.migrate()
        settings = private_load(self.directory/'customer-wallet/settings.json')
        root = self.root/'existing-customer'
        root.mkdir()
        (root/'xbt-observer-v1').touch()
        settings['roots']['receiver'] = str(root)
        args, env = node_command(settings, self.directory/'customer-wallet', 'receiver')
        self.assertIn('--local-peer-port=19835', args)
        self.assertFalse(any(a.startswith('--reverse-settings=') for a in args))
        self.assertEqual(env['XBT_RPC_PASSWORD'], 'PRIVATE')


if __name__ == '__main__':
    unittest.main()
