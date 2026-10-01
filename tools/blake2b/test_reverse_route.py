"""Bounded getroutes conversion and controller fee accounting; no live RPCs."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from reverse_route import convert, plan, validate
import reverse_controller
import test_reverse_controller


def fixture():
    policy = dict(source='a', destination='c', max_fee_msat=10000,
                  max_delay=80, max_hops=4, final_cltv=40)
    result = dict(routes=[dict(amount_msat=100000000, final_cltv=40, path=[
        dict(short_channel_id_dir='1x1x0/0', node_id_in='a', node_id_out='b',
             amount_in_msat=100005000, amount_out_msat=100005000, cltv_in=46, cltv_out=46),
        dict(short_channel_id_dir='2x1x0/0', node_id_in='b', node_id_out='c',
             amount_in_msat=100005000, amount_out_msat=100000000, cltv_in=46, cltv_out=40)])])
    return policy, result


class RouteTests(unittest.TestCase):
    def test_conversion_uses_far_end_amount_and_cltv(self):
        policy, result = fixture()
        route = convert(result, 100000000, policy)
        self.assertEqual(route, [dict(id='b', channel='1x1x0', amount_msat=100005000, delay=46),
                                 dict(id='c', channel='2x1x0', amount_msat=100000000, delay=40)])
        self.assertEqual(validate(route, 100000000, policy), 5000)

    def test_exact_fee_boundary_and_delay_cap(self):
        policy, result = fixture()
        route = convert(result, 100000000, policy)
        self.assertEqual(validate(route, 100000000, dict(policy, max_fee_msat=5000, max_delay=46)), 5000)
        for key, value in [('max_fee_msat', 4999), ('max_delay', 45), ('max_hops', 1)]:
            with self.assertRaises(ValueError):
                validate(route, 100000000, dict(policy, **{key: value}))

    def test_discontinuous_source_fee_direction_and_negative_hop_refused(self):
        policy, original = fixture()
        for index, key, value in [(0, 'amount_in_msat', 100006000), (0, 'node_id_in', 'other'),
                                  (1, 'node_id_in', 'other'), (1, 'amount_in_msat', 100004999),
                                  (1, 'cltv_in', 47), (1, 'amount_out_msat', 100006000),
                                  (1, 'short_channel_id_dir', '2x1x0/1')]:
            result = copy.deepcopy(original)
            result['routes'][0]['path'][index][key] = value
            with self.assertRaises(ValueError):
                convert(result, 100000000, policy)

    def test_mpp_wrong_final_destination_amount_and_loops_refused(self):
        policy, result = fixture()
        with self.assertRaises(ValueError):
            convert(dict(routes=result['routes']*2), 100000000, policy)
        route = convert(result, 100000000, policy)
        for index, key, value in [(1, 'id', 'other'), (1, 'amount_msat', 99999999),
                                  (1, 'delay', 39), (1, 'channel', '1x1x0'), (0, 'id', 'a'),
                                  (0, 'delay', True), (0, 'amount_msat', 99999999)]:
            changed = copy.deepcopy(route)
            changed[index][key] = value
            with self.assertRaises(ValueError):
                validate(changed, 100000000, policy)

    def test_planning_only_calls_getroutes_with_explicit_limits(self):
        policy, result = fixture()
        rpc = Mock(return_value=result)
        decoded = dict(valid=True, type='bolt11 invoice', currency='bcrt', payment_secret='22'*32,
                       min_final_cltv_expiry=18, payee='c', amount_msat=100000000)
        route, actual = plan(['/btc'], decoded, 'a', rpc)
        self.assertEqual(actual, policy)
        self.assertEqual(route, convert(result, 100000000, policy))
        rpc.assert_called_once()
        self.assertEqual(rpc.call_args.args[1], 'getroutes')
        for arg in ('maxparts=1', 'maxfee_msat=10000', 'maxdelay=80',
                    'layers=["auto.localchans","auto.sourcefree"]'):
            self.assertIn(arg, rpc.call_args.args)
        for changed in (dict(decoded, currency='bc'), dict(decoded, min_final_cltv_expiry=81)):
            rpc.reset_mock()
            with self.assertRaises(ValueError):
                plan(['/btc'], changed, 'a', rpc)
            rpc.assert_not_called()


class ControllerRouteTests(unittest.TestCase):
    def setUp(self):
        self.f = test_reverse_controller.ReverseTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.policy = dict(source='btc-node', destination='receiver', max_fee_msat=10000,
                           max_delay=80, max_hops=4, final_cltv=40)
        self.route = [dict(id='relay', channel='2x1x0', amount_msat=100005000, delay=46),
                      dict(id='receiver', channel='3x1x0', amount_msat=100000000, delay=40)]
        self.quote = dict(payment_hash=self.f.payment_hash, payment_secret='22'*32,
                          xbt_amount_msat=200000000, btc_amount_msat=100000000,
                          btc_invoice='lnbcrt-fixture', xbt_channel='1x1x0',
                          expires_at=9999999999, min_cltv_delta=100, max_cltv_delta=2000)
        self.f.write(route=self.route, routing=self.policy, durable_gate=True, reverse_quote=self.quote)
        self.phase = 'held'
        self.spendable = 900000000

    def rpc(self, cli, method, *args):
        if method == 'reverse-status':
            return dict(payment_hash=self.f.payment_hash, phase=self.phase, terms=self.quote,
                        binding=['1x1x0', 8], cltv_expiry=300, hook_ready=self.f.held)
        if cli[0] == '/btc' and method == 'listpeerchannels':
            return {'channels': [dict(short_channel_id='2x1x0', peer_id='relay',
                                      state='CHANNELD_NORMAL', peer_connected=True,
                                      htlcs=[], spendable_msat=self.spendable)]}
        if method == 'reverse-release':
            result = self.f.rpc(cli, 'xbt-release', args[2])
            self.phase = 'resolved'
            return result
        result = self.f.rpc(cli, method, *args)
        if method == 'sendpay':
            self.f.payments[0]['amount_sent_msat'] = 100005000
        return result

    def run_controller(self):
        with patch('reverse_controller.RPC.call', side_effect=self.rpc):
            return reverse_controller.run(self.f.path)

    def test_routed_completion_recovers_including_fee_without_new_route_or_send(self):
        self.assertEqual(self.run_controller()['outcome'], 'pending')
        self.f.payments = [self.f.attempt('complete', amount_sent_msat=100005000)]
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(self.run_controller(), {'phase': 'xbt_released'})
        self.assertEqual(self.f.calls.count('sendpay'), 1)
        self.assertNotIn('getroutes', self.f.calls)

    def test_first_hop_needs_liquidity_including_fees(self):
        self.spendable = 100000000
        before = self.f.path.read_bytes()
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(self.f.path.read_bytes(), before)
        self.assertNotIn('sendpay', self.f.calls)

    def test_over_budget_route_preserves_prepared_state_without_spend(self):
        self.f.write(routing=dict(self.policy, max_fee_msat=4999))
        before = self.f.path.read_bytes()
        with self.assertRaises(ValueError):
            self.run_controller()
        self.assertEqual(self.f.path.read_bytes(), before)
        self.assertNotIn('sendpay', self.f.calls)

    def test_route_delay_requires_fresh_incoming_margin(self):
        self.f.height = 195  # 105 blocks left, route needs 46 + 60 = 106.
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertNotIn('sendpay', self.f.calls)

    def test_recovery_rejects_unexpected_sent_amount(self):
        self.run_controller()
        self.f.payments = [self.f.attempt('complete', amount_sent_msat=100005001)]
        before = self.f.path.read_bytes()
        with self.assertRaises(RuntimeError):
            self.run_controller()
        self.assertEqual(self.f.path.read_bytes(), before)
        self.assertTrue(self.f.held)


if __name__ == '__main__':
    unittest.main()
