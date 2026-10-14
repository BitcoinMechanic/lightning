#!/usr/bin/env python3
"""0096: extend six funded deadline cases through explicit reviewed retirement.

BTC 0096a recovery-fee repair; unchanged compiled XBT 0094 engine. Closed-history transition, real pruning on restart,
new funding and reviewed renewal are tested with native scoped HTTPS. No live
node, installer, or synthetic replacement for payment/chain RPC is used.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import bounded_authority as ba
import cross_worker as cw
import customer_remote as remote
import held_gate as hg
import test_coordinator_engine as admin
import test_deadline_engine as base
from test_engine import require


class Lab(base.Lab):
    def lightning(self,*args,**kwargs):
        n=super().lightning(*args,**kwargs)
        names={r['command'].split()[0] for r in self.rpc(n['cli'],'help')['help']}
        require('listclosedchannels' in names,'retirement_required_RPC_missing')
        require(type(self.rpc(n['cli'],'listclosedchannels').get('closedchannels')) is list,'retirement_closed_history_schema')
        require(type(self.rpc_named(n['cli'],'listfunds',dict(spent=True)).get('outputs')) is list,'retirement_spent_funds_schema')
        return n


def refused_admin(node,**request):
    p=subprocess.run([sys.executable,str(admin.ADMIN),'--disposable-regtest','--network-directory',
        str(base.customer.state_dir(node)),'--profile',node['identity']['profile']],
        input=json.dumps(request),capture_output=True,text=True,timeout=120)
    require(p.returncode==1 and not p.stderr and json.loads(p.stdout).get('result')=='REFUSED',
            'retirement_expected_local_refusal_missing')


def completed(lab,node,pin):
    active=lab.rpc(node['cli'],'listpeerchannels')['channels']
    rows=lab.rpc(node['cli'],'listclosedchannels')['closedchannels']
    return not any(r.get('channel_id')==pin['channel_id'] for r in active) and any(
        r.get('channel_id')==pin['channel_id'] and hg.channel_pin(r,node['profile'])==pin for r in rows)


def finish(lab,direction,name,topologies,driver,proof):
    ib,ins,ic=topologies['btc' if direction=='forward' else 'xbt']
    ob,outs,oc=topologies['xbt' if direction=='forward' else 'btc']
    incoming,outgoing=ic[1],oc[0];c=driver.record()['contract'];eid=cw.enrollment(c,'incoming')
    original=copy.deepcopy(driver.record());pin=original['remote']['incoming']['entry']['incoming']['binding']['channel']
    lab.checkpoint(direction+'-'+name+'-retirement-depth-and-closed-history')
    refused_admin(ins[-1],action='recovery_review',entry_id=eid)
    base.mine_until(lab,ib,ins,lambda:completed(lab,ins[-1],pin),limit=180)
    # Wait for the peer too so the replacement channel has no lingering old row.
    base.mine_until(lab,ib,ins,lambda:not any(r.get('channel_id')==pin['channel_id']
        for r in lab.rpc(ins[-2]['cli'],'listpeerchannels')['channels']),limit=180)
    for n in (-2,-1):ins[n]=base.restart_node(lab,ins[n])
    lab.synced(ib,*ins)
    require(not any(h.get('payment_hash')==c['incoming']['payment_hash']
                    for h in lab.rpc(ins[-1]['cli'],'listhtlcs')['htlcs']), 'closed_HTLC_not_pruned_on_restart')
    before_entry=base.authority_record(ins[-1])['entries'][eid]
    require(before_entry['incoming']==original['remote']['incoming']['entry']['incoming']
            and before_entry['htlc']==original['remote']['incoming']['entry']['htlc'], 'saved_closed_binding_changed')
    driver.run('step')
    require(driver.run('status')['state']=='onchain_recovery','closed_history_automatically_retired')
    config=lab.config(incoming.node,incoming.credential['rune'],c['grants']['incoming'])
    for op in ('recovery_review','recovery_confirm'):
        base.transport.scope_response(lab,ins[-1]['profile'],'incoming',op,
            base.transport.raw(config,remote.CALL,dict(grant_id=incoming.gid,operation=op,body=dict(entry_id=eid))),401,1502)
    # New channel is ordinary fixture funding, never a retargeted close/recovery.
    lab.rpc(ins[-2]['cli'],'connect',ins[-1]['identity']['node_id'],'127.0.0.1',ins[-1]['port'])
    funds=lab.rpc(ins[-2]['cli'],'listfunds')['outputs']
    usable=[o for o in funds if o.get('status')=='confirmed' and o.get('reserved') is False]
    recovered=[o for o in usable if o['txid']==proof['proof']['commitment']
               and o['scriptpubkey'].startswith('0020')]
    require(len(recovered)==1,'replacement_recovered_anchor_output_missing')
    selected=[o['txid']+':'+str(o['output']) for o in usable]
    # Explicit inputs guarantee this exercises recovered-coin fees on both
    # engines. Keep fundchannel's default feerate and the backend relay floor.
    funded=lab.rpc_named(ins[-2]['cli'],'fundchannel',dict(id=ins[-1]['identity']['node_id'],
        amount=300000,announce=False,utxos=selected),timeout=90)
    decoded=lab.rpc(ib['cli'],'decoderawtransaction',funded['tx'])
    witnesses=[v['txinwitness'] for v in decoded['vin'] if v['txid']==recovered[0]['txid']
               and v['vout']==recovered[0]['output']]
    require(len(witnesses)==1 and len(witnesses[0])==2
            and len(bytes.fromhex(witnesses[0][1]))==37
            and witnesses[0][1].endswith('ad51b2'),'replacement_did_not_spend_anchor_recovery')
    require({v['txid']+':'+str(v['vout']) for v in decoded['vin']}==set(selected),
            'replacement_funding_inputs_changed')
    mempool=lab.rpc(ib['cli'],'getmempoolentry',funded['txid'])
    relay=base.Decimal(str(lab.rpc(ib['cli'],'getnetworkinfo')['relayfee']))
    fee=base.Decimal(str(mempool['fees']['base']))
    require(fee*1000>=relay*decoded['vsize'],'replacement_fee_below_relay_floor')
    lab.mine(ib);lab.synced(ib,*ins)
    newpin=hg.channel_pin(base.routes.edge(lab,ins[-1],ins[-2]),ins[-1]['profile'])
    require(newpin['funding_txid']!=pin['funding_txid'] and newpin['channel_id']!=pin['channel_id'], 'renewal_reused_closed_funding')
    limits=c['grants']['incoming']['limits']
    pending=admin.call(ins[-1],action='review',kind='incoming',limits=limits,lifetime_seconds=86400)
    refused_admin(ins[-1],action='confirm',confirmation=pending['confirmation'],confirmed=True)
    require(pending['confirmation'] not in base.authority_record(ins[-1])['grants'], 'unretired_grant_was_replaced')
    lab.checkpoint(direction+'-'+name+'-explicit-local-retirement')
    path=base.customer.state_dir(ins[-1])/'bounded-authority-v1/state.json'
    saved=path.read_bytes();review=admin.call(ins[-1],action='recovery_review',entry_id=eid)
    require(review['read_only'] is True and not review['confirmed'] and path.read_bytes()==saved, 'retirement_review_mutated')
    refused_admin(ins[-1],action='recovery_confirm',entry_id=eid,confirmation='0'*64,confirmed=True)
    require(path.read_bytes()==saved,'wrong_confirmation_mutated')
    confirmation=review['confirmation']
    admin.call(ins[-1],action='recovery_confirm',entry_id=eid,confirmation=confirmation,confirmed=True)
    saved=path.read_bytes() # Discard successful reply and reopen the local adapter.
    admin.call(ins[-1],action='recovery_confirm',entry_id=eid,confirmation=confirmation,confirmed=True)
    require(path.read_bytes()==saved,'retirement_confirmation_repeated_write')
    record=base.authority_record(ins[-1]);retired=record['entries'][eid]
    for k,v in before_entry.items():require(retired[k]==v,'retirement_replaced_original_history')
    require(retired['onchain_retirement']['evidence']['proof']==proof['proof'],'retirement_changed_original_chain_proof')
    lab.checkpoint(direction+'-'+name+'-controller-original-attempt-review')
    review=driver.run('recovery_review')
    require(not driver.run('status')['terminal'] and not review['confirmed'],'local_retirement_finished_controller')
    driver.run('recovery_confirm',confirmation=review['confirmation'],confirmed=True,
               fault=dict(kind='local',operation='retirement'),crash=True)
    controller=driver.record()
    # Fresh process repeats only inspection. No payment/close operation is authorized.
    driver.run('recovery_confirm',confirmation=review['confirmation'],confirmed=True)
    driver.run('step');state=driver.run('status')
    require(driver.record()==controller and state['terminal'] is True
            and state['state']==('onchain_failed' if name=='timeout' else 'onchain_settled'), 'controller_retirement_not_durable')
    for k in ('contract','invoice','send','decision'):require(controller[k]==original[k],'retirement_changed_worker_intent')
    require(controller['remote']['outgoing']['entry']['attempt']==original['remote']['outgoing']['entry']['attempt'],
            'retirement_changed_original_attempt')
    # Resume that exact locally reviewed grant; old budgets and all history stay.
    admin.call(ins[-1],action='confirm',confirmation=pending['confirmation'],confirmed=True)
    record=base.authority_record(ins[-1]);newg=record['grants'][pending['confirmation']]['terms']
    require(record['entries'][eid]==retired and record['current']['incoming']==pending['confirmation']
            and newg['channels']==[newpin] and incoming.gid in record['grants'],'renewal_lost_history_or_funding')
    require(incoming.call('info')['remaining']==c['grants']['incoming']['limits']['max_swaps']-1,'retirement_refunded_budget')
    require(incoming.mutations().count('close')==1 and outgoing.mutations().count('sendpay')==1,'retirement_duplicated_mutation')
    # Startup and original-scoped evidence still work after renewal/pruned rows.
    ins[-1]=base.restart_node(lab,ins[-1]);lab.synced(ib,*ins);driver.run('step')
    require(driver.record()==controller,'retirement_restart_changed_history')
    lab.retirements.append(dict(direction=direction,scenario=name,closed_history_verified=True,
        original_htlc_pruned_by_core=True,original_binding_preserved=True,depth_100=True,
        separate_local_and_controller_review=True,wrong_confirmation_refused=True,
        old_rune_retirement_refused=True,fresh_process_lost_reply=True,controller_retirement_SIGKILL=True,post_renewal_restart=True,
        original_attempt_preserved=True,one_close=True,one_send=True,
        premature_renewal_refused=True,new_funding_grant_renewed=True,budget_not_refunded=True,default_fee_recovered_input=True,backend_relay_verified=True,
        terminal_state=state['state'],phase=proof['phase']))
    print('PASS: '+direction+' '+name+'; reviewed retirement, pruned HTLC recovery and new-funding grant renewal',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--disposable-regtest',action='store_true',required=True)
    for n in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()));a=p.parse_args();os.umask(0o077)
    for n in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):setattr(a,n,getattr(a,n).resolve())
    base.cross.RECIPIENT_MSAT=8000000;lab=Lab(a);lab.retirements=[]
    try:
        cases=[base.scenario(lab,d,n,after_recovery=finish) for d in ('forward','reverse') for n in ('release','timeout','restart-held')]
        result=dict(schema=1,fixture='privkeyio-retirement-0096',regtest_only=True,installer=False,
            deadline_cases=cases,retirement_cases=lab.retirements)
        (lab.root/'retirement-result.json').write_text(json.dumps(result,indent=2)+'\n');lab.checkpoint('complete')
        print('RETIREMENT FIXTURE PASSED: 0096; six reviewed recoveries and fresh-funding renewals; no installer',flush=True)
    finally:lab.close()

if __name__=='__main__':main()
