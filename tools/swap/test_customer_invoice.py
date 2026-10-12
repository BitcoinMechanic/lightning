#!/usr/bin/env python3
"""Durable publisher and fresh scoped facade boundaries; no daemon claim."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import authority_plugin as old_plugin
import bounded_authority as ba
import customer_invoice as invoice
import customer_plugin as plugin
import customer_remote as remote
import held_gate as hg
import remote_authority as old
from test_bounded_authority import Node, bounds
from test_chain_identity import vector
from test_route_plan import NOW, pin


class CustomerNode(Node):
    def __init__(self, profile=ba.ci.BTC):
        super().__init__(profile); self.inventory = []; self.signed_decode = None
        self.signed_unsigned = []; self.corrupt_decode = None
        self.rows[0].update(receivable_msat=100000000, private=True, alias=dict(remote='123x2x1'),
            features=['option_anchors'], feerate=dict(perkw=1255), dust_limit_msat=546000,
            updates=dict(remote=dict(htlc_minimum_msat=1, htlc_maximum_msat=100000000,
                fee_base_msat=1000, fee_proportional_millionths=0, cltv_expiry_delta=80)))

    def __call__(self, method, **params):
        if method == 'decode' and params.get('string') == 'lnbcrt1signed-customer-fixture':
            result = copy.deepcopy(self.signed_decode)
            if self.corrupt_decode: self.corrupt_decode(result)
            return result
        if method not in ('showrunes', 'createrune', 'listfunds', 'signinvoice'):
            return super().__call__(method, **params)
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'showrunes': return dict(runes=copy.deepcopy(self.inventory))
        if method == 'createrune':
            uid = str(len(self.inventory)+1)
            self.inventory.append(dict(rune='A'*44+uid, unique_id=uid, restrictions=[dict(alternatives=[
                dict(fieldname=s.split('=')[0], condition='=', value=s.split('=',1)[1])
                for s in group]) for group in params['restrictions']]))
            return {k:self.inventory[-1][k] for k in ('rune','unique_id')}
        if method == 'listfunds':
            return dict(outputs=[dict(txid='99'*32,output=0,amount_msat=50000000,reserved=False,status='confirmed')])
        self.signed_unsigned.append(params['invstring'])
        if self.fail_reply == method:
            self.fail_reply = None; raise TimeoutError('lost signature reply')
        return dict(bolt11='lnbcrt1signed-customer-fixture')


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'regtest'; self.root.mkdir(); self.now = NOW
        self.node = CustomerNode(); self.journals = []; self.setup()
        self.addCleanup(self.close)

    def setup(self):
        ba.initialize_fixture(self.root/old_plugin.AUTHORITY_DIR, self.node.chain)
        remote.initialize_fixture(self.root/plugin.REMOTE_DIR, self.node.chain)
        invoice.initialize_fixture(self.root/plugin.INVOICE_DIR, self.node.chain)
        self.open()

    def close(self):
        for j in reversed(self.journals): j.close()
        self.journals = []

    def open(self):
        self.close()
        self.journals = [hg.Journal(self.root/n) for n in (old_plugin.AUTHORITY_DIR,plugin.REMOTE_DIR,plugin.INVOICE_DIR)]
        self.a = ba.Authority(self.journals[0],self.node.chain,self.node,lambda:self.now)
        self.p = invoice.Publisher(self.a,self.journals[2])
        self.r = remote.Remote(self.a,self.journals[1],self.node,self.p)

    def grant(self, kind='incoming'):
        b = bounds(kind)
        if kind == 'incoming': b.update(min_cltv_delta=350,max_cltv_delta=2016)
        self.gid = self.a.create_grant(kind,[pin(self.node.profile)],b,NOW+3600,confirmed=True)
        self.r.enable(self.gid,confirmed=True)
        return self.gid

    def call(self, op, **body):
        return self.r.call(dict(grant_id=self.gid,operation=op,body=body))

    def enrolled(self):
        self.grant(); q = self.node.quote_data(); q.update(min_cltv_delta=350,max_cltv_delta=2016)
        self.eid = self.call('enroll',data=q,confirmation=ba.digest(q))['entry_id']
        self.call('publish',entry_id=self.eid)
        hints = invoice.select_hints(self.a,q)
        self.node.signed_decode = dict(valid=True,type='bolt11 invoice',currency='bcrt',
            payment_hash=q['payment_hash'],payment_secret=q['payment_secret'],amount_msat=q['amount_msat'],
            payee=self.node.chain['node_id'],created_at=NOW,expiry=q['expires_at']-NOW,
            min_final_cltv_expiry=374,routes=[[h] for h in hints],
            description='BTC to XBT swap' if self.node.profile == ba.ci.BTC else 'XBT to BTC swap',
            features=vector(8,14,*([512] if self.node.profile == ba.ci.PRIVKEYIO_XBT else [])))
        return q

    def test_new_xbt_requires_invoice_bit_and_persisted_unified_channel(self):
        self.close(); self.root = Path(self.tmp.name)/'xbt'; self.root.mkdir()
        self.node = CustomerNode(ba.ci.PRIVKEYIO_XBT); self.setup(); self.enrolled()
        self.node.corrupt_decode = lambda d:d.update(features=vector(8,14))
        with self.assertRaises(ValueError): self.call('invoice',entry_id=self.eid)
        self.node.corrupt_decode = None
        self.call('invoice',entry_id=self.eid)
        self.assertIn(514,self.a.record['grants'][self.gid]['terms']['channels'][0]['channel_type_bits'])

    def test_lost_sign_reply_freezes_exact_hint_secret_and_expiry_across_workers(self):
        q = self.enrolled(); self.node.fail_reply = 'signinvoice'
        with self.assertRaises(TimeoutError): self.call('invoice',entry_id=self.eid)
        original = copy.deepcopy(self.p.record)
        self.open(); self.node.rows[0]['alias']['remote'] = '124x2x1'; self.now += 100
        value = self.call('invoice',entry_id=self.eid)
        self.assertEqual(self.node.signed_unsigned[0],self.node.signed_unsigned[1])
        self.assertEqual(self.p.record['invoices'][self.eid]['hints'],original['invoices'][self.eid]['hints'])
        self.open(); self.a.pause(self.gid); self.now += 10000
        self.assertEqual(self.call('invoice',entry_id=self.eid),value)
        self.assertEqual(len(self.node.signed_unsigned),2)
        self.assertEqual(value['expires_at'],q['expires_at'])
        self.assertEqual(len(self.a.record['entries']),1)

    def test_journal_failures_freeze_before_sign_and_after_signed_reply(self):
        self.enrolled()
        with patch.object(self.journals[2],'write',side_effect=OSError('disk')), self.assertRaises(hg.StorageError):
            self.call('invoice',entry_id=self.eid)
        self.assertFalse(self.node.signed_unsigned)
        with self.assertRaisesRegex(ValueError,'publisher_blocked'): self.call('info')
        self.open(); original = self.journals[2].write
        def crash(record):
            original(record)
            if 'invoice' in record['invoices'][self.eid]: raise OSError('lost fsync reply')
        with patch.object(self.journals[2],'write',side_effect=crash), self.assertRaises(hg.StorageError):
            self.call('invoice',entry_id=self.eid)
        self.open(); self.call('invoice',entry_id=self.eid)
        self.assertEqual(len(self.node.signed_unsigned),1)

    def test_unregistered_or_expired_first_publication_never_signs(self):
        q = self.enrolled(); self.now = q['expires_at']
        with self.assertRaises(ValueError): self.call('invoice',entry_id=self.eid)
        self.assertFalse(self.node.signed_unsigned)

    def test_all_authenticated_signed_fields_are_checked(self):
        self.enrolled()
        for key,value in dict(payment_hash='11'*32,payment_secret='11'*32,amount_msat=1,
            payee='03'+'22'*32,created_at=NOW+1,expiry=1,min_final_cltv_expiry=40,routes=[],
            features=vector(8,14,17),description='changed').items():
            self.node.corrupt_decode = lambda d,k=key,v=value:d.update({k:v})
            with self.assertRaises(ValueError): self.call('invoice',entry_id=self.eid)
            self.assertNotIn('invoice',self.p.record['invoices'][self.eid])
        self.node.corrupt_decode = None; result = self.call('invoice',entry_id=self.eid)
        self.node.corrupt_decode = lambda d:d.update(amount_msat=True)
        with self.assertRaises(ValueError): self.call('invoice',entry_id=self.eid)
        self.assertEqual(result['invoice'],self.p.record['invoices'][self.eid]['invoice'])

    def test_alias_liquidity_dust_and_policy_refuse_before_sign(self):
        self.enrolled(); original = copy.deepcopy(self.node.rows[0])
        variants = [dict(original,receivable_msat=1),dict(original,reestablished=False),
            dict(original,features=['option_scid_alias'],alias={}),
            dict(original,dust_limit_msat=2000000),dict(original,updates={}),
            dict(original,features=['option_anchors','option_anchor_outputs'])]
        for row in variants:
            self.node.rows[0] = row
            with self.assertRaises(ValueError): self.call('invoice',entry_id=self.eid)
            self.assertFalse(self.node.signed_unsigned)
        self.node.rows[0] = original
        self.call('invoice',entry_id=self.eid)
        self.assertEqual(self.p.record['invoices'][self.eid]['hints'][0]['short_channel_id'],'123x2x1')

    def test_old_credential_scope_and_journal_are_not_promoted(self):
        self.grant()
        self.assertNotEqual(old.restrictions(self.gid,'incoming'),self.r.restrictions(self.gid,'incoming'))
        self.assertEqual(old.restrictions(self.gid,'incoming')[0],['method='+old.CALL])
        self.assertNotIn('pnameoperation=invoice',sum(old.restrictions(self.gid,'incoming'),[]))
        old_record = copy.deepcopy(self.r.record); old_record['format'] = old.FORMAT
        self.journals[1].write(old_record)
        with self.assertRaisesRegex(ValueError,'remote_format_refused'): self.open()

    def test_new_scope_kind_body_entry_ownership_and_restore(self):
        self.enrolled()
        for op,body in [('plan',dict(invoice='lnbcrt1a')),('invoice',dict(entry_id='0'*64)),
            ('invoice',dict(entry_id=self.eid,invstring='arbitrary')),('inspect',{}),('inspect',dict(amount_msat=True))]:
            with self.assertRaises(ValueError): self.call(op,**body)
        report = self.call('inspect',amount_msat=2000000)
        self.assertEqual(report['reserve_msat'],50000000)
        self.assertNotIn('credentials',json.dumps(report))
        (self.journals[2].directory/'restored.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'restored_publisher_blocked'): self.call('info')
        with self.assertRaises(ValueError): self.call('invoice',entry_id=self.eid)

    def test_plan_is_readonly_and_never_widens_saved_grant(self):
        self.grant('outgoing'); before = [j.path.read_bytes() for j in self.journals]
        plan = self.call('plan',invoice='lnbcrt1synthetic')
        self.assertEqual(plan['route'][0]['delay'],200)
        self.assertEqual(before,[j.path.read_bytes() for j in self.journals])
        self.assertFalse(any(m in ('sendpay','signinvoice') for m,p in self.node.calls))
        with self.assertRaises(ValueError): self.call('plan',invoice='lnbcrt1synthetic',max_delay_blocks=2016)
        self.a.pause(self.gid)
        with self.assertRaises(ValueError): self.call('plan',invoice='lnbcrt1synthetic')

    def test_enrolled_inspection_is_readonly_owned_and_works_with_held_channel(self):
        self.enrolled(); before = [j.path.read_bytes() for j in self.journals]
        self.node.rows[0]['htlcs'] = [dict(direction='in',id=3)]
        with self.assertRaises(ValueError): self.call('inspect',amount_msat=2000000)
        report = self.call('inspect',entry_id=self.eid)
        self.assertEqual(report['entry_id'],self.eid); self.assertNotIn('hints',report)
        self.assertFalse(report['channel_ready'])  # no committed binding in this synthetic entry
        self.assertEqual(report['reserve_msat'],50000000)
        self.assertEqual(before,[j.path.read_bytes() for j in self.journals])
        for body in (dict(entry_id='0'*64),dict(entry_id=self.eid,amount_msat=2000000)):
            with self.assertRaises(ValueError): self.call('inspect',**body)
        # An existing entry owned by another grant is also refused.
        other = copy.deepcopy(self.a.record); gid = self.gid; old = other['grants'][gid]['terms']
        terms = dict(old,nonce='99'*32); other_id = ba.digest(terms)
        other['grants'][other_id] = dict(terms=terms,paused=False)
        other['current']['incoming'] = other_id
        self.a.commit(other); self.r.enable(other_id,confirmed=True); self.gid = other_id
        with self.assertRaisesRegex(ValueError,'enrollment_outside_grant'): self.call('inspect',entry_id=self.eid)

    def test_service_closes_all_three_locks_and_keeps_old_methods_unavailable(self):
        self.grant(); self.close()
        config = dict(network='regtest',startup=True,**{'lightning-dir':str(self.root),'rpc-file':'lightning-rpc'})
        service = plugin.Service(config,ba.ci.BTC,lambda config:self.node,clock=lambda:NOW)
        result = service.invoke(remote.CALL,dict(grant_id=self.gid,operation='info',body={}))
        self.assertEqual(result['format'],remote.FORMAT)
        with self.assertRaises(ValueError): service.invoke(old.CALL,{})
        self.open()
        self.assertEqual({r['name'] for r in plugin.manifest()['rpcmethods']},{remote.CALL,remote.CREDENTIAL})
        self.assertNotIn('signinvoice',old_plugin.RPC.methods)
        self.assertEqual(plugin.RPC.methods,remote.METHODS)

    def test_reserve_excludes_unconfirmed_reserved_and_duplicate_outputs(self):
        row = self.node('listfunds')['outputs'][0]
        self.assertEqual(remote.reserve(dict(outputs=[dict(row,reserved=True)])),0)
        self.assertEqual(remote.reserve(dict(outputs=[dict(row,status='unconfirmed')])),0)
        with self.assertRaises(ValueError): remote.reserve(dict(outputs=[row,row]))
        with self.assertRaises(ValueError): remote.reserve(dict(outputs=[dict(row,amount_msat=True)]))


if __name__ == '__main__': unittest.main()
