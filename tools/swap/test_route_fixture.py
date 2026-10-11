#!/usr/bin/env python3
"""Verify the fixture's API/dependency assumptions against the pinned source."""
import ast
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import route_plan as rp
import test_route_engine as fixture
from test_route_plan import Fake

ROOT = Path(__file__).resolve().parents[2]


class FixtureTests(unittest.TestCase):
    def test_installed_router_name_and_same_binary_worker(self):
        if not (ROOT/'plugins/Makefile').exists(): self.skipTest('source-only check')
        self.assertIn('plugins/cln-askrene', (ROOT/'plugins/Makefile').read_text())
        self.assertIn('plugins/cln-askrene:', (ROOT/'plugins/askrene/Makefile').read_text())
        lab = object.__new__(fixture.RouteLab)
        command = ['/opt/xbt/bin/lightningd','--disable-plugin=cln-askrene','--disable-plugin=cln-xpay']
        with patch.object(fixture.gate.GateLab,'start',side_effect=lambda command,log:command):
            result = lab.start(command,Path('/results/node/console.log'))
        self.assertNotIn('--disable-plugin=cln-askrene', result)
        self.assertIn('--disable-plugin=cln-xpay', result)
        self.assertIn('--cltv-delta=80', result)
        self.assertIn('--fee-per-satoshi=0', result)
        self.assertIn('--dev-fast-gossip', result)

    def test_all_literal_rpc_parameters_against_pinned_schemas(self):
        schemas = ROOT/'doc/schemas'
        if not schemas.is_dir(): self.skipTest('source-only check')
        for source in (Path(rp.__file__),Path(fixture.__file__)):
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if not isinstance(node,ast.Call): continue
                name = node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
                method, keys = None, None
                if name == 'rpc' and isinstance(node.func,ast.Name) and node.args and isinstance(node.args[0],ast.Constant):
                    method, keys = node.args[0].value, {k.arg for k in node.keywords}
                elif name == 'rpc_named' and len(node.args) == 3 and isinstance(node.args[1],ast.Constant):
                    params = node.args[2]
                    if isinstance(params,ast.Call) and isinstance(params.func,ast.Name) and params.func.id == 'dict':
                        method, keys = node.args[1].value, {k.arg for k in params.keywords}
                if method is None or method.startswith('swap-gate-'): continue
                request = json.loads((schemas/(method+'.json')).read_text())['request']
                self.assertLessEqual(keys,set(request['properties']),method)
                self.assertLessEqual(set(request['required']),keys,method)

    def test_rpc_adapter_keeps_only_numeric_errors_and_denies_writes(self):
        class Lab:
            def rpc_named(self,*args):
                raise fixture.FixtureRPCError(subprocess.CompletedProcess(['getroutes'],1,
                    stdout='{"code":205,"message":"private invoice details"}',stderr=''), 'getroutes',Path('/tmp/synthetic'))
        rpc = fixture.adapter(Lab(),dict(cli=['fixture']))
        with self.assertRaises(rp.RPCError) as caught: rpc('getroutes')
        self.assertEqual(caught.exception.code,205)
        self.assertNotIn('private',str(caught.exception))
        with self.assertRaisesRegex(RuntimeError,'read_only_adapter_mutation_refused'): rpc('sendpay')

    def test_one_send_site_no_automatic_payment_rpc(self):
        tree = ast.parse(Path(fixture.__file__).read_text())
        methods = [n.args[1].value for n in ast.walk(tree) if isinstance(n,ast.Call)
                   and isinstance(n.func,ast.Attribute) and n.func.attr == 'rpc_named'
                   and len(n.args) > 1 and isinstance(n.args[1],ast.Constant)]
        self.assertEqual(methods.count('sendpay'),1)
        self.assertFalse({'pay','xpay','sendonion','getroute'} & set(methods))

    def test_saved_plan_reload_and_attempt_changes(self):
        rpc = Fake(); rpc.hinted(); original = rpc.plan()
        reloaded = json.loads(json.dumps(original))
        self.assertEqual(rp.digest(original),rp.digest(reloaded))
        rpc.payments = [rpc.attempt(reloaded)]
        identity = rp.observe_attempt(rpc,reloaded)['identity']
        self.assertEqual(rp.observe_attempt(rpc,json.loads(json.dumps(original)),json.loads(json.dumps(identity)))['state'],'pending')
        rpc.payments[0]['groupid'] = 2
        with self.assertRaises(ValueError): rp.observe_attempt(rpc,reloaded,identity)


if __name__ == '__main__': unittest.main()
