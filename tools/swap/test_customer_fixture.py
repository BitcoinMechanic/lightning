#!/usr/bin/env python3
"""Pinned runtime APIs, payer route and plugin protocol; no funded claim."""
import ast
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import customer_invoice as invoice
import customer_plugin as plugin
import customer_remote as remote
import fixture_customer_plugin as fault
import test_customer_engine as fixture
from test_route_plan import Fake, BOUNDS, planner_reply


class Tests(unittest.TestCase):
    def test_literal_rpc_requests_match_pinned_schemas(self):
        schemas = Path(__file__).resolve().parents[2]/'doc/schemas'
        if not schemas.is_dir(): self.skipTest('source-only schema audit')
        observed = set()
        for source in (Path(invoice.__file__),Path(remote.__file__),Path(fixture.__file__)):
            for n in ast.walk(ast.parse(source.read_text())):
                if not isinstance(n,ast.Call) or not isinstance(n.func,ast.Attribute): continue
                method = fields = None
                if n.func.attr == 'rpc' and n.args and isinstance(n.args[0],ast.Constant):
                    method = n.args[0].value; fields = {k.arg for k in n.keywords}
                elif n.func.attr == 'rpc_named' and len(n.args)>2 and isinstance(n.args[1],ast.Constant):
                    p = n.args[2]
                    if isinstance(p,ast.Call) and isinstance(p.func,ast.Name) and p.func.id == 'dict' and not p.args:
                        method = n.args[1].value; fields = {k.arg for k in p.keywords}
                if method is None: continue
                spec = json.loads((schemas/(method+'.json')).read_text())['request']
                self.assertLessEqual(fields,set(spec['properties']),method)
                self.assertLessEqual(set(spec['required']),fields,method); observed.add(method)
        self.assertLessEqual({'signinvoice','decode','listfunds','listinvoices','getroutes','sendpay','listsendpays','commando'},observed)

    def test_actual_plugins_answer_new_manifest_and_exclude_raw_methods(self):
        request = json.dumps(dict(id=1,method='getmanifest',params={}))+'\n\n'
        for source in (plugin.__file__,fault.__file__):
            result = subprocess.run([sys.executable,source],input=request,capture_output=True,text=True,check=True)
            manifest = json.loads(result.stdout)['result']
            self.assertEqual({r['name'] for r in manifest['rpcmethods']},{remote.CALL,remote.CREDENTIAL})
            self.assertFalse(manifest['dynamic']); self.assertEqual(manifest['hooks'],[])
        self.assertNotIn('customer-drop-reply',Path(plugin.__file__).read_text())
        self.assertNotIn('customer-fixture-time',Path(plugin.__file__).read_text())

    def test_customer_payer_uses_public_prefix_and_signed_private_hint_without_widening_coordinator(self):
        f = Fake(); ids = [f.chain['node_id'],*(h['id'] for h in f.route)]
        nodes = [dict(identity=dict(node_id=v),cli=[str(i)]) for i,v in enumerate(ids)]
        decoded = dict(f.decoded,min_final_cltv_expiry=374,routes=[[dict(pubkey=ids[2],short_channel_id='999x2x1',
            fee_base_msat=1000,fee_proportional_millionths=0,cltv_expiry_delta=80)]])
        route = copy.deepcopy(f.route)
        for h in route: h['delay'] += 334
        prefix = planner_reply(route[:2]); prefix['routes'][0].update(amount_msat=2001000,final_cltv=454)
        class Lab:
            def rpc_named(self,cli,method,params):
                self.params = params; return prefix
        lab = Lab()
        def edge(lab,a,b): return dict(short_channel_id=f.route[nodes.index(a)]['channel'])
        with patch.object(fixture.routes,'edge',side_effect=edge): actual = fixture.payer_route(lab,nodes,decoded)
        route[-1]['channel'] = '999x2x1'
        self.assertEqual(actual,route); self.assertEqual(lab.params['maxparts'],1)
        self.assertEqual(BOUNDS['max_delay_blocks'],288)
        prefix['routes'][0]['path'][1]['cltv_out'] = 453
        with patch.object(fixture.routes,'edge',side_effect=edge),self.assertRaises(RuntimeError):
            fixture.payer_route(lab,nodes,decoded)


if __name__ == '__main__': unittest.main()
