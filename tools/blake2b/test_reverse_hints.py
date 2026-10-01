"""Private BOLT11 tails, bounded public prefixes and fail-closed planning."""
import json
import subprocess
import unittest
from unittest.mock import Mock

from reverse_route import plan, validate

A, B, C, D = ['02'+f'{i:064x}' for i in range(1, 5)]


def hint(node=B, channel='8000000x1x0', base=5000, ppm=0, delta=6):
    return dict(pubkey=node, short_channel_id=channel, fee_base_msat=base,
                fee_proportional_millionths=ppm, cltv_expiry_delta=delta)


def decoded(hints=None):
    return dict(valid=True, type='bolt11 invoice', currency='bcrt', payment_secret='22'*32,
                min_final_cltv_expiry=18, payee=C, amount_msat=100000000,
                routes=[[hint()]] if hints is None else hints)


def unavailable(code=205, output=None):
    return subprocess.CalledProcessError(1, ['/btc', 'getroutes'],
                                        json.dumps({'code': code}) if output is None else output)


def prefix(source=A, destination=B, amount=100005000, delay=46):
    return dict(routes=[dict(amount_msat=amount, final_cltv=delay, path=[dict(
        short_channel_id_dir='1x1x0/'+str(int(source > destination)),
        node_id_in=source, node_id_out=destination, amount_in_msat=amount,
        amount_out_msat=amount, cltv_in=delay, cltv_out=delay)])])


class HintTests(unittest.TestCase):
    def test_inspection_368_block_hint_route_and_exact_boundary(self):
        invoice = decoded([[hint(D, delta=144)]])
        invoice.update(currency='bc', amount_msat=1500000,
                       min_final_cltv_expiry=144)
        public_prefix = dict(routes=[dict(amount_msat=1505000,
            final_cltv=288, path=[
                dict(short_channel_id_dir='1x1x0/0', node_id_in=A,
                     node_id_out=B, amount_in_msat=1505000,
                     amount_out_msat=1505000, cltv_in=368, cltv_out=368),
                dict(short_channel_id_dir='2x1x0/0', node_id_in=B,
                     node_id_out=D, amount_in_msat=1505000,
                     amount_out_msat=1505000, cltv_in=368, cltv_out=288)])])
        for cap in (368, 576):
            rpc = Mock(side_effect=[unavailable(206), public_prefix])
            route, policy = plan(['/btc'], invoice, A, rpc,
                                 max_delay=cap, _inspection=True)
            self.assertEqual(route[0]['delay'], 368)
            self.assertEqual(route[-1]['delay'], 144)
            self.assertEqual(validate(route, 1500000, policy,
                                      _inspection=True), 5000)
            self.assertIn('final_cltv=288', rpc.call_args.args)
            self.assertIn('maxdelay='+str(cap), rpc.call_args.args)
        rpc = Mock(side_effect=[unavailable(206), public_prefix])
        with self.assertRaises(ValueError):
            plan(['/btc'], invoice, A, rpc, max_delay=367, _inspection=True)

    def test_inspection_prefix_206_tries_next_hint_without_relaxing_caps(self):
        invoice = decoded([[hint()], [hint()]])
        invoice.update(currency='bc', amount_msat=1500000)
        rpc = Mock(side_effect=[unavailable(206), unavailable(206),
                               prefix(amount=1505000)])
        route, policy = plan(['/btc'], invoice, A, rpc,
                             max_delay=576, _inspection=True)
        self.assertEqual(len(route), 2)
        self.assertEqual(rpc.call_count, 3)
        for call in rpc.call_args_list:
            self.assertIn('maxdelay=576', call.args)
            self.assertEqual(call.args[1], 'getroutes')

    def test_inspection_transport_and_other_errors_never_fallback(self):
        invoice = decoded()
        invoice.update(currency='bc', amount_msat=1500000)
        for code in (215, 500, -32603):
            rpc = Mock(side_effect=unavailable(code))
            with self.assertRaises(subprocess.CalledProcessError):
                plan(['/btc'], invoice, A, rpc, max_delay=576, _inspection=True)
            rpc.assert_called_once()

    def test_private_tail_fee_delay_and_residual_budget(self):
        rpc = Mock(side_effect=[unavailable(), prefix()])
        route, policy = plan(['/btc'], decoded(), A, rpc)
        self.assertEqual(route, [dict(id=B, channel='1x1x0', amount_msat=100005000, delay=46),
                                 dict(id=C, channel='8000000x1x0', amount_msat=100000000, delay=40)])
        self.assertEqual(validate(route, 100000000, policy), 5000)
        args = rpc.call_args.args
        for arg in ('destination='+B, 'amount_msat=100005000', 'maxfee_msat=5000',
                    'maxdelay=80', 'final_cltv=46', 'maxparts=1'):
            self.assertIn(arg, args)
        self.assertTrue(all(c.args[1] == 'getroutes' for c in rpc.call_args_list))

    def test_source_hint_is_source_free(self):
        rpc = Mock(side_effect=unavailable())
        route, policy = plan(['/btc'], decoded([[hint(node=A, base=0xffffffff, delta=65535)]]), A, rpc)
        self.assertEqual(route, [dict(id=C, channel='8000000x1x0', amount_msat=100000000, delay=40)])
        self.assertEqual(validate(route, 100000000, policy), 0)
        rpc.assert_called_once()

    def test_multihop_compounds_fees_with_integer_floor(self):
        invoice = decoded([[hint(B, '2x1x0', base=1, ppm=3, delta=7),
                            hint(D, '3x1x0', base=1, ppm=2, delta=8)]])
        # D charges 201msat. B charges 1 + floor(100000201*3/1e6) = 301.
        rpc = Mock(side_effect=[unavailable(), prefix(amount=100000502, delay=55)])
        route, policy = plan(['/btc'], invoice, A, rpc)
        self.assertEqual([h['amount_msat'] for h in route], [100000502, 100000201, 100000000])
        self.assertEqual([h['delay'] for h in route], [55, 48, 40])
        self.assertEqual([h['id'] for h in route], [B, D, C])
        self.assertEqual(validate(route, 100000000, policy), 502)

    def test_exact_tail_fee_and_delay_boundary(self):
        rpc = Mock(side_effect=[unavailable(), prefix(amount=100010000, delay=80)])
        route, policy = plan(['/btc'], decoded([[hint(base=10000, delta=40)]]), A, rpc)
        self.assertEqual(validate(route, 100000000, policy), 10000)
        self.assertIn('maxfee_msat=0', rpc.call_args.args)
        for changes in ({'base': 10001}, {'delta': 41}):
            rpc = Mock(side_effect=unavailable())
            with self.assertRaises(subprocess.CalledProcessError):
                plan(['/btc'], decoded([[hint(**changes)]]), A, rpc)
            rpc.assert_called_once()  # Never queries an over-budget prefix.

    def test_invalid_hints_do_not_trigger_prefix_queries(self):
        for key, value in [('pubkey', 'bad'), ('fee_base_msat', -1), ('fee_base_msat', True),
                           ('fee_proportional_millionths', 2**32), ('cltv_expiry_delta', -1),
                           ('cltv_expiry_delta', 65536), ('short_channel_id', '16777216x0x0'),
                           ('short_channel_id', '1x1x65536'), ('pubkey', C)]:
            h = hint()
            h[key] = value
            rpc = Mock(side_effect=unavailable())
            with self.subTest(key=key, value=value), self.assertRaises(subprocess.CalledProcessError):
                plan(['/btc'], decoded([[h]]), A, rpc)
            rpc.assert_called_once()

    def test_hint_loops_duplicates_and_excess_hops_refused(self):
        for hints in ([hint(), hint()], [hint(), hint(A, '2x1x0')], [],
                      [hint(), hint(D, '8000000x1x0')]):
            rpc = Mock(side_effect=unavailable())
            with self.assertRaises(subprocess.CalledProcessError):
                plan(['/btc'], decoded([hints]), A, rpc)
            rpc.assert_called_once()
        rpc = Mock(side_effect=unavailable())
        with self.assertRaises(subprocess.CalledProcessError):
            plan(['/btc'], decoded(), A, rpc, max_hops=1)
        rpc.assert_called_once()

    def test_bounded_alternative_hints(self):
        rpc = Mock(side_effect=[unavailable(), unavailable(), prefix()])
        route, _ = plan(['/btc'], decoded([[hint(D)], [hint()]]), A, rpc)
        self.assertEqual(route[0]['id'], B)
        self.assertEqual(rpc.call_count, 3)
        for hints in ([[hint()]]*9, 'not an array'):
            rpc.reset_mock()
            with self.assertRaises(ValueError):
                plan(['/btc'], decoded(hints), A, rpc)
            rpc.assert_not_called()

    def test_public_route_remains_preferred(self):
        rpc = Mock(return_value=prefix(destination=C, amount=100000000, delay=40))
        route, _ = plan(['/btc'], decoded(), A, rpc)
        self.assertEqual(route[-1]['channel'], '1x1x0')
        rpc.assert_called_once()

    def test_transport_timeout_and_other_rpc_failures_are_not_fallbacks(self):
        for error in (unavailable(999), unavailable(output='not JSON'),
                      subprocess.TimeoutExpired(['/btc'], 20)):
            for prefix_failure in (False, True):
                rpc = Mock(side_effect=[unavailable(), error] if prefix_failure else error)
                with self.assertRaises(type(error)):
                    plan(['/btc'], decoded(), A, rpc)
                self.assertEqual(rpc.call_count, 2 if prefix_failure else 1)

    def test_prefix_fee_budget_and_continuity_remain_enforced(self):
        for key, value in [('amount_msat', 100000000), ('final_cltv', 45)]:
            result = prefix()
            result['routes'][0][key] = value
            rpc = Mock(side_effect=[unavailable(), result])
            with self.assertRaises(ValueError):
                plan(['/btc'], decoded(), A, rpc)
        result = prefix()
        result['routes'][0]['path'][0]['amount_in_msat'] += 1
        rpc = Mock(side_effect=[unavailable(), result])
        with self.assertRaises(ValueError):
            plan(['/btc'], decoded(), A, rpc)

    def test_public_prefix_fees_share_total_budget(self):
        for prefix_fee in (5000, 5001):
            result = prefix()
            first = prefix(destination=D, amount=100005000+prefix_fee, delay=52)['routes'][0]['path'][0]
            second = prefix(source=D)['routes'][0]['path'][0]
            second.update(short_channel_id_dir='2x1x0/1', amount_in_msat=100005000+prefix_fee, cltv_in=52)
            result['routes'][0]['path'] = [first, second]
            rpc = Mock(side_effect=[unavailable(), result])
            if prefix_fee == 5000:
                route, policy = plan(['/btc'], decoded(), A, rpc)
                self.assertEqual(validate(route, 100000000, policy), 10000)
            else:
                with self.assertRaises(ValueError):
                    plan(['/btc'], decoded(), A, rpc)

    def test_prefix_cannot_intersect_private_tail(self):
        result = prefix()
        # Public prefix A -> C -> B would revisit destination C on the tail.
        result['routes'][0]['path'] = [
            prefix(destination=C)['routes'][0]['path'][0],
            prefix(source=C)['routes'][0]['path'][0]]
        result['routes'][0]['path'][1]['short_channel_id_dir'] = '2x1x0/1'
        rpc = Mock(side_effect=[unavailable(), result])
        with self.assertRaises(subprocess.CalledProcessError):
            plan(['/btc'], decoded(), A, rpc)

    def test_external_validation_does_not_allow_prefix_bounds(self):
        rpc = Mock(side_effect=[unavailable(), prefix()])
        route, policy = plan(['/btc'], decoded(), A, rpc)
        for amount, modified in ((100005000, policy), (100000000, dict(policy, final_cltv=46))):
            with self.assertRaises(ValueError):
                validate(route, amount, modified)
        for key, value in [('currency', 'bc'), ('valid', False), ('amount_msat', 100005000),
                           ('min_final_cltv_expiry', True), ('min_final_cltv_expiry', 41)]:
            rpc.reset_mock()
            with self.assertRaises(ValueError):
                plan(['/btc'], dict(decoded(), **{key: value}), A, rpc)
            rpc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
