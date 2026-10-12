#!/usr/bin/env python3
"""Price, identity, confirmation and held-margin boundaries; no execution."""
import copy
import unittest
import bounded_authority as ba
import cross_contract as cc
import customer_remote as remote
import market_quote as market
from test_route_plan import Fake, NOW, pin


def example(direction='forward', markup=0):
    ip,op = (ba.ci.BTC,ba.ci.PRIVKEYIO_XBT) if direction == 'forward' else (ba.ci.PRIVKEYIO_XBT,ba.ci.BTC)
    node = Fake(op); plan = node.plan()
    grants = {}
    for kind,profile in [('incoming',ip),('outgoing',op)]:
        limits = dict(max_swaps=3,max_amount_msat=10000000,total_amount_msat=30000000)
        limits.update(dict(min_cltv_delta=40,max_cltv_delta=2016) if kind == 'incoming' else plan['limits'])
        grants[kind] = dict(schema=1,format=ba.FORMAT,chain=ba.ci.binding(profile,'regtest',
            '03'+'88'*32 if kind == 'incoming' else plan['chain']['node_id']), nonce=('22' if kind == 'incoming' else '33')*32,
            kind=kind,created_at=NOW-10,expires_at=NOW+3600,limits=limits,channels=[pin(profile)])
    pricing = market.quote(dict(schema=1,source='neoxa.exchange',pair='BTCB2_BTC',direction=direction,markup_bps=markup,
        fetched_at_ms=NOW*1000,ticker_at_ms=NOW*1000,expires_at=NOW+120,bid='0.49',ask='0.5',
        levels=[dict(price='0.5' if direction=='forward' else '0.49',quantity='1')],
        recipient_msat=plan['amount_msat'],routing_fee_msat=2000,btc_msat=0,xbt_msat=0),compute=True)
    c = cc.create(grants,plan,pricing,now=NOW,payment_secret='77'*32,nonce='88'*32)
    observations = {}
    for kind,g in grants.items():
        observations[kind] = dict(format=remote.FORMAT,checked_at=NOW,blockheight=100 if kind=='incoming' else 900000,
            reserve_msat=cc.RESERVE_MSAT,info=dict(format=remote.FORMAT,grant=g,paused=False,current=True,
                remaining=3,reserved_msat=0,regtest_only=True))
    observations['incoming']['hints'] = [dict(pubkey=pin(ip)['peer_id'],short_channel_id='123x2x1',
        fee_base_msat=1000,fee_proportional_millionths=0,cltv_expiry_delta=80)]
    return c,observations


class Tests(unittest.TestCase):
    def test_both_directions_price_fee_markup_and_independent_heights(self):
        for direction in ('forward','reverse'):
            c,obs = example(direction)
            self.assertEqual(cc.confirm(c,cc.digest(c),obs,now=NOW),cc.digest(c))
            self.assertEqual(c['incoming']['min_cltv_delta'],350)
            self.assertEqual(cc.review(c)['incoming_channel'],'Detected when payment arrives')
            asset = 'btc' if direction=='forward' else 'xbt'
            marked,_ = example(direction,100)
            self.assertGreater(marked['pricing'][asset+'_msat'],c['pricing'][asset+'_msat'])
            self.assertEqual(c['pricing']['markup_bps'],0)
            no_fee = market.quote(dict(c['pricing'],routing_fee_msat=0),compute=True)
            self.assertGreater(c['pricing'][asset+'_msat'],no_fee[asset+'_msat'])
            self.assertNotIn(c['outgoing']['payment_secret'],str(cc.review(c)))

    def test_confirmation_is_bound_to_every_saved_term(self):
        c,obs = example(); approved = cc.digest(c)
        for transform in (lambda x:x.update(nonce='99'*32),
            lambda x:x['outgoing']['route'][0].update(amount_msat=2002001),
            lambda x:x['incoming'].update(payment_secret='66'*32),
            lambda x:x['grants']['incoming']['channels'][0].update(funding_txid='55'*32)):
            changed = copy.deepcopy(c); transform(changed)
            with self.assertRaises(ValueError): cc.confirm(changed,approved,obs,now=NOW)
        for v in (True, None, {}, '0'*64):
            with self.assertRaises(ValueError): cc.confirm(c,v,obs,now=NOW)

    def test_identity_secret_timing_and_market_tampering_refuse(self):
        c,_ = example()
        changes = [lambda x:x['incoming'].update(payment_hash='55'*32),
            lambda x:x['incoming'].update(payment_secret=x['outgoing']['payment_secret']),
            lambda x:x['incoming'].update(min_cltv_delta=349),
            lambda x:x['incoming'].update(max_cltv_delta=2015),
            lambda x:x.update(pay_until=NOW+121),
            lambda x:x['outgoing'].update(expires_at=NOW+299),
            lambda x:x['pricing'].update(xbt_msat=2001000),
            lambda x:x.update(format='startos-forward-market-v1'),
            lambda x:x['grants']['incoming']['chain'].update(profile=ba.ci.LEGACY_XBT)]
        for change in changes:
            value = copy.deepcopy(c); change(value)
            with self.assertRaises(ValueError): cc.validate(value)

    def test_admission_refuses_stale_future_reserve_pause_replacement_budget_expiry(self):
        c,obs = example()
        changes = [lambda x:x['incoming'].update(checked_at=NOW-31),
            lambda x:x['outgoing'].update(checked_at=NOW+1),
            lambda x:x['outgoing'].update(reserve_msat=cc.RESERVE_MSAT-1),
            lambda x:x['incoming']['info'].update(paused=True),
            lambda x:x['incoming']['info'].update(current=False),
            lambda x:x['outgoing']['info'].update(remaining=0),
            lambda x:x['outgoing']['info'].update(reserved_msat=30000000),
            lambda x:x['incoming']['info']['grant'].update(nonce='00'*32)]
        for change in changes:
            value = copy.deepcopy(obs); change(value)
            with self.assertRaises(ValueError): cc.admission(c,value,now=NOW)
        for now in (NOW-1,NOW+120,True):
            with self.assertRaises(ValueError): cc.admission(c,obs,now=now)

    def test_held_margin_requires_original_committed_remote_evidence(self):
        c,obs = example(); g = c['grants']['incoming']; q = c['incoming']; p = g['channels'][0]
        gid = ba.digest(g); eid = ba.digest(dict(grant_id=gid,payload=q))
        entry = dict(grant_id=gid,payload=q,publication='registered',
            incoming=dict(binding=dict(channel=p,htlc_id=3),expiry=474),
            htlc=dict(short_channel_id=p['short_channel_id'],id=3,expiry=474,direction='in',
                amount_msat=q['amount_msat'],payment_hash=q['payment_hash'],created_index=7))
        e = dict(entry_id=eid,entry=entry,status=dict(kind='incoming',state='held',terminal=False,regtest_only=True,blocked=False))
        self.assertEqual(cc.held_margin(c,e,incoming_height=100),entry['incoming'])
        with self.assertRaises(ValueError): cc.held_margin(c,e,incoming_height=125)
        for change in (lambda x:x['entry']['htlc'].update(id=4),
            lambda x:x['entry']['incoming']['binding']['channel'].update(funding_txid='99'*32),
            lambda x:x['entry'].update(decision=dict(action='fail')),lambda x:x['status'].update(blocked=True)):
            value = copy.deepcopy(e); change(value)
            with self.assertRaises(ValueError): cc.held_margin(c,value,incoming_height=100)

    def test_neoxa_snapshot_limits_and_exact_arithmetic_match_installed_policy(self):
        c,_ = example(); q = c['pricing']
        for update in (dict(ticker_at_ms=NOW*1000-30001),dict(ask='0.9'),dict(levels=[]),dict(markup_bps=501),
                       dict(bid='NaN'),dict(routing_fee_msat=10001),dict(recipient_msat=2000001)):
            with self.assertRaises(ValueError): market.quote(dict(q,**update))
        # 2,002 outgoing sats at 0.5 BTC/XBT, including the 2-sat route fee.
        self.assertEqual(q['btc_msat'],1001000)
        self.assertEqual(q['xbt_msat'],2000000)


if __name__ == '__main__': unittest.main()
