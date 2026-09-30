"""Crash after XBT submission; reconcile pending twice, then allow settlement.

Optionally restart both operators while pending. Payer and receiver stay up.
Optional definite rejection tests failure recovery.
No timeout-based refund policy. Invoke the controller again for each outcome.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from smoke_regtest import wait_until
from swap_controller import save


def kill_operators(nodes):
    """SIGKILL only the dedicated process groups created by this test lab."""
    processes = [node['proc'] for node in nodes]
    # Validate every target before sending signals. Never kill the harness's
    # group or fall back to a name-based process search.
    for proc in processes:
        if proc.poll() is not None or os.getpgid(proc.pid) != proc.pid:
            raise RuntimeError('operator is not a live dedicated process-group leader')
    for proc in processes:
        os.killpg(proc.pid, signal.SIGKILL)
    for proc in processes:
        if proc.wait(timeout=10) != -signal.SIGKILL:
            raise AssertionError('operator did not exit from SIGKILL')


def run_recovery(lab, payer, swap_btc, swap_xbt, receiver, invoice, binding, plugin,
                 fail_outgoing=False, initial=None, restart_backends=None, abrupt=False):
    if abrupt and restart_backends is None:
        raise ValueError('abrupt termination requires restart backends')
    payment_hash = invoice['payment_hash']

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected exactly one channel')
        return channels[0]

    def payment(node):
        payments = [p for p in rpc(node, 'listsendpays')['payments']
                    if p['payment_hash'] == payment_hash]
        if len(payments) != 1:
            raise AssertionError('expected exactly one payment attempt')
        return payments[0]

    statefile = lab.root / 'swap-state.json'
    save(statefile, {'phase': 'prepared', 'payment_hash': payment_hash,
                     'payment_secret': invoice['payment_secret'], 'quote_gate': True,
                     'xbt_invoice': invoice['bolt11'],
                     'btc_binding': binding, 'xbt_amount_msat': 200000000,
                     'xbt_cli': swap_xbt['cli'], 'btc_cli': swap_btc['cli'],
                     'route': [{'id': receiver['id'],
                                'channel': channel(swap_xbt)['short_channel_id'],
                                'amount_msat': 200000000, 'delay': 40}]})
    command = [sys.executable, str(Path(__file__).with_name('swap_controller.py')),
               '--state', str(statefile)]
    crashed = subprocess.run([*command, '--crash-after-sendpay'], text=True,
                             capture_output=True, timeout=60)
    if crashed.returncode != 88:
        raise AssertionError(f'controller missed submission crash: {crashed.stdout}\n{crashed.stderr}')
    before = statefile.read_bytes()
    saved = json.loads(before)
    if saved['phase'] != 'outgoing_started' or 'preimage' in saved:
        raise AssertionError('unexpected controller checkpoint at submission crash')

    def committed(node, state):
        htlcs = channel(node).get('htlcs', [])
        return (len(htlcs) == 1 and htlcs[0]['payment_hash'] == payment_hash
                and htlcs[0]['state'] == state)

    for node, state in ((payer, 'SENT_ADD_ACK_REVOCATION'),
                        (swap_btc, 'RCVD_ADD_ACK_REVOCATION'),
                        (swap_xbt, 'SENT_ADD_ACK_REVOCATION'),
                        (receiver, 'RCVD_ADD_ACK_REVOCATION')):
        wait_until(lambda: committed(node, state), node['proc'])
    quote_before = plugin.with_suffix('.quotes.json').read_bytes()

    def still_pending():
        for node in (payer, swap_xbt):
            p = payment(node)
            if p['status'] != 'pending' or p.get('payment_preimage'):
                raise AssertionError('payment did not remain pending without preimage')
        held = rpc(swap_btc, 'xbt-held')['held']
        if len(held) != 1 or held[0]['payment_hash'] != payment_hash:
            raise AssertionError('BTC hook no longer held')
        if rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]['status'] != 'unpaid':
            raise AssertionError('XBT invoice unexpectedly settled')
        if statefile.read_bytes() != before:
            raise AssertionError('pending recovery changed controller checkpoint')
        if plugin.with_suffix('.quotes.json').read_bytes() != quote_before:
            raise AssertionError('pending recovery changed BTC quote state')

    still_pending()
    outgoing_before = rpc(swap_xbt, 'listsendpays')['payments']
    print('PASS: controller crashed after XBT submission; both HTLCs committed and pending; '
          'disk lacks preimage', flush=True)
    if restart_backends is not None:
        # Capture the actual channel HTLC identities, not just payment hashes:
        # a replacement attempt must not satisfy the post-restart assertions.
        nodes = (payer, swap_btc, swap_xbt, receiver)

        def htlc_identity(node):
            c = channel(node)
            htlcs = c.get('htlcs', [])
            if len(htlcs) != 1:
                raise AssertionError('expected one pending HTLC at each node')
            h = htlcs[0]
            return (c['channel_id'], h['id'], h['direction'], h['payment_hash'],
                    h['amount_msat'], h['expiry'], h['state'])

        identities = {node['id']: htlc_identity(node) for node in nodes}
        if abrupt:
            kill_operators((swap_btc, swap_xbt))
            print('PASS: both operator process groups terminated by SIGKILL while HTLCs pending',
                  flush=True)
        for node in (swap_btc, swap_xbt):
            if not abrupt:
                lab.stop(node['proc'])
            node['log'].rename(node['log'].with_name('before-pending-restart.log'))
        for node, name, network, backend, plugins in (
                (swap_btc, 'swap-btc', 'regtest', restart_backends[0], (plugin,)),
                (swap_xbt, 'swap-xbt', 'xbt-regtest', restart_backends[1], ())):
            restarted = lab.lightning(name, network, backend, plugins=plugins)
            if restarted['id'] != node['id'] or restarted['cli'] != node['cli']:
                raise AssertionError('operator identity or saved RPC target changed')
            # Update the existing dictionaries so the caller also uses the
            # replacement processes and ports for its final settlement checks.
            node.update(restarted)
        rpc(payer, 'connect', swap_btc['id'], '127.0.0.1', swap_btc['port'])
        rpc(swap_xbt, 'connect', receiver['id'], '127.0.0.1', receiver['port'])
        wait_until(lambda: any(h['payment_hash'] == payment_hash
                              for h in rpc(swap_btc, 'xbt-held')['held']), swap_btc['proc'])
        for node, state in ((payer, 'SENT_ADD_ACK_REVOCATION'),
                            (swap_btc, 'RCVD_ADD_ACK_REVOCATION'),
                            (swap_xbt, 'SENT_ADD_ACK_REVOCATION'),
                            (receiver, 'RCVD_ADD_ACK_REVOCATION')):
            wait_until(lambda: channel(node)['peer_connected'] and committed(node, state),
                       node['proc'])
            if htlc_identity(node) != identities[node['id']]:
                raise AssertionError('original channel HTLC changed during restart')
        still_pending()
        # Compare stable payment identity fields across CLN restart; optional
        # presentation metadata need not remain byte-for-byte identical.
        outgoing_after = rpc(swap_xbt, 'listsendpays')['payments']
        if len(outgoing_before) != 1 or len(outgoing_after) != 1:
            raise AssertionError('operator restart changed XBT attempt count')
        for key in ('id', 'groupid', 'payment_hash', 'status', 'amount_msat'):
            if outgoing_after[0][key] != outgoing_before[0][key]:
                raise AssertionError('operator restart changed XBT payment field: ' + key)
        if outgoing_after[0].get('partid', 0) != outgoing_before[0].get('partid', 0):
            raise AssertionError('operator restart changed XBT payment part')
        outgoing_before = outgoing_after
        print('PASS: both operators restarted while pending; BTC hook replayed; '
              'original channel HTLCs, XBT attempt and quote binding preserved', flush=True)
    for attempt in range(2):
        resumed = subprocess.run(command, text=True, capture_output=True, timeout=60)
        if resumed.returncode:
            raise AssertionError(f'pending reconciliation failed: {resumed.stdout}\n{resumed.stderr}')
        if json.loads(resumed.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}:
            raise AssertionError('controller did not report pending outcome')
        still_pending()
        if rpc(swap_xbt, 'listsendpays')['payments'] != outgoing_before:
            raise AssertionError('pending recovery changed XBT payment history')
    print('PASS: two fresh controllers reported pending; BTC remained held; no XBT resend', flush=True)

    if fail_outgoing:
        if rpc(receiver, 'xbt-fail', payment_hash)['failed'] != 1:
            raise AssertionError('receiver did not reject exactly one XBT HTLC')

        def terminal_failure(node):
            p = payment(node)
            if p['status'] == 'complete' or p.get('payment_preimage'):
                raise AssertionError('rejected payment unexpectedly settled')
            return p['status'] == 'failed'

        wait_until(lambda: terminal_failure(swap_xbt), swap_xbt['proc'])
        if payment(payer)['status'] != 'pending':
            raise AssertionError('BTC was not held until outgoing failure was reconciled')
        print('PASS: original XBT attempt definitively failed; BTC still pending before recovery', flush=True)
        failed_history = rpc(swap_xbt, 'listsendpays')['payments']
        if failed_history[0]['id'] != outgoing_before[0]['id']:
            raise AssertionError('XBT attempt identity changed')
        for attempt in range(2):
            resumed = subprocess.run(command, text=True, capture_output=True, timeout=60)
            if resumed.returncode:
                raise AssertionError(f'failure recovery failed: {resumed.stdout}\n{resumed.stderr}')
            if json.loads(resumed.stdout) != {'phase': 'btc_failed', 'outcome': 'failed'}:
                raise AssertionError('controller did not report BTC failure')
            if rpc(swap_xbt, 'listsendpays')['payments'] != failed_history:
                raise AssertionError('failure recovery changed XBT payment history')
        wait_until(lambda: terminal_failure(payer), payer['proc'])
        for node in (payer, swap_btc, swap_xbt, receiver):
            def restored():
                c = channel(node)
                return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                        and c['to_us_msat'] == initial[node['id']])
            wait_until(restored, node['proc'])
        expected = json.loads(quote_before)[payment_hash]
        expected['phase'] = 'failed'
        if json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash] != expected:
            raise AssertionError('failed quote did not preserve original terms and binding')
        saved = json.loads(statefile.read_text())
        if saved['phase'] != 'btc_failed' or 'preimage' in saved:
            raise AssertionError('failure checkpoint missing or contains a preimage')
        if rpc(swap_btc, 'xbt-held')['held']:
            raise AssertionError('BTC hook still pending after failure')
        if rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]['status'] != 'unpaid':
            raise AssertionError('rejected XBT invoice unexpectedly paid')
        print('PASS: recovery failed bound BTC HTLC; repeat recovery succeeded; '
              'no pending HTLCs; all four balances restored', flush=True)
        return None

    # Resume native invoice processing. The harness never requests or supplies
    # the receiver's preimage: CLN settles its invoice and reveals it normally.
    if rpc(receiver, 'xbt-continue', payment_hash)['continued'] != 1:
        raise AssertionError('expected exactly one held XBT hook to continue')
    outgoing = rpc(swap_xbt, 'waitsendpay', payment_hash, 10)
    if outgoing['status'] != 'complete':
        raise AssertionError('XBT did not complete after receiver continued')
    resumed = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if resumed.returncode:
        raise AssertionError(f'completion recovery failed: {resumed.stdout}\n{resumed.stderr}')
    result = json.loads(resumed.stdout)
    if result != {'phase': 'btc_released', 'payment_preimage': outgoing['payment_preimage']}:
        raise AssertionError('controller did not recover completed XBT payment')
    if json.loads(statefile.read_text())['phase'] != 'btc_released':
        raise AssertionError('controller failed to checkpoint BTC release')
    final_payment = payment(swap_xbt)
    if final_payment['id'] != outgoing_before[0]['id'] or final_payment['status'] != 'complete':
        raise AssertionError('recovery did not use original XBT attempt')
    print('PASS: receiver resumed normal invoice settlement; controller recovered preimage '
          'and released BTC using the original XBT attempt', flush=True)
    return result['payment_preimage']
