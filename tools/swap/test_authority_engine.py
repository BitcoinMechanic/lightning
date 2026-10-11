#!/usr/bin/env python3
"""0074 local authority fixture: two independent sides on each regtest chain.

This is deliberately NOT a cross-chain controller test or credential boundary.
The trusted fixture approves each enrollment and chooses recipient resolution.
All actual payment and gate mutations then go through bounded_authority.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import bounded_authority as ba
import chain_identity as ci
import held_gate as hg
import route_plan as rp
import test_route_engine as routes
from test_engine import require, funding_address, FixtureRPCError

AMOUNT = routes.AMOUNT


class Worker:
    """Each call opens a fresh locked journal and a new authority instance."""
    def __init__(self, lab, node, *, fresh=False):
        self.lab, self.node = lab, node
        self.directory = node['root']/'bounded-authority-v1'
        self.calls, self.drop = [], None
        self.clock = time.time
        if fresh: ba.initialize_fixture(self.directory, node['identity'])

    def rpc(self, method, **params):
        require(method in ba.METHODS, 'authority_fixture_rpc_not_permitted')
        self.calls.append(method)
        try:
            result = self.lab.rpc_named(self.node['cli'], method, params, timeout=90)
        except FixtureRPCError as error:
            raise rp.RPCError(error.code) from None
        if method == self.drop:
            self.drop = None
            raise TimeoutError('fixture_lost_reply')
        return result

    def call(self, method, *args, **kwargs):
        journal = hg.Journal(self.directory)
        try:
            authority = ba.Authority(journal, self.node['identity'], self.rpc, self.clock)
            require(method in ('create_grant', 'enroll', 'publish', 'send', 'reconcile', 'resolve', 'pause', 'status'),
                    'authority_fixture_operation_refused')
            return getattr(authority, method)(*args, **kwargs)
        finally: journal.close()

    def record(self):
        journal = hg.Journal(self.directory)
        try: return ba.validate(journal.read(), self.node['identity'])
        finally: journal.close()


def expect_refusal(function, reason):
    try: function()
    except ValueError as error:
        require(str(error) == reason, 'unexpected_authority_refusal: '+str(error))
    else: raise RuntimeError('authority_request_not_refused: '+reason)


def lost(worker, rpc, method, *args, **kwargs):
    worker.drop = rpc
    try: worker.call(method, *args, **kwargs)
    except TimeoutError as error:
        require(str(error) == 'fixture_lost_reply', 'unexpected_rpc_timeout')
    else: raise RuntimeError('fixture_reply_not_lost')


def topology(lab, label, profile):
    lab.checkpoint(label+'-authority-runtime-preflight')
    backend = lab.backend(label+'-chain', profile == ci.PRIVKEYIO_XBT)
    nodes = [lab.lightning(label+'-'+name, profile, backend) for name in ('source', 'public', 'hint', 'recipient')]
    routes.preflight_route_rpc(lab, nodes)
    lab.checkpoint(label+'-fund-authority-channels')
    for n in nodes[:-1]: lab.rpc(backend['cli'], 'sendtoaddress', funding_address(lab, n), '0.01')
    lab.mine(backend); lab.synced(backend, *nodes)
    for n in nodes[:-1]:
        lab.wait(lambda n=n:any(o['status'] == 'confirmed' for o in lab.rpc(n['cli'], 'listfunds')['outputs']), n['proc'])
    for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
        lab.rpc(a['cli'], 'connect', b['identity']['node_id'], '127.0.0.1', b['port'])
        lab.rpc_named(a['cli'], 'fundchannel', dict(id=b['identity']['node_id'], amount=300000, announce=i < 2), timeout=90)
    lab.mine(backend); lab.synced(backend, *nodes)
    routes.endpoints(lab, nodes); routes.public_graph(lab, nodes)
    recipient = nodes[-1]; lab.stop(recipient['proc'])
    hg.initialize_fixture(routes.gate.private_dir(recipient), recipient['identity'])
    lab.gates[recipient['name']] = profile
    nodes[-1] = lab.lightning(recipient['name'], profile, backend)
    lab.rpc(nodes[-2]['cli'], 'connect', nodes[-1]['identity']['node_id'], '127.0.0.1', nodes[-1]['port'])
    routes.endpoints(lab, nodes)
    return backend, nodes


def invoice(lab, nodes, label):
    source, recipient = nodes[0], nodes[-1]
    secret = os.urandom(32).hex(); payment_hash = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
    private = routes.edge(lab, recipient, nodes[-2])
    reply = lab.rpc_named(recipient['cli'], 'invoice', dict(amount_msat=AMOUNT,
        label='authority-'+label+'-'+payment_hash, description='disposable authority fixture', expiry=1800,
        cltv=40, preimage=secret, exposeprivatechannels=[private['short_channel_id']]))
    decoded = ci.invoice(lab.rpc(source['cli'], 'decode', reply['bolt11']), source['profile'], 'regtest')
    quote = dict(chain=recipient['identity'], payment_hash=payment_hash, payment_secret=decoded['payment_secret'],
        amount_msat=AMOUNT, expires_at=decoded['created_at']+decoded['expiry'], min_cltv_delta=40, max_cltv_delta=80,
        approved_channels=[hg.channel_pin(private, recipient['profile'])])
    planned = rp.plan(routes.adapter(lab, source), source['identity'], reply['bolt11'],
        [hg.channel_pin(routes.edge(lab, source, nodes[1]), source['profile'])], routes.BOUNDS)
    require(len(planned['route']) == 3 and planned['route'][0]['delay'] == 200
            and planned['route'][0]['amount_msat'] == AMOUNT+2000, 'authority_fixture_route_changed')
    return quote, planned, secret


def scenario(lab, backend, nodes, workers, grants, name):
    lab.checkpoint(nodes[0]['profile']+'-authority-'+name)
    source, recipient = workers
    routes.quiet(lab, nodes); routes.public_graph(lab, nodes)
    before = [r['to_us_msat'] for _, r in routes.endpoints(lab, nodes)]
    quote, planned, secret = invoice(lab, nodes, name)
    immutable = rp.digest(planned)
    out_id = source.call('enroll', grants[0], planned, confirmed=True)
    in_id = recipient.call('enroll', grants[1], quote, confirmed=True)
    # Exact duplicate enrollment reserves once, including a fresh worker.
    require(source.call('enroll', grants[0], planned, confirmed=True) == out_id
            and recipient.call('enroll', grants[1], quote, confirmed=True) == in_id, 'enrollment_not_idempotent')
    if name == 'restart-held':
        extra_quote, extra_plan, _ = invoice(lab, nodes, 'budget-refusal')
        for worker, gid, data in zip(workers, grants, (extra_plan, extra_quote)):
            expect_refusal(lambda worker=worker,gid=gid,data=data:
                           worker.call('enroll', gid, data, confirmed=True), 'grant_budget_exhausted')
        require(rp.observe_attempt(routes.adapter(lab, nodes[0]), extra_plan) == {'state':'not_observed'},
                'refused_enrollment_created_payment')
    lost(recipient, 'swap-gate-register', 'publish', in_id)
    recipient.call('publish', in_id)  # Exact register is explicitly idempotent.
    send_before = source.calls.count('sendpay')
    lost(source, 'sendpay', 'send', out_id)
    held = routes.gate.held(lab, nodes[-1], quote['payment_hash'])
    require(source.call('reconcile', out_id)['state'] == 'pending', 'authority_attempt_not_pending')
    require(recipient.call('reconcile', in_id)['state'] == 'held', 'authority_incoming_not_held')
    out_original = copy.deepcopy(source.record()['entries'][out_id])
    in_original = copy.deepcopy(recipient.record()['entries'][in_id])
    require(in_original['incoming']['binding'] == held['binding'], 'authority_actual_pin_changed')
    require(not recipient.call('status', in_id)['terminal'], 'held_marked_terminal')
    if name == 'restart-held':
        nodes = routes.restart(lab, nodes, backend)
        source.node, recipient.node = nodes[0], nodes[-1]
        replay = routes.gate.held(lab, nodes[-1], quote['payment_hash'])
        require(replay['binding'] == held['binding'] and replay['cltv_expiry'] == held['cltv_expiry'],
                'authority_restart_incoming_changed')
        # Pause and advance ONLY the worker clock, never the backend/daemon.
        # Recovery must ignore grant/invoice expiry while preserving evidence.
        for worker, gid in zip(workers, grants):
            worker.call('pause', gid)
            expires = worker.record()['grants'][gid]['terms']['expires_at']
            worker.clock = lambda expires=expires:expires+1
    require(source.call('enroll', grants[0], planned, confirmed=True) == out_id
            and recipient.call('enroll', grants[1], quote, confirmed=True) == in_id, 'recovery_enrollment_changed')
    require(source.call('send', out_id)['state'] == 'pending', 'send_recovery_not_observation')
    recipient.call('reconcile', in_id)
    require(source.record()['entries'][out_id]['attempt'] == out_original['attempt']
            and source.record()['entries'][out_id]['htlc'] == out_original['htlc']
            and recipient.record()['entries'][in_id]['incoming'] == in_original['incoming'], 'original_identity_changed')
    releasing = name != 'fail'; kwargs = dict(payment_preimage=secret) if releasing else {}
    method = 'swap-gate-release' if releasing else 'swap-gate-fail'
    lost(recipient, method, 'resolve', in_id, **kwargs)
    recipient.call('resolve', in_id, **kwargs)  # The gate does not emit another hook reply.
    def terminal(worker, eid, wanted):
        result = worker.call('reconcile', eid)
        return result if result['terminal'] and result['state'] == wanted else None
    lab.wait(lambda:terminal(source, out_id, 'complete' if releasing else 'failed'), nodes[0]['proc'])
    lab.wait(lambda:terminal(recipient, in_id, 'released' if releasing else 'failed'), nodes[-1]['proc'])
    routes.quiet(lab, nodes)
    after = [r['to_us_msat'] for _, r in routes.endpoints(lab, nodes)]
    deltas = [-2002000, 2002000, -2001000, 2001000, -AMOUNT, AMOUNT] if releasing else [0]*6
    require(after == [a+d for a,d in zip(before, deltas)], 'authority_balances_or_fees_changed')
    final = source.record()['entries'][out_id]
    require(final['attempt']['identity'] == out_original['attempt']['identity']
            and rp.digest(final['payload']) == immutable and source.calls.count('sendpay') == send_before+1,
            'authority_send_or_route_repeated')
    require(recipient.record()['entries'][in_id]['incoming'] == in_original['incoming'], 'authority_binding_changed')
    print('PASS: '+nodes[0]['profile']+' '+name+'; bounded enrollments, lost replies, exact HTLC/attempt and six balances', flush=True)
    return nodes, dict(profile=nodes[0]['profile'], scenario=name, status='complete' if releasing else 'failed',
        one_send=True, same_attempt=True, same_incoming_binding=True, fee_msat=2000 if releasing else 0,
        exact_six_balances=True, terminal_history=True, durable_enrollment=True, fresh_workers=True,
        immutable_plan=True, lost_registration_reply=True, lost_send_reply=True, lost_resolution_reply=True,
        paused_expired_recovery=name == 'restart-held')


def run(lab):
    receipts = []
    for label, profile in (('btc', ci.BTC), ('xbt', ci.PRIVKEYIO_XBT)):
        backend, nodes = topology(lab, label, profile)
        workers = [Worker(lab, n, fresh=True) for n in (nodes[0], nodes[-1])]
        grants = []
        for worker, kind, peer in zip(workers, ('outgoing', 'incoming'), (nodes[1], nodes[-2])):
            approved = [hg.channel_pin(routes.edge(lab, worker.node, peer), profile)]
            # Four slots but only three payments' aggregate amount: failure
            # must not replenish that amount, even though it costs no fee.
            per_payment = AMOUNT+2000 if kind == 'outgoing' else AMOUNT
            bounds = dict(max_swaps=4, max_amount_msat=per_payment, total_amount_msat=3*per_payment)
            bounds.update(routes.BOUNDS if kind == 'outgoing' else dict(min_cltv_delta=40, max_cltv_delta=80))
            grants.append(worker.call('create_grant', kind, approved, bounds, int(time.time())+86400, confirmed=True))
        for name in ('release', 'fail', 'restart-held'):
            nodes, receipt = scenario(lab, backend, nodes, workers, grants, name); receipts.append(receipt)
        # Restored state blocks all further authority; read-only cached status
        # remains available. Real journal contents are left intact for inspection.
        for worker in workers:
            marker = worker.directory/'restored.json'
            marker.write_text('{}\n')
            entry = next(iter(worker.record()['entries']))
            expect_refusal(lambda:worker.call('reconcile', entry), 'restored_authority_blocked')
            require(worker.call('status')['blocked'] is True, 'restore_barrier_not_reported')
        # Aggregate counters are checked against immutable journal data; fresh
        # recovery did not refund failed attempts or reserve any duplicate slot.
        for worker, gid in zip(workers, grants):
            record = worker.record(); entries = [e for e in record['entries'].values() if e['grant_id'] == gid]
            g = record['grants'][gid]['terms']
            require(len(entries) == 3 and sum(ba.payload(e['payload'], g) for e in entries)
                    == g['limits']['total_amount_msat'], 'authority_budget_or_slot_accounting_changed')
        require(workers[0].calls.count('sendpay') == 3, 'authority_automatic_retry')
        for n in reversed(nodes): lab.stop(n['proc'])
        lab.stop(backend['proc'])
    result = dict(schema=1, fixture='privkeyio-authority-0074', scenarios=receipts, regtest_only=True,
        local_authority=True, aggregate_fee_inclusive_budgets=True, restore_barrier=True,
        remote_credentials=False, cross_chain_controller=False, onchain_recovery=False,
        automatic_payment_retries=False, production_activation_supported=False)
    (lab.root/'authority-result.json').write_text(json.dumps(result, indent=2)+'\n')
    lab.checkpoint('complete')
    print('AUTHORITY FIXTURE PASSED: 0074; BTC/XBT bounded local grants, exact enrollment and recovery; no installer', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest', action='store_true', required=True)
    for name in ('btc-prefix', 'xbt-prefix', 'bitcoind', 'bitcoin-cli'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--results-parent', type=Path, default=Path(tempfile.gettempdir()))
    args = parser.parse_args(); os.umask(0o077)
    for name in ('btc_prefix', 'xbt_prefix', 'bitcoind', 'bitcoin_cli'):
        setattr(args, name, getattr(args, name).resolve())
    for prefix in (args.btc_prefix, args.xbt_prefix):
        for relative in ('bin/lightningd', 'bin/lightning-cli', 'libexec/c-lightning/plugins/cln-askrene'):
            p = prefix/relative; require(p.is_file() and os.access(p, os.X_OK), 'authority_fixture_executable_missing')
    for p in (args.bitcoind, args.bitcoin_cli, routes.gate.PLUGIN):
        require(p.is_file() and os.access(p, os.X_OK), 'authority_fixture_executable_missing')
    lab = routes.RouteLab(args)
    try: run(lab)
    finally: lab.close()


if __name__ == '__main__': main()
