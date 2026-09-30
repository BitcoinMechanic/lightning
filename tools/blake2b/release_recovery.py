"""Lose the controller's release checkpoint, then reconcile the quote gate."""
import json
from pathlib import Path
import subprocess
import sys

from smoke_regtest import wait_until
from swap_controller import save


def run_recovery(lab, payer, swap_btc, swap_xbt, receiver, invoice, binding):
    payment_hash = invoice['payment_hash']

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    statefile = lab.root / 'swap-state.json'
    channels = rpc(swap_xbt, 'listpeerchannels')['channels']
    if len(channels) != 1:
        raise AssertionError('expected one outgoing channel')
    save(statefile, {'phase': 'prepared', 'payment_hash': payment_hash,
                     'payment_secret': invoice['payment_secret'],
                     'xbt_invoice': invoice['bolt11'],
                     'xbt_amount_msat': 200000000, 'quote_gate': True,
                     'btc_binding': binding,
                     'xbt_cli': swap_xbt['cli'], 'btc_cli': swap_btc['cli'],
                     'route': [{'id': receiver['id'],
                                'channel': channels[0]['short_channel_id'],
                                'amount_msat': 200000000, 'delay': 40}]})
    command = [sys.executable, str(Path(__file__).with_name('swap_controller.py')),
               '--state', str(statefile)]
    crashed = subprocess.run([*command, '--crash-after-btc'], text=True,
                             capture_output=True, timeout=60)
    if crashed.returncode != 87:
        raise AssertionError(f'controller missed release crash: {crashed.stdout}\n{crashed.stderr}')
    saved = json.loads(statefile.read_text())
    if saved['phase'] != 'xbt_paid' or 'preimage' not in saved:
        raise AssertionError('controller unexpectedly checkpointed BTC release')
    expected_status = {'payment_hash': payment_hash, 'phase': 'resolved', 'binding': binding}
    if rpc(swap_btc, 'xbt-quote-status', payment_hash) != expected_status:
        raise AssertionError('quote gate did not persist release for original binding')

    def btc_settled():
        payments = [p for p in rpc(payer, 'listsendpays')['payments']
                    if p['payment_hash'] == payment_hash]
        if len(payments) != 1 or payments[0]['status'] == 'failed':
            raise AssertionError('expected one successful BTC payment')
        if payments[0]['status'] != 'complete':
            return False
        if payments[0]['payment_preimage'] != saved['preimage']:
            raise AssertionError('BTC settled with wrong preimage')
        return True

    # Controller is dead while CLN commits settlement. Check payer-side proof
    # before restarting; quote phase alone is only proof of release intent.
    wait_until(btc_settled, payer['proc'])
    if rpc(swap_btc, 'xbt-held')['held']:
        raise AssertionError('BTC hook still held after settlement')
    print('PASS: controller crashed after BTC release; payer settled; controller disk still says xbt_paid',
          flush=True)
    outgoing_before = rpc(swap_xbt, 'listsendpays')['payments']
    if (len(outgoing_before) != 1 or outgoing_before[0]['status'] != 'complete'
            or outgoing_before[0]['payment_hash'] != payment_hash):
        raise AssertionError('expected exactly one completed XBT attempt')
    for attempt in range(2):
        resumed = subprocess.run(command, text=True, capture_output=True, timeout=60)
        if resumed.returncode:
            raise AssertionError(f'release recovery failed: {resumed.stdout}\n{resumed.stderr}')
        if json.loads(resumed.stdout) != {'payment_preimage': saved['preimage'], 'phase': 'btc_released'}:
            raise AssertionError('unexpected release recovery result')
        if json.loads(statefile.read_text())['phase'] != 'btc_released':
            raise AssertionError('recovered release was not checkpointed')
        if rpc(swap_xbt, 'listsendpays')['payments'] != outgoing_before:
            raise AssertionError('recovery changed outgoing payment history')
        if rpc(swap_btc, 'xbt-quote-status', payment_hash) != expected_status:
            raise AssertionError('recovery changed quote release or binding')
    print('PASS: fresh controller reconciled durable BTC release; second restart also succeeded; '
          'no additional XBT attempt', flush=True)
    return saved['preimage']
