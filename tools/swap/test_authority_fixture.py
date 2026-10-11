#!/usr/bin/env python3
"""Pinned API and authority fixture boundaries; not daemon execution evidence."""
import ast
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import bounded_authority as ba
import chain_identity as ci
import held_gate as hg
import test_authority_engine as fixture
from test_bounded_authority import Node, bounds
from test_route_plan import NOW, pin

ROOT = Path(__file__).resolve().parents[2]


class FixtureTests(unittest.TestCase):
    def test_literal_request_arguments_match_engine_schema(self):
        schemas = ROOT/'doc/schemas'
        if not schemas.is_dir(): self.skipTest('source-only check')
        methods = set()
        for source in (Path(ba.__file__), Path(fixture.__file__)):
            for node in ast.walk(ast.parse(source.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute): continue
                method, keys = None, None
                if node.func.attr == 'rpc' and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self':
                    if node.args and isinstance(node.args[0], ast.Constant):
                        method, keys = node.args[0].value, {k.arg for k in node.keywords}
                if node.func.attr == 'rpc_named' and len(node.args) >= 3 and isinstance(node.args[1], ast.Constant):
                    p = node.args[2]
                    if isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == 'dict':
                        method, keys = node.args[1].value, {k.arg for k in p.keywords}
                if method is None or method.startswith('swap-gate-'): continue
                spec = json.loads((schemas/(method+'.json')).read_text())['request']; methods.add(method)
                self.assertLessEqual(keys, set(spec['properties']), method)
                self.assertLessEqual(set(spec['required']), keys, method)
        self.assertLessEqual({'sendpay', 'listhtlcs', 'listsendpays', 'decode', 'invoice', 'fundchannel'}, methods)

    def test_mutations_only_in_authority_and_no_generic_dispatch(self):
        actual = []
        for node in ast.walk(ast.parse(Path(ba.__file__).read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'rpc'
                    and node.args and isinstance(node.args[0], ast.Constant)):
                actual.append(node.args[0].value)
        self.assertEqual(actual.count('sendpay'), 1)
        self.assertLessEqual(set(actual), ba.METHODS)
        self.assertFalse({'pay', 'xpay', 'sendonion', 'close', 'createrune', 'getroutes'} & ba.METHODS)
        for node in ast.walk(ast.parse(Path(fixture.__file__).read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'rpc_named':
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    self.assertNotIn(node.args[1].value, ('sendpay', 'swap-gate-register', 'swap-gate-release', 'swap-gate-fail'))

    def test_worker_uses_fresh_locked_state_and_preserves_lost_send(self):
        node = Node()
        class Lab:
            def rpc_named(self, cli, method, params, timeout): return node(method, **params)
        with tempfile.TemporaryDirectory() as folder:
            ref = dict(root=Path(folder), identity=node.chain, cli=['fake'])
            worker = fixture.Worker(Lab(), ref, fresh=True); worker.clock = lambda:NOW
            gid = worker.call('create_grant', 'outgoing', [pin()], bounds(), NOW+3600, confirmed=True)
            data = node.plan(); node.plan_value = data
            eid = worker.call('enroll', gid, data, confirmed=True)
            fixture.lost(worker, 'sendpay', 'send', eid)
            other = fixture.Worker(Lab(), ref); other.clock = lambda:NOW
            self.assertEqual(other.call('send', eid)['state'], 'pending')
            self.assertNotIn('sendpay', other.calls)
            self.assertEqual(worker.record()['entries'][eid]['payload'], data)

    def test_worker_error_privacy_and_raw_rpc_denial(self):
        class Lab:
            def rpc_named(self, *args, **kwargs):
                raise fixture.FixtureRPCError(subprocess.CompletedProcess(['synthetic'], 1,
                      stdout='{"code":215,"message":"private details"}', stderr=''), 'sendpay', Path('/tmp/fake'))
        worker = fixture.Worker(Lab(), dict(root=Path('/tmp'), identity={}, cli=[]))
        with self.assertRaisesRegex(RuntimeError, 'authority_fixture_rpc_not_permitted'): worker.rpc('close')
        with self.assertRaises(ValueError) as caught: worker.rpc('sendpay')
        self.assertEqual(caught.exception.code, 215)
        self.assertNotIn('private', str(caught.exception))

    def test_restored_path_and_incoming_history_replacement_refused(self):
        node = Node()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)/'auth'; ba.initialize_fixture(directory, node.chain)
            journal = hg.Journal(directory)
            try:
                authority = ba.Authority(journal, node.chain, node, lambda:NOW)
                gid = authority.create_grant('incoming', [pin()], bounds('incoming'), NOW+3600, confirmed=True)
                eid = authority.enroll(gid, node.quote_data(), confirmed=True)
                authority.publish(eid); node.hold(); authority.reconcile(eid)
                node.history_rows[0]['created_index'] += 1
                with self.assertRaisesRegex(ValueError, 'original_htlc_changed'): authority.reconcile(eid)
                (directory/'restored.json').symlink_to('missing')
                with self.assertRaisesRegex(ValueError, 'restored_authority_blocked'): authority.resolve(eid)
            finally: journal.close()


if __name__ == '__main__': unittest.main()
