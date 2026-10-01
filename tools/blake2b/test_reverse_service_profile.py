"""Service-regtest must never accept real-chain identities or enable live spending."""
import copy
import unittest
from unittest.mock import patch

import reverse_live as live
import reverse_service as service
import service_runtime
import test_reverse_live as fixtures
from reverse_gate import Gate, validate_terms
from swap_controller import save


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LiveTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = copy.deepcopy(self.fixture.state)
        self.state['profile'] = live.SERVICE_REGTEST
        self.state['btc_invoice'] = 'lnbcrt-private'
        self.terms = self.state['reverse_quote']
        self.terms.update(profile=live.SERVICE_REGTEST, btc_invoice='lnbcrt-private')

    def test_test_profile_does_not_enable_live_or_unknown_profiles(self):
        self.assertFalse(live.LIVE_EXECUTION_ENABLED)
        live.enabled(live.SERVICE_REGTEST)
        with self.assertRaises(RuntimeError):
            live.enabled()
        with self.assertRaises(ValueError):
            live.enabled('unknown')
        with self.assertRaises(RuntimeError):
            service.create({}, 'lnbc-private', self.fixture.root/'unused')
        self.assertFalse((self.fixture.root/'unused').exists())

    def test_exact_real_networks_required_without_rewriting(self):
        def rpc(cli, method):
            self.assertEqual(method, 'getinfo')
            return dict(network='xbt-regtest' if cli == ['/xbt'] else 'regtest',
                        id=fixtures.A if cli == ['/xbt'] else fixtures.B)
        live.verify_state(self.state, rpc)
        for wrong in ('bitcoin', 'xbt'):
            with self.subTest(network=wrong), self.assertRaises(RuntimeError):
                live.verify_state(self.state, lambda cli, method: dict(network=wrong, id=fixtures.A))
        self.state['reverse_quote']['profile'] = live.PROFILE
        with self.assertRaises(RuntimeError):
            live.verify_state(self.state, rpc)

    def test_invoice_currency_and_gate_profiles_cannot_cross(self):
        validate_terms(self.terms, service_regtest=True)
        wrong = dict(self.terms, btc_invoice='lnbc-private')
        with self.assertRaises(ValueError):
            validate_terms(wrong, service_regtest=True)
        with self.assertRaises(ValueError):
            validate_terms(dict(self.terms, profile=live.PROFILE), service_regtest=True)
        with self.assertRaises(ValueError):
            Gate(self.fixture.root/'gate', live=True, service_regtest=True)
        for network, active in (('xbt-regtest', True), ('xbt', False), ('regtest', False)):
            gate = Gate(self.fixture.root/'gate', service_regtest=True)
            gate.handle(dict(id=1, method='init', params=dict(configuration=dict(network=network))))
            self.assertEqual(gate.active, active)

    def test_live_recovery_settings_refuse_test_quote_before_rpc(self):
        quote = copy.deepcopy(self.fixture.quote)
        quote['terms'] = self.terms
        save(self.fixture.root/'reverse-quote.json', quote)
        def no_rpc(*args):
            self.fail('profile mismatch must fail before RPC')
        with self.assertRaises(ValueError):
            service.recover_record(self.fixture.root, self.fixture.settings, rpc=no_rpc)

    def test_background_test_settings_refuse_live_nodes(self):
        settings = dict(self.fixture.settings, reverse_profile=live.SERVICE_REGTEST)
        with patch('service_runtime.RPC.call', side_effect=self.fixture.rpc):
            result = service_runtime.tick(settings)
        self.assertEqual(result['error'], 'node_identity_mismatch')
        self.assertFalse(result['nodes_ready'])
        self.assertEqual(result['swaps'], [])
        self.assertTrue(all(method == 'getinfo' for method, _ in self.fixture.calls))


if __name__ == '__main__':
    unittest.main()
