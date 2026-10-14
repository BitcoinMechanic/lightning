#!/usr/bin/env python3
"""0094 real-daemon deadline controls and bidirectional on-chain recovery.

All endpoints/datadirs belong to this disposable fixture. Prices are synthetic.
The baseline MUST reproduce the closing-channel replay failure. Patched cases
use native HTTPS, actual grants, the local watchdog, and the original attempt.
"""
import argparse
import copy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import bounded_authority as ba
import chain_identity as ci
import chain_recovery as chain
import cross_contract as cc
import cross_worker as cw
import customer_https as https
import customer_remote as remote
import held_gate as hg
import test_coordinator_engine as admin
import test_cross_engine as cross
import test_customer_engine as customer
import test_https_engine as transport
import test_route_engine as routes
from test_engine import require

VERSIONS={ci.BTC:('v26.06.9-swap0094',),ci.PRIVKEYIO_XBT:('v26.06.9-blake2b.7-swap0094',)}


class Lab(transport.HttpsLab):
    engine_versions=VERSIONS
    def __init__(self,args):super().__init__(args);self.admin_receipts=[]
    def lightning(self,*args,**kwargs):
        n=super().lightning(*args,**kwargs)
        have={r['command'].split()[0] for r in self.rpc(n['cli'],'help')['help']}
        require({'close','listtransactions','listhtlcs','listfunds'}<=have,'deadline_required_RPC_missing')
        require(type(self.rpc(n['cli'],'listtransactions')['transactions']) is list,'deadline_transactions_schema_changed')
        return n


def grant_setup(lab,node,kind,limits):
    limits=copy.deepcopy(limits)
    if kind=='incoming':limits['close_before_blocks']=72
    return admin.grant_setup(lab,node,kind,limits)


def pinned_row(lab,node,pin):
    rows=[r for r in lab.rpc(node['cli'],'listpeerchannels')['channels'] if r.get('channel_id')==pin['channel_id']]
    require(len(rows)==1 and hg.channel_pin(rows[0],node['profile'])==pin,'deadline_original_funding_changed')
    return rows[0]


def authority_record(node):
    return json.loads((customer.state_dir(node)/'bounded-authority-v1/state.json').read_text())


def restart_node(lab,node,grant=None):
    lab.stop(node['proc'])
    for name in ('swap-gate-v1','bounded-authority-v1',customer.plugin.REMOTE_DIR,customer.plugin.INVOICE_DIR):
        directory=customer.state_dir(node)/name
        if directory.exists():
            def unlocked():
                j=hg.Journal(directory);j.close();return True
            lab.wait(unlocked,timeout=15)
    if grant is not None:
        j=hg.Journal(customer.state_dir(node)/'bounded-authority-v1')
        try:
            a=ba.Authority(j,node['identity'],lambda *a,**k:require(False,'offline_pause_called_RPC'))
            a.pause(ba.digest(grant))
        finally:j.close()
        (customer.state_dir(node)/'customer-fixture-time').write_text(str(grant['expires_at']+1))
    fresh=lab.lightning(node['name'],node['profile'],node['backend'])
    ci.same_binding(node['identity'],fresh['identity']);return fresh


def scoped(lab,client):
    return https.Client(lab.config(client.node,client.credential['rune'],client.call('info')['grant']))


def old_scope_control(lab,client):
    g=client.call('info')['grant'];config=lab.config(client.node,client.credential['rune'],g)
    # A rune with the previous operation set cannot acquire arm/close authority
    # merely because the node now runs 0094, even for a new incoming grant ID.
    old=lab.rpc_named(client.node['cli'],'createrune',dict(restrictions=remote.base.restrictions(
        client.gid,'incoming',operations=remote.OPERATIONS,method=remote.CALL)))
    before=client.mutations()
    for op in ('arm','protect','chain'):
        transport.scope_response(lab,g['chain']['profile'],'incoming','old_scope_'+op,
            transport.raw(config,remote.CALL,dict(grant_id=client.gid,operation=op,body=dict(entry_id='0'*64)),
                          rune=old['rune']),401,1502)
    require(client.mutations()==before,'old_scope_mutated')


def public_balances(lab,nodes):
    return [routes.edge(lab,n,p)['to_us_msat'] for a,b in zip(nodes[:2],nodes[1:3]) for n,p in ((a,b),(b,a))]


def wallet(lab,node):
    rows=lab.rpc(node['cli'],'listfunds')['outputs'];result={}
    for o in rows:
        if o['status']!='confirmed':continue
        key=(o['txid'],o['output']);require(key not in result,'duplicate_wallet_output')
        require(type(o['amount_msat']) is int and o['amount_msat']%1000==0,'wallet_fractional_output')
        result[key]=o
    return result


def sats(n):
    value=Decimal(str(n))*100000000
    require(value==value.to_integral_value(),'backend_fractional_satoshi');return int(value)


def reconcile_chain(lab,backend,nodes,pin,before,height_start,proof):
    """Walk confirmed descendants, independently of CLN's transaction list."""
    funds={(pin['funding_txid'],pin['funding_outnum']):300000}
    for rows in before:
        for key,o in rows.items():require(key not in funds,'duplicate_initial_outpoint');funds[key]=o['amount_msat']//1000
    initial=sum(funds.values());fees=0;seen={};residue={}
    tip=lab.rpc(backend['cli'],'getblockcount')
    for height in range(height_start+1,tip+1):
        block=lab.rpc(backend['cli'],'getblock',lab.rpc(backend['cli'],'getblockhash',height),2)
        for tx in block['tx']:
            inputs=[(i.get('txid'),i.get('vout')) for i in tx['vin']]
            if not any(key in funds for key in inputs):continue
            require(all(key in funds for key in inputs),'unknown_external_recovery_input')
            value=sum(funds.pop(key) for key in inputs);output=sum(sats(o['value']) for o in tx['vout'])
            require(value>=output,'negative_recovery_fee');fees+=value-output;seen[tx['txid']]=(tx,height)
            for o in tx['vout']:
                value=sats(o['value'])
                if value:
                    key=(tx['txid'],o['n']);funds[key]=value;residue[key]=o['scriptPubKey']['hex']
    require(proof['commitment'] in seen,'funding_spend_not_independently_confirmed')
    for k in ('success','sweep','timeout'):
        if k in proof:require(proof[k] in seen,'claim_descendant_not_independently_confirmed')
    returned=[wallet(lab,n) for n in nodes];owned={}
    for rows in returned:
        for key,o in rows.items():
            require(key in funds and key not in owned and funds[key]==o['amount_msat']//1000,'wallet_recovery_ledger_mismatch')
            owned[key]=funds[key]
    other={key:v for key,v in funds.items() if key not in owned}
    require(len(other)<=2 and all(v==330 for v in other.values()),'unrecovered_nonanchor_output')
    require(sum(owned.values())+sum(other.values())+fees==initial,'recovery_satoshi_reconciliation_failed')
    require(fees>0,'funded_recovery_fees_missing')
    report=dict(initial_sats=initial,returned_wallet_sats=sum(owned.values()),
        unspent_anchor_sats=sum(other.values()),fee_sats=fees,confirmed_transactions=len(seen),
        wallet_delta_sats=[sum(o['amount_msat']//1000 for o in r.values())-sum(o['amount_msat']//1000 for o in b.values())
                           for r,b in zip(returned,before)],independent_backend_walk=True)
    require(sum(report['wallet_delta_sats'])+report['unspent_anchor_sats']+fees==300000,'closed_channel_value_mismatch')
    return report


def mine_until(lab,backend,nodes,fn,*,limit=240):
    # Give onchaind each confirmation before mining the next; never mock heights.
    for _ in range(limit):
        result=fn()
        if result:return result
        lab.mine(backend,1);lab.synced(backend,*nodes);time.sleep(.2)
    raise RuntimeError('deadline_resolution_not_confirmed')


def baseline(lab,profile):
    label='baseline-'+('btc' if profile==ci.BTC else 'xbt')
    backend,nodes,clients=customer.topology(lab,label,profile,grant_amount_msat=50000000,grant_setup=grant_setup)
    incoming=clients[1];g=incoming.call('info')['grant'];ph=hashlib.sha256(os.urandom(32)).hexdigest()
    q=dict(chain=nodes[-1]['identity'],payment_hash=ph,payment_secret=os.urandom(32).hex(),amount_msat=8000000,
        expires_at=int(time.time())+1800,min_cltv_delta=350,max_cltv_delta=2016,approved_channels=g['channels'])
    eid=incoming.call('enroll',dict(data=q,confirmation=ba.digest(q)))['entry_id']
    incoming.call('publish',dict(entry_id=eid));bolt=incoming.call('invoice',dict(entry_id=eid))['invoice']
    d=ci.invoice(lab.rpc(nodes[0]['cli'],'decode',bolt),profile,'regtest')
    lab.rpc_named(nodes[0]['cli'],'sendpay',dict(route=customer.payer_route(lab,nodes,d),payment_hash=ph,
        payment_secret=q['payment_secret'],bolt11=bolt,groupid=1,partid=0))
    held=routes.gate.held(lab,nodes[-1],ph);incoming.call('evidence',dict(entry_id=eid));incoming.call('arm',dict(entry_id=eid))
    pin=held['binding']['channel'];lab.checkpoint(label+'-original-binary-closing-replay-control')
    lab.mine(backend,held['cltv_expiry']-72-lab.rpc(nodes[-1]['cli'],'getinfo')['blockheight']);lab.synced(backend,*nodes)
    lab.wait(lambda:pinned_row(lab,nodes[-1],pin)['state'] in hg.CLOSING_STATES,nodes[-1]['proc'])
    lab.wait(lambda:incoming.mutations().count('close')==1,nodes[-1]['proc'])
    nodes[-1]=restart_node(lab,nodes[-1])
    def rejected():
        s=routes.gate.status(lab,nodes[-1],ph)
        rows=lab.rpc_named(nodes[-1]['cli'],'listhtlcs',dict(id=pin['channel_id']))['htlcs']
        rows=[h for h in rows if h['payment_hash']==ph]
        (lab.root/(label+'-control.json')).write_text(json.dumps(dict(phase=s['phase'],hook_ready=s['hook_ready'],
            needs_attention=s['needs_attention'],htlc_states=[h['state'] for h in rows]))+'\n')
        return s['phase']=='held' and not s['hook_ready'] and len(rows)==1 and rows[0]['state']=='SENT_REMOVE_HTLC'
    lab.wait(rejected,nodes[-1]['proc'])
    require(lab.rpc_named(nodes[-1]['cli'],'listinvoices',dict(payment_hash=ph))['invoices']==[], 'baseline_core_invoice_exists')
    for n in reversed(nodes):lab.stop(n['proc'])
    lab.stop(backend['proc'])
    print('PASS: '+profile+' original binary rejects the held final HTLC after closing/restart',flush=True)
    return dict(profile=profile,original_binary=True,held_final_replay_rejected=True,no_core_invoice=True)


def start_worker(lab,direction,name,clients,contract):
    # The disposable worker entry points require this prefix. Keep all six
    # deadline scenarios on the same initialization and pairing path.
    directory=lab.root/('cross-worker-'+direction+'-'+name)
    cw.initialize_fixture(directory)
    driver=transport.Driver(lab,directory,clients)
    driver.cid=driver.run('prepare',contract=contract)['contract_id']
    driver.run('confirm',confirmation=driver.cid)
    return driver


def scenario(lab,direction,name):
    prefix=direction+'-'+name
    topologies={asset:customer.topology(lab,prefix+'-'+asset,profile,grant_amount_msat=50000000,grant_setup=grant_setup)
                for asset,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]}
    transport.scope_checks(lab,topologies)
    ib,ins,ic=topologies['btc' if direction=='forward' else 'xbt']
    ob,outs,oc=topologies['xbt' if direction=='forward' else 'btc']
    incoming,outgoing=ic[1],oc[0];cin,cout=scoped(lab,incoming),scoped(lab,outgoing)
    old_scope_control(lab,incoming)
    lab.mine(ob,41);lab.synced(ob,*outs)
    before_public=public_balances(lab,ins);before_out=[r['to_us_msat'] for _,r in routes.endpoints(lab,outs)]
    before_wallet=[wallet(lab,n) for n in ins[-2:]]
    closing_pins=[hg.channel_pin(routes.edge(lab,n,p),n['profile']) for n,p in ((ins[-2],ins[-1]),(ins[-1],ins[-2]))]
    bolt,preimage=cross.recipient_invoice(lab,outs,prefix)
    c=cross.contract(cin,cout,bolt,direction);ph=c['incoming']['payment_hash'];eid=cw.enrollment(c,'incoming')
    driver=start_worker(lab,direction,name,dict(incoming=incoming,outgoing=outgoing),c)
    directory=driver.directory
    for _ in range(3):driver.run('step')
    issued=driver.run('invoice');d=ci.invoice(lab.rpc(ins[0]['cli'],'decode',issued['invoice']),ins[0]['profile'],'regtest')
    lab.rpc_named(ins[0]['cli'],'sendpay',dict(route=customer.payer_route(lab,ins,d),payment_hash=ph,
        payment_secret=c['incoming']['payment_secret'],bolt11=issued['invoice'],groupid=1,partid=0))
    held_in=routes.gate.held(lab,ins[-1],ph)
    lab.checkpoint(prefix+'-arm-before-single-send')
    driver.run('step',fault=dict(kind='incoming',operation='arm'),crash=True)
    driver.run('step',fault=dict(kind='outgoing',operation='send'),crash=True)
    held_out=routes.gate.held(lab,outs[-1],ph);driver.run('step');saved=driver.record()
    require(saved['remote']['outgoing']['entry']['attempt']['state']=='pending','deadline_outgoing_not_pending')
    pin=held_in['binding']['channel'];expiry=held_in['cltv_expiry'];start=lab.rpc(ins[-1]['cli'],'getinfo')['blockheight']
    # No controller is running while these blocks are mined. In restart-held,
    # even the outgoing coordinator is offline at the protection boundary.
    if name=='restart-held':lab.stop(outs[0]['proc'])
    lab.checkpoint(prefix+'-coordinator-local-deadline')
    lab.mine(ib,expiry-73-start);lab.synced(ib,*ins)
    require(incoming.mutations().count('close')==0 and pinned_row(lab,ins[-1],pin)['state']=='CHANNELD_NORMAL','deadline_closed_early')
    (customer.state_dir(ins[-1])/'customer-drop-reply').write_text('close\n')
    lab.mine(ib,1);lab.synced(ib,*ins)
    lab.wait(lambda:incoming.mutations().count('close')==1,ins[-1]['proc'])
    lab.wait(lambda:not (customer.state_dir(ins[-1])/'customer-drop-reply').exists(),ins[-1]['proc'])
    lab.wait(lambda:pinned_row(lab,ins[-1],pin)['state'] in hg.CLOSING_STATES,ins[-1]['proc'])
    def committed():
        rows=lab.rpc(ins[-1]['cli'],'listtransactions')['transactions']
        return any(r.get('blockheight',0)>0 and any(i['txid']==pin['funding_txid'] and i['index']==pin['funding_outnum']
            for i in chain.transaction(r['rawtx'])['inputs']) for r in rows)
    mine_until(lab,ib,ins,committed,limit=8)
    if name=='restart-held':
        lab.checkpoint(prefix+'-restart-closing-without-preimage')
        ins[-1]=restart_node(lab,ins[-1],c['grants']['incoming']);outs[0]=restart_node(lab,outs[0],c['grants']['outgoing'])
        lab.rpc(ins[-2]['cli'],'connect',ins[-1]['identity']['node_id'],'127.0.0.1',ins[-1]['port'])
        lab.rpc(outs[0]['cli'],'connect',outs[1]['identity']['node_id'],'127.0.0.1',outs[1]['port'])
        lab.synced(ib,*ins);lab.synced(ob,*outs)
        replay=routes.gate.held(lab,ins[-1],ph)
        require(replay['binding']==held_in['binding'] and replay['cltv_expiry']==expiry,'deadline_replayed_HTLC_changed')
        driver=transport.Driver(lab,directory,dict(incoming=incoming,outgoing=outgoing));driver.cid=cc.digest(c)
    driver.run('step')
    release=name!='timeout';params=dict(payment_hash=ph,binding=held_out['binding'])
    if release:params['preimage']=preimage
    lab.rpc_named(outs[-1]['cli'],'swap-gate-release' if release else 'swap-gate-fail',params)
    def decided():driver.run('step');return 'decision' in driver.record()
    lab.wait(decided,outs[0]['proc'])
    driver.run('step',fault=dict(kind='incoming',operation='release' if release else 'fail'),crash=True)
    # Do not infer settlement from the lost reply. Keep original grant, attempt,
    # funding and HTLC pins; fresh workers can repeat only the gate resolution.
    driver.run('step');state=driver.run('status');final=driver.record()
    require(state['state']=='onchain_recovery' and state['terminal'] is False,'onchain_intent_mislabeled_settled')
    require(final['contract']==c and final['invoice']==issued and final['send']==saved['send'],'deadline_terms_changed')
    for kind in cw.KINDS:
        a,b=saved['remote'][kind]['entry'],final['remote'][kind]['entry']
        require(a['htlc']==b['htlc'],'deadline_original_HTLC_changed')
        require(a.get('incoming')==b.get('incoming') and a.get('attempt',{}).get('identity')==b.get('attempt',{}).get('identity'),
                'deadline_original_binding_or_attempt_changed')
    lab.checkpoint(prefix+'-confirm-claim-or-timeout-and-wallet')
    if not release:
        height=lab.rpc(ins[-1]['cli'],'getinfo')['blockheight']
        lab.mine(ib,expiry+1-height);lab.synced(ib,*ins)
    wanted='claim_and_sweep_verified' if release else 'timeout_verified'
    def evidence():
        result=cin.call('chain',dict(entry_id=eid))
        return result if result['phase']==wanted and result['verified'] else None
    proof=mine_until(lab,ib,ins,evidence)
    def resolved():
        return all(any('All outputs resolved:' in status for status in pinned_row(lab,n,p)['status'])
                   for n,p in zip(ins[-2:],closing_pins))
    mine_until(lab,ib,ins,resolved)
    # Re-read proof after additional confirmations; an earlier success is not cached.
    outcome='complete' if release else 'failed'
    mine_until(lab,ib,ins,lambda:cross.payer_attempt(lab,ins[0],issued['invoice'],c)[0]==outcome,limit=12)
    proof=evidence();require(proof is not None,'deadline_chain_evidence_regressed')
    routes.quiet(lab,ins[:3])
    routes.quiet(lab,outs)
    require(public_balances(lab,ins)==[v+d for v,d in zip(before_public,cross.deltas(c['incoming']['amount_msat'],release)[:4])],
            'deadline_incoming_public_balances_changed')
    require([r['to_us_msat'] for _,r in routes.endpoints(lab,outs)]==[v+d for v,d in zip(before_out,cross.deltas(c['outgoing']['amount_msat'],release))],
            'deadline_outgoing_six_balances_changed')
    accounting=reconcile_chain(lab,ib,ins[-2:],pin,before_wallet,start,proof['proof'])
    require(incoming.mutations().count('close')==1 and outgoing.mutations().count('sendpay')==1,'deadline_duplicate_mutation')
    require(lab.rpc_named(ins[-1]['cli'],'listinvoices',dict(payment_hash=ph))['invoices']==[], 'deadline_core_invoice_created')
    for b,nodes,_ in topologies.values():
        for n in reversed(nodes):lab.stop(n['proc'])
        lab.stop(b['proc'])
    print('PASS: '+direction+' '+name+'; local deadline, one close/send, original bindings and independently reconciled chain funds',flush=True)
    return dict(direction=direction,scenario=name,local_watchdog=True,worker_absent_at_boundary=True,
        outgoing_offline_at_boundary=name=='restart-held',closing_restart=name=='restart-held',
        paused_expired_recovery=name=='restart-held',
        arm_reply_SIGKILL=True,send_reply_SIGKILL=True,resolution_reply_SIGKILL=True,close_reply_lost=True,
        exact_attempt_and_HTLCs=True,one_close=True,one_send=True,ten_offchain_balances=True,
        phase=proof['phase'],nonterminal_recovery_status=True,accounting=accounting,scoped_native_https=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--disposable-regtest',action='store_true',required=True)
    p.add_argument('--mode',choices=('baseline','patched','regression'),required=True)
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()));a=p.parse_args();os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):setattr(a,name,getattr(a,name).resolve())
    if a.mode=='patched':cross.RECIPIENT_MSAT=8000000
    lab=Lab(a)
    if a.mode=='baseline':lab.engine_versions=routes.VERSIONS
    try:
        if a.mode=='regression':
            admin.base.run(lab,grant_setup=admin.grant_setup,after_stop=admin.after_stop)
            require(len(lab.admin_receipts)==8,'admin_setup_count_changed')
            result=dict(regression=True,grants=lab.admin_receipts,restore=lab.restore_receipt,
                        service=json.loads((lab.root/'service-result.json').read_text()))
        elif a.mode=='baseline':result=dict(controls=[baseline(lab,profile) for profile in (ci.BTC,ci.PRIVKEYIO_XBT)])
        else:result=dict(cases=[scenario(lab,direction,name) for direction in ('forward','reverse') for name in ('release','timeout','restart-held')])
        result.update(schema=1,fixture='privkeyio-deadline-0094a',mode=a.mode,regtest_only=True,installer=False)
        (lab.root/'deadline-result.json').write_text(json.dumps(result,indent=2)+'\n');lab.checkpoint('complete')
        print('DEADLINE FIXTURE PASSED: 0094a '+a.mode+'; disposable regtest only; no installer',flush=True)
    finally:lab.close()

if __name__=='__main__':main()
