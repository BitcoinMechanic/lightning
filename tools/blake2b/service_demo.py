"""Funded interactive lab and end-to-end exercise of the public service CLI."""
import json
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading

from smoke_regtest import wait_until


def config_file(lab, swap_btc, swap_xbt):
    path = lab.root / 'operators.json'
    path.write_text(json.dumps({'btc_cli': swap_btc['cli'], 'xbt_cli': swap_xbt['cli']}, indent=2))
    path.chmod(0o600)
    return path


def interactive(lab, payer, swap_btc, swap_xbt, receiver, btc, xbt):
    config_file(lab, swap_btc, swap_xbt)
    for name, node in (('payer', payer), ('receiver', receiver), ('btc-operator', swap_btc),
                       ('xbt-operator', swap_xbt), ('btc-chain', btc), ('xbt-chain', xbt)):
        path = lab.root / (name + '-cli')
        path.write_text('#!/bin/sh\nexec ' + shlex.join(node['cli']) + ' "$@"\n')
        path.chmod(0o700)
    print(f'Interactive regtest lab ready: {lab.root}', flush=True)
    print('Use operators.json and the executable *-cli wrappers from another terminal.', flush=True)
    print('Leave this terminal running. Ctrl-C stops all six nodes; data is retained with --work-dir.', flush=True)
    stop = threading.Event()
    old = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        stop.wait()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def demo(lab, payer, swap_btc, swap_xbt, receiver, initial):
    config = config_file(lab, swap_btc, swap_xbt)
    rpc = lambda node, *args: lab.rpc(node['cli'], *args)
    invoice = rpc(receiver, 'invoice', '200000000msat', 'service-demo', 'Service CLI demo')
    directory = lab.root / 'service-swap'
    command = [sys.executable, str(Path(__file__).with_name('swap_service.py'))]
    quoted = subprocess.run([*command, 'quote', '--config', str(config), '--directory', str(directory),
                             '--xbt-invoice', invoice['bolt11'], '--btc-sats', '123000'],
                            capture_output=True, text=True, timeout=60)
    if quoted.returncode:
        raise AssertionError('service quote failed: ' + quoted.stdout + quoted.stderr)
    quote = json.loads(quoted.stdout)
    if quote['btc_sats'] != 123000 or quote['xbt_msat'] != 200000000:
        raise AssertionError('service quote amounts differ')
    # Publication retry must preserve the original invoice.
    again = subprocess.run([*command, 'invoice', '--directory', str(directory)],
                           capture_output=True, text=True, timeout=60)
    if again.returncode or json.loads(again.stdout) != quote:
        raise AssertionError('invoice publication changed on retry')
    log = lab.root / 'service.log'
    process = lab.start([*command, 'run', '--directory', str(directory)], log)
    wait_until(lambda: 'waiting_for_btc' in log.read_text(), process)
    print('PASS: service published a 123,000-sat BTC quote and is waiting for payment', flush=True)
    paid = rpc(payer, 'pay', quote['btc_invoice'])
    process.wait(timeout=60)
    if process.returncode or paid['status'] != 'complete':
        raise AssertionError('service did not complete: ' + log.read_text())
    for node, delta in ((payer, -123000000), (swap_btc, 123000000),
                        (swap_xbt, -200000000), (receiver, 200000000)):
        def settled():
            c = rpc(node, 'listpeerchannels')['channels'][0]
            return not c.get('htlcs') and c['to_us_msat'] == initial[node['id']] + delta
        wait_until(settled, node['proc'])
    received = rpc(receiver, 'listinvoices', 'service-demo')['invoices'][0]
    if received['status'] != 'paid':
        raise AssertionError('receiver invoice unpaid')
    outgoing = rpc(swap_xbt, 'listsendpays')['payments']
    if len(outgoing) != 1 or outgoing[0]['status'] != 'complete':
        raise AssertionError('expected exactly one completed XBT attempt')
    restart = subprocess.run([*command, 'run', '--directory', str(directory)],
                             capture_output=True, text=True, timeout=60)
    status = subprocess.run([*command, 'status', '--directory', str(directory)],
                            capture_output=True, text=True, timeout=60)
    if restart.returncode or status.returncode or json.loads(status.stdout)['phase'] != 'btc_released':
        raise AssertionError('service restart/status failed')
    if rpc(swap_xbt, 'listsendpays')['payments'] != outgoing:
        raise AssertionError('service restart changed outgoing attempts')
    if paid['payment_preimage'] in log.read_text() + status.stdout + restart.stdout:
        raise AssertionError('service exposed preimage in status/logs')
    print('PASS: ordinary BTC pay completed; XBT invoice paid; four balances match; restart did not resend', flush=True)
    print('Service CLI demo OK (quote, run, status; direct channels; regtest only)', flush=True)
