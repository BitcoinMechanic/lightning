#!/usr/bin/env python3
"""Durable protection/credential boundaries; synthetic unit evidence only."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import bounded_authority as ba
import coordinator_admin as admin
import cross_contract as cc
import cross_worker as cw
import customer_https as https
import customer_plugin as plugin
import customer_remote as remote
import deadline_protection as dp
import held_gate as hg
import restore_barrier
import test_bounded_authority as ab
import test_cross_worker as wt
import test_held_gate as gt
from test_route_plan import NOW, PREIMAGE, pin


class Node(ab.Node):
    def __init__(self):
        super().__init__(); self.info['blockheight']=200; self.drop_close=False
    def __call__(self, method, **params):
        if method=='close':
            self.calls.append((method,copy.deepcopy(params)))
            self.rows[0]['state']='AWAITING_UNILATERAL'
            if self.drop_close: raise TimeoutError('close reply lost')
            return dict(type='unilateral')
        return super().__call__(method,**params)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/plugin.plugin.AUTHORITY_DIR
        self.node=Node();self.now=NOW;self.j=None
        ba.initialize_fixture(self.path,self.node.chain);self.open();self.addCleanup(self.close)
    def close(self):
        if self.j:self.j.close()
    def open(self):
        self.close();self.j=hg.Journal(self.path);self.a=ba.Authority(self.j,self.node.chain,self.node,lambda:self.now)
    def setup_entry(self, protected=True):
        bounds=ab.bounds('incoming');bounds.update(min_cltv_delta=350,max_cltv_delta=600)
        if protected:bounds['close_before_blocks']=72
        self.gid=self.a.create_grant('incoming',[pin()],bounds,NOW+3600,confirmed=True)
        q=self.node.quote_data();q.update(min_cltv_delta=350,max_cltv_delta=600)
        self.eid=self.a.enroll(self.gid,q,confirmed=True);self.a.publish(self.eid);self.node.hold();self.a.reconcile(self.eid)
    def closes(self):return [p for m,p in self.node.calls if m=='close']
    def test_old_grant_and_old_rune_never_gain_close_authority(self):
        self.setup_entry(False);g=self.a.record['grants'][self.gid]['terms']
        self.assertEqual(g['format'],ba.FORMAT)
        for op in ('arm','protect','chain'):
            with self.assertRaises(ValueError):https.parameters('incoming',op,{'entry_id':self.eid},g)
        with self.assertRaisesRegex(ValueError,'protection_not_granted'):dp.arm(self.a,self.eid)
        bad=copy.deepcopy(g);bad['limits']['close_before_blocks']=72
        with self.assertRaises(ValueError):ba.grant(bad,self.node.chain)
        self.assertNotIn('arm',remote.operations(g));self.assertFalse(self.closes())
    def test_review_names_close_scope_and_new_rune_stays_enrollment_scoped(self):
        self.node.rows[0]['receivable_msat']=10000000
        path=self.root/admin.DIRECTORY;admin.initialize(path,self.node.chain);j=hg.Journal(path)
        try:
            m=admin.Admin(self.a,j,lambda:self.now)
            bounds=dict(ab.bounds('incoming'),min_cltv_delta=350,max_cltv_delta=600,close_before_blocks=72)
            review=m.review('incoming',bounds,3600)
            self.assertIn('force-closed',review['protection']['scope'])
            self.assertEqual(review['protection']['close_dispatches_per_enrollment'],1)
            gid=review['confirmation'];m.confirm(gid,True)
            g=self.a.record['grants'][gid]['terms'];self.assertEqual(g['format'],ba.PROTECTED_FORMAT)
            r=object.__new__(remote.Remote);r.authority=self.a
            rules=r.restrictions(gid,'incoming')
            self.assertEqual(rules[:3],[['method=swap-customer-call'],['pnamegrant_id='+gid],['pnum=3']])
            self.assertIn('pnameoperation=arm',rules[-1]);self.assertNotIn('pnameoperation=close',rules[-1])
            for body in ({'entry_id':'0'*64,'channel_id':'f'*64},{'entry_id':'0'*64,'height':1}):
                with self.assertRaises(ValueError):https.parameters('incoming','protect',body,g)
        finally:j.close()
    def test_lost_close_reply_fresh_authority_never_repeats_after_pause_expiry(self):
        self.setup_entry();dp.arm(self.a,self.eid);bound=copy.deepcopy(self.a.record['entries'][self.eid]['incoming'])
        self.node.info['blockheight']=482;dp.protect(self.a,self.eid);self.assertFalse(self.closes())
        self.a.pause(self.gid);self.now+=5000;self.node.info['blockheight']=483;self.node.drop_close=True
        with self.assertRaises(TimeoutError):dp.protect(self.a,self.eid)
        intent=copy.deepcopy(self.j.read()['entries'][self.eid]['protection'])
        for _ in range(3):self.open();dp.protect(self.a,self.eid)
        self.assertEqual(self.closes(),[dict(id=bound['binding']['channel']['channel_id'],unilateraltimeout=1)])
        self.assertEqual(self.a.record['entries'][self.eid]['protection'],intent)
        self.assertEqual(self.a.status(self.eid)['state'],'onchain_recovery');self.assertFalse(self.a.status(self.eid)['terminal'])
    def test_crash_after_close_intent_before_dispatch_stays_uncertain(self):
        self.setup_entry();dp.arm(self.a,self.eid);self.node.info['blockheight']=483
        old=self.a.update
        def crash(eid,e):
            old(eid,e)
            if e.get('protection',{}).get('close'):raise RuntimeError('crash')
        with patch.object(self.a,'update',side_effect=crash),self.assertRaises(RuntimeError):dp.protect(self.a,self.eid)
        for _ in range(3):self.open();dp.protect(self.a,self.eid)
        self.assertFalse(self.closes());self.assertIn('close',self.a.record['entries'][self.eid]['protection'])
    def test_storage_failure_prevents_close(self):
        self.setup_entry();dp.arm(self.a,self.eid);self.node.info['blockheight']=483
        with patch.object(self.j,'write',side_effect=OSError('disk')),self.assertRaises(hg.StorageError):dp.protect(self.a,self.eid)
        self.assertFalse(self.closes())
    def test_wrong_binding_expiry_trim_or_sync_never_closes(self):
        self.setup_entry();dp.arm(self.a,self.eid);self.node.info['blockheight']=483
        row=copy.deepcopy(self.node.rows[0]);info=copy.deepcopy(self.node.info)
        cases=[lambda:self.node.rows[0].update(funding_txid='a1'*32),
               lambda:self.node.rows[0]['htlcs'][0].update(expiry=556),
               lambda:self.node.rows[0]['htlcs'][0].update(local_trimmed=True),
               lambda:self.node.rows[0]['htlcs'][0].update(id=4),
               lambda:self.node.info.update(warning_lightningd_sync='pending')]
        for change in cases:
            self.node.rows[0]=copy.deepcopy(row);self.node.info=copy.deepcopy(info);change()
            with self.assertRaises(ValueError):dp.protect(self.a,self.eid)
            self.assertFalse(self.closes())
    def test_arm_rejects_insufficient_margin_and_unheld_entry(self):
        self.setup_entry();self.node.info['blockheight']=206
        with self.assertRaisesRegex(ValueError,'protection_armed_too_late'):dp.arm(self.a,self.eid)
        self.assertNotIn('protection',self.a.record['entries'][self.eid])
    def test_local_watchdog_without_worker_and_restore_fence(self):
        self.setup_entry();dp.arm(self.a,self.eid);self.node.info['blockheight']=483;self.close()
        service=object.__new__(plugin.Service);service.root=self.root;service.chain=self.node.chain
        service.rpc=self.node;service.clock=lambda:self.now;service.serial=threading.RLock()
        service.protect_once();service.protect_once();self.assertEqual(len(self.closes()),1)
        restore_barrier.fence(self.root)
        with self.assertRaisesRegex(ValueError,'restored_authority_blocked'):service.protect_once()
    def test_onchain_preimage_replay_not_reported_as_offchain_settlement(self):
        self.setup_entry();dp.arm(self.a,self.eid);self.node.info['blockheight']=483;dp.protect(self.a,self.eid)
        self.open();self.a.resolve(self.eid,payment_preimage=PREIMAGE);self.open()
        self.assertEqual(self.a.reconcile(self.eid)['state'],'onchain_recovery')
        self.assertEqual(self.node.effective_resolutions,1)
        self.a.resolve(self.eid,payment_preimage=PREIMAGE);self.assertEqual(self.node.effective_resolutions,1)
        with self.assertRaises(ValueError):self.a.prior()


class GateRecovery(unittest.TestCase):
    def test_replay_then_release_on_closed_channel_and_no_new_admission(self):
        for state in hg.CLOSING_STATES:
            t=gt.GateTests();t.setUp()
            try:
                binding=t.held();t.row['state']=state;t.restart()
                self.assertIsNone(t.gate.accept(t.hook,'onchain-hook'))
                hook,_=t.gate.resolve(gt.HASH,binding,preimage=gt.PREIMAGE)
                self.assertEqual(hook[1]['payment_key'],gt.PREIMAGE)
                with self.assertRaises(ValueError):t.gate.actual(hg.snapshot(t.hook),t.quote,admission=True)
            finally:t.doCleanups()


class ProtectedRemote(wt.Remote):
    def __init__(self):
        super().__init__();g=self.c['grants']['incoming'];g['limits'].update(close_before_blocks=72,min_cltv_delta=350);g['format']=ba.PROTECTED_FORMAT
        self.obs['incoming']['info']['grant']=copy.deepcopy(g)
    def status(self,kind):
        value=super().status(kind);value['state']=ba.entry_state(self.entries[kind]);return value
    def __call__(self,kind,gid,op,body):
        if op in ('arm','protect'):
            self.calls.append((kind,op,copy.deepcopy(body)))
            if op=='arm':self.entries[kind].setdefault('protection',dict(armed_height=self.height))
            if self.lost==(kind,op):self.lost=None;raise TimeoutError('lost arm reply')
            return self.status(kind)
        return super().__call__(kind,gid,op,body)


class WorkerRecovery(unittest.TestCase):
    def test_arm_reply_loss_and_send_reply_loss_keep_original_intents(self):
        t=wt.Tests();t.setUp()
        try:
            t.remote=ProtectedRemote();t.open();t.setup_swap();t.remote.hold();t.remote.lost=('incoming','arm')
            with self.assertRaises(TimeoutError):t.w.step(t.cid)
            t.open();t.remote.lost=('outgoing','send')
            with self.assertRaises(TimeoutError):t.w.step(t.cid)
            t.open();t.w.step(t.cid)
            self.assertEqual(t.sends(),1)
            self.assertIn('protection',t.w.record['swaps'][t.cid]['send']['incoming']['entry'])
            t.remote.complete();t.finish();self.assertEqual(t.w.status(t.cid)['state'],'settled')
        finally:t.doCleanups()
    def test_margin_rechecked_after_arming_and_protection_before_outage(self):
        t=wt.Tests();t.setUp()
        try:
            t.remote=ProtectedRemote();t.open();t.setup_swap();t.remote.hold();t.w.step(t.cid)
            t.remote.height=130
            with self.assertRaisesRegex(ValueError,'contract_held_margin_insufficient'):t.w.step(t.cid)
            self.assertEqual(t.sends(),0)
            t.remote.height=100;t.w.step(t.cid);t.open();t.remote.calls=[]
            original=t.remote.__call__
            def offline(kind,gid,op,body):
                if kind=='outgoing':raise TimeoutError('outgoing offline')
                return original(kind,gid,op,body)
            t.w.call=offline
            with self.assertRaises(TimeoutError):t.w.step(t.cid)
            self.assertEqual(t.remote.calls[0][1],'protect')
        finally:t.doCleanups()

if __name__=='__main__':unittest.main()
