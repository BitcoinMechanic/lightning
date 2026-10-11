#!/usr/bin/env python3
"""Pinned RPC schemas, plugin framing, and actual fixture lifecycle paths."""
import ast
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import authority_plugin as plugin
import bounded_authority as ba
import fixture_authority_plugin as fault
import gate_plugin as gp
import held_gate as hg
import remote_authority as remote
import test_remote_engine as fixture
from test_bounded_authority import Node, bounds
from test_remote_authority import rune_row
from test_route_plan import NOW, pin


class FixtureTests(unittest.TestCase):
    def test_all_literal_node_arguments_match_pinned_schema(self):
        schemas = Path(__file__).resolve().parents[2]/'doc/schemas'
        if not schemas.is_dir(): self.skipTest('source-only schema check')
        observed = set()
        for source in (Path(remote.__file__), Path(fixture.__file__)):
            for n in ast.walk(ast.parse(source.read_text())):
                if not isinstance(n,ast.Call) or not isinstance(n.func,ast.Attribute): continue
                method = fields = None
                if n.func.attr == 'rpc' and n.args and isinstance(n.args[0],ast.Constant):
                    method = n.args[0].value; fields = {k.arg for k in n.keywords}
                if n.func.attr == 'rpc_named' and len(n.args)>2 and isinstance(n.args[1],ast.Constant):
                    p = n.args[2]
                    if isinstance(p,ast.Call) and isinstance(p.func,ast.Name) and p.func.id == 'dict' and not p.args:
                        method = n.args[1].value; fields = {k.arg for k in p.keywords}
                if method is None: continue
                spec = json.loads((schemas/(method+'.json')).read_text())['request']
                self.assertLessEqual(fields,set(spec['properties']),method)
                self.assertLessEqual(set(spec['required']),fields,method); observed.add(method)
        self.assertLessEqual({'showrunes','createrune','checkrune','blacklistrune','commando','fundchannel'},observed)

    def test_service_uses_network_directory_fresh_state_and_fixed_errors(self):
        node = Node()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); network = base/'regtest'; network.mkdir()
            self.assertEqual(fixture.state_dir(dict(root=base)),network)
            ba.initialize_fixture(network/plugin.AUTHORITY_DIR,node.chain)
            remote.initialize_fixture(network/plugin.REMOTE_DIR,node.chain)
            j = hg.Journal(network/plugin.AUTHORITY_DIR)
            try:
                a = ba.Authority(j,node.chain,node,lambda:NOW)
                gid = a.create_grant('outgoing',[pin()],bounds(),NOW+3600,confirmed=True)
            finally: j.close()
            inventory = []
            def rpc(method,**params):
                if method == 'showrunes': return dict(runes=copy.deepcopy(inventory))
                if method == 'createrune':
                    inventory.append(rune_row(gid,'outgoing'))
                    return {k:inventory[-1][k] for k in ('rune','unique_id')}
                return node(method,**params)
            config = dict(network='regtest',startup=True,**{'lightning-dir':str(network),'rpc-file':'lightning-rpc'})
            service = plugin.Service(config,ba.ci.BTC,lambda config:rpc,clock=lambda:NOW)
            creds = service.invoke(remote.CREDENTIAL,dict(grant_id=gid,confirmed=True))
            params = dict(grant_id=gid,operation='info',body={})
            self.assertEqual(service.invoke(remote.CALL,params)['remaining'],2)
            # Acquire both locks independently after an RPC: no plugin-held
            # writer survives an operation or obstructs local pause/restore.
            j = hg.Journal(network/plugin.AUTHORITY_DIR)
            try: ba.Authority(j,node.chain,node,lambda:NOW).pause(gid)
            finally: j.close()
            self.assertTrue(service.invoke(remote.CALL,params)['paused'])
            self.assertEqual(creds,service.invoke(remote.CREDENTIAL,dict(grant_id=gid,confirmed=True)))
            (network/plugin.REMOTE_DIR/'restored.json').write_text('{}')
            with self.assertRaises(ValueError): service.invoke(remote.CALL,params)
            with self.assertRaises(ValueError): plugin.Service(config,ba.ci.PRIVKEYIO_XBT,lambda config:rpc)
        class RefusingService:
            def __init__(self,*args): pass
            def invoke(self,*args): raise RuntimeError('rune secret invoice preimage')
        requests = [dict(id=1,method='init',params=dict(configuration=config,options={'swap-authority-profile':ba.ci.BTC})),
                    dict(id=2,method=remote.CALL,params=params)]
        out = io.StringIO()
        with patch.object(plugin.sys,'stdin',io.StringIO(''.join(json.dumps(r)+'\n\n' for r in requests))),\
             patch.object(plugin.sys,'stdout',out):
            plugin.main(service_factory=RefusingService)
        replies = list(gp.requests(io.StringIO(out.getvalue())))
        self.assertEqual(replies[-1]['error'],dict(code=-32603,message='swap authority reply uncertain'))
        self.assertNotIn('secret',out.getvalue())

    def test_local_rpc_validates_frame_id_error_and_method(self):
        class Socket:
            def __init__(self,reply):self.reply=reply;self.sent=[]
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,*args):pass
            def connect(self,*args):pass
            def sendall(self,data):self.sent.append(data)
            def recv(self,*args):return self.reply
        rpc=plugin.RPC({'lightning-dir':'/fixture/regtest','rpc-file':'lightning-rpc'})
        for reply in (dict(id='wrong',result={}),dict(id='authority-local',error=dict(code=215,message='private'))):
            with patch.object(plugin.socket,'socket',return_value=Socket(json.dumps(reply).encode()+b'\n\n')):
                with self.assertRaises(ValueError) as error: rpc('getinfo')
                self.assertNotIn('private',str(error.exception))
        socket=Socket(b'{"id":"authority-local","result":{"x":1}}\n\n')
        with patch.object(plugin.socket,'socket',return_value=socket):self.assertEqual(rpc('getinfo'),dict(x=1))
        self.assertEqual(json.loads(socket.sent[0])['params'],{})

    def test_fixture_mutation_counter_precedes_lost_reply_and_clock_is_local(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);config={'lightning-dir':folder,'rpc-file':'lightning-rpc'}
            rpc=fault.RPC(config);(path/'authority-drop-reply').write_text('createrune\n')
            with patch.object(plugin.RPC,'__call__',return_value={'synthetic':'private'}):
                with self.assertRaises(TimeoutError):rpc('createrune',restrictions=[])
            self.assertEqual((path/'authority-mutations.log').read_text(),'createrune\n')
            self.assertFalse((path/'authority-drop-reply').exists())
            self.assertNotIn('fixture-time',Path(plugin.__file__).read_text())
            self.assertNotIn('drop-reply',Path(plugin.__file__).read_text())


if __name__ == '__main__':unittest.main()
