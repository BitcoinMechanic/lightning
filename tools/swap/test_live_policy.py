"""Mainnet policy and synthetic recovery; these are not funded mainnet tests."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import chain_identity as ci
import cross_contract as cc
import cross_worker as cw
import held_gate as hg
import node_bootstrap as boot
import package_runtime
import package_verify
import service
from test_cross_worker import Remote
from test_route_plan import NOW
from test_customer_invoice import CustomerNode

SWAP=Path(__file__).parent
CONTROLLER=Path(os.environ['PACKAGE_CONTROLLER_SOURCE'])


class MainnetRemote(Remote):
    def __init__(self,direction):
        super().__init__(direction)
        for kind in ('incoming','outgoing'):
            g=self.c['grants'][kind]
            c=ci.binding(g['chain']['profile'],'mainnet',g['chain']['node_id'])
            g['chain']=c;self.c[kind]['chain']=copy.deepcopy(c)
            self.obs[kind]['info']['grant']=copy.deepcopy(g)
            self.obs[kind]['info']['regtest_only']=False
        self.c['outgoing']['invoice']='lnbc20n1'+'q'*150
        cc.validate(self.c)
    def status(self,kind):
        return dict(super().status(kind),regtest_only=False)
    def __call__(self,kind,gid,op,body):
        result=super().__call__(kind,gid,op,body)
        if op=='invoice':result['invoice']='lnbc20n1'+'q'*150
        return result


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def node(self,profile):
        n=CustomerNode(profile);n.chain=ci.binding(profile,'mainnet',n.chain['node_id'])
        n.info['network']='bitcoin'
        return n
    def test_fresh_explicit_setup_lost_reply_and_missing_journal_never_repaired(self):
        for profile in (ci.BTC,ci.PRIVKEYIO_XBT):
            root=self.root/profile/'bitcoin';root.mkdir(parents=True)
            n=self.node(profile)
            with self.assertRaises(ValueError):boot.prepare(root,profile,n)
            self.assertFalse((root/boot.DIRECTORY).exists())
            reply=boot.prepare(root,profile,n,confirmed=True);self.assertTrue(reply['restart_required'])
            state=root/boot.DIRECTORY
            before={str(p.relative_to(state)):p.read_bytes() for p in state.rglob('*') if p.is_file()}
            self.assertEqual(boot.activation(state,profile),n.chain)
            self.assertTrue(boot.prepare(root,profile,n,confirmed=True)['existing_records'])
            self.assertEqual(before,{str(p.relative_to(state)):p.read_bytes() for p in state.rglob('*') if p.is_file()})
            (state/'customer-invoices-v1/state.json').unlink()
            with self.assertRaises(FileNotFoundError):boot.prepare(root,profile,n,confirmed=True)
            self.assertFalse((state/'customer-invoices-v1/state.json').exists())
    def test_partial_setup_is_unpublished_and_restore_fence_covers_nested_namespace(self):
        root=self.root/'bitcoin';root.mkdir();n=self.node(ci.BTC)
        with patch.object(boot.os,'rename',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):boot.prepare(root,ci.BTC,n,confirmed=True)
        self.assertFalse((root/boot.DIRECTORY).exists());self.assertEqual(len(list(root.glob('.swap-setup-*'))),1)
        boot.prepare(root,ci.BTC,n,confirmed=True)
        (self.root/'node-restored.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'restored'):boot.prepare(root,ci.BTC,n,confirmed=True)
        self.assertTrue(hg.restore_barrier.blocked(root/boot.DIRECTORY/'swap-gate-v1'))
    def test_wrong_network_profile_and_existing_flat_authority_refused(self):
        root=self.root/'bitcoin';root.mkdir();n=CustomerNode(ci.BTC)
        with self.assertRaises(ValueError):boot.prepare(root,ci.BTC,n,confirmed=True)
        n=self.node(ci.PRIVKEYIO_XBT)
        with self.assertRaises(ValueError):boot.prepare(root,ci.BTC,n,confirmed=True)
        (root/'bounded-authority-v1').mkdir()
        with self.assertRaisesRegex(ValueError,'foreign_authority'):boot.prepare(root,ci.PRIVKEYIO_XBT,n,confirmed=True)
    def test_mainnet_invoice_prefix_and_shared_hrp_require_protocol_features(self):
        n=self.node(ci.BTC)
        self.assertTrue(ci.invoice_text('lnbc20n1'+'q'*100,n.chain))
        for value in ('lnbcrt20n1'+'q'*100,'lnxbt20n1'+'q'*100,'lnbc1'+'q'*100):self.assertFalse(ci.invoice_text(value,n.chain))
        for profile in (ci.BTC,ci.PRIVKEYIO_XBT):
            n=self.node(profile);ci.node(n.info,profile,'mainnet')
            with self.assertRaises(ValueError):ci.node(n.info,ci.BTC if profile==ci.PRIVKEYIO_XBT else ci.PRIVKEYIO_XBT,'mainnet')
    def test_cross_environment_and_changed_grant_do_not_gain_authority(self):
        remote=MainnetRemote('forward');c=remote.c
        old=cc.digest(c)
        c['grants']['incoming']['chain']['environment']='regtest'
        c['grants']['incoming']['chain']['network']='regtest'
        with self.assertRaises(ValueError):cc.validate(c)
        with self.assertRaises(ValueError):cc.confirm(c,old,remote.obs,now=NOW)
        service.initialize(self.root/'regtest')
        j=hg.Journal(self.root/'regtest')
        try:
            s=service.Service(j,factory=lambda _:self.fail('cross-network transport was attempted'))
            from test_customer_https import PEM
            config={k:dict(url='https://'+k+'.example',ca_pem=PEM,rune='A'*50,grant=MainnetRemote('forward').c['grants'][k]) for k in ('incoming','outgoing')}
            with self.assertRaisesRegex(ValueError,'environment'):s.pair(dict(direction='forward',nodes=config),confirmed=True)
        finally:j.close()
    def test_both_mainnet_directions_synthetic_failure_and_lost_send_restart(self):
        for direction in ('forward','reverse'):
            for failed in (False,True):
                with self.subTest(direction=direction,failed=failed):
                    root=self.root/(direction+str(failed));cw.initialize_fixture(root);remote=MainnetRemote(direction)
                    j=hg.Journal(root)
                    def opened():return cw.Worker(j,remote,lambda:NOW)
                    try:
                        w=opened();cid=w.prepare(remote.c);w.confirm(cid,cid)
                        for _ in range(3):w.step(cid)
                        issued=w.payment_invoice(cid);self.assertTrue(issued['invoice'].startswith('lnbc20n'))
                        remote.hold();remote.lost=('outgoing','send')
                        with self.assertRaises(TimeoutError):w.step(cid)
                        saved=copy.deepcopy(j.read()['swaps'][cid]['send']);j.close();j=hg.Journal(root)
                        w=opened();w.step(cid);remote.complete(failed)
                        for _ in range(4):w=opened();w.step(cid)
                        self.assertEqual(w.status(cid)['state'],'failed' if failed else 'settled')
                        self.assertIs(w.status(cid)['regtest_only'],False)
                        self.assertEqual(j.read()['swaps'][cid]['send'],saved)
                        self.assertEqual(w.payment_invoice(cid),issued)
                        self.assertEqual(sum(op=='send' for _,op,_ in remote.calls),1)
                    finally:j.close()
    def test_packaged_modes_pin_environment_without_migrating_old_service(self):
        target=self.root/'runtime';package_runtime.stage(SWAP,CONTROLLER,target,'controller','a',live=True)
        def call(path,mode,env):
            return subprocess.run([sys.executable,'-I',str(target/'package_service.py'),env,str(path),mode],
                input='{"action":"status"}',text=True,capture_output=True,timeout=10)
        for environment,flag,other in [('mainnet','--mainnet','--disposable-regtest'),('regtest','--disposable-regtest','--mainnet')]:
            path=self.root/environment
            self.assertEqual(call(path,'init',flag).returncode,0)
            before=(path/'state.json').read_bytes()
            self.assertNotEqual(call(path,'action',other).returncode,0)
            self.assertEqual((path/'state.json').read_bytes(),before)
            self.assertEqual(call(path,'action',flag).returncode,0)
        old=self.root/'old';package_runtime.stage(SWAP,CONTROLLER,old,'controller','a')
        p=subprocess.run([sys.executable,'-I',str(old/'package_service.py'),'--mainnet',str(self.root/'blocked'),'init'],capture_output=True)
        self.assertNotEqual(p.returncode,0);self.assertFalse((self.root/'blocked').exists())

if __name__=='__main__':unittest.main()
