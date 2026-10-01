"""Quote inspection must work without access to the payer wallet RPC."""
import copy
import unittest

import reverse_check
import test_reverse_check as fixtures


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CheckTests()
        self.fixture.setUp()
        self.roles = []

    def check(self, payer_id=fixtures.E):
        f = self.fixture
        def rpc(cli, method, *args):
            self.assertNotEqual(cli[0], '/payer', 'customer RPC must never be called')
            self.roles.append(cli[0])
            return f.rpc(cli, method, *args)
        return reverse_check.check('lnbc-private-invoice',
            {k: v for k, v in f.clis.items() if k != 'payer'}, rpc=rpc,
            payer_id=payer_id, now=lambda: 1000.001,
            market_fetch=lambda kind: f.ticker if kind == 'ticker' else f.book)

    def test_operator_only_reports_unobserved_payer_balance(self):
        result = self.check()
        self.assertTrue(result['feasible'])
        self.assertFalse(result['payer_rpc_checked'])
        self.assertIsNone(result['xbt_payer_spendable_sats'])
        self.assertEqual(result['xbt_operator_receivable_sats'], 400000)
        self.assertEqual(set(self.roles), {'/btc', '/operator'})

    def test_operator_receivable_is_authoritative_capacity_bound(self):
        self.fixture.channels['operator'][0]['receivable_msat'] = 1000
        result = self.check()
        self.assertFalse(result['feasible'])
        self.assertIn('insufficient XBT payer-to-operator liquidity', result['reasons'])

    def test_wrong_or_malformed_payer_cannot_select_other_channel(self):
        for payer in ('bad', fixtures.A, fixtures.D, fixtures.C):
            with self.subTest(payer=payer), self.assertRaises(reverse_check.CheckError):
                self.check(payer)

    def test_disconnected_pending_and_ambiguous_channels_refused(self):
        original = copy.deepcopy(self.fixture.channels['operator'])
        variants = [[dict(original[0], peer_connected=False)],
                    [dict(original[0], htlcs=[{'id': 1}])], original*2]
        for channels in variants:
            self.fixture.channels['operator'] = channels
            with self.assertRaises(reverse_check.CheckError):
                self.check()

    def test_operator_reserve_and_remote_policy_still_enforced(self):
        self.fixture.reserve = 0
        self.assertFalse(self.check()['feasible'])
        self.fixture.reserve = 50000000
        self.fixture.remote_policies[0]['active'] = False
        self.assertFalse(self.check()['feasible'])

    def test_existing_two_wallet_inspection_still_checks_both_ends(self):
        result = self.fixture.run_check()
        self.assertTrue(result['payer_rpc_checked'])
        self.assertEqual(result['xbt_payer_spendable_sats'], 400000)
        self.fixture.channels['payer'][0]['funding_txid'] = 'other'
        with self.assertRaises(reverse_check.CheckError):
            self.fixture.run_check()


if __name__ == '__main__':
    unittest.main()
