#!/usr/bin/env python3
"""Pinned schemas and real process-death driver with synthetic RPC replies."""
import ast
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import bounded_authority as ba
import cross_contract as cc
import cross_worker as cw
import fixture_cross_worker as driver
import held_gate as hg
import test_cross_engine as fixture
import test_customer_engine as customer
from test_cross_worker import Remote


class Tests(unittest.TestCase):
    def test_new_literal_RPCs_match_pinned_schemas(self):
        schemas = Path(__file__).resolve().parents[2]/'doc/schemas'
        if not schemas.is_dir(): self.skipTest('source-only schema audit')
        for node in ast.walk(ast.parse(Path(fixture.__file__).read_text())):
            if not (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                    and node.func.attr=='rpc_named' and len(node.args)>2
                    and isinstance(node.args[1],ast.Constant)):
                continue
            method = node.args[1].value
            if method.startswith('swap-gate-'): continue
            params = node.args[2]
            if not (isinstance(params,ast.Call) and isinstance(params.func,ast.Name) and params.func.id=='dict'):
                continue
            keys = {k.arg for k in params.keywords}
            schema = json.loads((schemas/(method+'.json')).read_text())['request']
            self.assertLessEqual(keys,set(schema['properties']),method)
            self.assertLessEqual(set(schema['required']),keys,method)

    def test_private_driver_uses_only_commando_and_faults_are_separate(self):
        source = Path(cw.__file__).read_text()
        for name in ('subprocess','SIGKILL','fault-consumed','commando','rune'):
            self.assertNotIn(name,source)
        self.assertIn("'-k','commando'",Path(driver.__file__).read_text())
        for amount in (1001000,4086000):
            delta = fixture.deltas(amount,True)
            self.assertEqual(sum(delta),0); self.assertEqual(delta[-1],amount)
            self.assertEqual(-delta[0]-delta[-1],2000)
        self.assertEqual(fixture.deltas(4086000,False),[0]*6)

    def test_actual_driver_SIGKILL_and_fresh_process_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); server = root/'synthetic-server.json'; r = Remote()
            # The worker uses real time in its subprocess. Shift the complete
            # synthetic contract and observations to the same current instant.
            import time
            from test_route_plan import NOW
            shift = int(time.time())-NOW
            for g in r.c['grants'].values(): g['created_at'] += shift; g['expires_at'] += shift
            for p in (r.c['incoming'],r.c['outgoing']): p['expires_at'] += shift
            r.c['outgoing']['created_at'] += shift; r.c['created_at'] += shift; r.c['pay_until'] += shift
            for key in ('fetched_at_ms','ticker_at_ms'): r.c['pricing'][key] += shift*1000
            r.c['pricing']['expires_at'] += shift
            for kind,o in r.obs.items(): o['checked_at'] += shift; o['info']['grant'] = r.c['grants'][kind]
            cc.validate(r.c)
            def save(): server.write_text(json.dumps(dict(c=r.c,obs=r.obs,entries=r.entries,calls=r.calls)))
            def load():
                value = json.loads(server.read_text())
                r.entries,r.calls = value['entries'],value['calls']
            save(); script = root/'fake_cli.py'
            script.write_text('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from test_cross_worker import Remote
import bounded_authority as ba
file=Path(sys.argv[1]); value=json.loads(file.read_text()); r=Remote()
r.c,r.obs,r.entries,r.calls=(value[k] for k in ('c','obs','entries','calls'))
assert sys.argv[3:5]==['-k','commando']
outer={k:json.loads(v) for k,v in (a.split('=',1) for a in sys.argv[5:])}
assert outer['method']=='swap-customer-call'
p=outer['params']; kind=next(k for k,g in r.c['grants'].items() if ba.digest(g)==p['grant_id'])
assert outer['peer_id']==r.c['grants'][kind]['chain']['node_id'] and outer['rune']=='fixture-'+kind
result=r(kind,p['grant_id'],p['operation'],p['body'])
value.update(entries=r.entries,calls=r.calls); file.write_text(json.dumps(value)); print(json.dumps(result))
''')
            clients = {k:SimpleNamespace(caller=dict(cli=[sys.executable,str(script),str(server),str(Path(cw.__file__).parent)]),
                node=dict(identity=g['chain']),gid=ba.digest(g),credential=dict(rune='fixture-'+k)) for k,g in r.c['grants'].items()}
            directory = root/'cross-worker-synthetic'; cw.initialize_fixture(directory)
            lab = SimpleNamespace(root=root,current_stage='synthetic-process-driver')
            d = fixture.Driver(lab,directory,clients); d.cid = d.run('prepare',contract=r.c)['contract_id']
            d.run('confirm',confirmation=d.cid)
            for kind in ('outgoing','incoming'):
                d.run('step',fault=dict(kind=kind,operation='enroll'),crash=True); d.run('step')
            d.run('step',fault=dict(kind='incoming',operation='invoice'),crash=True); d.run('step')
            saved = d.run('invoice'); load(); r.hold(); save()
            d.run('step',fault=dict(kind='outgoing',operation='send'),crash=True)
            d.run('step'); load(); r.complete(); save(); d.run('step')
            d.run('step',fault=dict(kind='incoming',operation='release'),crash=True)
            self.assertEqual(d.run('step')['state'],'settled'); self.assertEqual(d.run('invoice'),saved)
            load(); self.assertEqual(sum(op=='send' for _,op,_ in r.calls),1)
            self.assertGreater(d.processes,12)


if __name__ == '__main__': unittest.main()
