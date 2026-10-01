"""Read-only live inspection; fake RPCs and private temporary invoice files."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import reverse_check as check
import reverse_route

A, B, C, D, E = ['02'+f'{i:064x}' for i in range(1, 6)]


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.clis = {role: ['/'+role] for role in ('btc', 'operator', 'payer')}
        self.infos = {role: dict(id=node, network=network) for role, node, network in
                      (('btc', A, 'bitcoin'), ('operator', D, 'xbt'), ('payer', E, 'xbt'))}
        self.decoded = dict(valid=True, type='bolt11 invoice', currency='bc',
                            payee=C, payment_hash='aa'*32, payment_secret='bb'*32,
                            amount_msat=1500000, created_at=1000, expiry=3600,
                            min_final_cltv_expiry=18, features='024100')
        def channel(peer, scid, cid):
            return dict(peer_id=peer, short_channel_id=scid, channel_id=cid,
                        funding_txid='private-txid', funding_outnum=0,
                        state='CHANNELD_NORMAL', peer_connected=True, htlcs=[],
                        spendable_msat=400000000, receivable_msat=400000000,
                        feerate=dict(perkw=1250), dust_limit_msat=546000)
        self.channels = {'payer': [channel(D, '1x1x0', 'xbt-channel')],
                         'operator': [channel(E, '1x1x0', 'xbt-channel')],
                         'btc': [channel(B, '2x1x0', 'btc-channel')]}
        self.route = dict(routes=[dict(amount_msat=1500000, final_cltv=40, path=[
            dict(short_channel_id_dir='2x1x0/0', node_id_in=A, node_id_out=B,
                 amount_in_msat=1505000, amount_out_msat=1505000, cltv_in=46, cltv_out=46),
            dict(short_channel_id_dir='3x1x0/0', node_id_in=B, node_id_out=C,
                 amount_in_msat=1505000, amount_out_msat=1500000, cltv_in=46, cltv_out=40)])])
        self.ticker = dict(success=True, pair='BTCB2_BTC', ticker=dict(
            computedAt=1000000, bestBid='0.0043066', bestAsk='0.00431'))
        self.book = dict(success=True, pair='BTCB2_BTC', bids=[dict(price='0.0043066', quantity='1')])
        self.calls = []
        self.reserve = 50000000
        self.route_error = None
        self.remote_policies = [dict(short_channel_id='3x1x0', source=B,
            destination=C, direction=0, active=True, htlc_minimum_msat=1000,
            htlc_maximum_msat=100000000, base_fee_millisatoshi=5000,
            fee_per_millionth=0, delay=6)]

    def rpc(self, cli, method, *args):
        self.assertIn(method, check.READ_METHODS)
        self.calls.append((method, args))
        role = cli[0][1:]
        if method == 'getinfo':
            return self.infos[role]
        if method == 'decode':
            self.assertEqual(args, ('lnbc-private-invoice',))
            return self.decoded
        if method == 'listpeerchannels':
            return {'channels': self.channels[role]}
        if method == 'listfunds':
            return {'outputs': [dict(status='confirmed', reserved=False, amount_msat=self.reserve)]}
        if method == 'listchannels':
            self.assertEqual(args, ('3x1x0',))
            return {'channels': self.remote_policies}
        if method == 'getroutes':
            if self.route_error:
                raise self.route_error
            self.assertIn('maxparts=1', args)
            self.assertIn('maxfee_msat=10000', args)
            return self.route
        raise AssertionError(method)

    def run_check(self, **kwargs):
        return check.check('lnbc-private-invoice', self.clis, rpc=self.rpc,
                           market_fetch=lambda kind: self.ticker if kind == 'ticker' else self.book,
                           now=lambda: 1000.001, **kwargs)

    def test_remote_limits_exact_boundaries_and_direction_selection(self):
        row = self.remote_policies[0]
        row.update(htlc_minimum_msat=1500000, htlc_maximum_msat=1500000)
        self.remote_policies.append(dict(row, source=C, destination=B,
                                        direction=1, active=False))
        result = self.run_check()
        self.assertTrue(result['feasible'])
        self.assertEqual(result['remote_btc_policy_hops_checked'], 1)
        self.assertEqual(result['remote_btc_policy_hops_unknown'], 0)

    def test_remote_min_max_fee_delay_or_disabled_violation_is_infeasible(self):
        cases = [('htlc_minimum_msat', 1500001),
                 ('htlc_maximum_msat', 1499999), ('active', False),
                 ('base_fee_millisatoshi', 5001), ('delay', 7)]
        original = self.remote_policies[0].copy()
        for key, value in cases:
            self.remote_policies[0] = dict(original, **{key: value})
            result = self.run_check()
            self.assertTrue(result['route_found'])
            self.assertFalse(result['feasible'])
            self.assertFalse(result['remote_btc_htlc_limits_passed'])
            self.assertEqual(len(result['remote_btc_policy_violations']), 1)
            self.assertFalse(result['live_payment_enabled'])

    def test_missing_private_policy_or_wrong_endpoint_is_unknown(self):
        original = self.remote_policies[0].copy()
        for rows in ([], [dict(original, source=C, destination=B, direction=1)],
                     [dict(original, short_channel_id='9x9x9')]):
            self.remote_policies = rows
            result = self.run_check()
            self.assertFalse(result['feasible'])
            self.assertFalse(result['remote_btc_htlc_minima_checked'])
            self.assertEqual(result['remote_btc_policy_hops_unknown'], 1)
            self.assertEqual(result['remote_btc_policy_hops_checked'], 0)
            self.assertEqual(result['remote_btc_policy_violations'], [])

    def test_malformed_or_duplicate_remote_policy_fails_privately(self):
        original = self.remote_policies[0].copy()
        for rows in ([original, original],
                     [dict(original, htlc_minimum_msat=True)],
                     [dict(original, htlc_maximum_msat=None)],
                     [dict(original, htlc_minimum_msat=100000001)],
                     [dict(original, active='PRIVATE')],
                     [dict(original, direction=1)]):
            self.remote_policies = rows
            with self.assertRaises(check.DiagnosticError) as caught:
                self.run_check()
            self.assertEqual(caught.exception.details['stage'], 'btc.remote_policies')
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_remote_policy_transport_error_does_not_become_unknown(self):
        original_rpc = self.rpc
        def failing_rpc(cli, method, *args):
            if method == 'listchannels':
                raise subprocess.CalledProcessError(1, ['PRIVATE'],
                    '{"code":-32603,"message":"PRIVATE"}')
            return original_rpc(cli, method, *args)
        with patch.object(self, 'rpc', side_effect=failing_rpc):
            with self.assertRaises(check.DiagnosticError) as caught:
                self.run_check()
        self.assertEqual(caught.exception.details['rpc_code'], -32603)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_timing_proposal_does_not_claim_live_validation(self):
        result = self.run_check()
        self.assertEqual(result['timing_proposal']['btc_route_delay'], 46)
        self.assertEqual(result['timing_proposal']['minimum_xbt_remaining_blocks'], 196)
        self.assertTrue(result['timing_proposal']['fits_default_cltv_budget'])
        self.assertFalse(result['live_timing_policy_checked'])
        self.assertFalse(result['live_payment_enabled'])

    def test_route_fitting_inspection_can_exceed_timing_budget(self):
        self.route['routes'][0]['path'][0].update(cltv_in=1843, cltv_out=1843)
        self.route['routes'][0]['path'][1]['cltv_in'] = 1843
        result = self.run_check(max_delay=2016)
        self.assertTrue(result['route_found'])
        self.assertFalse(result['feasible'])
        self.assertIn('route exceeds proposed cross-chain timing budget', result['reasons'])

    def test_explicit_delay_cap_reaches_read_only_planner(self):
        result = self.run_check(max_delay=576)
        self.assertEqual(result['inspection_max_delay'], 576)
        self.assertFalse(result['live_payment_enabled'])
        queries = [args for method, args in self.calls if method == 'getroutes']
        self.assertEqual(len(queries), 1)
        self.assertIn('maxdelay=576', queries[0])

    def test_invalid_delay_caps_fail_before_any_rpc(self):
        for cap in (0, 2017, True, 576.0):
            with self.assertRaises(check.CheckError):
                self.run_check(max_delay=cap)
        self.assertEqual(self.calls, [])

    def test_read_only_feasible_result_omits_private_values(self):
        before = copy.deepcopy((self.decoded, self.channels))
        result = self.run_check()
        self.assertTrue(result['feasible'])
        self.assertFalse(result['live_payment_enabled'])
        self.assertTrue(result['remote_btc_htlc_minima_checked'])
        self.assertTrue(result['remote_btc_htlc_limits_passed'])
        self.assertFalse(result['live_timing_policy_checked'])
        self.assertEqual(result['estimated_xbt_sats'], 354131)
        self.assertEqual(result['routing_fee_msat'], 5000)
        self.assertEqual(result['route_hops'], 2)
        output = json.dumps(result)
        for secret in (A, B, C, D, E, 'aa'*32, 'bb'*32, 'lnbc-private-invoice', '1x1x0', 'private-txid'):
            self.assertNotIn(secret, output)
        self.assertEqual(before, (self.decoded, self.channels))

    def test_low_xbt_liquidity_reserve_and_cap_are_explicit(self):
        self.channels['payer'][0]['spendable_msat'] = 1000
        self.reserve = 49999999
        result = self.run_check(max_xbt_sats=300000)
        self.assertFalse(result['feasible'])
        self.assertEqual(len(result['reasons']), 3)
        self.assertTrue(result['route_found'])

    def test_btc_first_hop_liquidity_includes_fee(self):
        self.channels['btc'][0]['spendable_msat'] = 1500000
        result = self.run_check()
        self.assertFalse(result['feasible'])
        self.assertIn('insufficient BTC first-hop liquidity including routing fee', result['reasons'])

    def test_no_route_is_a_private_read_only_diagnostic(self):
        self.route_error = subprocess.CalledProcessError(1, ['private-command'], json.dumps({'code': 205, 'message': C}))
        result = self.run_check()
        self.assertFalse(result['route_found'])
        self.assertFalse(result['feasible'])
        self.assertNotIn(C, json.dumps(result))

    def test_wrong_network_warning_and_xbt_binding_refused(self):
        self.infos['btc']['network'] = 'regtest'
        with self.assertRaises(check.CheckError):
            self.run_check()
        self.infos['btc']['network'] = 'bitcoin'
        self.infos['payer']['warning_bitcoind_sync'] = 'private'
        with self.assertRaises(check.CheckError):
            self.run_check()
        del self.infos['payer']['warning_bitcoind_sync']
        self.channels['payer'][0]['funding_outnum'] = 1
        with self.assertRaises(check.CheckError):
            self.run_check()

    def test_historical_channels_ignored_but_ambiguous_active_refused(self):
        historical = dict(self.channels['operator'][0], state='ONCHAIN')
        self.channels['operator'].append(historical)
        self.assertTrue(self.run_check()['feasible'])
        historical['state'] = 'CHANNELD_NORMAL'
        with self.assertRaises(check.CheckError):
            self.run_check()

    def test_pending_or_disconnected_xbt_channel_refused(self):
        for key, value in (('peer_connected', False), ('htlcs', [{'private': True}])):
            old = self.channels['payer'][0][key]
            self.channels['payer'][0][key] = value
            with self.assertRaises(check.CheckError):
                self.run_check()
            self.channels['payer'][0][key] = old

    def test_invoice_network_features_amount_expiry_and_secret_refused(self):
        for key, value in (('currency', 'bcrt'), ('type', 'bolt12 invoice'), ('valid', False),
                           ('amount_msat', 1500001), ('amount_msat', 10000001),
                           ('payment_secret', None), ('expiry', 10), ('features', '1000000'),
                           ('payment_metadata', 'secret'), ('payee', A), ('min_final_cltv_expiry', 145)):
            changed = dict(self.decoded, **{key: value})
            with patch.object(self, 'decoded', changed), self.subTest(key=key), self.assertRaises(check.CheckError):
                self.run_check()

    def test_optional_or_required_metadata_accepted_without_exposure(self):
        private_metadata = 'deadbeefcafefeed'
        self.decoded['payment_metadata'] = private_metadata
        for feature in (0, 1 << 49, 1 << 48):
            self.decoded['features'] = format(0x24100 | feature, 'x')
            result = self.run_check()
            self.assertTrue(result['feasible'])
            self.assertTrue(result['payment_metadata_present'])
            self.assertFalse(result['live_payment_enabled'])
            self.assertNotIn(private_metadata, json.dumps(result))
            self.assertTrue(all(method in check.READ_METHODS for method, _ in self.calls))

    def test_metadata_hex_and_size_boundaries(self):
        for metadata in ('', 'AA', 'ab'*512):
            self.decoded['payment_metadata'] = metadata
            self.assertTrue(self.run_check()['payment_metadata_present'])
        for metadata in ('a', 'zz', 'ab'*513, True, 1, ['ab']):
            self.decoded['payment_metadata'] = metadata
            with self.subTest(metadata_type=type(metadata)), self.assertRaises(check.CheckError):
                self.run_check()

    def test_required_metadata_missing_or_unknown_required_feature_refused(self):
        self.decoded['features'] = format(0x24100 | (1 << 48), 'x')
        with self.assertRaises(check.CheckError):
            self.run_check()
        self.decoded['payment_metadata'] = 'ab'
        self.decoded['features'] = format(0x24100 | (1 << 48) | (1 << 50), 'x')
        with self.assertRaises(check.CheckError):
            self.run_check()

    def test_route_fee_and_hop_continuity_not_trusted(self):
        self.route['routes'][0]['path'][0]['amount_out_msat'] += 1
        with self.assertRaises(ValueError):
            self.run_check()

    def test_inspection_does_not_relax_default_regtest_validation(self):
        with self.assertRaises(ValueError):
            reverse_route.plan(self.clis['btc'], self.decoded, A, self.rpc)
        policy = dict(source=A, destination=C, max_fee_msat=10000,
                      max_delay=288, max_hops=8, final_cltv=40)
        with self.assertRaises(ValueError):
            reverse_route.validate([], 1500000, policy)

    def test_private_hint_live_amount_prefix_and_final_delay(self):
        decoded = dict(self.decoded, min_final_cltv_expiry=144, routes=[[
            dict(pubkey=B, short_channel_id='8000000x1x0', fee_base_msat=5000,
                 fee_proportional_millionths=0, cltv_expiry_delta=6)]])
        calls = []
        def rpc(cli, method, *args):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, ['/btc'], '{"code":205}')
            return dict(routes=[dict(amount_msat=1505000, final_cltv=150, path=[dict(
                short_channel_id_dir='2x1x0/0', node_id_in=A, node_id_out=B,
                amount_in_msat=1505000, amount_out_msat=1505000, cltv_in=150, cltv_out=150)])])
        route, policy = reverse_route.plan(['/btc'], decoded, A, rpc,
                                          max_delay=288, max_hops=8, _inspection=True)
        self.assertEqual(route[-1]['channel'], '8000000x1x0')
        self.assertEqual(route[-1]['delay'], 144)
        self.assertEqual(reverse_route.validate(route, 1500000, policy, _inspection=True), 5000)
        self.assertIn('maxfee_msat=5000', calls[1])

    def test_rpc_diagnostic_reports_code_not_command_or_response(self):
        self.route_error = subprocess.CalledProcessError(1, ['PRIVATE-INVOICE'],
            json.dumps({'code': -32601, 'message': 'PRIVATE-RESPONSE'}), 'PRIVATE-STDERR')
        with self.assertRaises(check.DiagnosticError) as caught:
            self.run_check()
        error = caught.exception
        self.assertEqual(error.details['stage'], 'btc.route_planning')
        self.assertEqual(error.details['rpc_code'], -32601)
        self.assertNotIn('PRIVATE', json.dumps(error.details)+str(error))

    def test_market_validation_and_channel_data_stages(self):
        self.ticker['ticker']['computedAt'] = 1
        with self.assertRaises(check.DiagnosticError) as caught:
            self.run_check()
        self.assertEqual(caught.exception.details['stage'], 'market.validation')
        self.assertEqual(caught.exception.details['validation'], 'stale or future ticker timestamp')
        self.ticker['ticker']['computedAt'] = 1000000
        del self.channels['payer'][0]['feerate']
        with self.assertRaises(check.DiagnosticError) as caught:
            self.run_check()
        self.assertEqual(caught.exception.details['stage'], 'xbt_channels_and_reserves')
        self.assertEqual(caught.exception.details['error_type'], 'KeyError')

    def test_arbitrary_exception_text_is_not_a_safe_validation_message(self):
        error = check.DiagnosticError('market.validation', ValueError('PRIVATE'))
        self.assertNotIn('validation', error.details)
        self.assertNotIn('PRIVATE', str(error))

    def test_cli_error_does_not_print_private_rpc_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'invoice'
            path.write_text('lnbc-private-invoice')
            path.chmod(0o600)
            with patch('sys.argv', ['reverse_check.py', '--invoice-file', str(path)]), \
                    patch('reverse_check.check', side_effect=subprocess.CalledProcessError(1, ['SECRET'], 'SECRET')), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(check.main(), 1)
                self.assertNotIn('SECRET', output.getvalue())

    def test_private_file_permissions_symlink_and_multiple_invoices_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'invoice'
            path.write_text('lnbc-private-invoice\n')
            path.chmod(0o600)
            self.assertEqual(check.private_invoice(path), 'lnbc-private-invoice')
            alias = Path(directory)/'alias'
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                check.private_invoice(alias)
            path.chmod(0o644)
            with self.assertRaises(check.CheckError):
                check.private_invoice(path)
            path.chmod(0o600)
            path.write_text('lnbc-one\nlnbc-two')
            with self.assertRaises(check.CheckError):
                check.private_invoice(path)


if __name__ == '__main__':
    unittest.main()
