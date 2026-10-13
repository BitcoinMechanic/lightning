#!/usr/bin/env python3
"""0078: signed customer publication through real scoped RPC, both regtests.

The fixture owns the synthetic preimage and chooses resolution. This is NOT a
cross-chain controller. Unlike earlier traps, customer hashes have no core
invoice entry; only the important held gate can fulfill them.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import authority_plugin as authority_plugin
import bounded_authority as ba
import chain_identity as ci
import customer_invoice as invoice
import customer_plugin as plugin
import customer_remote as remote
import held_gate as hg
import remote_authority as old_remote
import test_authority_engine as local
import test_remote_engine as previous
import test_route_engine as routes
from test_engine import require, funding_address

PLUGIN = Path(__file__).with_name('fixture_customer_plugin.py')
state_dir, refused, DENIED = previous.state_dir, previous.refused, previous.DENIED


class CustomerLab(routes.RouteLab):
    def start(self, command, log):
        if Path(command[0]).name == 'lightningd':
            command = [arg for arg in command if arg != '--disable-plugin=commando']
            root = log.parent/'regtest'
            if (root/plugin.REMOTE_DIR/'state.json').exists():
                record = json.loads((root/plugin.REMOTE_DIR/'state.json').read_text())
                command += ['--important-plugin='+str(PLUGIN), '--swap-authority-profile='+record['chain']['profile']]
        return super().start(command, log)

    def lightning(self, *args, **kwargs):
        node = super().lightning(*args, **kwargs)
        names = {'commando'}
        if (state_dir(node)/plugin.REMOTE_DIR/'state.json').exists(): names.add(PLUGIN.name)
        self.wait(lambda:names <= {Path(p['name']).name for p in self.rpc(node['cli'],'plugin','list')['plugins']
            if p.get('active') is True}, node['proc'])
        return node


class Client(previous.Client):
    def setup(self):
        params = dict(grant_id=self.gid, confirmed=True)
        (state_dir(self.node)/'customer-drop-reply').write_text('createrune\n')
        refused(lambda:self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params),-32603)
        self.credential = self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params)
        require(self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params) == self.credential,'customer_credential_changed')
        require(self.mutations().count('createrune') == 1,'customer_credential_minted_twice')

    def call(self, operation, body=None):
        return self.raw(remote.CALL,dict(grant_id=self.gid,operation=operation,body=body or {}))

    def lost(self, rpc, operation, body):
        arm = state_dir(self.node)/'customer-drop-reply'; arm.write_text(rpc+'\n')
        refused(lambda:self.call(operation,body),-32603)
        require(not arm.exists(),'customer_fault_not_consumed')

    def mutations(self):
        path = state_dir(self.node)/'customer-mutations.log'
        return path.read_text().splitlines() if path.exists() else []


def topology(lab,label,profile,*,grant_amount_msat=None,grant_setup=None):
    lab.checkpoint(label+'-customer-runtime-preflight')
    backend = lab.backend(label+'-chain',profile == ci.PRIVKEYIO_XBT)
    nodes = [lab.lightning(label+'-'+name,profile,backend) for name in ('source','public','hint','recipient')]
    routes.preflight_route_rpc(lab,nodes)
    for a,b in zip(nodes,nodes[1:]): lab.rpc(a['cli'],'connect',b['identity']['node_id'],'127.0.0.1',b['port'])
    for target,caller in ((nodes[0],nodes[1]),(nodes[-1],nodes[-2])):
        require(type(lab.rpc(target['cli'],'listfunds')['outputs']) is list,'funds_RPC_changed')
        require(lab.rpc(target['cli'],'listinvoices')['invoices'] == [],'fresh_invoice_history_not_empty')
        unsigned = invoice.bolt11.unsigned('33'*32,'44'*32,routes.AMOUNT,profile=profile,
            environment='regtest',expiry=1800,final_cltv=374)
        signed = lab.rpc_named(target['cli'],'signinvoice',dict(invstring=unsigned))['bolt11']
        decoded = ci.invoice(lab.rpc_named(target['cli'],'decode',dict(string=signed)),profile,'regtest')
        require(decoded['min_final_cltv_expiry'] == 374,'customer_final_CLTV_changed')
        probe = lab.rpc_named(target['cli'],'createrune',dict(restrictions=[['method=getinfo'],['pnum=0']]))
        reply = lab.rpc_named(caller['cli'],'commando',dict(peer_id=target['identity']['node_id'],
            method='getinfo',params={},rune=probe['rune']))
        ci.same_binding(ci.node(reply,profile,'regtest'),target['identity'])
    lab.checkpoint(label+'-customer-funding')
    for n in nodes: lab.rpc(backend['cli'],'sendtoaddress',funding_address(lab,n),'0.01')
    lab.mine(backend); lab.synced(backend,*nodes)
    for n in nodes:
        lab.wait(lambda n=n:remote.reserve(lab.rpc(n['cli'],'listfunds')) >= 50000000,n['proc'])
    for i,(a,b) in enumerate(zip(nodes,nodes[1:])):
        lab.rpc_named(a['cli'],'fundchannel',dict(id=b['identity']['node_id'],amount=300000,announce=i<2),timeout=90)
    lab.mine(backend); lab.synced(backend,*nodes)
    routes.endpoints(lab,nodes); routes.public_graph(lab,nodes)
    grants = []
    for n,peer,kind in ((nodes[0],nodes[1],'outgoing'),(nodes[-1],nodes[-2],'incoming')):
        worker = local.Worker(lab,dict(n,root=state_dir(n)),fresh=True)
        amount = grant_amount_msat if grant_amount_msat is not None else routes.AMOUNT+(2000 if kind=='outgoing' else 0)
        bounds = dict(max_swaps=4,max_amount_msat=amount,total_amount_msat=amount*3)
        bounds.update(routes.BOUNDS if kind=='outgoing' else dict(min_cltv_delta=350,max_cltv_delta=2016))
        grants.append(grant_setup(lab,n,kind,bounds) if grant_setup else
            worker.call('create_grant',kind,[hg.channel_pin(routes.edge(lab,n,peer),profile)],
                bounds,int(time.time())+86400,confirmed=True))
        remote.initialize_fixture(state_dir(n)/plugin.REMOTE_DIR,n['identity'])
        invoice.initialize_fixture(state_dir(n)/plugin.INVOICE_DIR,n['identity'])
    hg.initialize_fixture(routes.gate.private_dir(nodes[-1]),nodes[-1]['identity'])
    lab.gates[nodes[-1]['name']] = profile
    nodes = routes.restart(lab,nodes,backend)
    clients = [Client(lab,n,p,g) for n,p,g in zip((nodes[0],nodes[-1]),(nodes[1],nodes[-2]),grants)]
    for c in clients: c.setup()
    return backend,nodes,clients


def scope_and_plan(lab,nodes,clients):
    for c,kind in zip(clients,('outgoing','incoming')):
        legacy = lab.rpc_named(c.node['cli'],'createrune',dict(restrictions=old_remote.restrictions(c.gid,kind)))
        refused(lambda:c.raw(remote.CALL,dict(grant_id=c.gid,operation='info',body={}),rune=legacy['rune']),DENIED)
        for method in ('signinvoice','invoice','getroutes','listfunds','sendpay','close','withdraw',
            'createrune',remote.CREDENTIAL,old_remote.CALL,'swap-gate-register','swap-gate-release','swap-gate-fail'):
            refused(lambda method=method:c.raw(method,{}),DENIED)
        for params in (dict(grant_id='0'*64,operation='info',body={}),[c.gid,'info',{}],
            dict(grant_id=c.gid,operation='info',body={},extra=True)):
            refused(lambda params=params:c.raw(remote.CALL,params),DENIED)
        refused(lambda:c.call('invoice' if kind=='outgoing' else 'plan',{}),DENIED)
        body = {} if kind=='outgoing' else dict(amount_msat=routes.AMOUNT)
        report = c.call('inspect',body)
        require(report['reserve_msat'] >= 50000000 and report['info']['remaining'] == 4,'customer_inspection_changed')
    # An ordinary recipient invoice exercises the coordinator planner's final
    # CLTV <= 40 policy. Customer paying-in invoices have a different, larger
    # safety delta; the fixture payer is not a coordinator spending grant.
    core = lab.rpc_named(nodes[-1]['cli'],'invoice',dict(amount_msat=routes.AMOUNT,
        label='customer-plan-control',description='unpaid planning control',expiry=3600,cltv=40,
        exposeprivatechannels=[routes.edge(lab,nodes[-1],nodes[-2])['short_channel_id']]))['bolt11']
    paths = [state_dir(c.node)/name/'state.json' for c in clients
             for name in (authority_plugin.AUTHORITY_DIR,plugin.REMOTE_DIR,plugin.INVOICE_DIR)]
    before = [p.read_bytes() for p in paths]
    plan = clients[0].call('plan',dict(invoice=core))
    require(plan['route'][0]['delay'] == 200 and plan['route'][0]['amount_msat'] == routes.AMOUNT+2000
            and len(plan['route']) == 3 and before == [p.read_bytes() for p in paths],'scoped_readonly_plan_changed')
    require(lab.rpc(nodes[0]['cli'],'listsendpays')['payments'] == [],'planning_created_payment')


def payer_route(lab,nodes,decoded):
    hints = decoded.get('routes')
    require(type(hints) is list and len(hints) == 1 and len(hints[0]) == 1,'customer_hint_count_changed')
    h = hints[0][0]; final = decoded['min_final_cltv_expiry']; amount = decoded['amount_msat']
    require(h['pubkey'] == nodes[-2]['identity']['node_id'] and h['fee_base_msat'] == 1000
            and h['fee_proportional_millionths'] == 0 and h['cltv_expiry_delta'] == 80
            and final == 374,'customer_hint_policy_changed')
    reply = lab.rpc_named(nodes[0]['cli'],'getroutes',dict(source=nodes[0]['identity']['node_id'],
        destination=h['pubkey'],amount_msat=amount+1000,layers=['auto.localchans','auto.sourcefree'],
        maxfee_msat=9000,final_cltv=final+80,maxdelay=2016,maxparts=1))
    require(len(reply['routes']) == 1,'customer_prefix_multipart')
    r = reply['routes'][0]; path = r['path']
    require(r['amount_msat'] == amount+1000 and r['final_cltv'] == final+80 and len(path) == 2,
            'customer_prefix_shape_changed')
    route = []
    for i,hop in enumerate(path):
        a,b = nodes[i]['identity']['node_id'],nodes[i+1]['identity']['node_id']
        scid = routes.edge(lab,nodes[i],nodes[i+1])['short_channel_id']
        require(hop['node_id_in'] == a and hop['node_id_out'] == b
                and hop['short_channel_id_dir'] == scid+'/'+str(int(a>b))
                and hop['amount_out_msat'] == amount+(2-i)*1000
                and hop['cltv_out'] == final+(2-i)*80,'customer_public_prefix_changed')
        route.append(dict(id=b,channel=scid,amount_msat=hop['amount_out_msat'],delay=hop['cltv_out']))
    route.append(dict(id=decoded['payee'],channel=h['short_channel_id'],amount_msat=amount,delay=final))
    return route


def payer_attempt(lab,node,bolt,quote,old=None):
    rows = lab.rpc_named(node['cli'],'listsendpays',dict(payment_hash=quote['payment_hash']))['payments']
    require(len(rows) == 1,'customer_one_attempt_required'); row = rows[0]
    identity = {k:row.get(k,0) if k=='partid' else row.get(k) for k in
        ('payment_hash','groupid','partid','destination','amount_msat','amount_sent_msat','created_index','created_at')}
    expected = dict(payment_hash=quote['payment_hash'],groupid=1,partid=0,destination=quote['chain']['node_id'],
                    amount_msat=routes.AMOUNT,amount_sent_msat=routes.AMOUNT+2000)
    require(all(identity[k] == v for k,v in expected.items()) and row['bolt11'] == bolt
        and hg.integer(identity['created_index'],1,2**64-1) and hg.integer(identity['created_at'],1,2**35-1)
        and (old is None or identity == old),'customer_original_attempt_changed')
    if row['status'] == 'complete': ba.preimage(row['payment_preimage'],quote['payment_hash'])
    else: require('payment_preimage' not in row,'customer_unexpected_preimage')
    return row['status'],identity


def scenario(lab,backend,nodes,clients,name):
    lab.checkpoint(nodes[0]['profile']+'-customer-'+name)
    routes.quiet(lab,nodes); routes.public_graph(lab,nodes)
    before = [r['to_us_msat'] for _,r in routes.endpoints(lab,nodes)]
    incoming = clients[1]; g = incoming.call('info')['grant']
    preimage = os.urandom(32).hex(); payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    quote = dict(chain=nodes[-1]['identity'],payment_hash=payment_hash,payment_secret=os.urandom(32).hex(),
        amount_msat=routes.AMOUNT,expires_at=int(time.time())+1800,min_cltv_delta=350,max_cltv_delta=2016,
        approved_channels=g['channels'])
    eid = incoming.call('enroll',dict(data=quote,confirmation=ba.digest(quote)))['entry_id']
    incoming.lost('swap-gate-register','publish',dict(entry_id=eid)); incoming.call('publish',dict(entry_id=eid))
    incoming.lost('signinvoice','invoice',dict(entry_id=eid))
    journal = hg.Journal(state_dir(nodes[-1])/plugin.INVOICE_DIR)
    try: original_unsigned = journal.read()['invoices'][eid]['unsigned']
    finally: journal.close()
    result = incoming.call('invoice',dict(entry_id=eid)); bolt = result['invoice']
    # A lost facade reply is recovered from disk, without another signature.
    signs = incoming.mutations().count('signinvoice')
    require(incoming.call('invoice',dict(entry_id=eid)) == result and incoming.mutations().count('signinvoice') == signs,
            'customer_invoice_reply_replaced')
    require(lab.rpc_named(nodes[-1]['cli'],'listinvoices',dict(payment_hash=payment_hash))['invoices'] == [],
            'customer_hash_registered_with_core')
    decoded = ci.invoice(lab.rpc_named(nodes[0]['cli'],'decode',dict(string=bolt)),nodes[0]['profile'],'regtest')
    require(decoded['payment_hash'] == payment_hash and decoded['payment_secret'] == quote['payment_secret']
            and decoded['created_at']+decoded['expiry'] == quote['expires_at']
            and not ci.feature_bits(decoded['features']) & ((1<<16)|(1<<17)),'customer_signed_fields_changed')
    route = payer_route(lab,nodes,decoded)
    lab.rpc_named(nodes[0]['cli'],'sendpay',dict(route=route,payment_hash=payment_hash,
        payment_secret=quote['payment_secret'],bolt11=bolt,groupid=1,partid=0))
    held = routes.gate.held(lab,nodes[-1],payment_hash)
    original = incoming.evidence(eid)['entry']
    status,attempt = payer_attempt(lab,nodes[0],bolt,quote)
    require(status == 'pending' and original['incoming']['binding'] == held['binding'],'customer_payment_not_held')
    if name == 'restart-held':
        credentials = [c.credential for c in clients]
        nodes = routes.restart(lab,nodes,backend)
        for i,(n,p) in enumerate(((nodes[0],nodes[1]),(nodes[-1],nodes[-2]))):
            clients[i] = Client(lab,n,p,clients[i].gid); clients[i].credential = credentials[i]
        incoming = clients[1]
        worker = local.Worker(lab,dict(nodes[-1],root=state_dir(nodes[-1]))); worker.call('pause',incoming.gid)
        (state_dir(nodes[-1])/'customer-fixture-time').write_text(str(g['expires_at']+1))
    require(incoming.call('invoice',dict(entry_id=eid)) == result,'customer_restart_invoice_changed')
    journal = hg.Journal(state_dir(nodes[-1])/plugin.INVOICE_DIR)
    try: require(journal.read()['invoices'][eid]['unsigned'] == original_unsigned,'customer_unsigned_intent_changed')
    finally: journal.close()
    require(incoming.evidence(eid)['entry']['incoming'] == original['incoming'],'customer_binding_changed')
    releasing = name != 'fail'; op = 'release' if releasing else 'fail'; body = dict(entry_id=eid)
    if releasing: body['preimage'] = preimage
    incoming.lost('swap-gate-'+op,op,body); incoming.call(op,body)
    wanted = 'complete' if releasing else 'failed'
    lab.wait(lambda:payer_attempt(lab,nodes[0],bolt,quote,attempt)[0] == wanted,nodes[0]['proc'])
    lab.wait(lambda:incoming.call('reconcile',dict(entry_id=eid))['terminal'],nodes[-1]['proc'])
    routes.quiet(lab,nodes)
    after = [r['to_us_msat'] for _,r in routes.endpoints(lab,nodes)]
    deltas = [-2002000,2002000,-2001000,2001000,-routes.AMOUNT,routes.AMOUNT] if releasing else [0]*6
    require(after == [a+d for a,d in zip(before,deltas)],'customer_balances_or_fees_changed')
    final = incoming.evidence(eid)['entry']
    require(final['incoming'] == original['incoming'] and final['htlc'] == original['htlc']
            and final['terminal'] == ('released' if releasing else 'failed'),'customer_terminal_history_changed')
    require(lab.rpc_named(nodes[-1]['cli'],'listinvoices',dict(payment_hash=payment_hash))['invoices'] == [],
            'customer_core_invoice_created')
    print('PASS: '+nodes[0]['profile']+' '+name+'; signed invoice, public prefix/private hint, exact recovery and six balances',flush=True)
    return nodes,dict(profile=nodes[0]['profile'],scenario=name,status=wanted,signed_customer_invoice=True,
        no_core_invoice=True,authenticated_peer_rpc=True,one_payment_attempt=True,same_invoice=True,
        same_incoming_binding=True,exact_six_balances=True,terminal_history=True,fee_msat=2000 if releasing else 0,
        lost_signature_reply=True,lost_invoice_reply=True,lost_resolution_reply=True,paused_expired_recovery=name=='restart-held')


def run(lab):
    receipts = []
    for label,profile in (('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)):
        backend,nodes,clients = topology(lab,label,profile); scope_and_plan(lab,nodes,clients)
        for name in ('release','fail','restart-held'):
            nodes,r = scenario(lab,backend,nodes,clients,name); receipts.append(r)
        require(clients[1].call('info')['remaining'] == 1,'customer_recovery_consumed_extra_slots')
        require(clients[1].mutations().count('signinvoice') == 6,'customer_signature_recovery_changed')
        (state_dir(nodes[-1])/plugin.INVOICE_DIR/'restored.json').write_text('{}\n')
        refused(lambda:clients[1].call('info'),-32602)
        for n in reversed(nodes): lab.stop(n['proc'])
        lab.stop(backend['proc'])
    result = dict(schema=1,fixture='privkeyio-customer-0078',scenarios=receipts,regtest_only=True,
        old_credentials_not_promoted=True,scoped_readonly_planning=True,independent_signed_publication=True,
        restore_barrier=True,cross_chain_controller=False,https_transport=False,live_market_feed=False,
        onchain_recovery=False,production_activation_supported=False)
    (lab.root/'customer-result.json').write_text(json.dumps(result,indent=2)+'\n')
    lab.checkpoint('complete')
    print('CUSTOMER FIXTURE PASSED: 0078; scoped plans and signed customer invoices; both chains; no cross-chain worker or installer',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'): parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()))
    args = parser.parse_args(); os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'): setattr(args,name,getattr(args,name).resolve())
    for prefix in (args.btc_prefix,args.xbt_prefix):
        for relative in ('bin/lightningd','bin/lightning-cli','libexec/c-lightning/plugins/cln-askrene','libexec/c-lightning/plugins/commando'):
            p = prefix/relative; require(p.is_file() and os.access(p,os.X_OK),'customer_fixture_executable_missing')
    for p in (args.bitcoind,args.bitcoin_cli,PLUGIN,routes.gate.PLUGIN):
        require(p.is_file() and os.access(p,os.X_OK),'customer_fixture_executable_missing')
    lab = CustomerLab(args)
    try: run(lab)
    finally: lab.close()


if __name__ == '__main__': main()
