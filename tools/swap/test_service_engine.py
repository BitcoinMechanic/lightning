#!/usr/bin/env python3
"""0084: continuously running controller service over native CLNrest; regtest only."""
import argparse
import copy
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
import held_gate as hg
import service
import test_cross_engine as cross
import test_customer_engine as customer
import test_https_engine as previous
import test_authority_engine as local
import test_route_engine as routes
from test_engine import require

SCRIPT=Path(__file__).with_name('fixture_service_driver.py')


class Driver:
    def __init__(self,lab,directory):
        self.lab,self.directory=lab,directory;self.proc=None;self.cid=None
        if not directory.exists():self.invoke(dict(mode='init'))

    def invoke(self,request):
        deadline=time.monotonic()+120
        while True:
            p=subprocess.run([sys.executable,str(SCRIPT),'--disposable-regtest'],
                input=json.dumps(dict(directory=str(self.directory),**request)),capture_output=True,text=True,timeout=120)
            require(not p.stderr,'service_driver_stderr')
            result=json.loads(p.stdout)
            if p.returncode==0:return result['result']
            if result.get('reason')=='service_busy' and time.monotonic()<deadline:time.sleep(.05);continue
            raise RuntimeError('service_action_failed:'+json.dumps(result))

    def action(self,action,**fields):return self.invoke(dict(mode='action',request=dict(action=action,**fields)))

    def binding(self,clients,direction):
        return dict(direction=direction,nodes={k:self.lab.config(c.node,c.credential['rune'],c.call('info')['grant'])
                                              for k,c in clients.items()})

    def start(self,fault=None):
        require(self.proc is None or self.proc.poll() is not None,'fixture_service_already_running')
        (self.directory/'fault-consumed').unlink(missing_ok=True)
        self.proc=subprocess.Popen([sys.executable,str(SCRIPT),'--disposable-regtest'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.proc.stdin.write(json.dumps(dict(directory=str(self.directory),mode='run',fault=fault)))
        self.proc.stdin.close();self.proc.stdin=None
        self.lab.procs.append(self.proc)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate();out,err=self.proc.communicate(timeout=120)
            require(self.proc.returncode==0 and not err,'service_graceful_stop_failed')

    def crash(self,kind,op):
        out,err=self.proc.communicate(timeout=120)
        require(self.proc.returncode==-signal.SIGKILL and not err
                and (self.directory/'fault-consumed').read_text()==kind+' '+op+'\n','service_fault_not_reached')

    def record(self):
        deadline=time.monotonic()+120
        while True:
            try:j=hg.Journal(self.directory)
            except BlockingIOError:
                require(time.monotonic()<deadline,'service_record_lock_timeout');time.sleep(.05);continue
            try:return copy.deepcopy(service.validate(j.read())['jobs'][self.cid]['worker']['swaps'][self.cid])
            finally:j.close()

    def status(self):
        return next(s for s in self.action('status')['swaps'] if s['confirmation']==self.cid)

    def wait(self,predicate):self.lab.wait(predicate,self.proc)


def scenario(lab,topologies,direction,name,*,grant_setup=None):
    lab.checkpoint(direction+'-service-'+name)
    ia,oa=('btc','xbt') if direction=='forward' else ('xbt','btc')
    ib,ins,ic=topologies[ia];ob,outs,oc=topologies[oa]
    for nodes in (ins,outs):routes.quiet(lab,nodes);routes.public_graph(lab,nodes)
    before={asset:[r['to_us_msat'] for _,r in routes.endpoints(lab,t[1])] for asset,t in topologies.items()}
    incoming,outgoing=ic[1],oc[0];clients=dict(incoming=incoming,outgoing=outgoing)
    bolt,preimage=cross.recipient_invoice(lab,outs,direction+'-service-'+name)
    directory=lab.root/('service-worker-'+direction);d=Driver(lab,directory)
    d.action('pair',binding=d.binding(clients,direction),confirmed=True)
    review=d.action('prepare',direction=direction,invoice=bolt);d.cid=review['confirmation']
    c=d.record()['contract'];ph=c['outgoing']['payment_hash']
    require(review['markup_bps']==0 and review['routing_fee_msat']==2000,'service_review_changed')
    # Start the real loop while still unapproved; it may heartbeat but cannot enroll.
    d.start(dict(kind='outgoing',operation='enroll'))
    d.wait(lambda:d.action('status')['worker_fresh'])
    require(d.status()['state']=='ready_for_confirmation' and not d.record()['enroll'],'service_enrolled_before_confirmation')
    d.action('confirm',cid=d.cid,confirmation=d.cid,confirmed=True)
    d.crash('outgoing','enroll')
    d.start(dict(kind='incoming',operation='enroll'));d.crash('incoming','enroll')
    d.start(dict(kind='incoming',operation='invoice'));d.crash('incoming','invoice')
    issued=incoming.call('invoice',dict(entry_id=cw.enrollment(c,'incoming')))
    d.start(dict(kind='outgoing',operation='send'))
    d.wait(lambda:d.status()['state']=='waiting_for_payment')
    require(d.action('invoice',cid=d.cid)==issued,'service_invoice_replaced')
    customer_bolt=issued['invoice']
    require('send' not in d.record() and lab.rpc_named(outs[0]['cli'],'listsendpays',dict(payment_hash=ph))['payments']==[],
            'service_send_before_incoming')
    decoded=ci.invoice(lab.rpc_named(ins[0]['cli'],'decode',dict(string=customer_bolt)),ins[0]['profile'],'regtest')
    require(decoded['payment_hash']==ph and decoded['payment_secret']==c['incoming']['payment_secret']
            and decoded['payment_secret']!=c['outgoing']['payment_secret'],'service_customer_invoice_changed')
    count=outgoing.mutations().count('sendpay')
    route=customer.payer_route(lab,ins,decoded)
    lab.rpc_named(ins[0]['cli'],'sendpay',dict(route=route,payment_hash=ph,
        payment_secret=c['incoming']['payment_secret'],bolt11=customer_bolt,groupid=1,partid=0))
    held_in=routes.gate.held(lab,ins[-1],ph);d.crash('outgoing','send')
    held_out=routes.gate.held(lab,outs[-1],ph)
    state,payer=cross.payer_attempt(lab,ins[0],customer_bolt,c);require(state=='pending','service_payer_not_pending')
    release=name!='fail';resolve='release' if release else 'fail'
    d.start(dict(kind='incoming',operation=resolve))
    d.wait(lambda:d.status()['state']=='outgoing_pending');saved=d.record()
    require(saved['remote']['incoming']['entry']['incoming']['binding']==held_in['binding'],'service_binding_changed')
    d.stop()
    # Pausing new work or changing markup must not stop this enrolled recovery.
    d.action('configure',paused=True,markup_bps=100)
    if name=='restart-held':
        lab.checkpoint(direction+'-service-restart-both-chains')
        for asset in ('btc','xbt'):topologies[asset]=cross.refresh(lab,topologies[asset])
        ib,ins,ic=topologies[ia];ob,outs,oc=topologies[oa];incoming,outgoing=ic[1],oc[0]
        for kind,client in [('incoming',incoming),('outgoing',outgoing)]:
            local.Worker(lab,dict(client.node,root=customer.state_dir(client.node))).call('pause',client.gid)
            (customer.state_dir(client.node)/'customer-fixture-time').write_text(str(c['grants'][kind]['expires_at']+1))
        for nodes,held in ((ins,held_in),(outs,held_out)):
            actual=routes.gate.held(lab,nodes[-1],ph)
            require(actual['binding']==held['binding'] and actual['cltv_expiry']==held['cltv_expiry'],'service_restart_binding_changed')
    d=Driver(lab,directory);d.cid=cc.digest(c)
    require(d.action('invoice',cid=d.cid)==issued,'service_restart_invoice_changed')
    d.start(dict(kind='incoming',operation=resolve))
    d.wait(lambda:d.action('status')['worker_fresh'])
    params=dict(payment_hash=ph,binding=held_out['binding'])
    if release:params['preimage']=preimage
    lab.rpc_named(outs[-1]['cli'],'swap-gate-'+resolve,params)
    d.crash('incoming',resolve);d.start()
    d.wait(lambda:d.status()['terminal']);wanted='settled' if release else 'failed'
    require(d.status()['state']==wanted,'service_terminal_outcome_changed');d.stop()
    lab.wait(lambda:cross.payer_attempt(lab,ins[0],customer_bolt,c,payer)[0]==('complete' if release else 'failed'),ins[0]['proc'])
    for nodes in (ins,outs):routes.quiet(lab,nodes)
    final=d.record()
    require(ba.equal(final['contract'],c) and final['invoice']==issued and final['send']==saved['send'],'service_saved_terms_changed')
    for kind in cw.KINDS:
        old,new=saved['remote'][kind]['entry'],final['remote'][kind]['entry']
        require(old['htlc']==new['htlc'],'service_original_HTLC_changed')
        field='incoming' if kind=='incoming' else 'attempt'
        require(old[field]==new[field] if kind=='incoming' else old[field]['identity']==new[field]['identity'],
                'service_original_binding_or_attempt_changed')
    require(outgoing.mutations().count('sendpay')==count+1,'service_duplicate_send')
    for asset,amount in ((ia,c['incoming']['amount_msat']),(oa,c['outgoing']['amount_msat'])):
        after=[r['to_us_msat'] for _,r in routes.endpoints(lab,topologies[asset][1])]
        require(after==[a+delta for a,delta in zip(before[asset],cross.deltas(amount,release))],'service_twelve_balances_changed')
    require(lab.rpc_named(ins[-1]['cli'],'listinvoices',dict(payment_hash=ph))['invoices']==[],'service_core_invoice_created')
    # Retain three distinct swaps in one directional service journal.
    require(len(d.action('history')['swaps'])==('release','fail','restart-held').index(name)+1,'service_history_missing')
    d.action('configure',paused=False,markup_bps=0)
    if name=='restart-held':
        old_history=d.action('history')['swaps']
        renewed={}
        for kind,client in [('incoming',incoming),('outgoing',outgoing)]:
            (customer.state_dir(client.node)/'customer-fixture-time').unlink()
            g=c['grants'][kind];worker=local.Worker(lab,dict(client.node,root=customer.state_dir(client.node)))
            gid=grant_setup(lab,client.node,kind,g['limits']) if grant_setup else worker.call(
                'create_grant',kind,g['channels'],g['limits'],int(time.time())+86400,confirmed=True)
            fresh=customer.Client(lab,client.node,client.caller,gid)
            fresh.credential=lab.rpc_named(client.node['cli'],customer.remote.CREDENTIAL,dict(grant_id=gid,confirmed=True))
            renewed[kind]=fresh
        d.action('pair',binding=d.binding(renewed,direction),confirmed=True)
        after=d.action('history')['swaps']
        require([dict(s,selected=False) for s in old_history]==after,'service_renewal_changed_old_history')
    print('PASS: '+direction+' '+name+'; automatic service, exact recovery and twelve balances',flush=True)
    return dict(direction=direction,scenario=name,state=wanted,automatic_worker=True,
        confirmed_before_enrollment=True,signed_customer_invoice=True,one_outgoing_attempt=True,
        original_contract_invoice_HTLC_attempt=True,exact_twelve_balances=True,
        SIGKILL_after_enrollment_invoice_send_resolution=True,service_pause_recovery=True,
        saved_markup_unchanged=True,history_count=('release','fail','restart-held').index(name)+1,
        both_chains_restarted=name=='restart-held',paused_expired_recovery=name=='restart-held',
        renewed_grants_preserve_history=name=='restart-held')


def run(lab,*,grant_setup=None,after_stop=None):
    topologies={label:customer.topology(lab,label,profile,grant_amount_msat=10000000,grant_setup=grant_setup)
                for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]}
    backend,nodes,_=topologies['xbt'];lab.mine(backend,count=41);lab.synced(backend,*nodes)
    receipts=[scenario(lab,topologies,d,n,grant_setup=grant_setup) for d in ('forward','reverse') for n in ('release','fail','restart-held')]
    for backend,nodes,_ in topologies.values():
        for node in reversed(nodes):lab.stop(node['proc'])
        lab.stop(backend['proc'])
    for direction in ('forward','reverse'):
        d=Driver(lab,lab.root/('service-worker-'+direction))
        require(len(d.action('history')['swaps'])==3,'service_offline_history_failed')
    if after_stop: after_stop(lab,topologies)
    result=dict(schema=1,fixture='privkeyio-service-0084',scenarios=receipts,regtest_only=True,
        native_https=True,cached_offline_history=True,synthetic_price_snapshots=True,
        live_market_feed=False,startos_actions_registered=False,installer=False)
    (lab.root/'service-result.json').write_text(json.dumps(result,indent=2)+'\n');lab.checkpoint('complete')
    print('SERVICE FIXTURE PASSED: 0084; both directions, automatic recovery, history and grant renewal; no installer',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--disposable-regtest',required=True,action='store_true')
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()));args=p.parse_args();os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):setattr(args,name,getattr(args,name).resolve())
    for file in (SCRIPT,customer.PLUGIN,routes.gate.PLUGIN):require(file.is_file() and os.access(file,os.X_OK),'service_fixture_executable_missing')
    lab=previous.HttpsLab(args)
    try:run(lab)
    finally:lab.close()


if __name__=='__main__':main()
