#!/usr/bin/env python3
"""0072: routed planner and wallet-observation fixture; no swap authority.

Fresh loopback regtest only. The fixture, not the read-only adapter, registers
the trap invoice and sends/resolves one payment. Four nodes per chain, public
prefix and private last channel. Every send RPC is submitted exactly once.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

import chain_identity as ci
import held_gate as hg
import route_plan as rp
import test_gate_engine as gate
from test_engine import require, funding_address, FixtureRPCError

AMOUNT = 2000000
BOUNDS = dict(max_fee_msat=10000, max_delay_blocks=288, max_hops=4)
VERSIONS = {ci.BTC: ('v26.06.9-swap0070',), ci.PRIVKEYIO_XBT: ('v26.06.9-blake2b.7-swap0070',)}
REQUIRED = {'getroutes', 'listsendpays', 'listhtlcs', 'listclosedchannels', 'sendpay', 'waitsendpay'}


class RouteLab(gate.GateLab):
    engine_versions = VERSIONS

    def start(self, command, log):
        if Path(command[0]).name == 'lightningd':
            command = [arg for arg in command if arg != '--disable-plugin=cln-askrene']
            command += ['--dev-fast-gossip', '--cltv-delta=80', '--fee-base=1000', '--fee-per-satoshi=0']
        return super().start(command, log)

    def lightning(self, name, profile, backend, **kwargs):
        node = super().lightning(name, profile, backend, **kwargs)
        def ready():
            return any(Path(p['name']).name == 'cln-askrene' and p.get('active') is True
                       for p in self.rpc(node['cli'],'plugin','list')['plugins'])
        self.wait(ready,node['proc'])
        available = {r['command'].split()[0] for r in self.rpc(node['cli'],'help')['help']}
        require(REQUIRED <= available, 'route_fixture_required_RPC_missing: '+','.join(sorted(REQUIRED-available)))
        # Runtime calls, not just manifest/help registration, before funding.
        if not (node['root']/'fixture-started').exists():
            require(self.rpc(node['cli'],'listsendpays').get('payments') == [], 'fresh_node_payment_history_not_empty')
        for method, field in (('listhtlcs','htlcs'), ('listclosedchannels','closedchannels')):
            require(type(self.rpc(node['cli'],method).get(field)) is list, 'history_RPC_response_changed')
        (node['root']/'fixture-started').touch()
        return node


def adapter(lab, node, calls=None):
    def call(method, **params):
        require(method in rp.READ_METHODS, 'read_only_adapter_mutation_refused')
        if calls is not None:
            calls.append(method)
        try:
            return lab.rpc_named(node['cli'],method,params)
        except FixtureRPCError as error:
            raise rp.RPCError(error.code) from None
    return call


def rows(lab, node):
    return lab.rpc(node['cli'],'listpeerchannels')['channels']


def normal_row(lab, node, peer):
    found = [r for r in rows(lab,node) if r.get('peer_id') == peer['identity']['node_id']]
    if len(found) != 1:
        return None
    r = found[0]
    if r.get('state') != 'CHANNELD_NORMAL' or r.get('peer_connected') is not True or r.get('reestablished') is not True:
        return None
    ci.channel(r,node['profile'])
    return r if hg.scid(r.get('short_channel_id')) else None


def edge(lab, node, peer):
    return lab.wait(lambda:normal_row(lab,node,peer), node['proc'])


def endpoints(lab, nodes):
    return [(n,edge(lab,n,p)) for a,b in zip(nodes,nodes[1:]) for n,p in ((a,b),(b,a))]


def quiet(lab, nodes):
    for a,b in zip(nodes,nodes[1:]):
        for n,p in ((a,b),(b,a)):
            lab.wait(lambda n=n,p=p:(r:=normal_row(lab,n,p)) is not None and r.get('htlcs') == [],n['proc'])


def public_graph(lab, nodes):
    wanted = {edge(lab,nodes[i],nodes[i+1])['short_channel_id'] for i in (0,1)}
    def ready():
        graph = lab.rpc(nodes[0]['cli'],'listchannels')['channels']
        return all(len([r for r in graph if r.get('short_channel_id') == scid and r.get('active') is True
                        and r.get('public') is True and r.get('delay') == 80
                        and r.get('base_fee_millisatoshi') == 1000 and r.get('fee_per_millionth') == 0]) == 2 for scid in wanted)
    lab.wait(ready,nodes[0]['proc'])
    final = edge(lab,nodes[-1],nodes[-2])['short_channel_id']
    require(not lab.rpc_named(nodes[0]['cli'],'listchannels',dict(short_channel_id=final))['channels'],
            'private_final_channel_leaked_to_source_graph')
    require(len(rows(lab,nodes[0])) == len(rows(lab,nodes[-1])) == 1, 'non_neighbor_topology_changed')


def preflight_route_rpc(lab, nodes):
    for n,p in zip(nodes, nodes[1:]+nodes[:1]):
        try:
            rp.query(adapter(lab,n),n['identity']['node_id'],p['identity']['node_id'],AMOUNT,40,BOUNDS)
        except rp.RPCError as error:
            require(error.code == 205, 'empty_graph_getroutes_runtime_refused')
        else:
            raise RuntimeError('fresh_empty_graph_unexpected_route')


def restart(lab, nodes, backend):
    original = [(n['identity'],hg.channel_pin(r,n['profile'])) for n,r in endpoints(lab,nodes)]
    old = nodes
    for node in reversed(nodes): lab.stop(node['proc'])
    require(all(n['proc'].returncode in (0,-15) for n in old), 'route_fixture_shutdown_not_graceful')
    gate.wait_for_gate_writer_exit(lab,old[-1])
    nodes = [lab.lightning(n['name'],n['profile'],backend) for n in old]
    for a,b in zip(nodes,nodes[1:]):
        lab.rpc(a['cli'],'connect',b['identity']['node_id'],'127.0.0.1',b['port'])
    lab.synced(backend,*nodes)
    after = [(n['identity'],hg.channel_pin(r,n['profile'])) for n,r in endpoints(lab,nodes)]
    require(after == original, 'route_fixture_funding_or_signing_type_changed')
    return nodes


def scenario(lab, nodes, backend, name):
    source, recipient = nodes[0], nodes[-1]
    profile = source['profile']
    lab.checkpoint(profile+'-routed-'+name)
    quiet(lab,nodes); public_graph(lab,nodes)
    before = [r['to_us_msat'] for _,r in endpoints(lab,nodes)]
    preimage = os.urandom(32).hex()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    # Deliberate core-invoice trap, as in 0070. It supplies authentic private
    # route hints; the gate must intercept it before normal invoice settlement.
    private = edge(lab,recipient,nodes[-2])
    created = lab.rpc_named(recipient['cli'],'invoice',dict(amount_msat=AMOUNT,
        label='route-'+name+'-'+payment_hash, description='disposable routed fixture', expiry=1800,
        cltv=40, preimage=preimage, exposeprivatechannels=[private['short_channel_id']]))
    invoice = created['bolt11']
    decoded = ci.invoice(lab.rpc(source['cli'],'decode',invoice),profile,'regtest')
    hints = decoded.get('routes')
    require(type(hints) is list and len(hints) == 1 and len(hints[0]) == 1
            and hints[0][0]['pubkey'] == nodes[-2]['identity']['node_id']
            and hints[0][0]['fee_base_msat'] == 1000 and hints[0][0]['fee_proportional_millionths'] == 0
            and hints[0][0]['cltv_expiry_delta'] == 80, 'private_hint_shape_or_policy_changed')
    quote = dict(chain=recipient['identity'],payment_hash=payment_hash,payment_secret=decoded['payment_secret'],
        amount_msat=AMOUNT,expires_at=decoded['created_at']+decoded['expiry'],min_cltv_delta=40,max_cltv_delta=80,
        approved_channels=[hg.channel_pin(private,profile)])
    lab.rpc_named(recipient['cli'],'swap-gate-register',dict(quote=quote))
    approved = [hg.channel_pin(edge(lab,source,nodes[1]),profile)]
    calls = []
    planned = rp.plan(adapter(lab,source,calls),source['identity'],invoice,approved,BOUNDS)
    require(len(planned['route']) == 3 and planned['route'][0]['delay'] == 200
            and planned['route'][0]['amount_msat']-AMOUNT == 2000
            and planned['route'][-1]['channel'] == hints[0][0]['short_channel_id']
            and calls.count('getroutes') == 2, 'routed_plan_did_not_use_expected_public_prefix_and_hint')
    # The same invoice cannot fit the historical 144-block limit. Search may
    # not widen this bound or create any payment records.
    try:
        rp.plan(adapter(lab,source),source['identity'],invoice,approved,dict(BOUNDS,max_delay_blocks=144))
    except ValueError as error:
        require(str(error) == 'bounded_route_unavailable', 'unexpected_bounded_route_refusal')
    else:
        raise RuntimeError('144_block_bound_not_enforced')
    require(rp.observe_attempt(adapter(lab,source),planned) == {'state':'not_observed'},'planning_created_payment')
    path = lab.root/(source['name']+'-'+name+'-plan.json')
    with path.open('x') as output:
        json.dump(planned,output); output.flush(); os.fsync(output.fileno())
    expected_digest = rp.digest(planned)
    sent = 0
    # Simulate the worker losing the reply: discard sendpay's result entirely.
    # All remaining code only observes this attempt and controls the test gate.
    sent += 1
    lab.rpc_named(source['cli'],'sendpay',dict(route=planned['route'],payment_hash=payment_hash,
        payment_secret=planned['payment_secret'],bolt11=invoice,partid=0,groupid=1),timeout=90)
    first_gate = gate.held(lab,recipient,payment_hash)
    require(first_gate['binding']['channel'] == quote['approved_channels'][0], 'actual_incoming_pin_changed')
    original = rp.observe_attempt(adapter(lab,source),json.loads(path.read_text()))
    require(original['state'] == 'pending', 'routed_attempt_not_held')
    if name == 'restart-held':
        nodes = restart(lab,nodes,backend)
        source, recipient = nodes[0], nodes[-1]
        replay = gate.held(lab,recipient,payment_hash)
        require(replay['binding'] == first_gate['binding'] and replay['cltv_expiry'] == first_gate['cltv_expiry'],
                'routed_incoming_binding_changed_on_restart')
    saved = json.loads(path.read_text())
    require(rp.digest(saved) == expected_digest, 'saved_route_changed')
    observed = rp.observe_attempt(adapter(lab,source),saved,original['identity'])
    require(observed['state'] == 'pending', 'original_attempt_not_pending')
    params = dict(payment_hash=payment_hash,binding=first_gate['binding'])
    releasing = name != 'fail'
    if releasing: params['preimage'] = preimage
    method = 'swap-gate-release' if releasing else 'swap-gate-fail'
    lab.rpc_named(recipient['cli'],method,params)
    # An exact gate reply retry is distinct from retrying a payment attempt.
    lab.rpc_named(recipient['cli'],method,params)
    wanted = 'complete' if releasing else 'failed'
    def finished():
        value = rp.observe_attempt(adapter(lab,source),json.loads(path.read_text()),original['identity'])
        return value if value['state'] == wanted else None
    final = lab.wait(finished,source['proc'])
    if releasing: require(final['preimage'] == preimage, 'routed_preimage_changed')
    quiet(lab,nodes)
    after = [r['to_us_msat'] for _,r in endpoints(lab,nodes)]
    deltas = [-2002000,2002000,-2001000,2001000,-AMOUNT,AMOUNT] if releasing else [0]*6
    require(after == [a+d for a,d in zip(before,deltas)], 'six_endpoint_balances_or_routing_fees_changed')
    terminal = gate.status(lab,recipient,payment_hash)
    require(terminal['phase'] == ('release_intent' if releasing else 'fail_intent')
            and terminal['binding'] == first_gate['binding'] and terminal['terminal_is_intent'] is True
            and not terminal['hook_ready'] and not terminal['needs_attention'], 'gate_terminal_state_changed')
    # The core's terminal HTLC history identifies the same incoming commitment.
    history = lab.rpc_named(recipient['cli'],'listhtlcs',dict(id=private['channel_id']))['htlcs']
    found = [h for h in history if h['payment_hash'] == payment_hash and h['direction'] == 'in']
    require(len(found) == 1 and found[0]['id'] == first_gate['binding']['htlc_id']
            and found[0]['expiry'] == first_gate['cltv_expiry'] and found[0]['amount_msat'] == AMOUNT
            and found[0]['state'] == 'SENT_REMOVE_ACK_REVOCATION', 'incoming_terminal_history_not_exact')
    require(sent == 1 and rp.digest(json.loads(path.read_text())) == expected_digest, 'route_or_send_changed')
    print('PASS: '+profile+' '+name+'; 3 hops, 200 blocks, 2000 msat fee, six balances and original attempt',flush=True)
    return nodes, dict(profile=profile,scenario=name,status=wanted,hops=3,delay_blocks=200,
        fee_msat=2000 if releasing else 0,one_send=True,same_attempt=True,same_incoming_binding=True,
        exact_six_balances=True,terminal_history=True,saved_route_unchanged=True)


def run(lab):
    receipts = []
    for label,profile in (('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)):
        lab.checkpoint(label+'-route-runtime-preflight')
        backend = lab.backend(label+'-chain',profile == ci.PRIVKEYIO_XBT)
        nodes = [lab.lightning(label+'-'+name,profile,backend) for name in ('source','public','hint','recipient')]
        preflight_route_rpc(lab,nodes)
        lab.checkpoint(label+'-fund-routed-channels')
        for n in nodes[:-1]: lab.rpc(backend['cli'],'sendtoaddress',funding_address(lab,n),'0.01')
        lab.mine(backend); lab.synced(backend,*nodes)
        for n in nodes[:-1]:
            lab.wait(lambda n=n:any(o['status'] == 'confirmed' for o in lab.rpc(n['cli'],'listfunds')['outputs']),n['proc'])
        for i,(a,b) in enumerate(zip(nodes,nodes[1:])):
            lab.rpc(a['cli'],'connect',b['identity']['node_id'],'127.0.0.1',b['port'])
            lab.rpc_named(a['cli'],'fundchannel',dict(id=b['identity']['node_id'],amount=300000,announce=i < 2),timeout=90)
        lab.mine(backend); lab.synced(backend,*nodes)
        endpoints(lab,nodes); public_graph(lab,nodes)
        recipient = nodes[-1]
        lab.stop(recipient['proc'])
        hg.initialize_fixture(gate.private_dir(recipient),recipient['identity'])
        lab.gates[recipient['name']] = profile
        nodes[-1] = lab.lightning(recipient['name'],profile,backend)
        lab.rpc(nodes[-2]['cli'],'connect',nodes[-1]['identity']['node_id'],'127.0.0.1',nodes[-1]['port'])
        endpoints(lab,nodes)
        for name in ('release','fail','restart-held'):
            nodes,receipt = scenario(lab,nodes,backend,name); receipts.append(receipt)
        for n in reversed(nodes): lab.stop(n['proc'])
        lab.stop(backend['proc'])
    result = dict(schema=1,fixture='privkeyio-routes-0072',scenarios=receipts,regtest_only=True,
                  runtime_getroutes_preflight=True,read_only_adapter=True,automatic_payment_retries=False,
                  deprecated_apis_enabled=False,grants_ported=False,full_swap_controller_tested=False,
                  production_activation_supported=False)
    (lab.root/'route-result.json').write_text(json.dumps(result,indent=2)+'\n')
    lab.checkpoint('complete')
    print('ROUTE FIXTURE PASSED: 0072; BTC/XBT routed release, failure and held restart; no installer',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()))
    args = parser.parse_args(); os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):
        setattr(args,name,getattr(args,name).resolve())
    for prefix in (args.btc_prefix,args.xbt_prefix):
        # The C parent and its forked worker are one installed cln-askrene
        # executable; there is no separate askrened binary in this release.
        for relative in ('bin/lightningd','bin/lightning-cli','libexec/c-lightning/plugins/cln-askrene'):
            p = prefix/relative
            require(p.is_file() and os.access(p,os.X_OK), 'route_fixture_executable_missing: '+str(p))
    for p in (args.bitcoind,args.bitcoin_cli,gate.PLUGIN):
        require(p.is_file() and os.access(p,os.X_OK), 'route_fixture_executable_missing')
    lab = RouteLab(args)
    try: run(lab)
    finally: lab.close()


if __name__ == '__main__': main()
