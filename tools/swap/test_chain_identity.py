#!/usr/bin/env python3
"""Protocol boundary regressions; no node, network, wallet or credentials."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chain_identity as ci
import bolt11

NODE = '02' + '11' * 32
OTHER = '03' + '22' * 32
HASH = '33' * 32
SECRET = '44' * 32


def vector(*bits):
    value = sum(1 << b for b in bits)
    return value.to_bytes((value.bit_length() + 7) // 8, 'big').hex()


def info(profile, environment='mainnet'):
    fork = profile == ci.PRIVKEYIO_XBT
    return dict(id=NODE, network=ci.wire(profile, environment)[0], blockheight=1000000,
                our_features=dict(init=vector(8, 14, *([512, 515] if fork else [])),
                                  node=vector(8, 14, *([512, 515] if fork else [])),
                                  channel='', invoice=vector(8, 14, *([512] if fork else []))))


def decoded(profile, environment='mainnet'):
    return dict(valid=True, type='bolt11 invoice', currency=ci.wire(profile, environment)[1],
                features=vector(8, 14, 17, *([512] if profile == ci.PRIVKEYIO_XBT else [])),
                amount_msat=1500000, payment_hash=HASH, payment_secret=SECRET, payee=OTHER)


class IdentityTests(unittest.TestCase):
    def refuses(self, code, fn, *args, **kwargs):
        with self.assertRaisesRegex(ci.IdentityError, '^' + code + '$'):
            fn(*args, **kwargs)

    def test_all_explicit_profiles_and_environments(self):
        for profile in ci.PROFILES:
            for environment in ci.ENVIRONMENTS:
                with self.subTest(profile=profile, environment=environment):
                    original = info(profile, environment)
                    before = copy.deepcopy(original)
                    result = ci.node(original, profile, environment, expected_id=NODE)
                    self.assertEqual(result, ci.binding(profile, environment, NODE))
                    self.assertEqual(original, before)
                    self.assertIs(ci.invoice(decoded(profile, environment), profile, environment).__class__, dict)

    def test_same_network_and_hrp_do_not_make_same_chain(self):
        self.assertEqual(ci.wire(ci.BTC, 'mainnet'), ci.wire(ci.PRIVKEYIO_XBT, 'mainnet'))
        for wanted, actual in ((ci.BTC, ci.PRIVKEYIO_XBT), (ci.PRIVKEYIO_XBT, ci.BTC)):
            with self.subTest(wanted=wanted):
                with self.assertRaises(ci.IdentityError): ci.node(info(actual), wanted, 'mainnet')
                with self.assertRaises(ci.IdentityError): ci.invoice(decoded(actual), wanted, 'mainnet')

    def test_optional_blake_bit_is_not_chain_identity(self):
        for bits in ((513,), (512, 513), (515,), ()):
            for place in ('init', 'node', 'invoice'):
                with self.subTest(bits=bits, place=place):
                    value = info(ci.PRIVKEYIO_XBT)
                    value['our_features'][place] = vector(*bits)
                    with self.assertRaises(ci.IdentityError): ci.node(value, ci.PRIVKEYIO_XBT, 'mainnet')

    def test_missing_unified_capability_and_wrong_context_refused(self):
        for place, bits in (('init', (512,)), ('node', (512, 514)), ('invoice', (512, 515))):
            value = info(ci.PRIVKEYIO_XBT)
            value['our_features'][place] = vector(*bits)
            with self.assertRaises(ci.IdentityError): ci.node(value, ci.PRIVKEYIO_XBT, 'mainnet')

    def test_malformed_and_missing_feature_vectors_refused(self):
        for value in (None, True, 512, [], {}, '1', '0x01', ' 01', 'gg', '00' * 8193):
            with self.subTest(value=type(value).__name__):
                self.refuses('invalid_feature_vector', ci.feature_bits, value)
        value = info(ci.PRIVKEYIO_XBT)
        del value['our_features']
        self.refuses('node_features_missing', ci.node, value, ci.PRIVKEYIO_XBT, 'mainnet')
        value['features'] = dict(init=vector(512, 515))
        self.refuses('node_features_missing', ci.node, value, ci.PRIVKEYIO_XBT, 'mainnet')

    def test_bit_order_and_leading_zeros(self):
        self.assertEqual(ci.feature_bits('01' + '00' * 64), 1 << 512)
        self.assertEqual(ci.feature_bits('0001' + '00' * 64), 1 << 512)
        self.assertEqual(ci.feature_bits(''), 0)
        self.assertEqual(ci.feature_bits('AB'), 171)

    def test_identity_height_warning_and_network_refused(self):
        base = info(ci.PRIVKEYIO_XBT)
        for changes in ({'id':OTHER}, {'network':'xbt'}, {'blockheight':True},
                        {'blockheight':-1}, {'warning_bitcoind_sync':'secret raw error'}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ci.IdentityError) as caught:
                    ci.node(dict(base, **changes), ci.PRIVKEYIO_XBT, 'mainnet', expected_id=NODE)
                self.assertNotIn('secret', str(caught.exception))
        ci.node(dict(base, warning_bitcoind_sync=None), ci.PRIVKEYIO_XBT, 'mainnet')
        self.refuses('wrong_node_network', ci.node, base, ci.PRIVKEYIO_XBT, 'regtest')

    def test_invoice_currency_features_and_fields(self):
        base = decoded(ci.PRIVKEYIO_XBT)
        for changes in ({'valid':1}, {'currency':'xbt'}, {'currency':'bcrt'},
                        {'features':vector(513)}, {'features':vector(512, 515)},
                        {'amount_msat':True}, {'amount_msat':0}, {'payment_hash':'bad'},
                        {'payment_secret':None}, {'payee':'02'}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ci.IdentityError):
                    ci.invoice(dict(base, **changes), ci.PRIVKEYIO_XBT, 'mainnet')
        # An ordinary recipient invoice may advertise optional MPP. We still
        # send a single part; our own gate invoices do not advertise MPP.
        ci.invoice(base, ci.PRIVKEYIO_XBT, 'mainnet')

    def test_authority_never_infers_or_upgrades_a_binding(self):
        current = ci.node(info(ci.PRIVKEYIO_XBT), ci.PRIVKEYIO_XBT, 'mainnet')
        self.refuses('chain_binding_required', ci.same_binding, None, current)
        self.refuses('invalid_chain_binding', ci.same_binding, {}, current)
        for changes in ({'schema':True}, {'extra':1}, {'network':'xbt'}):
            self.refuses('invalid_chain_binding', ci.same_binding, dict(current, **changes), current)
        for old in (ci.binding(ci.LEGACY_XBT, 'mainnet', NODE),
                    ci.binding(ci.PRIVKEYIO_XBT, 'regtest', NODE),
                    ci.binding(ci.PRIVKEYIO_XBT, 'mainnet', OTHER)):
            self.refuses('chain_binding_changed', ci.same_binding, old, current)
        ci.same_binding(copy.deepcopy(current), current)

    def test_pairs_are_distinct_and_environment_bound(self):
        btc = ci.binding(ci.BTC, 'mainnet', NODE)
        xbt = ci.binding(ci.PRIVKEYIO_XBT, 'mainnet', OTHER)
        self.assertEqual(ci.pair(btc, xbt), dict(btc=btc, xbt=xbt))
        for other in (btc, ci.binding(ci.PRIVKEYIO_XBT, 'mainnet', NODE),
                      ci.binding(ci.PRIVKEYIO_XBT, 'regtest', OTHER)):
            self.refuses('invalid_chain_pair', ci.pair, btc, other)

    def test_channel_signing_type_survives_serialization(self):
        import json
        row = {'channel_type': {'bits':[12,22,514], 'names':['unified_sigs/even']}}
        ci.channel(json.loads(json.dumps(row)), ci.PRIVKEYIO_XBT)
        for bits in ([12,22], [12,22,515], [514,515], [True,514], [514,514]):
            with self.assertRaises(ci.IdentityError):
                ci.channel({'channel_type':{'bits':bits}}, ci.PRIVKEYIO_XBT)
        self.refuses('wrong_channel_signing_type', ci.channel, row, ci.BTC)
        self.refuses('wrong_channel_signing_type', ci.channel, row, ci.LEGACY_XBT)
        # Friendly names alone are not a persisted numeric signing type.
        self.refuses('channel_type_missing', ci.channel, {'channel_type':{'names':['unified_sigs/even']}}, ci.PRIVKEYIO_XBT)

    def test_mainnet_backend_policy_has_no_regtest_or_genesis_shortcut(self):
        chain = dict(chain='main', initialblockdownload=False, pruned=False, blocks=ci.ACTIVATION_HEIGHT)
        deployment = dict(blake2b=dict(active=True, height=ci.ACTIVATION_HEIGHT))
        ci.mainnet_xbt_backend(chain, deployment, ci.ACTIVATION_HASH)
        for changes in ({'chain':'regtest'}, {'initialblockdownload':True}, {'pruned':True},
                        {'blocks':ci.ACTIVATION_HEIGHT-1}, {'blocks':True}):
            self.refuses('xbt_backend_not_ready', ci.mainnet_xbt_backend, dict(chain, **changes), deployment, ci.ACTIVATION_HASH)
        for bad in ({}, {'blake2b':{'active':False,'height':ci.ACTIVATION_HEIGHT}},
                    {'blake2b':{'active':True,'height':1}}):
            with self.assertRaises(ci.IdentityError): ci.mainnet_xbt_backend(chain,bad,ci.ACTIVATION_HASH)
        self.refuses('wrong_xbt_backend', ci.mainnet_xbt_backend, chain, deployment, '00'*32)


def fields(text):
    hrp, encoded = text.rsplit('1', 1)
    values = [bolt11.CHARSET.index(ch) for ch in encoded]
    expanded = [ord(c)>>5 for c in hrp] + [0] + [ord(c)&31 for c in hrp]
    assert bolt11.polymod(expanded + values) == 1
    assert values[-110:-6] == [0] * 104
    result = {}; pos = 7; end = len(values)-110
    while pos < end:
        kind = bolt11.CHARSET[values[pos]]; size = (values[pos+1]<<5)|values[pos+2]
        pos += 3
        assert pos+size <= end and kind not in result
        result[kind] = values[pos:pos+size];pos += size
    assert pos == end
    return hrp, result


class InvoiceTests(unittest.TestCase):
    def make(self, **kw):
        args = dict(profile=ci.PRIVKEYIO_XBT, environment='regtest', expiry=1800,
                    final_cltv=120, timestamp=1791658800)
        args.update(kw)
        return bolt11.unsigned(HASH, SECRET, 1500000, **args)

    def test_invoice_feature_integer_and_currency(self):
        for profile in (ci.BTC, ci.PRIVKEYIO_XBT):
            for environment, prefix in (('mainnet','lnbc'),('regtest','lnbcrt')):
                hrp, tags = fields(self.make(profile=profile, environment=environment))
                self.assertEqual(hrp, prefix+'15000000p')
                value = 0
                for digit in tags['9']: value = (value << 5) | digit
                self.assertEqual(value, (1<<8)|(1<<14)|((1<<512) if profile==ci.PRIVKEYIO_XBT else 0))
                self.assertFalse(value & ((1<<16)|(1<<17)|(1<<513)|(1<<514)|(1<<515)))
                self.assertEqual(set(tags), set('psdxc9'))
                # Hash/secret are byte fields, the feature tag is an integer.
                for name, wanted in (('p', HASH), ('s', SECRET)):
                    bits = ''.join(format(v,'05b') for v in tags[name])
                    self.assertEqual(int(bits[:256],2).to_bytes(32,'big').hex(), wanted)

    def test_out_of_range_and_legacy_generation_refused(self):
        for changes in ({'profile':ci.LEGACY_XBT}, {'environment':'main'}, {'expiry':True},
                        {'expiry':3601}, {'expiry':0}, {'final_cltv':2017}, {'final_cltv':False},
                        {'timestamp':-1}, {'timestamp':1<<35}, {'timestamp':True}):
            with self.subTest(changes=changes):
                with self.assertRaises(ci.IdentityError): self.make(**changes)
        for amount in (True, 0, -1, '1000', 2100000000000000001):
            with self.assertRaises(ci.IdentityError):
                bolt11.unsigned(HASH,SECRET,amount,profile=ci.BTC,environment='regtest',expiry=10,final_cltv=40)


if __name__ == '__main__':
    unittest.main()
