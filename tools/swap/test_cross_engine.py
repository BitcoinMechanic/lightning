#!/usr/bin/env python3
"""0080: actual bidirectional worker, two independent funded regtest chains.

Only the test recipient owns the outgoing preimage. Its held gate allows
deterministic failure/restart controls. The worker obtains that preimage solely
from its original outgoing payment and decides the incoming resolution itself.
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
import held_gate as hg
import market_quote as market
import test_authority_engine as local
import test_customer_engine as customer
import test_route_engine as routes
from test_engine import require

DRIVER = Path(__file__).with_name('fixture_cross_worker.py')
RECIPIENT_MSAT = 4000000


class DriverError(subprocess.CalledProcessError):
    def __init__(self, result):
        super().__init__(result.returncode,['fixture_cross_worker'])
        try: self.safe = json.loads(result.stdout)
        except ValueError: self.safe = dict(error_type='invalid_driver_reply')

    def __str__(self): return 'fixture worker failed: '+json.dumps(self.safe,sort_keys=True)


class Driver:
    script = DRIVER

    def __init__(self, lab, directory, clients):
        self.lab,self.directory,self.clients = lab,directory,clients
        self.cid = None; self.processes = 0

    def request(self, action, **kwargs):
        transports = {kind:dict(cli=list(map(str,c.caller['cli'])),peer_id=c.node['identity']['node_id'],
            grant_id=c.gid,rune=c.credential['rune']) for kind,c in self.clients.items()}
        return dict(directory=str(self.directory),clients=transports,action=action,contract_id=self.cid,**kwargs)

    def run(self, action, *, fault=None, crash=False, **kwargs):
        request = self.request(action,**kwargs)
        if fault: request['fault'] = fault
        marker = self.directory/'fault-consumed'; marker.unlink(missing_ok=True)
        proc = subprocess.Popen([sys.executable,str(self.script),'--disposable-regtest'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.processes += 1
        try: stdout,stderr = proc.communicate(json.dumps(request),timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate(); raise RuntimeError('cross_worker_process_timeout') from None
        result = subprocess.CompletedProcess(proc.args,proc.returncode,stdout,stderr)
        if crash:
            require(result.returncode == -signal.SIGKILL and marker.read_text() == fault['kind']+' '+fault['operation']+'\n',
                    'worker_SIGKILL_not_at_expected_reply')
            return None
        if result.returncode:
            error = DriverError(result)
            with (self.lab.root/'worker-errors.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(stage=self.lab.current_stage,**error.safe))+'\n')
            raise error
        require(not stderr,'worker_stderr_not_empty')
        return json.loads(stdout)['result']

    def record(self):
        journal = hg.Journal(self.directory)
        try: return cw.validate(journal.read())['swaps'][self.cid]
        finally: journal.close()


def refresh(lab, topology):
    backend,nodes,clients = topology
    credentials = [copy.deepcopy(c.credential) for c in clients]
    nodes = routes.restart(lab,nodes,backend)
    fresh = [customer.Client(lab,n,p,c.gid) for n,p,c in zip((nodes[0],nodes[-1]),(nodes[1],nodes[-2]),clients)]
    for c,saved in zip(fresh,credentials): c.credential = saved
    return backend,nodes,fresh


def recipient_invoice(lab,nodes,label):
    preimage = os.urandom(32).hex(); ph = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    created = lab.rpc_named(nodes[-1]['cli'],'invoice',dict(amount_msat=RECIPIENT_MSAT,
        label=label,description='disposable cross-chain recipient',expiry=3600,cltv=40,preimage=preimage,
        exposeprivatechannels=[routes.edge(lab,nodes[-1],nodes[-2])['short_channel_id']]))
    d = ci.invoice(lab.rpc_named(nodes[0]['cli'],'decode',dict(string=created['bolt11'])),nodes[0]['profile'],'regtest')
    q = dict(chain=nodes[-1]['identity'],payment_hash=ph,payment_secret=d['payment_secret'],amount_msat=RECIPIENT_MSAT,
        expires_at=d['created_at']+d['expiry'],min_cltv_delta=40,max_cltv_delta=80,
        approved_channels=[hg.channel_pin(routes.edge(lab,nodes[-1],nodes[-2]),nodes[-1]['profile'])])
    lab.rpc_named(nodes[-1]['cli'],'swap-gate-register',dict(quote=q))
    return created['bolt11'],preimage


def contract(incoming,outgoing,bolt,direction):
    plan = outgoing.call('plan',dict(invoice=bolt)); now = int(time.time())
    pricing = market.quote(dict(schema=1,source='neoxa.exchange',pair='BTCB2_BTC',direction=direction,markup_bps=0,
        fetched_at_ms=now*1000,ticker_at_ms=now*1000,expires_at=now+120,bid='0.49',ask='0.5',
        levels=[dict(price='0.5' if direction=='forward' else '0.49',quantity='1')],
        recipient_msat=plan['amount_msat'],routing_fee_msat=plan['route'][0]['amount_msat']-plan['amount_msat'],
        btc_msat=0,xbt_msat=0),compute=True)
    return cc.create({k:c.call('info')['grant'] for k,c in [('incoming',incoming),('outgoing',outgoing)]},
        plan,pricing,now=now)


def payer_attempt(lab,node,invoice,c,old=None):
    q = c['incoming']
    rows = lab.rpc_named(node['cli'],'listsendpays',dict(payment_hash=q['payment_hash']))['payments']
    require(len(rows) == 1,'cross_customer_one_attempt_required'); row = rows[0]
    identity = {k:row.get(k,0) if k=='partid' else row.get(k) for k in
        ('payment_hash','groupid','partid','destination','amount_msat','amount_sent_msat','created_at','created_index')}
    expected = dict(payment_hash=q['payment_hash'],groupid=1,partid=0,destination=q['chain']['node_id'],
        amount_msat=q['amount_msat'],amount_sent_msat=q['amount_msat']+2000)
    require(all(identity[k] == v for k,v in expected.items()) and row['bolt11'] == invoice
            and hg.integer(identity['created_index'],1,2**64-1) and hg.integer(identity['created_at'],1,2**35-1)
            and (old is None or identity == old),'cross_customer_attempt_changed')
    if row['status'] == 'complete': ba.preimage(row['payment_preimage'],q['payment_hash'])
    else: require('payment_preimage' not in row,'cross_customer_unexpected_preimage')
    return row['status'],identity


def deltas(amount, paid):
    return [-amount-2000,amount+2000,-amount-1000,amount+1000,-amount,amount] if paid else [0]*6


def scenario(lab,topologies,direction,name,*,driver_class=Driver,contract_factory=contract):
    lab.checkpoint(direction+'-cross-'+name)
    ia,oa = ('btc','xbt') if direction=='forward' else ('xbt','btc')
    ib,ins,ic = topologies[ia]; ob,outs,oc = topologies[oa]
    for nodes in (ins,outs): routes.quiet(lab,nodes); routes.public_graph(lab,nodes)
    before = {asset:[r['to_us_msat'] for _,r in routes.endpoints(lab,t[1])] for asset,t in topologies.items()}
    incoming,outgoing = ic[1],oc[0]
    bolt,preimage = recipient_invoice(lab,outs,direction+'-'+name)
    c = contract_factory(incoming,outgoing,bolt,direction); ph = c['outgoing']['payment_hash']
    directory = lab.root/('cross-worker-'+direction+'-'+name); cw.initialize_fixture(directory)
    driver = driver_class(lab,directory,dict(incoming=incoming,outgoing=outgoing))
    driver.cid = driver.run('prepare',contract=c)['contract_id']
    require(driver.run('step')['state'] == 'ready_for_confirmation','unconfirmed_worker_advanced')
    require('approval' not in driver.record() and not driver.record()['enroll'],'worker_enrolled_before_confirmation')
    driver.run('confirm',confirmation=driver.cid)
    # Kill the controller after receiving each enrollment and the invoice. The
    # next process recovers the same node record and same signed invoice bytes.
    for kind in ('outgoing','incoming'):
        driver.run('step',fault=dict(kind=kind,operation='enroll'),crash=True)
        driver.run('step')
    driver.run('step',fault=dict(kind='incoming',operation='invoice'),crash=True)
    issued = incoming.call('invoice',dict(entry_id=cw.enrollment(c,'incoming')))
    driver.run('step'); require(driver.run('invoice') == issued,'cross_invoice_replaced_after_crash')
    customer_bolt = issued['invoice']
    require(lab.rpc_named(ins[-1]['cli'],'listinvoices',dict(payment_hash=ph))['invoices'] == [],
            'cross_customer_core_invoice_exists')
    driver.run('step')
    require('send' not in driver.record() and lab.rpc_named(outs[0]['cli'],'listsendpays',dict(payment_hash=ph))['payments'] == [],
            'cross_send_before_incoming')
    decoded = ci.invoice(lab.rpc_named(ins[0]['cli'],'decode',dict(string=customer_bolt)),ins[0]['profile'],'regtest')
    require(decoded['payment_hash'] == ph and decoded['payment_secret'] == c['incoming']['payment_secret']
            and decoded['payment_secret'] != c['outgoing']['payment_secret'],'cross_customer_secret_changed')
    route = customer.payer_route(lab,ins,decoded)
    lab.rpc_named(ins[0]['cli'],'sendpay',dict(route=route,payment_hash=ph,
        payment_secret=c['incoming']['payment_secret'],bolt11=customer_bolt,groupid=1,partid=0))
    held_in = routes.gate.held(lab,ins[-1],ph)
    lab.wait(lambda:incoming.evidence(cw.enrollment(c,'incoming'))['status']['state'] == 'held',ins[-1]['proc'])
    paid_state,payer = payer_attempt(lab,ins[0],customer_bolt,c); require(paid_state == 'pending','cross_payer_not_held')
    count = outgoing.mutations().count('sendpay')
    driver.run('step',fault=dict(kind='outgoing',operation='send'),crash=True)
    held_out = routes.gate.held(lab,outs[-1],ph)
    driver.run('step'); saved = driver.record()
    require(saved['remote']['outgoing']['entry']['attempt']['state'] == 'pending'
            and saved['remote']['incoming']['entry']['incoming']['binding'] == held_in['binding'],
            'cross_original_pending_evidence_missing')
    if name == 'restart-held':
        lab.checkpoint(direction+'-cross-restart-both-chains')
        for asset in ('btc','xbt'): topologies[asset] = refresh(lab,topologies[asset])
        ib,ins,ic = topologies[ia]; ob,outs,oc = topologies[oa]
        incoming,outgoing = ic[1],oc[0]
        for client in (incoming,outgoing):
            worker = local.Worker(lab,dict(client.node,root=customer.state_dir(client.node)))
            worker.call('pause',client.gid)
            expiry = c['grants']['incoming' if client is incoming else 'outgoing']['expires_at']
            (customer.state_dir(client.node)/'customer-fixture-time').write_text(str(expiry+1))
        driver = driver_class(lab,directory,dict(incoming=incoming,outgoing=outgoing)); driver.cid = cc.digest(c)
        for nodes,held in ((ins,held_in),(outs,held_out)):
            actual = routes.gate.held(lab,nodes[-1],ph)
            require(actual['binding'] == held['binding'] and actual['cltv_expiry'] == held['cltv_expiry'],
                    'cross_restart_held_binding_changed')
        driver.run('step'); driver.run('step')
        require(driver.run('invoice') == issued,'cross_restart_invoice_changed')
    # Only the outgoing recipient fixture controls its own gate. No preimage
    # or instruction to resolve the incoming side is supplied to the worker.
    release = name != 'fail'; params = dict(payment_hash=ph,binding=held_out['binding'])
    if release: params['preimage'] = preimage
    lab.rpc_named(outs[-1]['cli'],'swap-gate-release' if release else 'swap-gate-fail',params)
    def decided():
        driver.run('step'); return 'decision' in driver.record()
    lab.wait(decided,outs[0]['proc'])
    driver.run('step',fault=dict(kind='incoming',operation='release' if release else 'fail'),crash=True)
    lab.wait(lambda:driver.run('step')['terminal'],ins[-1]['proc'])
    wanted = 'settled' if release else 'failed'
    require(driver.run('status')['state'] == wanted,'cross_terminal_outcome_changed')
    lab.wait(lambda:payer_attempt(lab,ins[0],customer_bolt,c,payer)[0] == ('complete' if release else 'failed'),ins[0]['proc'])
    for nodes in (ins,outs): routes.quiet(lab,nodes)
    final = driver.record()
    require(ba.equal(final['contract'],c) and final['invoice'] == issued
            and final['send'] == saved['send'],'cross_immutable_terms_changed')
    for kind in cw.KINDS:
        old,new = saved['remote'][kind]['entry'],final['remote'][kind]['entry']
        require(old['htlc'] == new['htlc'],'cross_original_HTLC_changed')
        if kind=='incoming': require(old['incoming'] == new['incoming'],'cross_incoming_binding_changed')
        else: require(old['attempt']['identity'] == new['attempt']['identity'],'cross_outgoing_attempt_changed')
    if release: require(final['remote']['outgoing']['entry']['attempt']['preimage'] == preimage,'cross_preimage_changed')
    require(outgoing.mutations().count('sendpay') == count+1,'cross_duplicate_sendpay')
    for asset,amount in ((ia,c['incoming']['amount_msat']),(oa,c['outgoing']['amount_msat'])):
        after = [r['to_us_msat'] for _,r in routes.endpoints(lab,topologies[asset][1])]
        require(after == [a+d for a,d in zip(before[asset],deltas(amount,release))], 'cross_twelve_balances_or_fees_changed')
    for _ in range(2): driver.run('step')
    require(outgoing.mutations().count('sendpay') == count+1,'cross_terminal_replay_sent_again')
    require(lab.rpc_named(ins[-1]['cli'],'listinvoices',dict(payment_hash=ph))['invoices'] == [],'cross_core_invoice_created')
    print('PASS: '+direction+' '+name+'; worker-controlled resolution, original invoice/HTLCs/attempt, twelve balances',flush=True)
    return dict(direction=direction,scenario=name,state=wanted,confirmed_before_enrollment=True,
        authenticated_peer_rpc=True,signed_customer_invoice=True,no_incoming_core_invoice=True,
        one_outgoing_attempt=True,same_contract=True,same_invoice=True,same_incoming_binding=True,
        exact_twelve_balances=True,terminal_history=True,coordinator_fee_msat=2000 if release else 0,
        payer_fee_msat=2000 if release else 0,worker_SIGKILL_after_enrollment=True,
        worker_SIGKILL_after_invoice=True,worker_SIGKILL_after_send=True,worker_SIGKILL_after_resolution=True,
        both_chains_restarted=name=='restart-held',paused_expired_recovery=name=='restart-held')


def run(lab):
    topologies = {label:customer.topology(lab,label,profile,grant_amount_msat=10000000)
        for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]}
    # Distinct chain clocks; the worker must use relative delays, not subtract
    # the BTC and XBT heights when checking the incoming safety margin.
    backend,nodes,_ = topologies['xbt']; lab.mine(backend,count=41); lab.synced(backend,*nodes)
    heights = [lab.rpc(t[1][0]['cli'],'getinfo')['blockheight'] for t in topologies.values()]
    require(heights[0] != heights[1],'cross_chain_heights_not_independent')
    receipts = []
    for direction in ('forward','reverse'):
        for name in ('release','fail','restart-held'):
            receipts.append(scenario(lab,topologies,direction,name))
    for backend,nodes,_ in topologies.values():
        for node in reversed(nodes): lab.stop(node['proc'])
        lab.stop(backend['proc'])
    result = dict(schema=1,fixture='privkeyio-cross-0080',scenarios=receipts,regtest_only=True,
        cross_chain_worker=True,independent_chain_heights=True,synthetic_price_snapshots=True,
        live_market_feed=False,https_transport=False,onchain_recovery=False,automatic_payment_retries=False,
        production_activation_supported=False)
    (lab.root/'cross-result.json').write_text(json.dumps(result,indent=2)+'\n'); lab.checkpoint('complete')
    print('CROSS-CHAIN FIXTURE PASSED: 0080; both directions, failure and pending restart; no installer',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'): parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()))
    args = parser.parse_args(); os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'): setattr(args,name,getattr(args,name).resolve())
    for source in (DRIVER,customer.PLUGIN,routes.gate.PLUGIN):
        require(source.is_file() and os.access(source,os.X_OK),'cross_fixture_executable_missing')
    lab = customer.CustomerLab(args)
    try: run(lab)
    finally: lab.close()


if __name__ == '__main__': main()
