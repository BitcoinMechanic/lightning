#!/usr/bin/env python3
"""Actual package launcher/bootstrap on funded regtest channels; never mainnet."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import chain_identity as ci
import held_gate as hg
import node_bootstrap as boot
import test_engine as engine
import test_gate_engine as gate
import test_deadline_engine as deadline

NODE=Path('/usr/local/libexec/swap-node/package_node.py')


class Lab(engine.Lab):
    engine_versions={**deadline.VERSIONS,ci.PRIVKEYIO_XBT:(*deadline.VERSIONS[ci.PRIVKEYIO_XBT],'v26.06.9-blake2b.7-swap0088b')}
    before_xbt=True
    def start(self,command,log):
        if Path(command[0]).name=='lightningd':
            profile=ci.PRIVKEYIO_XBT if '/xbt/' in str(command[0]) or '/xbt-before/' in str(command[0]) else ci.BTC
            if profile==ci.PRIVKEYIO_XBT and self.before_xbt:return super().start(command,log)
            root=next(str(a).split('=',1)[1] for a in command if str(a).startswith('--lightning-dir='))
            extra=[str(a) for a in command[1:] if not str(a).startswith(('--network=','--lightning-dir='))]
            command=[NODE,'--disposable-regtest','--profile',profile,'--root',root,'launch',*extra]
        return super().start(command,log)
    def lightning(self,name,profile,backend,**kw):
        node=super().lightning(name,profile,backend,**kw)
        node.update(name=name,profile=profile,backend=backend)
        return node


def run(lab):
    result=[]
    for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]:
        lab.checkpoint(label+'-fund-channel-before-coordinator-setup')
        backend=lab.backend(label+'-backend',profile==ci.PRIVKEYIO_XBT)
        if profile==ci.PRIVKEYIO_XBT:lab.args.xbt_prefix=Path('/opt/xbt-before')
        a=lab.lightning(label+'-payer',profile,backend);b=lab.lightning(label+'-receiver',profile,backend)
        lab.rpc(backend['cli'],'sendtoaddress',engine.funding_address(lab,a),'0.01');lab.mine(backend);lab.synced(backend,a,b)
        lab.rpc(a['cli'],'connect',b['identity']['node_id'],'127.0.0.1',b['port'])
        lab.rpc(a['cli'],'fundchannel',b['identity']['node_id'],300000,timeout=90)
        lab.mine(backend);lab.synced(backend,a,b);gate.connect(lab,a,b)
        old_binary=(lab.args.xbt_prefix if profile==ci.PRIVKEYIO_XBT else lab.args.btc_prefix)/'bin/lightningd'
        version_before=lab.rpc(a['cli'],'getinfo')['version'];sha_before=hashlib.sha256(old_binary.read_bytes()).hexdigest()
        before={n['name']:(n['identity'],hg.channel_pin(gate.channel(lab,n),profile)) for n in (a,b)}
        gate.ordinary_payment(lab,a,b,label+'-before-setup')
        lab.checkpoint(label+'-atomic-setup-and-same-volume-restart')
        snapshots={}
        for n in (a,b):
            cmd=[NODE,'--disposable-regtest','--profile',profile,'--root',str(n['root']),'prepare']
            def prepare():
                p=subprocess.run(list(map(str,cmd)),input='{"confirmed":true}',capture_output=True,text=True,timeout=60)
                engine.require(p.returncode==0 and not p.stderr,'bootstrap_prepare_failed')
                return json.loads(p.stdout)
            engine.require(prepare()['initialized'] is True,'bootstrap_not_initialized')
            namespace=n['root']/'regtest'/boot.DIRECTORY
            saved={str(p.relative_to(namespace)):p.read_bytes() for p in namespace.rglob('*') if p.is_file()}
            engine.require(prepare()['existing_records'] is True,'bootstrap_lost_reply_changed')
            engine.require(saved=={str(p.relative_to(namespace)):p.read_bytes() for p in namespace.rglob('*') if p.is_file()},'bootstrap_repeat_modified_state')
            snapshots[n['name']]=saved
        for n in (b,a):lab.stop(n['proc']);engine.require(n['proc'].returncode in (0,-15),'bootstrap_shutdown_not_graceful')
        for n in (a,b):
            for journal in boot.JOURNALS:
                def released(n=n,journal=journal):
                    j=hg.Journal(n['root']/'regtest'/boot.DIRECTORY/journal);j.close();return True
                lab.wait(released,timeout=15)
        if profile==ci.PRIVKEYIO_XBT:
            lab.before_xbt=False;lab.args.xbt_prefix=Path('/opt/xbt')
        a=lab.lightning(a['name'],profile,backend);b=lab.lightning(b['name'],profile,backend)
        gate.connect(lab,a,b)
        for n in (a,b):
            methods={row['command'].split()[0] for row in lab.rpc(n['cli'],'help')['help']}
            engine.require({'swap-gate-status','swap-customer-call','swap-customer-credential'}<=methods,'bootstrap_plugins_missing')
            engine.require((n['identity'],hg.channel_pin(gate.channel(lab,n),profile))==before[n['name']],'bootstrap_channel_or_identity_changed')
            engine.require(lab.rpc(n['cli'],'listclosedchannels')['closedchannels']==[],'bootstrap_closed_channel')
            for rel,raw in snapshots[n['name']].items():
                engine.require((n['root']/'regtest'/boot.DIRECTORY/rel).read_bytes()==raw,'bootstrap_restart_rewrote_state')
        gate.ordinary_payment(lab,a,b,label+'-after-setup')
        new_binary=(lab.args.xbt_prefix if profile==ci.PRIVKEYIO_XBT else lab.args.btc_prefix)/'bin/lightningd'
        result.append(dict(profile=profile,version_before=version_before,version_after=lab.rpc(a['cli'],'getinfo')['version'],
                           engine_before=sha_before,engine_after=hashlib.sha256(new_binary.read_bytes()).hexdigest(),payments=2,original_channel=True,original_identity=True,closed_channels=0,
                           repeated_setup_unchanged=True,static_plugins_ready=True,regtest_only=True))
    lab.checkpoint('complete')
    (lab.root/'bootstrap-result.json').write_text(json.dumps(dict(schema=1,fixture='0102-bootstrap',cases=result),indent=2)+'\n')
    print('BOOTSTRAP PASSED: both native engines; funded channels retained, four payments; regtest only',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--disposable-regtest',required=True,action='store_true')
    p.add_argument('--bitcoind',type=Path,required=True);p.add_argument('--bitcoin-cli',type=Path,default=Path('/usr/bin/bitcoin-cli'))
    p.add_argument('--btc-prefix',type=Path,default=Path('/opt/btc'));p.add_argument('--xbt-prefix',type=Path,default=Path('/opt/xbt'))
    p.add_argument('--results-parent',type=Path,required=True);args=p.parse_args()
    lab=Lab(args)
    try:run(lab)
    finally:lab.close()
