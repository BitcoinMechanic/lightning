#!/usr/bin/env python3
"""Read-only planner boundaries, not funded daemon evidence."""
import ast
import copy
import hashlib
from pathlib import Path
import unittest

import chain_identity as ci
import held_gate as hg
import route_plan as rp
from test_chain_identity import info, decoded, NODE, vector

A, B, C = ('02' + c * 64 for c in ('2', '3', '4'))
NOW, AMOUNT = 1000000, 2000000
BOUNDS = dict(max_fee_msat=10000, max_delay_blocks=288, max_hops=4)
PREIMAGE = '55' * 32


def pin(profile=ci.BTC):
    return dict(channel_id='66'*32, short_channel_id='110x1x0', funding_txid='77'*32,
                funding_outnum=0, peer_id=A, channel_type_bits=[12, 22] + ([514] if profile == ci.PRIVKEYIO_XBT else []))


def row(profile=ci.BTC):
    p = pin(profile)
    bits = p.pop('channel_type_bits')
    return dict(p, channel_type=dict(bits=bits), state='CHANNELD_NORMAL', peer_connected=True,
                reestablished=True, htlcs=[], spendable_msat=10000000)


def planner_reply(route, source=NODE):
    path, previous = [], None
    for h in route:
        path.append(dict(short_channel_id_dir=h['channel']+'/'+str(int(source > h['id'])),
                         node_id_in=source, node_id_out=h['id'], amount_out_msat=h['amount_msat'], cltv_out=h['delay'],
                         amount_in_msat=previous['amount_msat'] if previous else h['amount_msat'],
                         cltv_in=previous['delay'] if previous else h['delay']))
        source, previous = h['id'], h
    return dict(routes=[dict(amount_msat=route[-1]['amount_msat'], final_cltv=route[-1]['delay'], path=path)])


class Fake:
    def __init__(self, profile=ci.BTC):
        self.profile = profile
        self.chain = ci.binding(profile, 'regtest', NODE)
        self.info = info(profile, 'regtest')
        self.decoded = dict(decoded(profile, 'regtest'), payee=C, amount_msat=AMOUNT,
                            payment_hash=hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest(),
                            created_at=NOW-10, expiry=1800, min_final_cltv_expiry=40)
        self.rows, self.calls, self.payments = [row(profile)], [], []
        self.route = [dict(id=A, channel='110x1x0', amount_msat=2002000, delay=200),
                      dict(id=B, channel='111x1x0', amount_msat=2001000, delay=120),
                      dict(id=C, channel='112x1x0', amount_msat=AMOUNT, delay=40)]
        self.answers = []

    def __call__(self, method, **params):
        self.calls.append((method, copy.deepcopy(params)))
        if method not in rp.READ_METHODS:
            raise AssertionError('unexpected write RPC')
        if method == 'getinfo': return copy.deepcopy(self.info)
        if method == 'decode': return copy.deepcopy(self.decoded)
        if method == 'listpeerchannels': return dict(channels=copy.deepcopy(self.rows))
        if method == 'listsendpays': return dict(payments=copy.deepcopy(self.payments))
        result = self.answers.pop(0) if self.answers else planner_reply(self.route)
        if isinstance(result, Exception): raise result
        return copy.deepcopy(result)

    def plan(self, bounds=None, pins=None):
        return rp.plan(self, self.chain, 'lnbcrt1synthetic-decoded-by-fake',
                       pins if pins is not None else [pin(self.profile)], bounds or BOUNDS, clock=lambda:NOW)

    def hinted(self):
        self.decoded['routes'] = [[dict(pubkey=B, short_channel_id='112x1x0', fee_base_msat=1000,
                                       fee_proportional_millionths=0, cltv_expiry_delta=80)]]
        self.answers = [rp.RPCError(205), planner_reply(self.route[:-1])]

    def attempt(self, plan):
        return dict(payment_hash=plan['payment_hash'], groupid=1, destination=C, amount_msat=AMOUNT,
                    amount_sent_msat=2002000, created_index=7, created_at=NOW+1, status='pending', bolt11=plan['invoice'])


class PlannerTests(unittest.TestCase):
    def test_both_protocols_private_tail_200_blocks_exact_fees(self):
        for profile in (ci.BTC, ci.PRIVKEYIO_XBT):
            rpc = Fake(profile); rpc.hinted(); result = rpc.plan()
            self.assertEqual(result['route'], rpc.route)
            self.assertEqual(result['channel'], pin(profile))
            self.assertEqual(result['chain'], rpc.chain)
            queries = [p for m,p in rpc.calls if m == 'getroutes']
            self.assertEqual(len(queries), 2)
            self.assertEqual(queries[1]['amount_msat'], 2001000)
            self.assertEqual(queries[1]['final_cltv'], 120)
            self.assertEqual(queries[1]['maxfee_msat'], 9000)
            self.assertTrue(all(p['maxparts'] == 1 and p['maxdelay'] == 288 for p in queries))
            self.assertEqual(queries[0]['layers'], ['auto.localchans', 'auto.sourcefree'])
            self.assertEqual(rp.digest(result), rp.digest(copy.deepcopy(result)))

    def test_shared_names_do_not_authorize_wrong_profile(self):
        for profile in (ci.BTC, ci.PRIVKEYIO_XBT):
            other = ci.PRIVKEYIO_XBT if profile == ci.BTC else ci.BTC
            for field in ('info', 'decoded'):
                rpc = Fake(profile)
                setattr(rpc, field, getattr(Fake(other), field))
                with self.assertRaises(ValueError): rpc.plan()
                self.assertFalse(any(m == 'getroutes' for m,_ in rpc.calls))

    def test_legacy_mainnet_and_missing_signing_type_refused(self):
        for profile, environment in ((ci.LEGACY_XBT, 'regtest'), (ci.BTC, 'mainnet')):
            rpc = Fake(); rpc.chain = ci.binding(profile, environment, NODE)
            with self.assertRaises(ValueError): rpc.plan()
            self.assertEqual(rpc.calls, [])
        rpc = Fake(ci.PRIVKEYIO_XBT); rpc.rows[0]['channel_type']['bits'].remove(514)
        with self.assertRaises(ValueError): rpc.plan()
        self.assertFalse(any(m == 'getroutes' for m,_ in rpc.calls))

    def test_amount_timing_final_cltv_limits_before_routing(self):
        for updates in ({'amount_msat':True}, {'amount_msat':1000000001}, {'expiry':1},
                        {'created_at':NOW+1}, {'min_final_cltv_expiry':80}, {'payment_secret':None}):
            rpc = Fake(); rpc.decoded.update(updates)
            with self.assertRaises(ValueError): rpc.plan()
            self.assertFalse(any(m == 'getroutes' for m,_ in rpc.calls))

    def test_no_widening_fee_delay_or_hop_limits(self):
        for bounds in (dict(BOUNDS,max_fee_msat=1999), dict(BOUNDS,max_delay_blocks=144), dict(BOUNDS,max_hops=2)):
            rpc = Fake()
            with self.assertRaises(ValueError): rpc.plan(bounds)
            self.assertEqual(sum(m == 'getroutes' for m,_ in rpc.calls), 1)
        for key in BOUNDS:
            rpc = Fake()
            with self.assertRaises(ValueError): rpc.plan(dict(BOUNDS, **{key:True}))
            self.assertEqual(rpc.calls, [])

    def test_hint_fee_delay_and_prefix_intersection(self):
        rpc = Fake(); rpc.hinted(); rpc.decoded['routes'][0][0]['cltv_expiry_delta'] = 300
        with self.assertRaisesRegex(ValueError,'bounded_route_unavailable'): rpc.plan()
        self.assertEqual(sum(m == 'getroutes' for m,_ in rpc.calls), 1)
        rpc = Fake(); rpc.hinted()
        bad_prefix = [dict(id=C, channel='110x1x0', amount_msat=2002000, delay=200), rpc.route[1]]
        rpc.answers[1] = planner_reply(bad_prefix)
        with self.assertRaisesRegex(ValueError,'bounded_route_unavailable'): rpc.plan()

    def test_hint_source_is_free(self):
        rpc = Fake(); rpc.decoded['payee'] = A
        rpc.decoded['routes'] = [[dict(pubkey=NODE, short_channel_id='110x1x0', fee_base_msat=10000,
                                      fee_proportional_millionths=1000000, cltv_expiry_delta=1000)]]
        rpc.answers = [rp.RPCError(205)]
        result = rpc.plan()
        self.assertEqual(result['route'], [dict(id=A,channel='110x1x0',amount_msat=AMOUNT,delay=40)])
        self.assertEqual(sum(m == 'getroutes' for m,_ in rpc.calls), 1)

    def test_transport_unknown_and_insufficient_funds_stop(self):
        for error in (rp.RPCError(215), rp.RPCError(-32601), rp.RPCError(), TimeoutError()):
            rpc = Fake(); rpc.hinted(); rpc.answers[0] = error
            with self.assertRaises(type(error)): rpc.plan()
            self.assertEqual(sum(m == 'getroutes' for m,_ in rpc.calls), 1)
        rpc = Fake(); rpc.hinted(); rpc.answers[1] = rp.RPCError(215)
        with self.assertRaises(rp.RPCError): rpc.plan()

    def test_multipart_discontinuous_direction_and_source_fee_refused(self):
        for variant in ('mpp','direction','source_fee','discontinuous','boolean','overfee','loop'):
            rpc = Fake(); reply = planner_reply(rpc.route); path = reply['routes'][0]['path']
            if variant == 'mpp': reply['routes'] *= 2
            if variant == 'direction': path[0]['short_channel_id_dir'] = '110x1x0/1'
            if variant == 'source_fee': path[0]['amount_in_msat'] += 1
            if variant == 'discontinuous': path[1]['amount_in_msat'] += 1
            if variant == 'boolean': path[0]['cltv_in'] = True
            if variant == 'overfee': path[0]['amount_in_msat'] = path[0]['amount_out_msat'] = 2020000
            if variant == 'loop': path[-1]['short_channel_id_dir'] = path[0]['short_channel_id_dir']
            rpc.answers = [reply]
            with self.subTest(variant=variant), self.assertRaises(ValueError): rpc.plan()

    def test_actual_first_pin_liquidity_and_channel_readiness(self):
        for change in ({'funding_txid':'88'*32}, {'spendable_msat':AMOUNT}, {'htlcs':[{}]},
                       {'reestablished':False}, {'lost_state':True}, {'peer_connected':False}):
            rpc = Fake(); rpc.rows[0].update(change)
            with self.assertRaises(ValueError): rpc.plan()
        rpc = Fake(); rpc.route[0]['channel'] = '110x2x0'
        with self.assertRaisesRegex(ValueError,'route_outside_approved_channels'): rpc.plan()

    def test_plans_are_detached_and_legacy_records_not_upgraded(self):
        rpc = Fake(); result = rpc.plan(); saved = rp.digest(result)
        rpc.route[0]['delay'] = 288; rpc.rows[0]['funding_txid'] = '88'*32
        self.assertEqual(rp.digest(result), saved)
        for change in ({'format':'startos-forward-routed-v1'}, {'schema':True}, {'unexpected':0}):
            with self.assertRaises(ValueError): rp.validate_plan(dict(result, **change))
        changed = copy.deepcopy(result); del changed['channel']['channel_type_bits']
        with self.assertRaises(ValueError): rp.validate_plan(changed)

    def test_observation_preserves_attempt_after_lost_reply_and_expiry(self):
        rpc = Fake(); plan = rpc.plan(); rpc.payments = [rpc.attempt(plan)]
        first = rp.observe_attempt(rpc, plan)
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(first['identity']['partid'], 0)
        rpc.payments[0].update(status='complete',payment_preimage=PREIMAGE)
        second = rp.observe_attempt(rpc, plan, first['identity'])
        self.assertEqual(second['identity'], first['identity'])
        self.assertEqual(second['preimage'], PREIMAGE)
        self.assertEqual(second['state'], 'complete')
        rpc.payments[0]['created_index'] += 1
        with self.assertRaisesRegex(ValueError,'attempt_changed'): rp.observe_attempt(rpc,plan,first['identity'])

    def test_empty_rows_are_uncertain_failed_and_preimage_checks(self):
        rpc = Fake(); plan = rpc.plan()
        self.assertEqual(rp.observe_attempt(rpc, plan), {'state':'not_observed'})
        with self.assertRaisesRegex(ValueError,'original_attempt_missing'): rp.observe_attempt(rpc,plan,{})
        base = rpc.attempt(plan)
        for changes in ({'groupid':2}, {'partid':1}, {'partid':True}, {'amount_msat':1}, {'amount_sent_msat':AMOUNT},
                        {'bolt11':'other'}, {'status':'complete','payment_preimage':'00'*32},
                        {'status':'failed','payment_preimage':PREIMAGE}):
            rpc.payments = [dict(base, **changes)]
            with self.assertRaises(ValueError): rp.observe_attempt(rpc,plan)
        rpc.payments = [base,base]
        with self.assertRaisesRegex(ValueError,'one_attempt_required'): rp.observe_attempt(rpc,plan)
        rpc.payments = [dict(base,status='failed')]
        self.assertEqual(rp.observe_attempt(rpc,plan)['state'],'failed')

    def test_adapter_contains_no_mutation_rpc(self):
        tree = ast.parse(Path(rp.__file__).read_text())
        methods = {n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call)
                   and isinstance(n.func,ast.Name) and n.func.id == 'rpc' and n.args
                   and isinstance(n.args[0],ast.Constant)}
        self.assertEqual(methods, rp.READ_METHODS)


if __name__ == '__main__': unittest.main()
