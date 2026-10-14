#!/usr/bin/env python3
"""0098: exact packaged code/binaries on unchanged volumes, no on-chain closes.

The container contains two generations extracted directly from the six runtime
images. Changing generation replaces executable paths, not stored node state.
This is a package-content replacement test, not StartOS update acceptance.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import bounded_authority as ba
import chain_identity as ci
import cross_contract as cc
import cross_worker as cw
import customer_remote as remote
import held_gate as hg
import service
import test_coordinator_engine as admin
import test_cross_engine as cross
import test_customer_engine as customer
import test_deadline_engine as deadline
import test_https_engine as transport
import test_route_engine as routes
from test_engine import require

RELEASES=Path('/opt/releases')
SCRIPT=Path(__file__).with_name('fixture_package_service.py')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


class Client(customer.Client):
    def setup(self):
        params=dict(grant_id=self.gid,confirmed=True)
        self.credential=self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params)
        require(self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params)==self.credential,'credential_replaced')
    def mutations(self): return []  # Scope controls compare payment history separately below.


class Lab(transport.HttpsLab):
    engine_versions=deadline.VERSIONS
    def __init__(self,args):
        super().__init__(args);self.admin_receipts=[];self.select('a')
    def select(self,generation):
        require(generation in ('a','b'),'generation_invalid');self.generation=generation
        self.runtime=RELEASES/generation
        self.args.btc_prefix=self.runtime/'btc';self.args.xbt_prefix=self.runtime/'xbt'
        customer.PLUGIN=self.runtime/'node/package_customer.py'
        routes.gate.PLUGIN=self.runtime/'node/package_gate.py'
    def rpc(self,cli,*args,**kwargs):
        # Any accidental close/withdraw/recovery call aborts this test before
        # dispatch. Actual closed-channel/funding checks are independent below.
        require(not args or args[0] not in ('close','withdraw','emergencyrecover','recover'),
                'package_fixture_close_or_restore_refused')
        return super().rpc(cli,*args,**kwargs)


class Driver:
    def __init__(self,lab,directory):
        self.lab,self.directory=lab,directory;self.proc=None;self.cid=None
        if not directory.exists():self.command('init')
    def argv(self,mode):
        return [sys.executable,'-I',str(SCRIPT),str(self.lab.runtime/'controller'),
                '--disposable-regtest',str(self.directory),mode]
    def command(self,mode,request=None):
        until=time.monotonic()+120
        while True:
            p=subprocess.run(self.argv(mode),input='' if request is None else json.dumps(request),
                capture_output=True,text=True,timeout=120)
            require(not p.stderr,'packaged_service_stderr');r=json.loads(p.stdout)
            if p.returncode==0:return r['result']
            if r.get('reason')=='service_busy' and time.monotonic()<until:time.sleep(.05);continue
            raise RuntimeError('packaged_service_action_refused: '+str(r.get('reason')))
    def action(self,action,**fields):return self.command('action',dict(action=action,**fields))
    def start(self):
        self.proc=subprocess.Popen(self.argv('run'),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.lab.procs.append(self.proc)
        self.lab.wait(lambda:self.action('status')['worker_fresh'],self.proc)
    def stop(self,crash=False):
        if self.proc and self.proc.poll() is None:
            self.proc.kill() if crash else self.proc.terminate()
            out,err=self.proc.communicate(timeout=120)
            require(not out and not err and self.proc.returncode==(-signal.SIGKILL if crash else 0),
                    'packaged_service_stop_failed')
    def status(self):return next(s for s in self.action('status')['swaps'] if s['confirmation']==self.cid)
    def record(self):
        def read():
            j=hg.Journal(self.directory)
            try:return copy.deepcopy(service.validate(j.read())['jobs'][self.cid]['worker']['swaps'][self.cid])
            finally:j.close()
        return self.lab.wait(read, self.proc if self.proc and self.proc.poll() is None else None)
    def wait(self,fn):return self.lab.wait(fn,self.proc)


def credentials(topologies):
    return {asset:[copy.deepcopy(c.credential) for c in t[2]] for asset,t in topologies.items()}


def continuity(lab,topologies):
    result={}
    for asset,(_,nodes,clients) in topologies.items():
        result[asset]=dict(identities=[n['identity'] for n in nodes],
            pins=[hg.channel_pin(r,n['profile']) for n,r in routes.endpoints(lab,nodes)],
            credentials=[c.credential for c in clients],
            certificates={n['name']:{p.name:digest(p) for p in sorted((n['root']/'rest-certs').glob('*.pem'))}
                          for n in (nodes[0],nodes[-1])})
        for n in nodes:
            require(lab.rpc(n['cli'],'listclosedchannels')['closedchannels']==[], 'unexpected_closed_channel')
            require(all(r['state']=='CHANNELD_NORMAL' for r in lab.rpc(n['cli'],'listpeerchannels')['channels']),
                    'channel_not_normal')
    return copy.deepcopy(result)


def switch(lab,topologies,generation):
    before=continuity(lab,topologies)
    # Stop all nodes, wait for important plugin EOF and journal lock release,
    # then choose the replacement image generation without touching any volume.
    old=copy.copy(topologies)
    for _,nodes,_ in old.values():
        for n in reversed(nodes):lab.stop(n['proc'])
        require(all(n['proc'].returncode in (0,-15) for n in nodes),'node_shutdown_not_graceful')
        routes.gate.wait_for_gate_writer_exit(lab,nodes[-1])
        for n in (nodes[0],nodes[-1]):
            for name in ('bounded-authority-v1','customer-remote-v1','customer-invoices-v1'):
                def unlocked(n=n,name=name):
                    j=hg.Journal(customer.state_dir(n)/name);j.close();return True
                lab.wait(unlocked,timeout=15)
    lab.select(generation)
    for asset,(backend,previous,clients) in old.items():
        nodes=[lab.lightning(n['name'],n['profile'],backend) for n in previous]
        for a,b in zip(nodes,nodes[1:]):lab.rpc(a['cli'],'connect',b['identity']['node_id'],'127.0.0.1',b['port'])
        lab.synced(backend,*nodes)
        fresh=[Client(lab,n,p,c.gid) for n,p,c in zip((nodes[0],nodes[-1]),(nodes[1],nodes[-2]),clients)]
        for c,prior in zip(fresh,clients):c.credential=copy.deepcopy(prior.credential)
        topologies[asset]=(backend,nodes,fresh)
    require(continuity(lab,topologies)==before,'package_replacement_changed_identity_funding_or_credentials')


def admin_call(lab,node,**request):
    # Execute the packaged shebang, including -I; invoking "python FILE"
    # would discard the image entry point's interpreter-isolation option.
    p=subprocess.run([str(lab.runtime/'node/package_admin.py'),'--disposable-regtest',
        '--network-directory',str(customer.state_dir(node)),'--profile',node['identity']['profile']],
        input=json.dumps(request),capture_output=True,text=True,timeout=120)
    require(p.returncode==0 and not p.stderr,'packaged_admin_action_failed_'+request['action'])
    return json.loads(p.stdout)


def grant_setup(lab,node,kind,limits):
    limits=copy.deepcopy(limits)
    if kind=='incoming':limits['close_before_blocks']=72
    root=customer.state_dir(node)
    if not (root/admin.admin.DIRECTORY).exists():admin_call(lab,node,action='initialize',confirmed=True)
    before=(root/'bounded-authority-v1/state.json').read_bytes()
    review=admin_call(lab,node,action='review',kind=kind,limits=limits,lifetime_seconds=86400)
    require((root/'bounded-authority-v1/state.json').read_bytes()==before,'admin_review_installed_authority')
    require(review['channel_count']==1 and review['limits']==limits and review['phase']=='review','admin_review_changed')
    gid=review['confirmation']
    admin_call(lab,node,action='confirm',confirmation=gid,confirmed=True)  # discard reply
    saved=(root/'bounded-authority-v1/state.json').read_bytes()
    again=admin_call(lab,node,action='confirm',confirmation=gid,confirmed=True)
    require(again['confirmation']==gid and again['current'] is True and again['phase']=='saved'
        and (root/'bounded-authority-v1/state.json').read_bytes()==saved,'admin_repeat_changed_authority')
    require(len(json.loads(saved)['grants'])==len(json.loads(before)['grants'])+1,'admin_duplicate_grant')
    lab.admin_receipts.append(dict(profile=node['identity']['profile'],kind=kind,channel_count=1,
        review_without_authority=True,fresh_process_confirmation=True,lost_reply_same_grant=True))
    return gid


def scenario(lab,topologies,direction,name):
    lab.checkpoint(direction+'-packaged-'+name)
    ia,oa=('btc','xbt') if direction=='forward' else ('xbt','btc')
    _,ins,ic=topologies[ia];_,outs,oc=topologies[oa]
    for _,nodes,_ in topologies.values():routes.quiet(lab,nodes);routes.public_graph(lab,nodes)
    before=continuity(lab,topologies)
    balances={asset:[r['to_us_msat'] for _,r in routes.endpoints(lab,t[1])] for asset,t in topologies.items()}
    incoming,outgoing=ic[1],oc[0]
    bolt,preimage=cross.recipient_invoice(lab,outs,direction+'-package-'+name)
    d=Driver(lab,lab.root/('service-worker-'+direction))
    binding=dict(direction=direction,nodes={k:lab.config(c.node,c.credential['rune'],c.call('info')['grant'])
                  for k,c in [('incoming',incoming),('outgoing',outgoing)]})
    d.action('pair',binding=binding,confirmed=True)
    review=d.action('prepare',direction=direction,invoice=bolt);d.cid=review['confirmation'];c=d.record()['contract']
    require(review['markup_bps']==0 and review['routing_fee_msat']==2000,'packaged_review_changed')
    d.start();require(d.status()['state']=='ready_for_confirmation' and not d.record()['enroll'],'unconfirmed_enrollment')
    d.action('confirm',cid=d.cid,confirmation=d.cid,confirmed=True)
    d.wait(lambda:d.status()['state']=='waiting_for_payment')
    issued=d.action('invoice',cid=d.cid);customer_bolt=issued['invoice'];ph=c['incoming']['payment_hash']
    require(lab.rpc_named(outs[0]['cli'],'listsendpays',dict(payment_hash=ph))['payments']==[], 'send_before_payment')
    decoded=ci.invoice(lab.rpc_named(ins[0]['cli'],'decode',dict(string=customer_bolt)),ins[0]['profile'],'regtest')
    route=customer.payer_route(lab,ins,decoded)
    lab.rpc_named(ins[0]['cli'],'sendpay',dict(route=route,payment_hash=ph,payment_secret=c['incoming']['payment_secret'],
        bolt11=customer_bolt,groupid=1,partid=0))
    held_in=routes.gate.held(lab,ins[-1],ph);held_out=routes.gate.held(lab,outs[-1],ph)
    d.wait(lambda:d.status()['state']=='outgoing_pending');saved=d.record()
    require('protection' in saved['remote']['incoming']['entry'],'deadline_protection_not_armed')
    _,payer=cross.payer_attempt(lab,ins[0],customer_bolt,c)
    # Same-generation restart and image-generation replacement are separate
    # cases. Failed payment also resolves off-chain, leaving all channels open.
    generation_before=lab.generation
    if name in ('restart-held','update-held'):
        d.stop(crash=True)
        switch(lab,topologies,('b' if lab.generation=='a' else 'a') if name=='update-held' else lab.generation)
        _,ins,ic=topologies[ia];_,outs,oc=topologies[oa];incoming,outgoing=ic[1],oc[0]
        d=Driver(lab,d.directory);d.cid=cc.digest(c)
        require(d.action('invoice',cid=d.cid)==issued,'package_invoice_replaced')
        for nodes,held in ((ins,held_in),(outs,held_out)):
            now=routes.gate.held(lab,nodes[-1],ph)
            require(now['binding']==held['binding'] and now['cltv_expiry']==held['cltv_expiry'],'pending_HTLC_changed')
        d.start();d.wait(lambda:d.status()['state']=='outgoing_pending')
    params=dict(payment_hash=ph,binding=held_out['binding'])
    release=name!='fail'
    if release:params['preimage']=preimage
    lab.rpc_named(outs[-1]['cli'],'swap-gate-'+('release' if release else 'fail'),params)
    d.wait(lambda:d.status()['terminal']);wanted='settled' if release else 'failed'
    require(d.status()['state']==wanted,'packaged_terminal_state_changed');d.stop()
    lab.wait(lambda:cross.payer_attempt(lab,ins[0],customer_bolt,c,payer)[0]==('complete' if release else 'failed'),ins[0]['proc'])
    for _,nodes,_ in topologies.values():routes.quiet(lab,nodes)
    final=d.record()
    require(final['contract']==c and final['invoice']==issued and final['send']==saved['send'],'original_contract_or_send_changed')
    for kind in cw.KINDS:
        old,new=saved['remote'][kind]['entry'],final['remote'][kind]['entry']
        require(old['htlc']==new['htlc'],'original_HTLC_changed')
        field='incoming' if kind=='incoming' else 'attempt'
        require(old[field]==new[field] if kind=='incoming' else old[field]['identity']==new[field]['identity'],
                'original_binding_or_attempt_changed')
    for nodes in (ins,outs):
        rows=lab.rpc_named(nodes[0]['cli'],'listsendpays',dict(payment_hash=ph))['payments']
        require(len(rows)==1,'duplicate_payment_attempt')
    for asset,amount in ((ia,c['incoming']['amount_msat']),(oa,c['outgoing']['amount_msat'])):
        after=[r['to_us_msat'] for _,r in routes.endpoints(lab,topologies[asset][1])]
        require(after==[a+delta for a,delta in zip(balances[asset],cross.deltas(amount,release))],'twelve_balances_changed')
    require(continuity(lab,topologies)==before,'channel_or_identity_changed')
    require(len(d.action('history')['swaps'])==('restart-held','fail','update-held').index(name)+1,'history_lost')
    # Run a fresh packaged worker again against completed history. No mutation
    # and no second payment follows a discarded status reply or restart.
    d.start();d.stop();require(d.record()==final,'terminal_worker_changed_swap')
    print('PASS: '+direction+' '+name+'; packaged runtime, original channels/HTLCs/attempt, twelve balances; no closes',flush=True)
    return dict(direction=direction,scenario=name,state=wanted,generation_before=generation_before,generation_after=lab.generation,
        original_identity_channels_credentials=True,original_contract_invoice_HTLC_attempt=True,one_outgoing_attempt=True,
        exact_twelve_balances=True,deadline_armed=True,closed_channels=0,history_count=('restart-held','fail','update-held').index(name)+1,
        controller_SIGKILL=name!='fail',node_restart=name!='fail',package_replaced=name=='update-held')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--disposable-regtest',required=True,action='store_true')
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()));args=p.parse_args();os.umask(0o077)
    for n in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):setattr(args,n,getattr(args,n).resolve())
    customer.Client=Client;cross.RECIPIENT_MSAT=8000000
    lab=Lab(args)
    try:
        topologies={label:customer.topology(lab,label,profile,grant_amount_msat=50000000,grant_setup=grant_setup)
            for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]}
        backend,nodes,_=topologies['xbt'];lab.mine(backend,count=41);lab.synced(backend,*nodes)
        # Scope checks run against the packaged plugin entry points. Independently
        # compare persistent authority and core payment histories across refusals.
        before_scope={asset:[lab.rpc(n['cli'],'listsendpays') for n in (t[1][0],t[1][-1])]
                      for asset,t in topologies.items()}
        before_journals={str(p):digest(p) for t in topologies.values() for n in (t[1][0],t[1][-1])
                         for p in customer.state_dir(n).glob('*/state.json')}
        transport.scope_checks(lab,topologies)
        require(before_scope=={asset:[lab.rpc(n['cli'],'listsendpays') for n in (t[1][0],t[1][-1])]
            for asset,t in topologies.items()},'denied_scope_changed_payment_history')
        require(all(digest(Path(p))==h for p,h in before_journals.items()),'denied_scope_changed_authority')
        cases=[scenario(lab,topologies,d,n) for d in ('forward','reverse') for n in ('restart-held','fail','update-held')]
        for _,nodes,_ in topologies.values():
            for n in reversed(nodes):lab.stop(n['proc'])
        for direction in ('forward','reverse'):
            d=Driver(lab,lab.root/('service-worker-'+direction))
            require(len(d.action('history')['swaps'])==3,'offline_history_lost')
        result=dict(schema=1,fixture='privkeyio-packaged-runtime-0098',regtest_only=True,installer=False,
            startos_acceptance=False,synthetic_market_reader=True,fixture_code_in_runtime=False,
            engine_recompiled=False,backup_restore=False,closed_channels=0,grants=lab.admin_receipts,cases=cases)
        (lab.root/'package-result.json').write_text(json.dumps(result,indent=2)+'\n');lab.checkpoint('complete')
        print('PACKAGED RUNTIME FIXTURE PASSED: 0098; six swaps, ordinary restart and image-content replacement; no closes or installer',flush=True)
    finally:lab.close()

if __name__=='__main__':main()
