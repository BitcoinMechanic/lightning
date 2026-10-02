import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import switch_customer as m
from reverse_activation import record, configured
from reverse_service import binding
from reverse_authorize import process_record
from service_manager import private_load
from swap_controller import save


class MigrationTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.old_id = '02'+'33'*32
        self.new_id = '02'+'44'*32
        self.settings = dict(deployment='operator-pair-v1', node_ids=['02'+'11'*32,'02'+'22'*32],
            btc_cli=['btc'], xbt_cli=['xbt'], roots=dict(xbt=str(self.root/'node')),
            swap_root=str(self.root/'swaps'), receiver_id=self.old_id)
        self.settings['reverse_live'] = record(self.settings)
        (self.root/'swaps').mkdir()
        self.path = self.root/'settings.json'; save(self.path, self.settings)
        self.identity = self.root/'vm-customer.json'; save(self.identity, dict(customer_id=self.new_id))
        self.token = dict(token='ab'*32, payer_id=self.old_id)
        save(self.root/'customer-api.json', self.token)
        self.pending = False
        self.calls = []

    def rpc(self, cli, method, *args):
        self.calls.append(method)
        index = 0 if cli == ['btc'] else 1
        if method == 'getinfo':
            return dict(id=self.settings['node_ids'][index], network='bitcoin' if index==0 else 'xbt')
        if method == 'listpeerchannels':
            return dict(channels=[dict(peer_id=self.new_id, state='CHANNELD_NORMAL',
                peer_connected=True, htlcs=[{}] if self.pending else [])])
        if method == 'xbt-held': return dict(held=[])
        if method == 'reverse-status': return dict(phase='resolved', binding={'original': True})
        self.fail('Unexpected or mutating RPC')

    def migrate(self):
        return m.migrate(self.path, self.identity, self.rpc, lambda: None)

    def test_repeat_preserves_new_token_and_original_backup(self):
        self.migrate()
        new = private_load(self.path); token = private_load(self.root/'customer-api.json')
        self.assertTrue(configured(new))
        self.assertEqual(new['receiver_id'], self.new_id)
        self.assertNotEqual(token['token'], self.token['token'])
        self.assertEqual(private_load(self.root/'vm-customer-migration.json')['old'], self.settings)
        self.migrate()
        self.assertEqual(token, private_load(self.root/'customer-api.json'))

    def test_interruption_between_settings_and_token_resumes(self):
        def broken(path, value):
            if Path(path).name == 'customer-api.json': raise OSError('interrupted')
            save(path, value)
        with patch.object(m, 'save', side_effect=broken), self.assertRaises(OSError): self.migrate()
        self.assertEqual(private_load(self.path)['receiver_id'], self.new_id)
        self.assertEqual(private_load(self.root/'customer-api.json'), self.token)
        self.migrate()
        self.assertEqual(private_load(self.root/'customer-api.json')['payer_id'], self.new_id)

    def test_pending_refused_without_changing_settings(self):
        self.pending = True
        with self.assertRaises(ValueError): self.migrate()
        self.assertEqual(private_load(self.path), self.settings)
        self.assertFalse((self.root/'vm-customer-migration.json').exists())

    def test_changed_target_or_settings_refused(self):
        self.migrate()
        save(self.identity, dict(customer_id=self.old_id))
        with self.assertRaises(ValueError): self.migrate()
        save(self.identity, dict(customer_id=self.new_id))
        settings = private_load(self.path); settings['btc_cli'] = ['other']; save(self.path, settings)
        with self.assertRaises(ValueError): self.migrate()

    def test_historical_quote_recovers_only_with_old_binding(self):
        quote = dict(config=binding(self.settings))
        self.migrate(); new = private_load(self.path)
        old = m.recovery_settings(new, quote)
        self.assertEqual(old['receiver_id'], self.old_id)
        directory = self.root/'swaps/historical'; directory.mkdir(mode=0o700)
        save(directory/'reverse-quote.json', quote)
        with patch('reverse_service.recover_record', return_value={'phase':'xbt_released'}) as recover:
            self.assertEqual(process_record(directory, new)['phase'], 'xbt_released')
            self.assertEqual(recover.call_args.args[1]['receiver_id'], self.old_id)
        new['reverse_previous_customers'][0]['btc_cli'] = ['wrong']
        with self.assertRaises(ValueError): m.recovery_settings(new, quote)

    def test_unfinished_quote_refuses_switch(self):
        directory = self.root/'swaps/unstarted'; directory.mkdir(mode=0o700)
        save(directory/'reverse-quote.json', dict(config=binding(self.settings)))
        with self.assertRaises((ValueError, FileNotFoundError)): self.migrate()
        self.assertEqual(private_load(self.path), self.settings)

    def test_terminal_reverse_records_unchanged(self):
        directory = self.root/'swaps/done'; directory.mkdir(mode=0o700)
        terms = {'payment_hash': 'private-hash'}
        save(directory/'reverse-quote.json', dict(config=binding(self.settings), terms=terms))
        save(directory/'reverse-state.json', dict(phase='xbt_released', reverse_quote=terms,
             payment_hash='private-hash', xbt_binding={'original': True}))
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        self.migrate()
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_unaccepted_forward_quote_must_be_expired(self):
        directory = self.root/'swaps/forward'; directory.mkdir(mode=0o700)
        path = directory/'quote.json'
        save(path, dict(terms=dict(payment_hash='private', expires_at=9999999999)))
        original = self.rpc
        def rpc(cli, method, *args):
            if method == 'xbt-quote-status': return dict(phase='quoted', payment_hash='private')
            return original(cli, method, *args)
        with self.assertRaises(ValueError):
            m.migrate(self.path, self.identity, rpc, lambda: None)
        save(path, dict(terms=dict(payment_hash='private', expires_at=1)))
        self.assertTrue(m.migrate(self.path, self.identity, rpc, lambda: None)['customer_switched'])

    def test_running_services_block_before_writes(self):
        def running(): raise ValueError('still active')
        with self.assertRaises(ValueError): m.migrate(self.path, self.identity, self.rpc, running)
        self.assertEqual(private_load(self.path), self.settings)
        self.assertFalse(self.calls)


if __name__ == '__main__': unittest.main()
