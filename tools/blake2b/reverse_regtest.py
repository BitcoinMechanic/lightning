#!/usr/bin/env python3
"""XBT -> BTC direct Lightning swap fixture. Disposable regtest coins only.

Basic modes use a nondurable holding hook. New gate modes test durable quote
replay across orderly operator restarts and XBT resolution recovery. No routed
payment, oracle price or live entry point is provided here.
The fixed 200k XBT / 100k BTC sat amounts have no market significance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from smoke_regtest import Lab, wait_until
from swap_invoice import unsigned_invoice


def run(lab, fail_outgoing=False, recovery=None):
    fail_outgoing = fail_outgoing or recovery in ('pending-failure', 'gate-restart-failure', 'gate-failure')
    durable = recovery is not None and recovery.startswith('gate-')
    btc = lab.node('knots-btc', False)
    xbt = lab.node('knots-xbt', True)
    plugin = lab.root/'hold_htlc.py'
    plugin.write_text(f'#!{sys.executable}\n' + Path(__file__).with_name('hold_htlc.py').read_text())
    plugin.chmod(0o700)
    gate_plugin = None
    if durable:
        gate_plugin = lab.root/'reverse_gate.py'
        gate_plugin.write_text(f'#!{sys.executable}\n' + Path(__file__).with_name('reverse_gate.py').read_text())
        gate_plugin.chmod(0o700)
        (lab.root/'quote_plugin.py').write_text(Path(__file__).with_name('quote_plugin.py').read_text())
    payer = lab.lightning('xbt-payer', 'xbt-regtest', xbt)
    incoming = lab.lightning('xbt-operator', 'xbt-regtest', xbt, plugins=(gate_plugin or plugin,))
    outgoing = lab.lightning('btc-operator', 'regtest', btc)
    receiver = lab.lightning('btc-receiver', 'regtest', btc,
                             plugins=(plugin,) if fail_outgoing or recovery in ('pending', 'gate-restart', 'gate-onchain', 'gate-deadline', 'gate-btc-claim', 'gate-btc-timeout') else ())
    nodes = (payer, incoming, outgoing, receiver)

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('fixture requires exactly one channel per node')
        return channels[0]

    def mine(backend, peers, count):
        rpc(backend, 'generatetoaddress', count, rpc(backend, 'getnewaddress'))
        height = rpc(backend, 'getblockcount')
        for node in peers:
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height,
                       node['proc'], timeout=90)

    def fund(backend, sender, recipient):
        mine(backend, (sender, recipient), 1)
        address = rpc(sender, 'newaddr', 'bech32')['bech32']
        deposit = rpc(backend, 'sendtoaddress', address, '0.02')
        mine(backend, (sender, recipient), 1)
        wait_until(lambda: any(o['txid'] == deposit and o['status'] == 'confirmed'
                              for o in rpc(sender, 'listfunds')['outputs']), sender['proc'])
        rpc(sender, 'connect', recipient['id'], '127.0.0.1', recipient['port'])
        funding = rpc(sender, 'fundchannel', recipient['id'], '1000000sat')
        wait_until(lambda: funding['txid'] in rpc(backend, 'getrawmempool'))
        mine(backend, (sender, recipient), 6)
        for node in (sender, recipient):
            wait_until(lambda: channel(node)['state'] == 'CHANNELD_NORMAL', node['proc'])

    fund(xbt, payer, incoming)
    fund(btc, outgoing, receiver)
    print('PASS: funded XBT payer -> operator and BTC operator -> receiver channels', flush=True)
    initial = {n['id']: channel(n)['to_us_msat'] for n in nodes}
    receiver_preimage = None
    if recovery == 'gate-btc-claim':
        # Harness-only secret to trigger a receiver on-chain success. It is
        # never passed to the controller or put in its pending checkpoint.
        receiver_preimage = secrets.token_hex(32)
        invoice = lab.rpc([*receiver['cli'], '-k'], 'invoice', 'amount_msat=100000000msat',
                          'label=reverse-receive', 'description=XBT to BTC regtest',
                          'preimage='+receiver_preimage)
    else:
        invoice = rpc(receiver, 'invoice', '100000000msat', 'reverse-receive', 'XBT to BTC regtest')
    payment_hash = invoice['payment_hash']
    # The operator receives only the BTC invoice; no receiver preimage is used.
    decoded = rpc(outgoing, 'decode', invoice['bolt11'])
    expected = dict(valid=True, type='bolt11 invoice', currency='bcrt',
                    payee=receiver['id'], payment_hash=payment_hash, amount_msat=100000000)
    if (any(decoded.get(k) != v for k, v in expected.items())
            or not decoded.get('payment_secret') or decoded['min_final_cltv_expiry'] > 40):
        raise AssertionError('BTC invoice does not match the reverse fixture')
    secret = secrets.token_hex(32)
    quote = None
    if durable:
        quote = dict(payment_hash=payment_hash, payment_secret=secret,
                     xbt_amount_msat=200000000, btc_amount_msat=100000000,
                     btc_invoice=invoice['bolt11'], xbt_channel=channel(incoming)['short_channel_id'],
                     expires_at=int(time.time())+3600, min_cltv_delta=100, max_cltv_delta=2000)
        if rpc(incoming, 'reverse-register', json.dumps(quote)) != {'registered': True}:
            raise AssertionError('reverse quote registration failed')
    unsigned = unsigned_invoice(payment_hash, secret, amount_msat=200000000,
                                currency='xbtrt', final_cltv=120)
    xbt_invoice = rpc(incoming, 'signinvoice', unsigned)['bolt11']
    signed = rpc(payer, 'decode', xbt_invoice)
    expected = dict(valid=True, currency='xbtrt', payee=incoming['id'],
                    payment_hash=payment_hash, payment_secret=secret,
                    amount_msat=200000000, min_final_cltv_expiry=120)
    if not xbt_invoice.startswith('lnxbtrt') or any(signed.get(k) != v for k, v in expected.items()):
        raise AssertionError('signed XBT invoice fields do not match')
    pay_log = lab.root/'xbt-pay.log'
    paying = lab.start([*payer['cli'], 'pay', xbt_invoice], pay_log)

    def committed(node, state):
        return next((h for h in channel(node).get('htlcs', [])
                     if h['payment_hash'] == payment_hash and h['state'] == state), None)

    def held():
        if paying.poll() is not None:
            raise AssertionError('XBT pay exited before the incoming HTLC was held')
        return any(h['payment_hash'] == payment_hash
                   for h in rpc(incoming, 'xbt-held')['held'])

    wait_until(held, incoming['proc'])
    htlc = wait_until(lambda: committed(payer, 'SENT_ADD_ACK_REVOCATION'), payer['proc'])
    wait_until(lambda: committed(incoming, 'RCVD_ADD_ACK_REVOCATION'), incoming['proc'])
    # Compare remaining blocks only in this controlled fixture. Independent
    # live chains cannot be made safe by subtracting their absolute heights.
    if htlc['amount_msat'] != 200000000 or htlc['expiry'] - rpc(xbt, 'getblockcount') < 100:
        raise AssertionError('incoming XBT amount or test timelock margin differs')

    def attempts(node):
        return [p for p in rpc(node, 'listsendpays')['payments'] if p['payment_hash'] == payment_hash]

    if len(attempts(payer)) != 1 or attempts(payer)[0]['status'] != 'pending' or attempts(outgoing):
        raise AssertionError('unexpected payment attempt before outgoing submission')
    print('PASS: ordinary XBT pay held 200,000 sats under BTC invoice hash; test margin verified', flush=True)
    if durable:
        status = rpc(incoming, 'reverse-status', payment_hash)
        if status['terms'] != quote or status['phase'] != 'held' or not status['hook_ready']:
            raise AssertionError('durable reverse quote not accepted')
        print('PASS: reverse gate durably bound validated XBT HTLC before BTC spending', flush=True)
    route = [dict(id=decoded['payee'], channel=channel(outgoing)['short_channel_id'],
                  amount_msat=decoded['amount_msat'], delay=40)]
    if recovery in ('gate-btc-claim', 'gate-btc-timeout'):
        from reverse_btc_onchain import exercise_btc_onchain
        exercise_btc_onchain(lab, payer, incoming, outgoing, receiver, xbt, btc,
                             invoice, decoded, route, quote, gate_plugin, initial,
                             paying, pay_log, receiver_preimage,
                             timeout=recovery == 'gate-btc-timeout')
        return
    if recovery in ('gate-onchain', 'gate-deadline'):
        from reverse_onchain import exercise_onchain
        exercise_onchain(lab, payer, incoming, outgoing, receiver, xbt, btc,
                         invoice, decoded, xbt_invoice, route, quote, gate_plugin,
                         initial, paying, pay_log, deadline=recovery == 'gate-deadline')
        return
    if recovery:
        from reverse_recovery import exercise
        gate = dict(quote=quote, plugin=gate_plugin, xbt=xbt, btc=btc) if durable else None
        exercise(lab, payer, incoming, outgoing, receiver, invoice, decoded, route, recovery, gate)
    else:
        lab.rpc([*outgoing['cli'], '-k'], 'sendpay', 'route='+json.dumps(route),
                'payment_hash='+payment_hash, 'payment_secret='+decoded['payment_secret'],
                'bolt11='+invoice['bolt11'])

    if fail_outgoing:
        if not recovery:
            wait_until(lambda: any(h['payment_hash'] == payment_hash
                                  for h in rpc(receiver, 'xbt-held')['held']), receiver['proc'])
            wait_until(lambda: committed(outgoing, 'SENT_ADD_ACK_REVOCATION'), outgoing['proc'])
            wait_until(lambda: committed(receiver, 'RCVD_ADD_ACK_REVOCATION'), receiver['proc'])
            if rpc(receiver, 'xbt-fail', payment_hash)['failed'] != 1:
                raise AssertionError('expected exactly one rejected BTC HTLC')

        def terminal_failure(node):
            records = attempts(node)
            if len(records) != 1:
                raise AssertionError('unexpected number of payment attempts')
            if records[0]['status'] == 'complete' or records[0].get('payment_preimage'):
                raise AssertionError('rejected payment unexpectedly completed')
            return records[0]['status'] == 'failed'

        wait_until(lambda: terminal_failure(outgoing), outgoing['proc'])
        if not recovery:
            if attempts(payer)[0]['status'] != 'pending':
                raise AssertionError('XBT did not remain held until definite BTC failure')
            print('PASS: BTC attempt definitively failed without preimage; XBT still held', flush=True)
            if rpc(incoming, 'xbt-fail', payment_hash)['failed'] != 1:
                raise AssertionError('expected exactly one failed incoming XBT HTLC')
        paying.wait(timeout=30)
        if paying.returncode == 0:
            raise AssertionError('XBT payer unexpectedly succeeded')
        wait_until(lambda: terminal_failure(payer), payer['proc'])
        changes = [(n, 0) for n in nodes]
    else:
        result = rpc(outgoing, 'waitsendpay', payment_hash, 10)
        if result['status'] != 'complete' or result['payment_hash'] != payment_hash:
            raise AssertionError('BTC outgoing payment did not complete')
        preimage = result['payment_preimage']
        if hashlib.sha256(bytes.fromhex(preimage)).hexdigest() != payment_hash:
            raise AssertionError('BTC returned a mismatching preimage')
        if not recovery and rpc(incoming, 'xbt-release', preimage)['released'] != 1:
            raise AssertionError('expected exactly one XBT HTLC settlement')
        paying.wait(timeout=30)
        if paying.returncode:
            raise AssertionError('XBT payer failed after BTC completion')
        paid = json.loads(pay_log.read_text())
        if (paid['status'] != 'complete' or paid['payment_preimage'] != preimage
                or paid['amount_msat'] != 200000000 or paid['amount_sent_msat'] != 200000000):
            raise AssertionError('XBT payer result differs from agreed settlement')
        records = attempts(outgoing)
        if len(records) != 1 or records[0]['status'] != 'complete':
            raise AssertionError('expected exactly one completed BTC attempt')
        changes = [(payer, -200000000), (incoming, 200000000),
                   (outgoing, -100000000), (receiver, 100000000)]
        print('PASS: BTC receiver paid 100,000 sats; same learned preimage settles XBT', flush=True)

    for node, delta in changes:
        def settled():
            c = channel(node)
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[node['id']] + delta)
        wait_until(settled, node['proc'])
    received = rpc(receiver, 'listinvoices', 'reverse-receive')['invoices'][0]
    if fail_outgoing:
        if received['status'] != 'unpaid':
            raise AssertionError('rejected BTC invoice unexpectedly paid')
        print('PASS: XBT payment failed; no pending HTLCs; all four balances restored', flush=True)
    elif received['status'] != 'paid' or received['amount_received_msat'] != 100000000:
        raise AssertionError('BTC receiver did not receive agreed amount')
    else:
        print('PASS: no pending HTLCs; all four balances match agreed amounts', flush=True)
    mode = 'controller recovery: '+recovery if recovery else 'definite BTC rejection' if fail_outgoing else 'happy path'
    print(f'XBT -> BTC reverse swap test OK ({mode}; direct channels; regtest only)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', required=True, type=Path)
    parser.add_argument('--bitcoin-cli', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fail-outgoing', action='store_true')
    mode.add_argument('--crash-after-btc', dest='recovery', action='store_const', const='after-btc')
    mode.add_argument('--crash-while-pending', dest='recovery', action='store_const', const='pending')
    mode.add_argument('--pending-failure', dest='recovery', action='store_const', const='pending-failure')
    mode.add_argument('--pending-restart', dest='recovery', action='store_const', const='gate-restart')
    mode.add_argument('--pending-restart-failure', dest='recovery', action='store_const', const='gate-restart-failure')
    mode.add_argument('--release-recovery', dest='recovery', action='store_const', const='gate-release')
    mode.add_argument('--failure-release-recovery', dest='recovery', action='store_const', const='gate-failure')
    mode.add_argument('--onchain-claim', dest='recovery', action='store_const', const='gate-onchain')
    mode.add_argument('--xbt-deadline', dest='recovery', action='store_const', const='gate-deadline')
    mode.add_argument('--btc-onchain-preimage', dest='recovery', action='store_const', const='gate-btc-claim')
    mode.add_argument('--btc-onchain-timeout', dest='recovery', action='store_const', const='gate-btc-timeout')
    parser.add_argument('--work-dir', type=Path)
    args = parser.parse_args()
    temporary = None
    if args.work_dir:
        root = args.work_dir.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        temporary = tempfile.TemporaryDirectory(prefix='cln-reverse-')
        root = Path(temporary.name)
    lab = Lab(root, str(args.bitcoind.resolve()), str(args.bitcoin_cli.resolve()))
    print(f'Test directory: {root}', flush=True)
    try:
        run(lab, args.fail_outgoing, args.recovery)
    finally:
        lab.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
