import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import receive_activation as a
from service_manager import private_load
from service_runtime import node_command
from swap_controller import save


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.swaps=self.root/'swaps';self.swaps.mkdir()
        self.btc=self.root/'btc';self.btc.mkdir();(self.btc/'btc-https-observer-v1').touch()
        self.path=self.root/'settings.json'
        self.old=dict(deployment='operator-pair-v1',btc_cli=['btc'],xbt_cli=['xbt'],
                      node_ids=['02'+'11'*32,'03'+'22'*32],roots=dict(btc=str(self.btc),xbt='/xbt'),
                      swap_root=str(self.swaps),receive_config={'legacy':'preserve in backup'},
                      reverse_live={'untouched':True},repo='/repo',python='/repo/.venv/bin/python')
        save(self.path,self.old);self.calls=[];self.pending=False;self.connected=True;self.held=False
        self.gates={};self.now=1000;self.limits={}
        self.reserve=patch.object(a,'require_reserves');self.reserve.start();self.addCleanup(self.reserve.stop)

    def rpc(self,cli,method,*args):
        self.calls.append(method)
        i=0 if cli==['btc'] else 1
        if method=='getinfo':return dict(id=self.old['node_ids'][i],network='bitcoin' if i==0 else 'xbt')
        if method=='listpeerchannels':return dict(channels=[dict(state='CHANNELD_NORMAL',peer_connected=self.connected,htlcs=[{}] if self.pending else [])])
        if method=='xbt-held':return dict(held=[{}] if self.held else [])
        if method=='xbt-pilot-info':return dict(profile='live-market-v1')
        if method in ('xbt-quote-status','reverse-status'):return self.gates[args[0]]
        raise AssertionError(method)

    def install(self):return a.install(self.path,self.limits,rpc=self.rpc,check_stopped=lambda:None,now=lambda:self.now)

    def quote(self,reverse=False,state=None,expiry=900):
        root=self.swaps/('reverse' if reverse else 'forward');root.mkdir(exist_ok=True)
        h='33'*32
        save(root/('reverse-quote.json' if reverse else 'quote.json'),dict(terms=dict(payment_hash=h,expires_at=expiry),node_ids=self.old['node_ids'],
             config=dict(btc_cli=['btc'],xbt_cli=['xbt'],node_ids=list(reversed(self.old['node_ids'])) if reverse else self.old['node_ids'])))
        self.gates[h]=dict(payment_hash=h,phase='quoted',binding=None)
        if state:
            binding=['1x1x1',0];phase=('xbt_' if reverse else 'btc_')+state
            save(root/('reverse-state.json' if reverse else 'state.json'),dict(phase=phase,payment_hash=h,**{('xbt_binding' if reverse else 'btc_binding'):binding}))
            self.gates[h].update(phase='resolved' if state=='released' else 'failed',binding=binding)
        return root

    def test_install_repeat_preserves_other_settings_and_private_backup(self):
        before=self.path.read_bytes();self.install();new=private_load(self.path)
        self.assertEqual(new['reverse_live'],self.old['reverse_live'])
        self.assertNotIn('receive_config',new);self.assertEqual(new['receive_policy']['profile'],a.live.PROFILE)
        plan=self.root/'routed-receive-activation.json'
        self.assertEqual(private_load(plan)['old'],self.old)
        self.assertEqual(plan.stat().st_mode&0o777,0o600)
        snapshots=(self.path.read_bytes(),plan.read_bytes());self.install()
        self.assertEqual(snapshots,(self.path.read_bytes(),plan.read_bytes()))
        self.assertNotEqual(before,self.path.read_bytes())
        self.assertTrue(set(self.calls)<= {'getinfo','listpeerchannels','xbt-held','xbt-pilot-info'})

    def test_interrupted_settings_write_resumes_same_plan(self):
        real=a.save
        def fail(path,value):
            if path==self.path:raise OSError('simulated')
            real(path,value)
        with patch.object(a,'save',side_effect=fail),self.assertRaises(OSError):self.install()
        self.assertEqual(private_load(self.path),self.old)
        self.install();self.assertEqual(private_load(self.path)['receive_policy']['profile'],a.live.PROFILE)

    def test_changed_limits_or_settings_refused_after_plan(self):
        self.install();self.limits={'btc':1600}
        with self.assertRaises(a.Blocked):self.install()
        self.limits={};changed=private_load(self.path);changed['extra']=1;save(self.path,changed)
        with self.assertRaises(a.Blocked):self.install()

    def test_pending_disconnected_and_held_refused_without_writes(self):
        for key,value in [('pending',True),('connected',False),('held',True)]:
            original=getattr(self,key);setattr(self,key,value)
            with self.assertRaises(a.Blocked):self.install()
            self.assertEqual(private_load(self.path),self.old)
            self.assertFalse((self.root/'routed-receive-activation.json').exists())
            setattr(self,key,original)

    def test_unexpired_quote_blocks_expired_unstarted_allowed(self):
        root=self.quote(expiry=1100)
        with self.assertRaises(a.Blocked):self.install()
        q=private_load(root/'quote.json');q['terms']['expires_at']=900;save(root/'quote.json',q)
        self.install()

    def test_terminal_forward_and_reverse_history_preserved(self):
        roots=[self.quote(state='released'),self.quote(reverse=True,state='released')]
        before={p:p.read_bytes() for root in roots for p in root.iterdir()}
        self.install();self.assertTrue(all(p.read_bytes()==b for p,b in before.items()))

    def test_nonterminal_and_changed_binding_refused(self):
        root=self.quote(state='released');p=root/'state.json';s=private_load(p)
        s['phase']='outgoing_started';save(p,s)
        with self.assertRaises(a.Blocked):self.install()
        s['phase']='btc_released';s['btc_binding']=['wrong',2];save(p,s)
        with self.assertRaises(a.Blocked):self.install()

    def test_services_must_be_stopped_before_any_rpc_or_write(self):
        with self.assertRaises(ValueError):
            a.install(self.path,{},rpc=self.rpc,check_stopped=lambda:(_ for _ in ()).throw(ValueError()))
        self.assertFalse(self.calls);self.assertEqual(private_load(self.path),self.old)

    def test_launcher_binds_settings_to_original_btc_root(self):
        self.install();self.assertEqual(a.launcher_profile(self.path,self.btc),a.live.PROFILE)
        with self.assertRaises(a.Blocked):a.launcher_profile(self.path,self.root/'other')
        settings=private_load(self.path);settings['receive_policy']['node_ids'].reverse();save(self.path,settings)
        with self.assertRaises(ValueError):a.launcher_profile(self.path,self.btc)

    def test_runtime_passes_profile_only_when_explicitly_configured(self):
        values={k:'value' for k in ('BTC_RPC_HOST','BTC_RPC_PORT','BTC_RPC_USER','BTC_RPC_PASSWORD','BTC_RPC_CA','BTC_LN_HOST')}
        save(self.root/'btc-rpc.json',values)
        cmd,_=node_command(self.old,self.root,'btc')
        self.assertFalse(any(s.startswith('--routed-receive-settings=') for s in cmd))
        self.install();cmd,_=node_command(private_load(self.path),self.root,'btc')
        self.assertIn('--routed-receive-settings='+str(self.path),cmd)

    def test_invalid_caps_and_symlink_settings_refused(self):
        for limits in ({'btc':10001},{'xbt':500001},{'fee':101},{'fee':-1},{'delay':1843}):
            with self.assertRaises(ValueError):a.candidate(self.old,**limits)
        original=self.root/'original.json';self.path.rename(original);self.path.symlink_to(original)
        with self.assertRaises(OSError):self.install()


if __name__=='__main__':unittest.main()
