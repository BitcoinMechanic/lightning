#!/usr/bin/env python3
"""0070a: real SIGKILL of a gate child, fresh loopback regtest only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
from unittest.mock import patch

import test_gate_engine as gate
from test_engine import Lab, require, funding_address, check_received

ci, hg = gate.ci, gate.hg
PATCHED_VERSIONS = {ci.BTC: ('v26.06.9-swap0070',),
                    ci.PRIVKEYIO_XBT: ('v26.06.9-blake2b.7-swap0070',)}


class CrashLab(gate.GateLab):
    engine_versions = PATCHED_VERSIONS


def core_quote(lab, receiver):
    # A fallthrough trap: normal core processing could settle this invoice.
    # Production gate invoices are independent of the core invoice database.
    preimage = os.urandom(32).hex()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    created = lab.rpc_named(receiver['cli'], 'invoice', dict(amount_msat=gate.AMOUNT,
        label='crash-trap-'+payment_hash, description='disposable gate crash trap',
        expiry=1800, cltv=120, preimage=preimage))
    invoice = created['bolt11']
    decoded = ci.invoice(lab.rpc(receiver['cli'],'decode',invoice), receiver['profile'],'regtest')
    require(decoded['payment_hash'] == payment_hash and decoded['amount_msat'] == gate.AMOUNT
            and decoded['payee'] == receiver['identity']['node_id']
            and decoded['min_final_cltv_expiry'] == 120, 'core_trap_invoice_changed')
    quote = dict(chain=receiver['identity'],payment_hash=payment_hash,
        payment_secret=decoded['payment_secret'],amount_msat=gate.AMOUNT,
        expires_at=decoded['created_at']+decoded['expiry'],min_cltv_delta=100,max_cltv_delta=200,
        approved_channels=[hg.channel_pin(gate.channel(lab,receiver),receiver['profile'])])
    lab.rpc_named(receiver['cli'],'swap-gate-register',dict(quote=quote))
    return quote,invoice,preimage


def database(receiver, query, params=()):
    path = receiver['root']/'regtest'/'lightningd.sqlite3'
    require(path.is_file(), 'fixture_database_missing')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query,params)]


def pending_snapshot(receiver):
    # RCVD_ADD_ACK_REVOCATION=14 is a stable persisted enum, not an RPC label.
    rows = database(receiver,'SELECT * FROM channel_htlcs WHERE hstate=14')
    require(len(rows) == 1 and rows[0]['payment_key'] is None
            and rows[0]['msatoshi'] == gate.AMOUNT, 'one_committed_incoming_HTLC_required')
    return rows[0]


def core_unpaid(lab, receiver, payment_hash):
    rows = lab.rpc_named(receiver['cli'],'listinvoices',dict(payment_hash=payment_hash))['invoices']
    require(len(rows) == 1 and rows[0]['status'] == 'unpaid', 'held_hook_fell_through_to_invoice')


def isolate_invoice_hook(lab, receiver):
    # offers supplies decode AND an asynchronous invoice_payment hook. The
    # latter can delay settlement until after important-plugin shutdown, masking
    # fallthrough in a database-only assertion. Stop it only after creating,
    # decoding and holding the trap invoice. The next daemon start restores it.
    expected = {'bcli','topology','offers','txprepare','spenderp',gate.PLUGIN.name}
    rows = lab.rpc(receiver['cli'],'plugin','list')['plugins']
    require(len(rows) == len(expected)
            and {Path(p['name']).name for p in rows} == expected
            and all(p.get('active') is True for p in rows), 'crash_plugin_set_changed')
    offers = next(p for p in rows if Path(p['name']).name == 'offers')
    require(offers.get('dynamic') is True, 'offers_not_stoppable')
    lab.rpc(receiver['cli'],'plugin','stop',offers['name'])
    rows = lab.rpc(receiver['cli'],'plugin','list')['plugins']
    require(len(rows) == len(expected)-1
            and {Path(p['name']).name for p in rows} == expected-{'offers'}
            and all(p.get('active') is True for p in rows), 'invoice_hook_isolation_failed')


def crash_log(receiver, start):
    with (receiver['root']/'console.log').open('rb') as stream:
        stream.seek(start)
        return stream.read().decode('utf-8',errors='replace')


def check_crash_log(text, *, fallthrough, important):
    died = 'Plugin died from htlc_accepted hook call'
    assembled = 'HTLC set contains 1 HTLCs, for a total of 2000000msat out of 2000000msat'
    shutdown = 'Plugin marked as important, shutting down lightningd!'
    require((shutdown in text) == important, 'important_shutdown_log_mismatch')
    if fallthrough:
        require(text.count(died) == 1 and text.count(assembled) == 1
                and text.index(died) < text.index(assembled), 'hook_fallthrough_not_observed')
        if important:
            require(text.index(assembled) < text.index(shutdown), 'fallthrough_not_before_shutdown')
    else:
        require(died not in text and 'HTLC set contains' not in text
                and 'Resolved invoice' not in text, 'core_continued_after_important_plugin_death')


def record_crash(lab, receiver, snapshot, *, phase):
    # Small synthetic-fixture diagnostics; no hashes, secrets or database dump.
    rows = database(receiver,'SELECT hstate, payment_key FROM channel_htlcs WHERE id=?',
                    (snapshot['id'],))
    evidence = dict(profile=receiver['profile'],phase=phase,row_count=len(rows),
                    hstate=rows[0]['hstate'] if len(rows)==1 else None,
                    preimage_present=rows[0]['payment_key'] is not None if len(rows)==1 else None,
                    receiver_exit=receiver['proc'].returncode,invoice_hook_isolated=True)
    with (lab.root/'crash-observations.jsonl').open('a') as stream:
        stream.write(json.dumps(evidence)+'\n')


def kill_gate(receiver):
    require(receiver['proc'].poll() is None, 'receiver_exited_before_fault')
    marker = gate.private_dir(receiver)/'fixture-process.json'
    process = json.loads(marker.read_text())
    pid = process['pid']
    require(type(pid) is int and pid > 1 and process['parent'] == receiver['proc'].pid,
            'fixture_plugin_parent_mismatch')
    procdir = Path('/proc')/str(pid)
    # Check the current child, not merely a potentially stale PID file.
    fields = (procdir/'stat').read_text().rsplit(')',1)[1].split()
    require(int(fields[1]) == receiver['proc'].pid
            and procdir.stat().st_uid == os.getuid()
            and os.fsencode(gate.PLUGIN) in (procdir/'cmdline').read_bytes().split(b'\0'),
            'fixture_plugin_process_mismatch')
    os.kill(pid, signal.SIGKILL)


def crash_restart(lab, payer, receiver):
    original = {n['name']:(n['identity'],hg.channel_pin(gate.channel(lab,n),n['profile']))
                for n in (payer,receiver)}
    snapshot = pending_snapshot(receiver)
    payment_hash = bytes(snapshot['payment_hash']).hex()
    core_unpaid(lab,receiver,payment_hash)
    saved = (gate.private_dir(receiver)/'state.json').read_bytes()
    original_attempt = gate.attempt(lab,payer,payment_hash)
    isolate_invoice_hook(lab,receiver)
    log_start = (receiver['root']/'console.log').stat().st_size
    kill_gate(receiver)
    receiver['proc'].wait(timeout=45)
    require(receiver['proc'].returncode == 1, 'important_plugin_death_did_not_stop_daemon')
    gate.wait_for_gate_writer_exit(lab,receiver)
    record_crash(lab,receiver,snapshot,phase='patched')
    check_crash_log(crash_log(receiver,log_start),fallthrough=False,important=True)
    require(pending_snapshot(receiver) == snapshot, 'core_advanced_held_HTLC_during_plugin_death')
    require((gate.private_dir(receiver)/'state.json').read_bytes() == saved,
            'journal_changed_during_plugin_death')
    require(gate.attempt(lab,payer,payment_hash)['status'] == 'pending'
            and gate.attempt_identity(gate.attempt(lab,payer,payment_hash)) ==
                gate.attempt_identity(original_attempt), 'payer_changed_before_receiver_restart')
    lab.stop(payer['proc'])
    require(payer['proc'].returncode in (0,-15), 'payer_shutdown_failed')
    receiver = lab.lightning(receiver['name'],receiver['profile'],receiver['backend'])
    payer = lab.lightning(payer['name'],payer['profile'],payer['backend'])
    gate.connect(lab,payer,receiver)
    for node in (payer,receiver):
        identity,pin = original[node['name']]
        ci.same_binding(identity,node['identity'])
        require(hg.channel_pin(gate.channel(lab,node),node['profile']) == pin,
                'funding_or_signing_type_changed')
    # Even replaying a durable release must resolve through the gate.
    core_unpaid(lab,receiver,payment_hash)
    return payer,receiver


def setup(lab, label, profile):
    lab.checkpoint(label+'-fresh-crash-channel')
    backend = lab.backend(label+'-chain',profile == ci.PRIVKEYIO_XBT)
    payer = lab.lightning(label+'-payer',profile,backend)
    receiver = lab.lightning(label+'-receiver',profile,backend)
    lab.rpc(backend['cli'],'sendtoaddress',funding_address(lab,payer),'0.01')
    lab.mine(backend)
    lab.synced(backend,payer,receiver)
    lab.wait(lambda:any(o['status'] == 'confirmed' for o in
                       lab.rpc(payer['cli'],'listfunds')['outputs']),payer['proc'])
    lab.rpc(payer['cli'],'connect',receiver['identity']['node_id'],'127.0.0.1',receiver['port'])
    lab.rpc(payer['cli'],'fundchannel',receiver['identity']['node_id'],300000,timeout=90)
    lab.mine(backend)
    lab.synced(backend,payer,receiver)
    gate.channel(lab,payer)
    gate.channel(lab,receiver)
    lab.stop(receiver['proc'])
    hg.initialize_fixture(gate.private_dir(receiver),receiver['identity'])
    lab.gates[receiver['name']] = profile
    receiver = lab.lightning(receiver['name'],profile,backend)
    gate.connect(lab,payer,receiver)
    return backend,payer,receiver


def control(lab, payer, receiver, *, important):
    lab.checkpoint(receiver['profile']+('-unpatched-important-control' if important else '-nonimportant-control'))
    if not important:
        lab.stop(receiver['proc'])
        gate.wait_for_gate_writer_exit(lab,receiver)
        lab.important = False
        receiver = lab.lightning(receiver['name'],receiver['profile'],receiver['backend'])
        gate.connect(lab,payer,receiver)
    quote,invoice,preimage = core_quote(lab,receiver)
    payment_hash = quote['payment_hash']
    gate.send_once(lab,payer,receiver,invoice,payment_hash)
    gate.held(lab,receiver,payment_hash)
    snapshot = pending_snapshot(receiver)
    core_unpaid(lab,receiver,payment_hash)
    isolate_invoice_hook(lab,receiver)
    log_start = (receiver['root']/'console.log').stat().st_size
    kill_gate(receiver)
    if important:
        receiver['proc'].wait(timeout=45)
        require(receiver['proc'].returncode == 1, 'baseline_important_exit_missing')
        record_crash(lab,receiver,snapshot,phase='baseline')
        check_crash_log(crash_log(receiver,log_start),fallthrough=True,important=True)
        rows = database(receiver,'SELECT * FROM channel_htlcs WHERE id=?',(snapshot['id'],))
        require(len(rows) == 1 and rows[0]['hstate'] != 14
                and rows[0]['payment_key'] == bytes.fromhex(preimage),
                'baseline_did_not_reproduce_hook_fallthrough')
    else:
        lab.wait(lambda:gate.attempt(lab,payer,payment_hash)['status'] == 'complete',receiver['proc'])
        complete = lab.rpc_named(payer['cli'],'waitsendpay',dict(payment_hash=payment_hash,
                                partid=0,groupid=1,timeout=30),timeout=45)
        require(complete['payment_preimage'] == preimage, 'control_preimage_mismatch')
        check_received(lab.rpc_named(receiver['cli'],'listinvoices',dict(payment_hash=payment_hash)),
                       payment_hash,gate.AMOUNT)
        gate.quiet(lab,payer,receiver)
        check_crash_log(crash_log(receiver,log_start),fallthrough=True,important=False)
        lab.stop(receiver['proc'])
    print('PASS: '+receiver['profile']+' '+('original important-plugin fallthrough reproduced'
          if important else 'ordinary non-important-plugin fallthrough preserved'),flush=True)
    return dict(profile=receiver['profile'],important=important,fallthrough_observed=True,
                invoice_hook_isolated=True,exact_preimage_verified=True)


def run(lab, baseline):
    scenarios,controls = [],[]
    for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]:
        lab.important = True
        backend,payer,receiver = setup(lab,label,profile)
        if not baseline:
            gate.ordinary_payment(lab,payer,receiver,'ordinary-before-crashes')
            with patch.object(gate,'restart_pair',crash_restart), patch.object(gate,'make_quote',core_quote):
                for interrupt_terminal in (False,True):
                    for releasing in (True,False):
                        payer,receiver,receipt = gate.scenario(lab,payer,receiver,
                            releasing=releasing,interrupt_terminal=interrupt_terminal)
                        receipt.update(plugin_sigkill=True,core_HTLC_unchanged_before_restart=True,
                                       core_invoice_fallthrough_prevented=True,
                                       invoice_hook_isolated=True,no_core_continuation=True)
                        scenarios.append(receipt)
            gate.ordinary_payment(lab,payer,receiver,'ordinary-after-crashes')
        controls.append(control(lab,payer,receiver,important=baseline))
        lab.stop(payer['proc'])
        lab.stop(backend['proc'])
    result = dict(schema=1,fixture='privkeyio-plugin-crash-0070a',regtest_only=True,
        mode='baseline' if baseline else 'patched',scenarios=scenarios,controls=controls,
        important_plugin_crash_matrix_passed=not baseline,
        full_swap_controller_tested=False,production_activation_supported=False,
        automatic_payment_retries=False)
    (lab.root/'crash-result.json').write_text(json.dumps(result,indent=2)+'\n')
    lab.checkpoint('complete')
    print('PLUGIN CRASH FIXTURE PASSED: 0070a '+result['mode']+'; regtest only; no installer',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    parser.add_argument('--mode',choices=('baseline','regression','patched'),required=True)
    for arg in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):
        parser.add_argument('--'+arg,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,required=True)
    args = parser.parse_args()
    os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli','results_parent'):
        setattr(args,name,getattr(args,name).resolve())
    for p in (args.btc_prefix/'bin/lightningd',args.xbt_prefix/'bin/lightningd',
              args.bitcoind,args.bitcoin_cli,gate.PLUGIN):
        require(p.is_file() and os.access(p,os.X_OK),'fixture_executable_missing')
    lab = gate.GateLab(args) if args.mode == 'baseline' else CrashLab(args)
    try:
        if args.mode == 'regression':
            gate.run(lab)
        else:
            run(lab, args.mode == 'baseline')
    finally:
        lab.close()


if __name__ == '__main__':
    main()
