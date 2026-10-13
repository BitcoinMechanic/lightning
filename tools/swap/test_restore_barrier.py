#!/usr/bin/env python3
"""Restored copies cannot reuse gates, grants, credentials or controller state."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

import bounded_authority as ba
import coordinator_admin as ca
import cross_worker as cw
import customer_invoice as invoice
import held_gate as hg
import remote_authority as remote
import restore_barrier as rb
import worker_pairing as wp
from test_bounded_authority import Node
import test_held_gate as gate_test


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_markers_cover_volume_network_and_old_per_journal_locations(self):
        for marker,journal in [('swap-restored.json','authority'),('node-restored.json','authority'),
                ('node-restored.json','bitcoin/authority'),('swap-restored.json','regtest/authority'),
                ('authority/restored.json','authority')]:
            with self.subTest(marker=marker,journal=journal):
                p=self.root/marker; p.parent.mkdir(parents=True,exist_ok=True)
                p.symlink_to('missing-target')
                self.assertTrue(rb.blocked(self.root/journal)); p.unlink()

    def test_fence_durable_idempotent_and_never_repairs_invalid_marker(self):
        result=rb.fence(self.root); self.assertTrue(result['execution_blocked'])
        path=self.root/rb.MARKER; before=path.read_bytes(); st=path.stat()
        self.assertEqual(rb.fence(self.root),result)
        self.assertEqual((path.read_bytes(),path.stat().st_ino),(before,st.st_ino))
        path.write_text('partial')
        with self.assertRaises(ValueError): rb.fence(self.root)
        self.assertEqual(path.read_text(),'partial'); self.assertTrue(rb.blocked(self.root/'worker'))

    def test_fence_refuses_symlink_public_directory_and_nonregular_marker(self):
        link=self.root/'link'; link.symlink_to(self.root)
        with self.assertRaises(ValueError): rb.fence(link)
        public=self.root/'public'; public.mkdir(mode=0o755)
        with self.assertRaises(ValueError): rb.fence(public)
        os.mkfifo(self.root/rb.MARKER,mode=0o600)
        with self.assertRaises(ValueError): rb.fence(self.root)

    def test_all_initializers_refuse_restored_parent_without_creating_state(self):
        chain=Node().chain; rb.fence(self.root)
        for n,init,args in [('gate',hg.initialize_fixture,(chain,)),('authority',ba.initialize_fixture,(chain,)),
                ('remote',remote.initialize_fixture,(chain,)),('invoice',invoice.initialize_fixture,(chain,)),
                ('worker',cw.initialize_fixture,()),('pairing',wp.initialize_fixture,()),('admin',ca.initialize,(chain,))]:
            with self.assertRaises(ValueError): init(self.root/n,*args)
            self.assertFalse((self.root/n).exists())

    def test_gate_held_and_release_intent_never_fall_through_or_reveal_after_restore(self):
        for release in (False,True):
            fixture=gate_test.GateTests(methodName='runTest'); fixture.setUp()
            try:
                original=fixture.held()
                if release: fixture.gate.resolve(gate_test.HASH,original,preimage=gate_test.PREIMAGE)
                before=fixture.journal.read(); rb.fence(fixture.directory.parent)
                self.assertIsNone(fixture.gate.accept(fixture.hook,'restored-hook'))
                self.assertFalse(fixture.gate.status(gate_test.HASH)['hook_ready'])
                self.assertTrue(fixture.gate.status(gate_test.HASH)['needs_attention'])
                with self.assertRaises(ValueError): fixture.gate.resolve(gate_test.HASH,original,preimage=gate_test.PREIMAGE)
                def forbidden(*args,**kwargs): self.fail('restored gate called RPC')
                with self.assertRaisesRegex(ValueError,'restored_gate_blocked'):
                    hg.Gate(fixture.journal,fixture.chain,forbidden)
                self.assertEqual(fixture.journal.read(),before)
            finally: fixture.doCleanups()

    def test_remote_publisher_worker_pairing_and_authority_block_before_RPC(self):
        node=Node(); journals=[]; self.addCleanup(lambda:[j.close() for j in reversed(journals)])
        def opened(name,initialize,*args):
            path=self.root/name; initialize(path,*args); j=hg.Journal(path); journals.append(j); return j
        a=ba.Authority(opened('authority',ba.initialize_fixture,node.chain),node.chain,node)
        r=remote.Remote(a,opened('remote',remote.initialize_fixture,node.chain),node)
        p=invoice.Publisher(a,opened('invoice',invoice.initialize_fixture,node.chain))
        w=cw.Worker(opened('worker',cw.initialize_fixture),node)
        pair=wp.Pairing(opened('pairing',wp.initialize_fixture))
        rb.fence(self.root); before=copy.deepcopy(node.calls)
        for obj in (a,r,p,w,pair):
            with self.assertRaises(ValueError): obj.guard()
        self.assertEqual(node.calls,before)


if __name__=='__main__': unittest.main()
