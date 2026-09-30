"""Two overlapping controllers for the same state path, one XBT submission."""
import json
from pathlib import Path
import subprocess
import sys

from smoke_regtest import wait_until
from swap_controller import save


def run_concurrent(lab, payer, swap_btc, swap_xbt, receiver, invoice, binding, plugin):
    payment_hash = invoice['payment_hash']

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected one channel')
        return channels[0]

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
    logfile = lab.root / 'first-controller.log'
    first = lab.start([*command, '--wait-pending'], logfile)
    for node, state in ((swap_xbt, 'SENT_ADD_ACK_REVOCATION'),
                        (receiver, 'RCVD_ADD_ACK_REVOCATION')):
        def committed():
            htlcs = channel(node).get('htlcs', [])
            return (len(htlcs) == 1 and htlcs[0]['payment_hash'] == payment_hash
                    and htlcs[0]['state'] == state)
        wait_until(committed, first)
    before = statefile.read_bytes()
    saved = json.loads(before)
    if saved['phase'] != 'outgoing_started' or 'preimage' in saved:
        raise AssertionError('first controller not awaiting outgoing completion')
    history = rpc(swap_xbt, 'listsendpays')['payments']
    if len(history) != 1 or history[0]['status'] != 'pending':
        raise AssertionError('expected one pending XBT attempt')
    quote_before = plugin.with_suffix('.quotes.json').read_bytes()
    second = subprocess.run(command, text=True, capture_output=True, timeout=20)
    if second.returncode or json.loads(second.stdout) != {'outcome': 'busy'}:
        raise AssertionError(f'competing controller did not report busy: {second.stdout}\n{second.stderr}')
    if first.poll() is not None:
        raise AssertionError('first controller stopped during contention: ' + logfile.read_text())
    if statefile.read_bytes() != before or plugin.with_suffix('.quotes.json').read_bytes() != quote_before:
        raise AssertionError('competing controller changed swap or quote state')
    if rpc(swap_xbt, 'listsendpays')['payments'] != history:
        raise AssertionError('competing controller changed XBT payment history')
    btc_payments = rpc(payer, 'listsendpays')['payments']
    if len(btc_payments) != 1 or btc_payments[0]['status'] != 'pending':
        raise AssertionError('BTC did not remain pending during contention')
    print('PASS: first controller holds pending XBT; competing controller reports busy; '
          'state unchanged and exactly one XBT attempt', flush=True)
    if rpc(receiver, 'xbt-continue', payment_hash)['continued'] != 1:
        raise AssertionError('receiver did not resume exactly one XBT hook')
    first.wait(timeout=60)
    if first.returncode:
        raise AssertionError('first controller failed: ' + logfile.read_text())
    result = json.loads(logfile.read_text())
    if result['phase'] != 'btc_released':
        raise AssertionError('first controller did not release BTC')
    complete = rpc(swap_xbt, 'listsendpays')['payments']
    if len(complete) != 1 or complete[0]['status'] != 'complete' or complete[0]['id'] != history[0]['id']:
        raise AssertionError('original XBT attempt did not complete exactly once')
    again = subprocess.run(command, text=True, capture_output=True, timeout=20)
    if again.returncode or json.loads(again.stdout) != result:
        raise AssertionError('completed controller could not be reopened after lock release')
    if rpc(swap_xbt, 'listsendpays')['payments'] != complete:
        raise AssertionError('post-completion recovery changed outgoing history')
    print('PASS: original controller completed swap; lock released; later recovery created no new attempt',
          flush=True)
    return result['payment_preimage']
