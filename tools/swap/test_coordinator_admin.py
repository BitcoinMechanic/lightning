#!/usr/bin/env python3
"""Durable local grant review/confirmation, using both chain identities."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bounded_authority as ba
import chain_identity as ci
import coordinator_admin as ca
import held_gate as hg
import restore_barrier as rb
from test_bounded_authority import Node, bounds
from test_route_plan import NOW


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.now = NOW; self.js = []
        self.addCleanup(self.close)
        self.open(ci.BTC, fresh=True)

    def close(self):
        for j in reversed(self.js): j.close()
        self.js = []

    def open(self, profile=None, fresh=False):
        self.close()
        if fresh:
            self.node = Node(profile); self.node.rows[0]['receivable_msat'] = 10000000
            ba.initialize_fixture(self.root/'bounded-authority-v1', self.node.chain)
            ca.initialize(self.root/ca.DIRECTORY, self.node.chain)
        self.js = [hg.Journal(self.root/'bounded-authority-v1'), hg.Journal(self.root/ca.DIRECTORY)]
        self.auth = ba.Authority(self.js[0], self.node.chain, self.node, lambda:self.now)
        self.admin = ca.Admin(self.auth, self.js[1], lambda:self.now)

    def review(self, kind='outgoing'):
        return self.admin.review(kind, bounds(kind), 3600)['confirmation']

    def confirm(self, gid): return self.admin.confirm(gid, True)

    def test_both_chains_and_roles_select_channels_without_RPC_mutations(self):
        for profile in (ci.BTC, ci.PRIVKEYIO_XBT):
            if profile != ci.BTC:
                self.close(); self.root = self.root/'xbt'; self.root.mkdir(mode=0o700)
                self.open(profile, fresh=True)
            for kind in ('incoming','outgoing'):
                gid = self.review(kind)
                self.assertNotIn(gid, self.auth.record['grants'])
                self.assertEqual(self.confirm(gid)['phase'], 'saved')
                self.assertEqual(len(self.auth.record['grants'][gid]['terms']['channels']),1)
            self.assertTrue(all(m in ('getinfo','listpeerchannels') for m,_ in self.node.calls))

    def test_three_lost_write_boundaries_recover_one_exact_grant(self):
        for boundary in ('intent','authority','saved'):
            gid = self.review(); original = copy.deepcopy(self.admin.record['reviews'][gid]['grant'])
            journal = self.js[0] if boundary == 'authority' else self.js[1]
            write = journal.write
            def lost(record):
                write(record)
                if boundary == 'authority' or record['reviews'][gid]['phase'] == boundary:
                    raise OSError('lost persisted reply')
            with patch.object(journal,'write',side_effect=lost), self.assertRaises(hg.StorageError): self.confirm(gid)
            self.open(); self.confirm(gid); self.open(); self.confirm(gid)
            self.assertEqual(self.auth.record['grants'][gid]['terms'],original)
            self.assertEqual(len(self.auth.record['grants']), ('intent','authority','saved').index(boundary)+1)
            self.assertEqual(self.admin.record['reviews'][gid]['phase'],'saved')

    def test_lost_success_never_renews_unpauses_or_reselects(self):
        old = self.review(); self.confirm(old); self.auth.pause(old)
        new = self.review(); self.confirm(new); before=copy.deepcopy(self.auth.record)
        self.now += 86401; self.node.rows=[]; self.open()
        result=self.confirm(old)
        self.assertFalse(result['current']); self.assertTrue(result['paused'])
        self.assertEqual(self.auth.record,before)

    def test_saved_review_cannot_reinstall_missing_authority(self):
        gid=self.review(); self.confirm(gid)
        self.auth.record['grants'].pop(gid); self.auth.record['current']['outgoing']=None
        before=copy.deepcopy(self.auth.record)
        with self.assertRaisesRegex(ValueError,'admin_saved_authority_missing'): self.confirm(gid)
        self.assertEqual(self.auth.record,before)

    def test_changed_channel_liquidity_fee_and_review_expiry_refuse_before_grant(self):
        for field,value in [('funding_txid','88'*32),('spendable_msat',3000000),('htlcs',[{}])]:
            gid=self.review(); before=copy.deepcopy(self.node.rows)
            self.node.rows[0][field]=value
            with self.assertRaises(ValueError): self.confirm(gid)
            self.assertNotIn(gid,self.auth.record['grants']); self.node.rows=before
        gid=self.review(); self.now+=ca.REVIEW_SECONDS
        with self.assertRaisesRegex(ValueError,'admin_review_expired'): self.confirm(gid)
        self.assertNotIn(gid,self.auth.record['grants'])

    def test_refused_then_cancelled_confirmation_does_not_create_authority(self):
        gid=self.review()
        with self.assertRaises(ValueError): self.admin.confirm(gid,False)
        self.admin.cancel(gid)
        with self.assertRaisesRegex(ValueError,'admin_review_cancelled'): self.confirm(gid)
        self.assertEqual(self.auth.record['grants'],{})

    def test_unfinished_intent_blocks_new_review_but_can_be_cancelled(self):
        gid=self.review(); record=copy.deepcopy(self.admin.record)
        record['reviews'][gid]['phase']='intent'; self.admin.commit(record)
        with self.assertRaisesRegex(ValueError,'admin_setup_unfinished'): self.review()
        self.admin.cancel(gid); self.review()

    def test_requests_are_finite_and_mainnet_refused(self):
        self.close()
        for action in (dict(action='sendpay'),dict(action='status',method='sendpay'),dict(action='confirm',confirmation='x')):
            with self.assertRaises(ValueError): ca.execute(self.root,self.node.chain,self.node,action)
        chain=ci.binding(ci.BTC,'mainnet',self.node.chain['node_id'])
        with self.assertRaises(ValueError): ca.execute(self.root,chain,self.node,dict(action='status'))

    def test_parent_restore_blocks_confirmation_and_reinitialization(self):
        gid=self.review(); before=self.js[0].read(); calls=len(self.node.calls)
        rb.fence(self.root)
        with self.assertRaises(ValueError): self.confirm(gid)
        self.assertEqual(len(self.node.calls),calls); self.assertEqual(self.js[0].read(),before)
        self.assertTrue(self.admin.status()['blocked'])
        with self.assertRaises(ValueError): ca.initialize(self.root/'other-admin',self.node.chain)
        with self.assertRaises(ValueError): self.js[0].write(before)


if __name__=='__main__': unittest.main()
