"""Explicit activation, private settings, launcher targeting and recovery scope."""
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import reverse_activation as activation
import reverse_live as live
from reverse_gate import Gate
from service_runtime import node_command
from service_manager import private_load
from swap_controller import save
import test_reverse_live as fixtures


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LiveTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.settings = dict(self.fixture.settings, python='/venv/python', repo='/repo',
                             bitcoin_cli='/bitcoin-cli', roots={k: str(self.root/k)
                             for k in ('btc', 'xbt', 'receiver')})
        for role in ('xbt', 'receiver'):
            root = Path(self.settings['roots'][role])
            root.mkdir()
            (root/'xbt-observer-v1').touch()
            (root/'xbt').mkdir()
            (root/'xbt/hsm_secret').touch()
        root = Path(self.settings['roots']['btc'])
        root.mkdir()
        (root/'btc-https-observer-v1').touch()
        self.path = self.root/'settings.json'
        save(self.path, self.settings)
        self.calls = []

    def rpc(self, cli, method, *args):
        self.calls.append(method)
        if method == 'getinfo':
            return self.fixture.rpc(cli, method, *args)
        if method == 'listpeerchannels':
            return {'channels': []}
        if method == 'listfunds':
            return {'outputs': [dict(status='confirmed', reserved=False, amount_msat=50000000)]}
        raise AssertionError('installer attempted non-read RPC: '+method)

    def enable(self):
        self.settings['reverse_live'] = activation.record(self.settings)
        save(self.path, self.settings)

    def test_installer_read_only_rpcs_backup_idempotency(self):
        before = self.path.read_bytes()
        result = activation.install(self.path, self.rpc)
        self.assertFalse(result['payment_started'])
        self.assertEqual(private_load(self.path)['reverse_live'], activation.record(self.settings))
        backup = self.path.with_name('settings.before-reverse-live.json')
        self.assertEqual(private_load(backup), json.loads(before))
        after = self.path.read_bytes()
        activation.install(self.path, self.rpc)
        self.assertEqual(self.path.read_bytes(), after)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(self.calls), {'getinfo', 'listpeerchannels', 'listfunds'})

    def test_pending_or_wrong_network_refuses_before_write(self):
        before = self.path.read_bytes()
        for mode in ('pending', 'network'):
            def rpc(cli, method, *args):
                result = self.rpc(cli, method, *args)
                if mode == 'pending' and method == 'listpeerchannels':
                    return {'channels': [{'htlcs': [{'id': 1}]}]}
                if mode == 'network' and method == 'getinfo':
                    result['network'] = 'regtest'
                return result
            with self.assertRaises(ValueError):
                activation.install(self.path, rpc)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name('settings.before-reverse-live.json').exists())

    def test_activation_scope_restored_on_error_and_missing_optin(self):
        self.enable()
        with self.assertRaises(RuntimeError):
            live.enabled()
        with self.assertRaisesRegex(ValueError, 'test'):
            with activation.activation(self.settings):
                live.enabled()
                raise ValueError('test')
        with self.assertRaises(RuntimeError):
            live.enabled()
        with activation.activation(self.fixture.settings), self.assertRaises(RuntimeError):
            live.enabled()

    def test_changed_identity_path_caps_or_profile_refused(self):
        self.enable()
        for key, value in (('receiver_id', fixtures.D), ('swap_root', '/other'),
                           ('reverse_profile', live.SERVICE_REGTEST)):
            settings = copy.deepcopy(self.settings)
            settings[key] = value
            with self.assertRaises(ValueError):
                with activation.activation(settings):
                    self.fail('changed binding enabled')
        settings = copy.deepcopy(self.settings)
        settings['reverse_live']['btc_sats'] = 10000
        with self.assertRaises(ValueError):
            activation.configured(settings)

    def test_gate_live_network_only_and_unrelated_invoice_continues(self):
        self.enable()
        for network, active in (('xbt', True), ('bitcoin', False), ('xbt-regtest', False)):
            gate = Gate(self.root/'gate', live=True)
            with activation.activation(self.settings):
                gate.handle(dict(id=1, method='init', params=dict(configuration=dict(network=network))))
            self.assertEqual(gate.active, active)
            if active:
                reply = gate.handle(dict(id=2, method='htlc_accepted', params=dict(htlc={'payment_hash': '00'*32}, onion={})))
                self.assertEqual(reply[0]['result'], {'result': 'continue'})
        gate = Gate(self.root/'gate', live=True)
        gate.handle(dict(id=1, method='init', params=dict(configuration=dict(network='xbt'))))
        self.assertFalse(gate.active)

    def test_plugin_persistent_same_path_operator_only(self):
        self.enable()
        root = Path(self.settings['roots']['xbt'])
        path = activation.plugin(root, self.path)
        self.assertEqual(activation.plugin(root, self.path), path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertIn('gate_main', path.read_text())
        with self.assertRaises(ValueError):
            activation.plugin(Path(self.settings['roots']['receiver']), self.path)
        path.write_text('unrelated wrapper')
        with self.assertRaises(ValueError):
            activation.plugin(root, self.path)

    def test_generated_plugin_boots_in_fresh_process_without_global_override(self):
        self.enable()
        wrapper = activation.plugin(Path(self.settings['roots']['xbt']), self.path)
        requests = [dict(id=1, method='getmanifest'),
                    dict(id=2, method='init', params=dict(configuration=dict(network='xbt'))),
                    dict(id=3, method='reverse-pilot-info'),
                    dict(id=4, method='htlc_accepted', params=dict(htlc={'payment_hash': '00'*32}, onion={}))]
        result = subprocess.run([str(wrapper)], input='\n'.join(map(json.dumps, requests))+'\n',
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        by_id = {r['id']: r['result'] for r in responses}
        self.assertEqual(by_id[2], {})
        self.assertEqual(by_id[3], dict(profile=live.PROFILE, gate_active=True))
        self.assertEqual(by_id[4], {'result': 'continue'})
        self.assertFalse(live.LIVE_EXECUTION_ENABLED)

    def test_launcher_only_enables_xbt_operator(self):
        self.enable()
        for kind in ('btc', 'xbt'):
            values = {kind.upper()+'_RPC_'+k: 'private' for k in ('HOST','PORT','USER','PASSWORD')}
            if kind == 'btc':
                values.update(BTC_RPC_CA='/private-ca', BTC_LN_HOST='127.0.0.1')
            save(self.root/(kind+'-rpc.json'), values)
        for role in ('btc', 'xbt', 'receiver'):
            command, env = node_command(self.settings, self.root, role)
            self.assertEqual(any(s.startswith('--reverse-settings=') for s in command), role == 'xbt')
            self.assertNotIn('private', command)

    def test_configured_recovery_never_starts_prepared_and_releases_once(self):
        import reverse_controller as controller
        import reverse_service as service
        self.enable()
        with patch.object(controller.RPC, 'call', side_effect=self.fixture.rpc):
            result = service.recover_record(self.root, self.settings, rpc=self.fixture.rpc)
            self.assertEqual(result['outcome'], 'needs_manual_start')
            self.assertFalse(self.fixture.payments)
            with activation.activation(self.settings):
                self.assertEqual(controller.run(self.fixture.path)['outcome'], 'pending')
            self.fixture.payments[0].update(status='complete', payment_preimage=self.fixture.preimage)
            for _ in range(2):
                result = service.recover_record(self.root, self.settings, rpc=self.fixture.rpc)
                self.assertEqual(result['phase'], 'xbt_released')
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.fixture.calls), 1)
        self.assertEqual(sum(m == 'reverse-release' for m, _ in self.fixture.calls), 1)
        self.assertFalse(activation.ACTIVE.get())

    def test_quote_directory_and_binding_checked_before_manual_run(self):
        self.enable()
        directory = self.root/'swap'
        directory.mkdir()
        save(directory/'reverse-quote.json', self.fixture.quote)
        self.assertEqual(activation.directory_settings(directory, self.path), self.settings)
        quote = copy.deepcopy(self.fixture.quote)
        quote['config']['payer_id'] = fixtures.D
        save(directory/'reverse-quote.json', quote)
        with self.assertRaises(ValueError):
            activation.directory_settings(directory, self.path)


if __name__ == '__main__':
    unittest.main()
