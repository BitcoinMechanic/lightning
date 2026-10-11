#!/usr/bin/env python3
"""0076: real rune-authenticated peer RPC and bounded authority on both engines.

Reuses the proven four-node topology and compiled engines. No HTTPS, controller
policy, cross-chain payment, backup migration or service activation is claimed.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
import time

import authority_plugin as plugin
import bounded_authority as ba
import chain_identity as ci
import held_gate as hg
import remote_authority as remote
import test_authority_engine as local
import test_route_engine as routes
from test_engine import require, funding_address, FixtureRPCError

PLUGIN = Path(__file__).with_name('fixture_authority_plugin.py')
DENIED = 19537


def state_dir(node):
    # plugin init receives config_netdir, not --lightning-dir's base directory.
    return node['root']/'regtest'


def refused(call, code):
    try: call()
    except FixtureRPCError as error: require(error.code == code, 'unexpected_remote_refusal_code')
    else: raise RuntimeError('remote_request_not_refused')


class RemoteLab(routes.RouteLab):
    def start(self, command, log):
        if Path(command[0]).name == 'lightningd':
            command = [arg for arg in command if arg != '--disable-plugin=commando']
            root = log.parent/'regtest'
            if (root/plugin.REMOTE_DIR/'state.json').exists():
                state = json.loads((root/plugin.REMOTE_DIR/'state.json').read_text())
                command += ['--important-plugin='+str(PLUGIN), '--swap-authority-profile='+state['chain']['profile']]
        return super().start(command, log)

    def lightning(self, *args, **kwargs):
        node = super().lightning(*args, **kwargs)
        names = {'commando'}
        if (state_dir(node)/plugin.REMOTE_DIR/'state.json').exists(): names.add(PLUGIN.name)
        self.wait(lambda:names <= {Path(p['name']).name for p in self.rpc(node['cli'],'plugin','list')['plugins']
                                  if p.get('active') is True}, node['proc'])
        return node


def topology(lab, label, profile):
    lab.checkpoint(label+'-remote-runtime-preflight')
    backend = lab.backend(label+'-chain', profile == ci.PRIVKEYIO_XBT)
    nodes = [lab.lightning(label+'-'+name, profile, backend) for name in ('source','public','hint','recipient')]
    routes.preflight_route_rpc(lab, nodes)
    for a,b in zip(nodes, nodes[1:]):
        lab.rpc(a['cli'], 'connect', b['identity']['node_id'], '127.0.0.1', b['port'])
    for target, caller in ((nodes[0],nodes[1]), (nodes[-1],nodes[-2])):
        # Invoke all credential APIs and actual authenticated peer dispatch
        # before funding. A help/manifest assertion alone is insufficient.
        require(lab.rpc(target['cli'],'showrunes')['runes'] == [], 'fresh_rune_inventory_not_empty')
        probe = lab.rpc_named(target['cli'],'createrune',dict(restrictions=[['method=getinfo'],['pnum=0']]))
        checked = lab.rpc_named(target['cli'],'checkrune',dict(rune=probe['rune'], method='getinfo', params={}))
        require(checked == dict(valid=True), 'checkrune_runtime_unavailable')
        params = dict(peer_id=target['identity']['node_id'], method='getinfo', params={}, rune=probe['rune'])
        ci.node(lab.rpc_named(caller['cli'],'commando',params), profile,'regtest',expected_id=target['identity']['node_id'])
        refused(lambda:lab.rpc_named(caller['cli'],'commando',dict(params,method='close')), DENIED)
    lab.checkpoint(label+'-fund-remote-channels')
    for n in nodes[:-1]: lab.rpc(backend['cli'],'sendtoaddress',funding_address(lab,n),'0.01')
    lab.mine(backend); lab.synced(backend,*nodes)
    for n in nodes[:-1]:
        lab.wait(lambda n=n:any(o['status'] == 'confirmed' for o in lab.rpc(n['cli'],'listfunds')['outputs']),n['proc'])
    for i,(a,b) in enumerate(zip(nodes,nodes[1:])):
        lab.rpc_named(a['cli'],'fundchannel',dict(id=b['identity']['node_id'],amount=300000,announce=i<2),timeout=90)
    lab.mine(backend); lab.synced(backend,*nodes)
    routes.endpoints(lab,nodes); routes.public_graph(lab,nodes)
    workers = [local.Worker(lab,dict(n,root=state_dir(n)),fresh=True) for n in (nodes[0],nodes[-1])]
    grants = []
    for worker, kind, peer in zip(workers, ('outgoing','incoming'), (nodes[1],nodes[-2])):
        approved = [hg.channel_pin(routes.edge(lab,worker.node,peer),profile)]
        amount = routes.AMOUNT + (2000 if kind == 'outgoing' else 0)
        bounds = dict(max_swaps=4,max_amount_msat=amount,total_amount_msat=3*amount)
        bounds.update(routes.BOUNDS if kind == 'outgoing' else dict(min_cltv_delta=40,max_cltv_delta=80))
        grants.append(worker.call('create_grant',kind,approved,bounds,int(time.time())+86400,confirmed=True))
        remote.initialize_fixture(worker.node['root']/plugin.REMOTE_DIR,worker.node['identity'])
    # Activate the static authority facade and held gate only after explicit
    # fresh fixture initialization. No legacy journal conversion or auto-init.
    hg.initialize_fixture(routes.gate.private_dir(nodes[-1]),nodes[-1]['identity'])
    lab.gates[nodes[-1]['name']] = profile
    nodes = routes.restart(lab,nodes,backend)
    return backend,nodes,grants


class Client:
    """No cached execution state; each request is authorized by real commando."""
    def __init__(self, lab, node, caller, gid):
        self.lab, self.node, self.caller, self.gid = lab,node,caller,gid
        self.credential = None

    def setup(self):
        params = dict(grant_id=self.gid,confirmed=True)
        (state_dir(self.node)/'authority-drop-reply').write_text('createrune\n')
        refused(lambda:self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params), -32603)
        self.credential = self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params)
        require(self.lab.rpc_named(self.node['cli'],remote.CREDENTIAL,params) == self.credential,
                'credential_setup_not_idempotent')
        require(self.mutations().count('createrune') == 1, 'credential_minted_twice')

    def raw(self, method, params, rune=None):
        return self.lab.rpc_named(self.caller['cli'],'commando',dict(peer_id=self.node['identity']['node_id'],
            method=method,params=params,rune=self.credential['rune'] if rune is None else rune),timeout=90)

    def call(self, operation, body=None):
        return self.raw(remote.CALL,dict(grant_id=self.gid,operation=operation,body=body or {}))

    def lost(self, rpc, operation, body):
        arm = state_dir(self.node)/'authority-drop-reply'; arm.write_text(rpc+'\n')
        refused(lambda:self.call(operation,body),-32603)
        require(not arm.exists(),'fault_injection_not_consumed')

    def evidence(self,eid): return self.call('evidence',dict(entry_id=eid))

    def mutations(self):
        path = state_dir(self.node)/'authority-mutations.log'
        return path.read_text().splitlines() if path.exists() else []


def deny_scope(client):
    # Refused by the core's rune check BEFORE dispatch, not by missing method
    # handlers. None of these test requests is authorized to mutate the node.
    for method in ('sendpay','pay','xpay','sendonion','close','withdraw','createrune','invokerune',
                   'showrunes','checkrune','swap-gate-register','swap-gate-release','swap-gate-fail',
                   remote.CREDENTIAL,'swap-session-call','reverse-session-call'):
        refused(lambda method=method:client.raw(method,{}),DENIED)
    base = dict(grant_id=client.gid,operation='info',body={})
    for params in (dict(base,grant_id='0'*64), list(base.values()), dict(base,extra=True),
                   dict(base,operation='close'), dict(base,operation='plan')):
        refused(lambda params=params:client.raw(remote.CALL,params),DENIED)
    rune = client.credential['rune']
    forged = ('A' if rune[0] != 'A' else 'B')+rune[1:]
    refused(lambda:client.raw(remote.CALL,base,rune=forged),DENIED)
    # The facade validates names as well as pnum, so an extra field cannot
    # replace the required body while retaining an allowed count of three.
    refused(lambda:client.raw(remote.CALL,dict(grant_id=client.gid,operation='info',wrong={})), -32602)
    info = client.call('info')
    require(info['grant']['chain'] == client.node['identity'] and info['remaining'] == 4,
            'remote_info_not_bound')


def scenario(lab,backend,nodes,clients,name):
    source,recipient = clients
    lab.checkpoint(nodes[0]['profile']+'-remote-'+name)
    routes.quiet(lab,nodes); routes.public_graph(lab,nodes)
    before = [r['to_us_msat'] for _,r in routes.endpoints(lab,nodes)]
    quote,plan,preimage = local.invoice(lab,nodes,'remote-'+name)
    bodies = [dict(data=data,confirmation=ba.digest(data)) for data in (plan,quote)]
    ids = [c.call('enroll',body)['entry_id'] for c,body in zip(clients,bodies)]
    for c,body,eid in zip(clients,bodies,ids):
        require(c.call('enroll',body) == dict(entry_id=eid),'remote_enrollment_changed')
    if name == 'restart-held':
        extra_quote,extra_plan,_ = local.invoice(lab,nodes,'remote-budget-refusal')
        for c,data in zip(clients,(extra_plan,extra_quote)):
            refused(lambda c=c,data=data:c.call('enroll',dict(data=data,confirmation=ba.digest(data))),-32602)
            info = c.call('info'); g = info['grant']
            require(info['remaining'] == 1 and info['reserved_msat'] == g['limits']['total_amount_msat'],
                    'remote_aggregate_budget_changed')
    recipient.lost('swap-gate-register','publish',dict(entry_id=ids[1]))
    recipient.call('publish',dict(entry_id=ids[1]))
    count = source.mutations().count('sendpay')
    source.lost('sendpay','send',dict(entry_id=ids[0]))
    held = routes.gate.held(lab,nodes[-1],quote['payment_hash'])
    originals = [c.evidence(eid)['entry'] for c,eid in zip(clients,ids)]
    require(originals[0]['attempt']['state'] == 'pending' and originals[1]['incoming']['binding'] == held['binding'],
            'remote_held_evidence_missing')
    if name == 'restart-held':
        credentials = [copy.deepcopy(c.credential) for c in clients]
        nodes = routes.restart(lab,nodes,backend)
        for i,(n,peer) in enumerate(((nodes[0],nodes[1]),(nodes[-1],nodes[-2]))):
            clients[i] = Client(lab,n,peer,clients[i].gid); clients[i].credential = credentials[i]
            worker = local.Worker(lab,dict(n,root=state_dir(n))); worker.call('pause',clients[i].gid)
            expires = worker.record()['grants'][clients[i].gid]['terms']['expires_at']
            (state_dir(n)/'authority-fixture-time').write_text(str(expires+1))
        source,recipient = clients
        replay = routes.gate.held(lab,nodes[-1],quote['payment_hash'])
        require(replay['binding'] == held['binding'] and replay['cltv_expiry'] == held['cltv_expiry'],
                'remote_restart_binding_changed')
    for c,body,eid in zip(clients,bodies,ids):
        require(c.call('enroll',body) == dict(entry_id=eid),'remote_recovery_enrollment_changed')
    require(source.call('send',dict(entry_id=ids[0]))['state'] == 'pending','remote_send_not_observation')
    releasing = name != 'fail'; op = 'release' if releasing else 'fail'
    body = dict(entry_id=ids[1])
    if releasing: body['preimage'] = preimage
    recipient.lost('swap-gate-'+op,op,body); recipient.call(op,body)
    def terminal(client,eid,wanted):
        state = client.call('reconcile',dict(entry_id=eid))
        return state if state['terminal'] and state['state'] == wanted else None
    for c,eid,wanted in zip(clients,ids,('complete' if releasing else 'failed','released' if releasing else 'failed')):
        lab.wait(lambda c=c,eid=eid,wanted=wanted:terminal(c,eid,wanted),c.node['proc'])
    routes.quiet(lab,nodes)
    after = [r['to_us_msat'] for _,r in routes.endpoints(lab,nodes)]
    deltas = [-2002000,2002000,-2001000,2001000,-routes.AMOUNT,routes.AMOUNT] if releasing else [0]*6
    require(after == [a+d for a,d in zip(before,deltas)],'remote_balance_or_fee_changed')
    finals = [c.evidence(eid)['entry'] for c,eid in zip(clients,ids)]
    require(finals[0]['attempt']['identity'] == originals[0]['attempt']['identity']
        and finals[0]['htlc'] == originals[0]['htlc'] and finals[0]['payload'] == plan
        and finals[1]['incoming'] == originals[1]['incoming']
        and source.mutations().count('sendpay') == count+1,'remote_original_identity_or_attempt_changed')
    print('PASS: '+nodes[0]['profile']+' '+name+'; real scoped rune, lost replies, original HTLC/attempt and six balances',flush=True)
    return nodes,dict(profile=nodes[0]['profile'],scenario=name,status='complete' if releasing else 'failed',
        authenticated_peer_rpc=True,one_send=True,same_attempt=True,same_incoming_binding=True,exact_six_balances=True,
        terminal_history=True,fee_msat=2000 if releasing else 0,lost_registration_reply=True,lost_send_reply=True,
        lost_resolution_reply=True,paused_expired_recovery=name == 'restart-held')


def run(lab):
    receipts = []
    for label,profile in (('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)):
        backend,nodes,grants = topology(lab,label,profile)
        clients = [Client(lab,n,p,gid) for n,p,gid in zip((nodes[0],nodes[-1]),(nodes[1],nodes[-2]),grants)]
        for c in clients: c.setup(); deny_scope(c)
        for name in ('release','fail','restart-held'):
            nodes,receipt = scenario(lab,backend,nodes,clients,name); receipts.append(receipt)
        for c in clients:
            require(c.mutations().count('createrune') == 1,'remote_credential_changed_after_restart')
            (state_dir(c.node)/plugin.REMOTE_DIR/'restored.json').write_text('{}\n')
            refused(lambda:c.call('info'),-32602)
            # Keep the barrier, and independently exercise actual core revocation.
            lab.rpc_named(c.node['cli'],'blacklistrune',dict(start=int(c.credential['unique_id'])))
            refused(lambda:c.call('info'),DENIED)
        require(clients[0].mutations().count('sendpay') == 3,'remote_send_repeated')
        for n in reversed(nodes): lab.stop(n['proc'])
        lab.stop(backend['proc'])
    result = dict(schema=1,fixture='privkeyio-remote-0076',scenarios=receipts,regtest_only=True,
        real_rune_enforcement=True,scope_denials=True,lost_mint_recovery=True,aggregate_fee_inclusive_budgets=True,
        restore_barrier=True,core_revocation=True,https_transport=False,cross_chain_controller=False,
        onchain_recovery=False,automatic_payment_retries=False,production_activation_supported=False)
    (lab.root/'remote-result.json').write_text(json.dumps(result,indent=2)+'\n')
    lab.checkpoint('complete')
    print('REMOTE FIXTURE PASSED: 0076; BTC/XBT scoped credentials and exact remote recovery; no installer',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'): parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()))
    args = parser.parse_args(); os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'): setattr(args,name,getattr(args,name).resolve())
    for prefix in (args.btc_prefix,args.xbt_prefix):
        for relative in ('bin/lightningd','bin/lightning-cli','libexec/c-lightning/plugins/cln-askrene','libexec/c-lightning/plugins/commando'):
            path = prefix/relative; require(path.is_file() and os.access(path,os.X_OK),'remote_fixture_executable_missing')
    for path in (args.bitcoind,args.bitcoin_cli,PLUGIN,routes.gate.PLUGIN):
        require(path.is_file() and os.access(path,os.X_OK),'remote_fixture_executable_missing')
    lab = RemoteLab(args)
    try: run(lab)
    finally: lab.close()


if __name__ == '__main__': main()
