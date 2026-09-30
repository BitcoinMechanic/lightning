"""Refuse substituted outgoing terms, then complete the original quoted swap."""
import copy
import json
from pathlib import Path
import subprocess
import sys

from swap_controller import save


def run_binding(lab, payer, swap_btc, swap_xbt, receiver, invoice, binding, plugin):
    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    payment_hash = invoice['payment_hash']
    state = {'phase': 'prepared', 'quote_gate': True, 'payment_hash': payment_hash,
             'payment_secret': invoice['payment_secret'], 'xbt_invoice': invoice['bolt11'],
             'btc_binding': binding, 'xbt_amount_msat': 200000000,
             'xbt_cli': swap_xbt['cli'], 'btc_cli': swap_btc['cli'],
             'route': [{'id': receiver['id'], 'amount_msat': 200000000, 'delay': 40,
                        'channel': rpc(swap_xbt, 'listpeerchannels')['channels'][0]['short_channel_id']}]}
    alternative = rpc(receiver, 'invoice', '200000000msat', 'binding-alternative',
                      'Must never be paid')
    if alternative['payment_hash'] == payment_hash:
        raise AssertionError('alternative invoice did not have a distinct hash')
    path = lab.root / 'swap-state.json'
    command = [sys.executable, str(Path(__file__).with_name('swap_controller.py')),
               '--state', str(path)]
    quote_before = plugin.with_suffix('.quotes.json').read_bytes()
    cases = []
    substituted = copy.deepcopy(state)
    substituted['xbt_invoice'] = alternative['bolt11']
    cases.append(('invoice', substituted, 'quoted_invoice_mismatch'))
    substituted = copy.deepcopy(state)
    substituted['route'][0]['id'] = swap_xbt['id']
    cases.append(('destination', substituted, 'xbt_route_mismatch'))
    for name, bad_state, reason in cases:
        save(path, bad_state)
        before = path.read_bytes()
        result = subprocess.run(command, text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError(f'binding check failed: {result.stdout}\n{result.stderr}')
        if json.loads(result.stdout) != {'phase': 'prepared', 'outcome': 'refused', 'reason': reason}:
            raise AssertionError('substitution not refused for expected reason: ' + name)
        if path.read_bytes() != before or plugin.with_suffix('.quotes.json').read_bytes() != quote_before:
            raise AssertionError('refusal changed controller or quote state')
        if rpc(swap_xbt, 'listsendpays')['payments']:
            raise AssertionError('substitution created an XBT attempt')
        payments = rpc(payer, 'listsendpays')['payments']
        if len(payments) != 1 or payments[0]['status'] != 'pending':
            raise AssertionError('BTC did not remain pending')
        held = rpc(swap_btc, 'xbt-held')['held']
        if len(held) != 1 or held[0]['payment_hash'] != payment_hash:
            raise AssertionError('BTC hook did not remain held')
        for label in ('swap-receive', 'binding-alternative'):
            if rpc(receiver, 'listinvoices', label)['invoices'][0]['status'] != 'unpaid':
                raise AssertionError('invoice paid during refusal test')
        print(f'PASS: substituted XBT {name} refused; no XBT attempt; BTC remains held', flush=True)
    # Restore the trusted fixture state only after proving neither variant sent.
    save(path, state)
    result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise AssertionError(f'correct quoted payment failed: {result.stdout}\n{result.stderr}')
    recovered = json.loads(result.stdout)
    payments = rpc(swap_xbt, 'listsendpays')['payments']
    if (recovered['phase'] != 'btc_released' or len(payments) != 1
            or payments[0]['status'] != 'complete' or payments[0]['payment_hash'] != payment_hash
            or payments[0]['destination'] != receiver['id']):
        raise AssertionError('expected exactly one completed payment to quoted receiver')
    if rpc(receiver, 'listinvoices', 'binding-alternative')['invoices'][0]['status'] != 'unpaid':
        raise AssertionError('alternative invoice unexpectedly paid')
    print('PASS: original quoted invoice and destination accepted; exactly one XBT attempt completed', flush=True)
    return recovered['payment_preimage']
