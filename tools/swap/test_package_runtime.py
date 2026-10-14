"""Packaged import, startup, privacy and fail-closed journal regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import package_runtime as build
import package_verify

SWAP=Path(__file__).parent
CONTROLLER=Path(os.environ['PACKAGE_CONTROLLER_SOURCE'])


class Packaged(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.node=self.root/'node';self.controller=self.root/'controller'
        build.stage(SWAP,CONTROLLER,self.node,'node','a')
        build.stage(SWAP,CONTROLLER,self.controller,'controller','a')
    def tearDown(self):self.tmp.cleanup()
    def cmd(self,script,args=(),data='',cwd=None,env=None):
        return subprocess.run([sys.executable,'-I',str(script),*map(str,args)],input=data,
            text=True,capture_output=True,timeout=10,cwd=cwd,env=env)
    def test_closure_and_both_plugin_manifests(self):
        for directory,role in ((self.node,'node'),(self.controller,'controller')):
            m=package_verify.verify(directory,role)
            self.assertFalse(any(n.startswith(('test_','fixture_')) for n in m['files']))
        for filename,method in [('package_gate.py','swap-gate-status'),('package_customer.py','swap-customer-call')]:
            p=self.cmd(self.node/filename,data=json.dumps(dict(jsonrpc='2.0',id=1,method='getmanifest',params={}))+'\n\n')
            self.assertEqual(p.returncode,0,p.stderr);self.assertIn(method,p.stdout)
    def test_missing_state_stays_missing_and_errors_do_not_leak(self):
        path=self.root/'service-secret-path'
        for mode,data in [('run',''),('action','{"action":"history"}')]:
            p=self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,mode],data)
            self.assertNotEqual(p.returncode,0);self.assertFalse(path.exists());self.assertNotIn(str(path),p.stdout+p.stderr)
    def test_explicit_init_history_and_duplicate_init_preserve_bytes(self):
        path=self.root/'service'
        p=self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,'init']);self.assertEqual(p.returncode,0,p.stdout)
        saved=(path/'state.json').read_bytes()
        p=self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,'init']);self.assertNotEqual(p.returncode,0)
        self.assertEqual((path/'state.json').read_bytes(),saved)
        p=self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,'action'],'{"action":"history"}')
        self.assertEqual(p.returncode,0,p.stdout);self.assertEqual(json.loads(p.stdout)['result']['swaps'],[])
    def test_tamper_extra_fixture_symlink_and_missing_dependency_refused(self):
        file=self.node/'chain_identity.py';raw=file.read_bytes();file.write_bytes(raw+b'\n')
        with self.assertRaises(ValueError):package_verify.verify(self.node,'node')
        file.write_bytes(raw);extra=self.node/'fixture_evil.py';extra.write_text('')
        with self.assertRaises(ValueError):package_verify.verify(self.node,'node')
        extra.unlink();file.unlink();file.symlink_to(SWAP/'chain_identity.py')
        with self.assertRaises(ValueError):package_verify.verify(self.node,'node')
        source=self.root/'source';source.mkdir();(source/'a.py').write_text('import unavailable_package\n')
        with self.assertRaisesRegex(ValueError,'runtime_dependency_missing'):build.closure(['a'],[source])
        (source/'a.py').write_text('import fixture_evil\n')
        with self.assertRaisesRegex(ValueError,'fixture_import_refused'):build.closure(['a'],[source])
    def test_isolated_entry_ignores_volume_and_pythonpath_imports(self):
        volume=self.root/'volume';volume.mkdir();(volume/'service_cli.py').write_text('raise RuntimeError("VOLUME_CODE_EXECUTED")')
        (volume/'sitecustomize.py').write_text('raise RuntimeError("VOLUME_SITE_EXECUTED")')
        path=volume/'state'
        p=self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,'init'],cwd=volume,
            env=dict(os.environ,PYTHONPATH=str(volume)))
        self.assertEqual(p.returncode,0,p.stdout+p.stderr);self.assertNotIn('VOLUME_',p.stdout+p.stderr)
    def test_network_guard_and_restored_state_stay_blocked(self):
        import authority_plugin, chain_identity as ci
        with self.assertRaisesRegex(ValueError,'static_regtest_required'):
            authority_plugin.Service(dict(network='bitcoin',startup=True),ci.BTC)
        path=self.root/'service';self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,'init'])
        saved=(path/'state.json').read_bytes();(self.root/'swap-restored.json').write_text('{}')
        p=self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,'run'])
        self.assertNotEqual(p.returncode,0);self.assertEqual((path/'state.json').read_bytes(),saved)
        p=self.cmd(self.controller/'package_service.py',['--disposable-regtest',path,'action'],'{"action":"history"}')
        self.assertTrue(json.loads(p.stdout)['result']['restored'])
    def test_packaged_CLI_TLS_and_pending_generation_replacement(self):
        import test_package_process
        for direction in ('forward','reverse'):
            for failed in (False,True):
                test_package_process.run(direction,failed,SWAP,CONTROLLER)

    def test_second_generation_same_code_different_manifest(self):
        b=self.root/'b';m=build.stage(SWAP,CONTROLLER,b,'node','b')
        self.assertEqual(m['files'],package_verify.verify(self.node,'node')['files'])
        self.assertNotEqual((b/'runtime-manifest.json').read_bytes(),(self.node/'runtime-manifest.json').read_bytes())
        with self.assertRaises(FileExistsError):build.stage(SWAP,CONTROLLER,b,'node','b')

if __name__=='__main__':unittest.main()
