#!/usr/bin/env python3
"""Exercise the fixture's fresh CLI and restore wiring before real VM funding."""
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bounded_authority as ba
import chain_identity as ci
import customer_invoice as invoice
import customer_remote as remote
import held_gate as hg
import service
import test_coordinator_engine as fixture
from test_bounded_authority import Node
import test_customer_invoice as customer_tests
from test_route_plan import NOW


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.lab=SimpleNamespace(root=self.root,admin_receipts=[])

    def node(self,profile,name):
        fake=Node(profile);fake.rows[0]['receivable_msat']=10000000
        root=self.root/name;root.mkdir(mode=0o700);net=root/'regtest';net.mkdir(mode=0o700)
        ba.initialize_fixture(net/'bounded-authority-v1',fake.chain)
        return dict(root=root,identity=fake.chain),fake

    def test_fresh_CLI_grants_and_renewal_using_real_local_socket_both_profiles(self):
        for profile in (ci.BTC,ci.PRIVKEYIO_XBT):
            node,fake=self.node(profile,profile)
            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    request=json.loads(self.rfile.readline())
                    reply=dict(jsonrpc='2.0',id=request['id'],result=fake(request['method'],**request['params']))
                    self.wfile.write(json.dumps(reply).encode()+b'\n\n')
            try: server=socketserver.UnixStreamServer(str(node['root']/'regtest/lightning-rpc'),Handler)
            except PermissionError: self.skipTest('Local runtime prohibits sockets; mandatory on packaging VM')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                for kind in ('incoming','outgoing'):
                    limits=dict(max_swaps=4,max_amount_msat=3000000,total_amount_msat=9000000)
                    limits.update(dict(min_cltv_delta=350,max_cltv_delta=2016) if kind=='incoming' else
                                  dict(max_fee_msat=10000,max_delay_blocks=288,max_hops=4))
                    for _ in range(2): fixture.grant_setup(self.lab,node,kind,limits)
            finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(len(self.lab.admin_receipts),8)

    def test_fresh_CLI_processes_with_injected_test_only_RPC(self):
        original=fixture.subprocess.run
        code = """import sys
from test_bounded_authority import Node
import test_customer_invoice as customer_tests
from test_route_plan import NOW
import coordinator_admin_cli as cli
profile=sys.argv[sys.argv.index('--profile')+1]
n=Node(profile);n.rows[0]['receivable_msat']=10000000
cli.ap.RPC=lambda config:n
cli.main()
"""
        def child(command,**kwargs):
            return original([command[0],'-c',code,*command[2:]],**kwargs)
        with patch.object(fixture.subprocess,'run',side_effect=child):
            for profile in (ci.BTC,ci.PRIVKEYIO_XBT):
                node,_=self.node(profile,profile)
                for kind in ('incoming','outgoing'):
                    limits=dict(max_swaps=4,max_amount_msat=3000000,total_amount_msat=9000000)
                    limits.update(dict(min_cltv_delta=350,max_cltv_delta=2016) if kind=='incoming' else
                                  dict(max_fee_msat=10000,max_delay_blocks=288,max_hops=4))
                    for _ in range(2):fixture.grant_setup(self.lab,node,kind,limits)
        self.assertEqual(len(self.lab.admin_receipts),8)

    def stopped_topologies(self, *, populated=False):
        topologies={}
        for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]:
            nodes=[]
            for role in ('source','recipient'):
                node,_=self.node(profile,label+role);nodes.append(node)
                net=node['root']/'regtest';chain=node['identity']
                fixture.admin.initialize(net/fixture.admin.DIRECTORY,chain)
                remote.initialize_fixture(net/fixture.base.customer.plugin.REMOTE_DIR,chain)
                invoice.initialize_fixture(net/fixture.base.customer.plugin.INVOICE_DIR,chain)
                if role=='recipient':hg.initialize_fixture(net/'swap-gate-v1',chain)
                if populated:
                    # Use normal grant/enrollment/publication operations to build
                    # real validated records, including signed customer invoices.
                    helper=customer_tests.Tests(methodName='runTest')
                    helper.root=net;helper.now=NOW;helper.node=customer_tests.CustomerNode(profile);helper.journals=[]
                    try:
                        helper.open()
                        if role=='recipient':
                            helper.enrolled();helper.call('invoice',entry_id=helper.eid)
                            self.assertEqual(len(helper.p.record['invoices']),1)
                        else:helper.grant('outgoing')
                        self.assertEqual(len(helper.r.record['credentials']),1)
                    finally:helper.close()
            topologies[label]=(None,nodes,None)
        for direction in ('forward','reverse'):service.initialize(self.root/('service-worker-'+direction))
        return topologies

    def test_stopped_journal_fixture_constructors_status_and_restore_guards(self):
        topologies=self.stopped_topologies()
        fixture.after_stop(self.lab,topologies,history_count=0)
        self.assertTrue(self.lab.restore_receipt['execution_refused_before_RPC'])

    def test_populated_publishers_and_credentials_refuse_before_RPC_after_fence(self):
        topologies=self.stopped_topologies(populated=True)
        fixture.after_stop(self.lab,topologies,history_count=0)
        self.assertTrue(self.lab.restore_receipt['execution_refused_before_RPC'])
        self.assertEqual(self.lab.restore_receipt['populated_publishers'],2)
        self.assertEqual(self.lab.restore_receipt['credential_journals'],4)
        self.assertTrue(self.lab.restore_receipt['loaded_objects_refused'])
        self.assertTrue(self.lab.restore_receipt['fresh_facade_startup_refused'])

    def test_corrupt_populated_publisher_is_not_accepted_as_restore_refusal(self):
        topologies=self.stopped_topologies(populated=True)
        node=topologies['btc'][1][-1];root=node['root']/'regtest'
        path=root/fixture.base.customer.plugin.INVOICE_DIR/'state.json'
        value=json.loads(path.read_text());next(iter(value['invoices'].values()))['unsigned']='corrupt'
        path.write_text(json.dumps(value));before=path.read_bytes()
        with self.assertRaisesRegex(hg.Refused,'publication_intent_changed'):
            fixture.after_stop(self.lab,topologies,history_count=0)
        self.assertFalse((root/fixture.barrier.MARKER).exists())
        self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
