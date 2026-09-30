#!/usr/bin/env python3
"""BTC -> XBT Lightning swap happy-path prototype. Disposable regtest only.

Uses direct sendpay HTLCs by default; --invoice exercises a signed BTC invoice.
This is not a production swap service.
The fixture rate (100k BTC sats for 200k XBT sats) has no market significance.
Optional receiver rejection tests a definite failed-outgoing refund.
Optional controller crash test uses a durable checkpoint while CLN stays up.
Optional operator restart test reloads the BTC holding hook at startup.
No general ambiguous-outcome, power-loss or chain-stall policy.
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


def run(lab, fail_outgoing=False, crash_after_xbt=False, restart_operators=False,
        pay_invoice=False, quoted_invoice=False, reject_quotes=False, crash_after_btc=False,
        crash_while_pending=False, pending_failure=False, restart_pending=False,
        kill_pending=False, stale_timelock=False, concurrent=False, outgoing_binding=False,
        onchain_preimage=False, onchain_timeout=False, btc_deadline=False, watch_deadline=False, service_demo=False, service_lab=False):
    restart_pending = restart_pending or kill_pending
    crash_while_pending = crash_while_pending or pending_failure or restart_pending
    quoted_invoice = quoted_invoice or crash_after_btc or crash_while_pending or stale_timelock or concurrent
    quoted_invoice = quoted_invoice or outgoing_binding or onchain_preimage or onchain_timeout
    btc_deadline = btc_deadline or watch_deadline
    quoted_invoice = quoted_invoice or btc_deadline or service_demo or service_lab
    pay_invoice = pay_invoice or quoted_invoice
    btc = lab.node('knots-btc', False)
    xbt = lab.node('knots-xbt', True)
    plugin_name = 'quote_plugin.py' if quoted_invoice or reject_quotes else 'hold_htlc.py'
    plugin = lab.root / plugin_name
    plugin.write_text(f'#!{sys.executable}\n' +
                      Path(__file__).with_name(plugin_name).read_text())
    plugin.chmod(0o700)
    payer = lab.lightning('payer', 'regtest', btc)
    swap_btc = lab.lightning('swap-btc', 'regtest', btc, plugins=(plugin,))
    swap_xbt = lab.lightning('swap-xbt', 'xbt-regtest', xbt)
    receiver = lab.lightning('receiver', 'xbt-regtest', xbt)

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError(channels)
        return channels[0]

    def mine(backend, nodes, count):
        rpc(backend, 'generatetoaddress', count, rpc(backend, 'getnewaddress'))
        height = rpc(backend, 'getblockcount')
        for node in nodes:
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height,
                       node['proc'], timeout=90)

    def open_channel(backend, sender, recipient):
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

    open_channel(btc, payer, swap_btc)
    open_channel(xbt, swap_xbt, receiver)
    print('PASS: funded BTC payer -> operator and XBT operator -> receiver channels', flush=True)
    initial = {node['id']: channel(node)['to_us_msat']
               for node in (payer, swap_btc, swap_xbt, receiver)}

    if service_lab:
        from service_demo import interactive
        interactive(lab, payer, swap_btc, swap_xbt, receiver, btc, xbt)
        return
    if service_demo:
        from service_demo import demo
        demo(lab, payer, swap_btc, swap_xbt, receiver, initial)
        return

    if reject_quotes:
        from quote_rejections import run_rejections
        run_rejections(lab, payer, swap_btc, swap_xbt, receiver, plugin, initial)
        return

    if fail_outgoing:
        rpc(receiver, 'plugin', 'start', plugin)
    if crash_while_pending or concurrent or onchain_preimage or onchain_timeout or btc_deadline:
        hold = lab.root / 'hold_htlc.py'
        hold.write_text(f'#!{sys.executable}\n' +
                        Path(__file__).with_name('hold_htlc.py').read_text())
        hold.chmod(0o700)
        rpc(receiver, 'plugin', 'start', hold)
    receiver_preimage = None
    if onchain_preimage:
        # This fixture chooses a receiver secret solely to drive its on-chain
        # claim, as in funded_regtest --preimage-claim. It is never passed to
        # the swap controller or used to release BTC directly.
        receiver_preimage = secrets.token_hex(32)
        invoice = lab.rpc([*receiver['cli'], '-k'], 'invoice', 'amount_msat=200000000',
                          'label=swap-receive', 'description=XBT swap onchain claim',
                          'preimage=' + receiver_preimage)
    else:
        # Ordinary scenarios leave preimage generation entirely to the receiver.
        invoice = rpc(receiver, 'invoice', '200000000msat', 'swap-receive', 'BTC to XBT test')
    payment_hash = invoice['payment_hash']
    if not invoice['bolt11'].startswith('lnxbtrt'):
        raise AssertionError('receiver did not issue an XBT invoice')

    def send(sender, recipient, amount, delay, secret):
        route = [{'id': recipient['id'], 'channel': channel(sender)['short_channel_id'],
                  'amount_msat': amount, 'delay': delay}]
        return lab.rpc([*sender['cli'], '-k'], 'sendpay',
                       'route=' + json.dumps(route), 'payment_hash=' + payment_hash,
                       'payment_secret=' + secret)

    def committed(node, state):
        return next((h for h in channel(node).get('htlcs', [])
                     if h['payment_hash'] == payment_hash and h['state'] == state), None)

    pay_process = None
    if pay_invoice:
        from swap_invoice import unsigned_invoice
        btc_secret = secrets.token_hex(32)
        if quoted_invoice:
            quote = {'payment_hash': payment_hash, 'payment_secret': btc_secret,
                     'btc_amount_msat': 100000000, 'xbt_amount_msat': 200000000,
                     'xbt_invoice': invoice['bolt11'], 'expires_at': int(time.time()) + 3600,
                     'min_cltv_delta': 100, 'max_cltv_delta': 2000}
            if not rpc(swap_btc, 'xbt-register', json.dumps(quote))['registered']:
                raise AssertionError('quote not registered')
        unsigned = unsigned_invoice(payment_hash, btc_secret)
        btc_invoice = rpc(swap_btc, 'signinvoice', unsigned)['bolt11']
        decoded = rpc(payer, 'decode', btc_invoice)
        if (not decoded['valid'] or decoded['currency'] != 'bcrt'
                or decoded['payee'] != swap_btc['id']
                or decoded['payment_hash'] != payment_hash
                or decoded['payment_secret'] != btc_secret
                or decoded['amount_msat'] != 100000000
                or decoded['min_final_cltv_expiry'] != 120):
            raise AssertionError('signed BTC invoice fields do not match swap terms')
        pay_log = lab.root / 'payer-pay.log'
        pay_process = lab.start([*payer['cli'], 'pay', btc_invoice], pay_log)
        print('PASS: signed BTC invoice verified; payer started ordinary pay command', flush=True)
    else:
        send(payer, swap_btc, 100000000, 120, secrets.token_hex(32))
    def incoming_held():
        if pay_process is not None and pay_process.poll() is not None:
            raise RuntimeError('BTC pay exited before quote acceptance:\n' +
                               pay_log.read_text(errors='replace'))
        return any(h['payment_hash'] == payment_hash
                   for h in rpc(swap_btc, 'xbt-held')['held'])

    wait_until(incoming_held, swap_btc['proc'])
    incoming = wait_until(lambda: committed(payer, 'SENT_ADD_ACK_REVOCATION'), payer['proc'])
    wait_until(lambda: committed(swap_btc, 'RCVD_ADD_ACK_REVOCATION'), swap_btc['proc'])
    if quoted_invoice:
        stored = json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash]
        if stored['terms'] != quote or stored['phase'] != 'held':
            raise AssertionError('accepted HTLC does not have persisted matching quote')
        print('PASS: quote gate validated and durably bound incoming BTC HTLC before XBT spending', flush=True)
    if incoming['amount_msat'] != 100000000:
        raise AssertionError('unexpected incoming amount')
    # Heights belong to different chains. Compare remaining blocks only in
    # this controlled fixture, where both chains advance solely when we mine.
    remaining = incoming['expiry'] - rpc(btc, 'getblockcount')
    if remaining < 100:
        raise AssertionError('insufficient test incoming timelock margin')
    pending = [p for p in rpc(payer, 'listsendpays')['payments']
               if p['payment_hash'] == payment_hash]
    if len(pending) != 1 or pending[0]['status'] != 'pending':
        raise AssertionError('BTC payment did not remain held')
    if rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]['status'] != 'unpaid':
        raise AssertionError('XBT invoice already paid before swap')
    print('PASS: 100,000 BTC sats held under XBT invoice hash; incoming expiry has test margin', flush=True)

    recovered_preimage = None
    if btc_deadline:
        from btc_deadline import run_deadline
        run_deadline(lab, payer, swap_btc, swap_xbt, receiver, btc, xbt, invoice,
                     btc_invoice, stored['binding'], plugin, initial, pay_process, pay_log,
                     watch=watch_deadline)
        return
    if onchain_preimage or onchain_timeout:
        from onchain_swap import run_onchain
        run_onchain(lab, payer, swap_btc, swap_xbt, receiver, btc, xbt, invoice,
                    receiver_preimage, stored['binding'], plugin, initial, pay_process, pay_log,
                    timeout=onchain_timeout)
        return
    if stale_timelock:
        from stale_timelock import run_stale
        run_stale(lab, payer, swap_btc, swap_xbt, receiver, btc, invoice,
                  stored['binding'], quote, mine, initial, pay_process)
        return
    if outgoing_binding:
        from outgoing_binding import run_binding
        recovered_preimage = run_binding(lab, payer, swap_btc, swap_xbt, receiver,
                                         invoice, stored['binding'], plugin)
    elif concurrent:
        from concurrent_swap import run_concurrent
        recovered_preimage = run_concurrent(lab, payer, swap_btc, swap_xbt, receiver,
                                            invoice, stored['binding'], plugin)
    elif crash_while_pending:
        from pending_recovery import run_recovery
        recovered_preimage = run_recovery(lab, payer, swap_btc, swap_xbt, receiver,
                                         invoice, stored['binding'], plugin,
                                         pending_failure, initial,
                                         (btc, xbt) if restart_pending else None, kill_pending)
        if pending_failure:
            pay_process.wait(timeout=30)
            if pay_process.returncode == 0:
                raise AssertionError('BTC pay unexpectedly succeeded after XBT rejection')
            detail = 'controller crashes and orderly operator restarts' if restart_pending else 'controller crashes'
            if kill_pending:
                detail = 'controller crashes and operator SIGKILL/restarts'
            print(f'BTC quoted-invoice pending failure recovery test OK ({detail}; regtest only)',
                  flush=True)
            return
    elif crash_after_btc:
        from release_recovery import run_recovery
        recovered_preimage = run_recovery(lab, payer, swap_btc, swap_xbt, receiver,
                                         invoice, stored['binding'])
    elif crash_after_xbt or restart_operators:
        from swap_controller import save
        statefile = lab.root / 'swap-state.json'
        save(statefile, {'phase': 'prepared', 'payment_hash': payment_hash,
                         'payment_secret': invoice['payment_secret'],
                         'xbt_invoice': invoice['bolt11'],
                         'xbt_amount_msat': 200000000,
                         'quote_gate': quoted_invoice,
                         'btc_binding': stored['binding'] if quoted_invoice else None,
                         'xbt_cli': swap_xbt['cli'], 'btc_cli': swap_btc['cli'],
                         'route': [{'id': receiver['id'],
                                    'channel': channel(swap_xbt)['short_channel_id'],
                                    'amount_msat': 200000000, 'delay': 40}]})
        command = [sys.executable, str(Path(__file__).with_name('swap_controller.py')),
                   '--state', str(statefile)]
        crashed = subprocess.run([*command, '--crash-after-xbt'], text=True,
                                 capture_output=True, timeout=60)
        if crashed.returncode != 86:
            raise AssertionError(f'controller did not reach crash point: {crashed.stdout}\n{crashed.stderr}')
        saved = json.loads(statefile.read_text())
        if saved['phase'] != 'outgoing_started' or 'preimage' in saved:
            raise AssertionError('crash did not precede outgoing completion checkpoint')
        pending = [p for p in rpc(payer, 'listsendpays')['payments']
                   if p['payment_hash'] == payment_hash]
        if len(pending) != 1 or pending[0]['status'] != 'pending':
            raise AssertionError('BTC did not remain pending across controller crash')
        if rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]['status'] != 'paid':
            raise AssertionError('XBT invoice not paid at crash point')
        print('PASS: controller crashed after XBT settlement; BTC still held; disk lacks preimage', flush=True)
        if restart_operators:
            old_btc_id, old_xbt_id = swap_btc['id'], swap_xbt['id']
            if quoted_invoice:
                quote_before_restart = json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash]
                if (quote_before_restart['terms'] != quote
                        or quote_before_restart['phase'] != 'held'
                        or 'preimage' in quote_before_restart):
                    raise AssertionError('quote not held without preimage before restart')
            # Orderly node restarts using their existing wallets/databases.
            # The holding hook must be loaded before CLN replays pending HTLCs.
            for node in (swap_btc, swap_xbt):
                lab.stop(node['proc'])
                node['log'].rename(node['log'].with_name('before-restart.log'))
            swap_btc = lab.lightning('swap-btc', 'regtest', btc, plugins=(plugin,))
            swap_xbt = lab.lightning('swap-xbt', 'xbt-regtest', xbt)
            if (swap_btc['id'], swap_xbt['id']) != (old_btc_id, old_xbt_id):
                raise AssertionError('operator node identity changed on restart')
            rpc(payer, 'connect', swap_btc['id'], '127.0.0.1', swap_btc['port'])
            rpc(swap_xbt, 'connect', receiver['id'], '127.0.0.1', receiver['port'])
            wait_until(lambda: any(h['payment_hash'] == payment_hash
                                  for h in rpc(swap_btc, 'xbt-held')['held']), swap_btc['proc'])
            wait_until(lambda: committed(swap_btc, 'RCVD_ADD_ACK_REVOCATION'), swap_btc['proc'])
            pending = [p for p in rpc(payer, 'listsendpays')['payments']
                       if p['payment_hash'] == payment_hash]
            if len(pending) != 1 or pending[0]['status'] != 'pending':
                raise AssertionError('BTC payment did not survive operator restart')
            persisted = [p for p in rpc(swap_xbt, 'listsendpays')['payments']
                         if p['payment_hash'] == payment_hash]
            if len(persisted) != 1 or persisted[0]['status'] != 'complete':
                raise AssertionError('XBT completion did not survive operator restart')
            print('PASS: both operator nodes restarted; BTC hook replayed; XBT completion persisted', flush=True)
            if quoted_invoice:
                quote_after_restart = json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash]
                if quote_after_restart != quote_before_restart:
                    raise AssertionError('quote terms or HTLC binding changed on restart')
                print('PASS: persisted quote and original HTLC binding survived replay unchanged', flush=True)
        resumed = subprocess.run(command, text=True, capture_output=True, timeout=60)
        if resumed.returncode:
            raise AssertionError(f'controller recovery failed: {resumed.stdout}\n{resumed.stderr}')
        recovered_preimage = json.loads(resumed.stdout)['payment_preimage']
        attempts = [p for p in rpc(swap_xbt, 'listsendpays')['payments']
                    if p['payment_hash'] == payment_hash]
        if len(attempts) != 1 or attempts[0]['status'] != 'complete':
            raise AssertionError('expected exactly one completed outgoing attempt after recovery')
        if json.loads(statefile.read_text())['phase'] != 'btc_released':
            raise AssertionError('missing recovery checkpoint')
        print('PASS: fresh controller recovered preimage from CLN and released BTC without another XBT attempt', flush=True)
    else:
        if quoted_invoice:
            from swap_controller import check_spend
            reason = check_spend({'btc_cli': swap_btc['cli'], 'payment_hash': payment_hash,
                                  'xbt_cli': swap_xbt['cli'], 'xbt_invoice': invoice['bolt11'],
                                  'payment_secret': invoice['payment_secret'],
                                  'route': [{'id': receiver['id'], 'amount_msat': 200000000, 'delay': 40}],
                                  'btc_binding': stored['binding'], 'xbt_amount_msat': 200000000})
            if reason:
                raise AssertionError('quote refused before direct XBT submission: ' + reason)
        send(swap_xbt, receiver, 200000000, 40, invoice['payment_secret'])
    if fail_outgoing:
        wait_until(lambda: any(h['payment_hash'] == payment_hash
                              for h in rpc(receiver, 'xbt-held')['held']), receiver['proc'])
        wait_until(lambda: committed(swap_xbt, 'SENT_ADD_ACK_REVOCATION'), swap_xbt['proc'])
        wait_until(lambda: committed(receiver, 'RCVD_ADD_ACK_REVOCATION'), receiver['proc'])
        if rpc(receiver, 'xbt-fail', payment_hash)['failed'] != 1:
            raise AssertionError('receiver did not reject exactly one outgoing HTLC')

        def failed_payment(node):
            try:
                rpc(node, 'waitsendpay', payment_hash, 10)
            except subprocess.CalledProcessError:
                # A timeout/transport error alone is not proof of failure.
                # Require a terminal failed record before refunding BTC.
                payments = [p for p in rpc(node, 'listsendpays')['payments']
                            if p['payment_hash'] == payment_hash]
                if (len(payments) != 1 or payments[0]['status'] != 'failed'
                        or payments[0].get('payment_preimage')):
                    raise AssertionError('payment not definitively failed without a preimage')
            else:
                raise AssertionError('rejected payment unexpectedly succeeded')

        failed_payment(swap_xbt)
        print('PASS: receiver rejected XBT HTLC; outgoing payment definitively failed', flush=True)
        if rpc(swap_btc, 'xbt-fail', payment_hash)['failed'] != 1:
            raise AssertionError('operator did not reject exactly one held BTC HTLC')
        failed_payment(payer)
        for node in (payer, swap_btc, swap_xbt, receiver):
            def refunded():
                c = channel(node)
                return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                        and c['to_us_msat'] == initial[node['id']])
            wait_until(refunded, node['proc'])
        if rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]['status'] != 'unpaid':
            raise AssertionError('rejected XBT invoice unexpectedly paid')
        print('PASS: BTC payment failed; no pending HTLCs; all four balances unchanged', flush=True)
        print('BTC -> XBT rejected-payment test OK (definite rejection; regtest coins only)', flush=True)
        return
    outgoing = rpc(swap_xbt, 'waitsendpay', payment_hash, 10)
    if outgoing['status'] != 'complete' or outgoing['payment_hash'] != payment_hash:
        raise AssertionError('XBT payment did not complete')
    preimage = outgoing['payment_preimage']
    if hashlib.sha256(bytes.fromhex(preimage)).hexdigest() != payment_hash:
        raise AssertionError('outgoing payment returned wrong preimage')
    received = rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]
    if received['status'] != 'paid' or received['amount_received_msat'] != 200000000:
        raise AssertionError('receiver did not get agreed XBT amount')
    print('PASS: receiver paid 200,000 XBT sats; operator learns matching preimage', flush=True)

    if recovered_preimage is None:
        if rpc(swap_btc, 'xbt-release', preimage)['released'] != 1:
            raise AssertionError('operator did not resolve exactly one BTC HTLC')
    elif recovered_preimage != preimage:
        raise AssertionError('recovered preimage differs from outgoing payment')
    if pay_process is not None:
        pay_process.wait(timeout=30)
        if pay_process.returncode:
            raise AssertionError('BTC pay failed: ' + pay_log.read_text())
        paid = json.loads(pay_log.read_text())
    else:
        paid = rpc(payer, 'waitsendpay', payment_hash, 10)
    if paid['status'] != 'complete' or paid['payment_preimage'] != preimage:
        raise AssertionError('BTC payment did not settle with same preimage')
    changes = ((payer, -100000000), (swap_btc, 100000000),
               (swap_xbt, -200000000), (receiver, 200000000))
    for node, delta in changes:
        def settled():
            c = channel(node)
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[node['id']] + delta)
        wait_until(settled, node['proc'])
    print('PASS: BTC settles with same preimage; all four balances match agreed test amounts', flush=True)
    if quoted_invoice:
        stored = json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash]
        if stored['phase'] != 'resolved' or stored['preimage'] != preimage:
            raise AssertionError('settlement preimage not persisted by quote gate')
        if stored['terms'] != quote:
            raise AssertionError('quote terms changed during settlement')
        if outgoing_binding:
            print('BTC quoted-invoice outgoing binding test OK (substitutions refused; regtest only)', flush=True)
        elif concurrent:
            print('BTC quoted-invoice concurrent controller test OK (one shared state path; regtest only)', flush=True)
        elif crash_while_pending:
            detail = 'controller crashes and orderly operator restarts' if restart_pending else 'controller crashes'
            if kill_pending:
                detail = 'controller crashes and operator SIGKILL/restarts'
            print(f'BTC quoted-invoice pending recovery test OK ({detail}; regtest only)', flush=True)
        elif crash_after_btc:
            print('BTC quoted-invoice release recovery test OK (controller crash; regtest only)', flush=True)
        elif restart_operators:
            if stored['binding'] != quote_before_restart['binding']:
                raise AssertionError('settlement used a different HTLC binding')
            print('BTC quoted-invoice restart test OK (controller crash and orderly operator restarts; regtest only)', flush=True)
        else:
            print('BTC quoted-invoice -> XBT swap test OK (experimental regtest quote gate)', flush=True)
    elif pay_invoice:
        print('BTC invoice -> XBT Lightning swap test OK (ordinary pay; regtest only)', flush=True)
    elif restart_operators:
        print('BTC -> XBT operator restart test OK (orderly node restarts; regtest only)', flush=True)
    elif crash_after_xbt:
        print('BTC -> XBT controller crash-recovery test OK (CLN nodes stayed running; regtest only)', flush=True)
    else:
        print('BTC -> XBT Lightning swap prototype OK (happy path; regtest coins only)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', required=True, type=Path)
    parser.add_argument('--bitcoin-cli', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fail-outgoing', action='store_true',
                        help='Reject the XBT HTLC and verify the BTC payment is released.')
    mode.add_argument('--crash-after-xbt', action='store_true',
                      help='Crash the controller after XBT success, then recover in a new process.')
    mode.add_argument('--restart-operators', action='store_true',
                      help='Also restart both operator CLN nodes before controller recovery.')
    mode.add_argument('--invoice', action='store_true',
                      help='Pay a signed BTC BOLT11 invoice using the ordinary pay RPC.')
    mode.add_argument('--quoted-invoice', action='store_true',
                      help='Require persisted matching quote terms before accepting the BTC HTLC.')
    mode.add_argument('--quoted-restart', action='store_true',
                      help='Combine quote-validated invoice payment with controller and operator restarts.')
    mode.add_argument('--reject-quotes', action='store_true',
                      help='Reject wrong secret/amount, expired quote and short CLTV without XBT spending.')
    mode.add_argument('--crash-after-btc', action='store_true',
                      help='Recover a quote-backed BTC release whose controller checkpoint was lost.')
    mode.add_argument('--crash-while-pending', action='store_true',
                      help='Reconcile a pending XBT payment across controller restarts, then settle.')
    mode.add_argument('--pending-failure', action='store_true',
                      help='Recover a pending XBT payment that is rejected, then fail the bound BTC HTLC.')
    mode.add_argument('--pending-restart', action='store_true',
                      help='Restart both operators while BTC and XBT are pending, then settle.')
    mode.add_argument('--pending-restart-failure', action='store_true',
                      help='Restart both operators while pending, then reject XBT and recover BTC failure.')
    mode.add_argument('--pending-kill', action='store_true',
                      help='SIGKILL both operator process groups while pending, restart, then settle.')
    mode.add_argument('--pending-kill-failure', action='store_true',
                      help='SIGKILL both operator groups while pending, restart, then reject XBT.')
    mode.add_argument('--stale-timelock', action='store_true',
                      help='Advance BTC after quote acceptance and refuse XBT when the margin is too short.')
    mode.add_argument('--concurrent', action='store_true',
                      help='Start a competing controller while XBT is pending and verify only one attempt.')
    mode.add_argument('--outgoing-binding', action='store_true',
                      help='Refuse substituted XBT invoice/destination, then complete the correct swap.')
    mode.add_argument('--onchain-preimage', action='store_true',
                      help='Claim XBT on-chain after receiver force-close, then recover BTC settlement.')
    mode.add_argument('--onchain-timeout', action='store_true',
                      help='Recover XBT via on-chain timeout, then fail BTC after definitive outgoing failure.')
    mode.add_argument('--btc-deadline', action='store_true',
                      help='Advance BTC with XBT pending, force-close BTC, then claim with the XBT preimage.')
    mode.add_argument('--watch-deadline', action='store_true',
                      help='Restart a pending swap watcher, then let it close BTC and recover settlement.')
    mode.add_argument('--service-demo', action='store_true', help='Exercise quote/run/status service commands.')
    mode.add_argument('--service-lab', action='store_true', help='Keep funded regtest nodes running for interactive service use.')
    parser.add_argument('--work-dir', type=Path, help='New short directory to retain data/logs.')
    args = parser.parse_args()
    temporary = None
    if args.work_dir:
        root = args.work_dir.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        temporary = tempfile.TemporaryDirectory(prefix='cln-swap-')
        root = Path(temporary.name)
    lab = Lab(root, str(args.bitcoind.resolve()), str(args.bitcoin_cli.resolve()))
    print(f'Test directory: {root}', flush=True)
    try:
        run(lab, args.fail_outgoing, args.crash_after_xbt,
            args.restart_operators or args.quoted_restart,
            args.invoice, args.quoted_invoice or args.quoted_restart, args.reject_quotes,
            args.crash_after_btc, args.crash_while_pending,
            args.pending_failure or args.pending_restart_failure or args.pending_kill_failure,
            args.pending_restart or args.pending_restart_failure,
            args.pending_kill or args.pending_kill_failure, args.stale_timelock, args.concurrent,
            args.outgoing_binding, args.onchain_preimage, args.onchain_timeout, args.btc_deadline, args.watch_deadline,
            args.service_demo, args.service_lab)
    except Exception as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print(f'RPC stdout: {exc.stdout}\nRPC stderr: {exc.stderr}', flush=True)
        for log in root.glob('*/console.log'):
            print(f'\n--- {log.parent.name}: last log lines ---', flush=True)
            print('\n'.join(log.read_text(errors='replace').splitlines()[-30:]), flush=True)
        raise
    finally:
        lab.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
