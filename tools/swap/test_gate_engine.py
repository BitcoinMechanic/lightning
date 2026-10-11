#!/usr/bin/env python3
"""0068a: fresh BTC/XBT regtest held-HTLC and durable-intent restart fixture."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import chain_identity as ci
import held_gate as hg
from bolt11 import unsigned
from test_engine import Lab, FixtureRPCError, require, funding_address, check_received

AMOUNT = 2000000
PLUGIN = Path(__file__).with_name('fixture_gate_plugin.py')


class GateLab(Lab):
    important = True

    def __init__(self, args):
        super().__init__(args)
        self.gates = {}
        self.commands = {}

    def start(self, command, log):
        if Path(command[0]).name == 'lightningd':
            name = log.parent.name
            if name in self.gates:
                command = [arg for arg in command if not str(arg).startswith(
                    ('--important-plugin='+str(PLUGIN), '--plugin='+str(PLUGIN), '--swap-gate-profile='))]
                option = '--important-plugin=' if self.important else '--plugin='
                command = [*command, option+str(PLUGIN),
                           '--swap-gate-profile='+self.gates[name]]
            self.commands[name] = list(command)
        return super().start(command, log)

    def lightning(self, name, profile, backend, **kwargs):
        node = super().lightning(name, profile, backend, **kwargs)
        node.update(name=name, profile=profile, backend=backend)
        if name in self.gates:
            def ready():
                reply = self.rpc(node['cli'],'plugin','list')
                return any(Path(p['name']).name == PLUGIN.name and p.get('active') is True
                           for p in reply['plugins'])
            self.wait(ready,node['proc'])
            methods = {r['command'].split()[0] for r in self.rpc(node['cli'],'help')['help']}
            require({'swap-gate-register','swap-gate-status','swap-gate-release',
                     'swap-gate-fail','listsendpays'} <= methods, 'gate_RPC_missing')
        return node


def normal(lab, node):
    rows = lab.rpc(node['cli'],'listpeerchannels')['channels']
    if (len(rows) == 1 and rows[0]['state'] == 'CHANNELD_NORMAL'
            and rows[0]['peer_connected'] is True and rows[0].get('reestablished') is True):
        return ci.channel(rows[0], node['profile'])


def channel(lab, node):
    return lab.wait(lambda:normal(lab,node), node['proc'])


def quiet(lab, *nodes):
    for node in nodes:
        lab.wait(lambda node=node:(row:=normal(lab,node)) is not None and row.get('htlcs') == [], node['proc'])


def connect(lab, payer, receiver):
    # Core connect is safe to repeat; sendpay is never retried here.
    lab.rpc(payer['cli'],'connect',receiver['identity']['node_id'],'127.0.0.1',receiver['port'])
    channel(lab,payer)
    channel(lab,receiver)


def wait_for_gate_writer_exit(lab, receiver):
    # The daemon exiting need not mean a child plugin has finished processing
    # pipe EOF. Probe the same OS lock, without deleting or weakening it, before
    # spawning a replacement. Lab.wait retries BlockingIOError (an OSError).
    def released():
        journal = hg.Journal(private_dir(receiver))
        journal.close()
        return True
    lab.wait(released, timeout=15)


def restart_pair(lab, payer, receiver):
    original = {n['name']:(n['identity'], hg.channel_pin(channel(lab,n),n['profile'])) for n in (payer,receiver)}
    lab.stop(receiver['proc'])
    lab.stop(payer['proc'])
    # These are graceful daemon restarts, including pending-HTLC resubmission.
    require(receiver['proc'].returncode in (0, -15) and payer['proc'].returncode in (0,-15),
            'node_shutdown_was_not_graceful')
    wait_for_gate_writer_exit(lab,receiver)
    receiver = lab.lightning(receiver['name'],receiver['profile'],receiver['backend'])
    payer = lab.lightning(payer['name'],payer['profile'],payer['backend'])
    connect(lab,payer,receiver)
    for node in (payer,receiver):
        identity, pin = original[node['name']]
        ci.same_binding(identity,node['identity'])
        require(hg.channel_pin(channel(lab,node),node['profile']) == pin, 'channel_changed_after_restart')
    return payer,receiver


def attempt(lab, payer, payment_hash):
    rows = lab.rpc_named(payer['cli'],'listsendpays',{'payment_hash':payment_hash})['payments']
    require(len(rows) == 1, 'not_exactly_one_payment_attempt')
    row = rows[0]
    require(row['payment_hash'] == payment_hash and row.get('partid',0) == 0 and row['groupid'] == 1
            and row['amount_msat'] == AMOUNT and row['amount_sent_msat'] == AMOUNT
            and type(row.get('created_index')) is int, 'payment_attempt_mismatch')
    return row


def attempt_identity(row):
    return {k:row[k] for k in ('payment_hash','groupid','destination','amount_msat',
                               'amount_sent_msat','created_index','created_at')}


def send_once(lab, payer, receiver, invoice, payment_hash):
    d = ci.invoice(lab.rpc(payer['cli'],'decode',invoice),payer['profile'],'regtest')
    require(d['payee'] == receiver['identity']['node_id'] and d['amount_msat'] == AMOUNT
            and d['payment_hash'] == payment_hash, 'invoice_changed_before_send')
    row = channel(lab,payer)
    require(row['peer_id'] == d['payee'], 'route_peer_mismatch')
    route = [dict(id=d['payee'],channel=row['short_channel_id'],amount_msat=AMOUNT,
                  delay=d['min_final_cltv_expiry'])]
    reply = lab.rpc_named(payer['cli'],'sendpay',dict(route=route,payment_hash=payment_hash,
                         payment_secret=d['payment_secret'],bolt11=invoice,partid=0,groupid=1),timeout=90)
    require(reply.get('status') in ('pending','complete') and reply.get('payment_hash') == payment_hash
            and reply.get('groupid') == 1 and reply.get('partid',0) == 0, 'sendpay_reply_mismatch')


def status(lab, receiver, payment_hash):
    return lab.rpc_named(receiver['cli'],'swap-gate-status',{'payment_hash':payment_hash})


def held(lab, receiver, payment_hash):
    def ready():
        row = status(lab,receiver,payment_hash)
        require(not row['needs_attention'],'gate_needs_attention')
        return row if row['phase'] == 'held' and row['hook_ready'] else None
    return lab.wait(ready,receiver['proc'])


def ordinary_payment(lab, payer, receiver, label):
    created = lab.rpc(receiver['cli'],'invoice',AMOUNT,label,'ordinary fixture payment')
    send_once(lab,payer,receiver,created['bolt11'],created['payment_hash'])
    paid = lab.rpc_named(payer['cli'],'waitsendpay',dict(payment_hash=created['payment_hash'],
                         partid=0,groupid=1,timeout=60),timeout=90)
    require(paid['status'] == 'complete','ordinary_payment_failed')
    check_received(lab.rpc(receiver['cli'],'listinvoices',label),created['payment_hash'],AMOUNT)
    quiet(lab,payer,receiver)


def make_quote(lab, receiver):
    preimage, secret = os.urandom(32).hex(), os.urandom(32).hex()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    timestamp = int(time.time())
    invoice = lab.rpc(receiver['cli'],'signinvoice',unsigned(payment_hash,secret,AMOUNT,
                      profile=receiver['profile'],environment='regtest',expiry=1800,
                      final_cltv=120,timestamp=timestamp))['bolt11']
    d = ci.invoice(lab.rpc(receiver['cli'],'decode',invoice),receiver['profile'],'regtest')
    require(d['payee'] == receiver['identity']['node_id'] and d['payment_hash'] == payment_hash
            and d['payment_secret'] == secret and d['amount_msat'] == AMOUNT
            and d['min_final_cltv_expiry'] == 120 and d['expiry'] == 1800
            and d['created_at'] == timestamp and not ci.feature_bits(d['features']) & ((1<<16)|(1<<17)),
            'signed_gate_invoice_mismatch')
    quote = dict(chain=receiver['identity'],payment_hash=payment_hash,payment_secret=secret,
                 amount_msat=AMOUNT,expires_at=timestamp+1800,min_cltv_delta=100,max_cltv_delta=200,
                 approved_channels=[hg.channel_pin(channel(lab,receiver),receiver['profile'])])
    lab.rpc_named(receiver['cli'],'swap-gate-register',{'quote':quote})
    # Lost registration response: exact retry is harmless.
    lab.rpc_named(receiver['cli'],'swap-gate-register',{'quote':quote})
    return quote,invoice,preimage


def private_dir(receiver):
    return receiver['root']/'regtest'/'swap-gate-v1'


def pause_resolution(lab, receiver, quote, binding, preimage, releasing):
    directory = private_dir(receiver)
    marker = directory/'fixture-paused.json'
    marker.unlink(missing_ok=True)
    expected = dict(payment_hash=quote['payment_hash'],phase='release_intent' if releasing else 'fail_intent')
    (directory/'fixture-arm.json').write_text(json.dumps(expected))
    params = dict(payment_hash=quote['payment_hash'],binding=binding)
    if releasing:
        params['preimage'] = preimage
    command = [*map(str,receiver['cli']), '-k', 'swap-gate-release' if releasing else 'swap-gate-fail',
               *(k+'='+json.dumps(v,separators=(',',':')) for k,v in params.items())]
    proc = lab.start(command, receiver['root']/'interrupted-resolution.log')
    def paused():
        if not marker.is_file():
            return False
        try:
            observed = json.loads(marker.read_text())
        except json.JSONDecodeError:
            return False
        require(observed == expected,'wrong_paused_resolution')
        saved = json.loads((directory/'state.json').read_text())['quotes'][quote['payment_hash']]
        require(saved['phase'] == expected['phase'] and saved['binding'] == binding,
                'terminal_intent_not_durable')
        require(proc.poll() is None,'resolution_replied_before_interruption')
        return True
    lab.wait(paused,receiver['proc'])
    return proc


def scenario(lab, payer, receiver, *, releasing, interrupt_terminal):
    name = ('release' if releasing else 'fail') + ('-terminal-reply' if interrupt_terminal else '-held')
    lab.checkpoint(receiver['profile']+'-'+name)
    quiet(lab,payer,receiver)
    before = [channel(lab,n)['to_us_msat'] for n in (payer,receiver)]
    quote, invoice, preimage = make_quote(lab,receiver)
    payment_hash = quote['payment_hash']
    send_once(lab,payer,receiver,invoice,payment_hash)
    first = held(lab,receiver,payment_hash)
    expected_pin = hg.channel_pin(channel(lab,receiver),receiver['profile'])
    require(first['binding']['channel'] == expected_pin, 'wrong_actual_incoming_channel')
    original_attempt = attempt(lab,payer,payment_hash)
    require(original_attempt['status'] == 'pending','payment_not_held')
    resolution_proc = None
    if interrupt_terminal:
        resolution_proc = pause_resolution(lab,receiver,quote,first['binding'],preimage,releasing)
        require(attempt(lab,payer,payment_hash)['status'] == 'pending','hook_replied_before_restart')
    payer,receiver = restart_pair(lab,payer,receiver)
    if resolution_proc is not None:
        resolution_proc.wait(timeout=15)
        require(resolution_proc.returncode != 0,'interrupted_resolution_unexpectedly_succeeded')
    else:
        second = held(lab,receiver,payment_hash)
        require(second['binding'] == first['binding'] and second['cltv_expiry'] == first['cltv_expiry'],
                'held_binding_changed_after_restart')
        require(attempt(lab,payer,payment_hash)['status'] == 'pending','pending_attempt_did_not_survive')
    require(attempt_identity(attempt(lab,payer,payment_hash)) == attempt_identity(original_attempt),
            'outgoing_attempt_changed_after_restart')
    method = 'swap-gate-release' if releasing else 'swap-gate-fail'
    params = dict(payment_hash=payment_hash,binding=first['binding'])
    if releasing:
        params['preimage'] = preimage
    expected_phase = 'release_intent' if releasing else 'fail_intent'
    for _ in range(2):
        require(lab.rpc_named(receiver['cli'],method,params) == {'recorded':expected_phase},
                'terminal_intent_retry_changed')
    expected_status = 'complete' if releasing else 'failed'
    lab.wait(lambda:attempt(lab,payer,payment_hash)['status'] == expected_status,payer['proc'])
    finished = attempt(lab,payer,payment_hash)
    require(attempt_identity(finished) == attempt_identity(original_attempt), 'duplicate_payment_attempt')
    if releasing:
        complete = lab.rpc_named(payer['cli'],'waitsendpay',dict(payment_hash=payment_hash,
                                partid=0,groupid=1,timeout=30),timeout=45)
        require(complete['status'] == 'complete' and complete['payment_preimage'] == preimage,
                'settlement_preimage_mismatch')
    else:
        try:
            lab.rpc_named(payer['cli'],'waitsendpay',dict(payment_hash=payment_hash,
                          partid=0,groupid=1,timeout=30),timeout=45)
        except FixtureRPCError as error:
            require(error.code == 204,'unexpected_payment_failure_code')
        else:
            raise RuntimeError('failed_payment_returned_success')
    quiet(lab,payer,receiver)
    after = [channel(lab,n)['to_us_msat'] for n in (payer,receiver)]
    delta = AMOUNT if releasing else 0
    require(after == [before[0]-delta,before[1]+delta], 'channel_balance_delta_mismatch')
    final = status(lab,receiver,payment_hash)
    require(final['phase'] == expected_phase and final['binding'] == first['binding']
            and not final['hook_ready'] and not final['needs_attention'], 'final_gate_state_invalid')
    # Gate invoices deliberately are not core invoice records. Actual transfer
    # proof is sender preimage/attempt + both settled channel balance changes.
    receipt = dict(profile=receiver['profile'],scenario=name,one_attempt=True,
                   exact_incoming_binding=True,settled_balance_delta_msat=delta,
                   sender_status=expected_status,terminal_phase=expected_phase)
    print('PASS: '+receiver['profile']+' '+name+'; same funding/HTLC and payment attempt, exact balances',flush=True)
    return payer,receiver,receipt


def startup_refusals(lab, receiver):
    lab.checkpoint(receiver['profile']+'-journal-startup-refusals')
    quiet(lab,receiver)
    lab.stop(receiver['proc'])
    path = private_dir(receiver)/'state.json'
    original = path.read_bytes()
    wrong = json.loads(original)
    wrong['chain']['profile'] = ci.BTC if receiver['profile'] == ci.PRIVKEYIO_XBT else ci.PRIVKEYIO_XBT
    try:
        for label, value in [('legacy',{'schema':1,'quotes':{}}),('wrong-profile',wrong),('missing',None)]:
            if value is None:
                path.unlink()
            else:
                path.write_text(json.dumps(value))
                path.chmod(0o600)
            log = receiver['root']/('refusal-'+label+'.log')
            proc = lab.start(lab.commands[receiver['name']], log)
            proc.wait(timeout=45)
            content = log.read_text()
            require(proc.returncode != 0 and 'important' in content
                    and 'swap gate startup or framing refused' in content, 'unsafe_journal_startup_accepted')
    finally:
        path.write_bytes(original)
        path.chmod(0o600)
    print('PASS: '+receiver['profile']+' refuses legacy, wrong-profile and missing gate journals at startup',flush=True)


def run(lab):
    receipts = []
    for label, profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]:
        lab.checkpoint(label+'-fresh-chain-and-channel')
        backend = lab.backend(label+'-chain',profile == ci.PRIVKEYIO_XBT)
        payer = lab.lightning(label+'-payer',profile,backend)
        receiver = lab.lightning(label+'-receiver',profile,backend)
        lab.rpc(backend['cli'],'sendtoaddress',funding_address(lab,payer),'0.01')
        lab.mine(backend)
        lab.synced(backend,payer,receiver)
        lab.wait(lambda:any(o['status'] == 'confirmed' for o in lab.rpc(payer['cli'],'listfunds')['outputs']),payer['proc'])
        lab.rpc(payer['cli'],'connect',receiver['identity']['node_id'],'127.0.0.1',receiver['port'])
        lab.rpc(payer['cli'],'fundchannel',receiver['identity']['node_id'],300000,timeout=90)
        lab.mine(backend)
        lab.synced(backend,payer,receiver)
        channel(lab,payer)
        channel(lab,receiver)
        lab.stop(receiver['proc'])
        hg.initialize_fixture(private_dir(receiver),receiver['identity'])
        lab.gates[receiver['name']] = profile
        receiver = lab.lightning(receiver['name'],profile,backend)
        connect(lab,payer,receiver)
        ordinary_payment(lab,payer,receiver,'ordinary-before')
        for interrupt_terminal in (False,True):
            for releasing in (True,False):
                payer,receiver,receipt = scenario(lab,payer,receiver,releasing=releasing,
                                                 interrupt_terminal=interrupt_terminal)
                receipts.append(receipt)
        ordinary_payment(lab,payer,receiver,'ordinary-after')
        startup_refusals(lab,receiver)
        lab.stop(payer['proc'])
        lab.stop(backend['proc'])
    result = dict(schema=1,fixture='privkeyio-bound-gate-0068a',regtest_only=True,
                  scenarios=receipts,ordinary_payments_before_after=True,
                  legacy_wrong_profile_missing_journals_refused=True,
                  deprecated_apis_enabled=False,automatic_payment_retries=False,
                  full_swap_controller_tested=False,plugin_only_crash_safe=False,
                  production_activation_supported=False)
    (lab.root/'gate-result.json').write_text(json.dumps(result,indent=2)+'\n')
    lab.checkpoint('complete')
    print('GATE FIXTURE PASSED: 0068a; BTC/XBT held and terminal-intent restarts; regtest only; no installer',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for arg in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):
        parser.add_argument('--'+arg,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()))
    args = parser.parse_args()
    os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):
        setattr(args,name,getattr(args,name).resolve())
    for p in (args.btc_prefix/'bin/lightningd',args.xbt_prefix/'bin/lightningd',
              args.bitcoind,args.bitcoin_cli,PLUGIN):
        require(p.is_file() and os.access(p,os.X_OK),'fixture_executable_missing')
    lab = GateLab(args)
    try:
        run(lab)
    finally:
        lab.close()


if __name__ == '__main__':
    main()
