"""BTC deadline/claim fixture with an unresolved outgoing XBT payment.

Pending controller reconciliation closes BTC with 30 blocks remaining.
Optionally use a restarted foreground watcher for automatic reconciliation.
XBT height stays fixed and the receiver cooperates after the BTC commitment
confirms. No cross-chain stall guarantee is implied.
"""
import json
from pathlib import Path
import subprocess
import sys

from preimage_claim import run_claim
from smoke_regtest import wait_until
from swap_controller import save


def run_deadline(lab, payer, swap_btc, swap_xbt, receiver, btc, xbt, invoice,
                 btc_invoice, binding, plugin, initial, pay_process, pay_log, watch=False):
    payment_hash = invoice['payment_hash']

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected one channel')
        return channels[0]

    def mine(count):
        blocks = rpc(btc, 'generatetoaddress', count, rpc(btc, 'getnewaddress'))
        height = rpc(btc, 'getblockcount')
        for node in (payer, swap_btc):
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height, node['proc'], timeout=90)
        return blocks

    def confirmed_outputs(node, txid):
        return [o for o in rpc(node, 'listfunds')['outputs']
                if o['txid'] == txid and o['status'] == 'confirmed']

    # Wallet inputs for the BTC operator's anchor HTLC-success transaction.
    deposit = rpc(btc, 'sendtoaddress', rpc(swap_btc, 'newaddr', 'bech32')['bech32'], '0.01')
    mine(1)
    wait_until(lambda: confirmed_outputs(swap_btc, deposit), swap_btc['proc'])
    incoming_channel = channel(swap_btc)
    funding = {'txid': incoming_channel['funding_txid'], 'outnum': incoming_channel['funding_outnum']}
    incoming = next(h for h in incoming_channel.get('htlcs', []) if h['payment_hash'] == payment_hash)
    xbt_height = rpc(xbt, 'getblockcount')
    quote_before = plugin.with_suffix('.quotes.json').read_bytes()
    path = lab.root / 'swap-state.json'
    save(path, {'phase': 'prepared', 'quote_gate': True, 'btc_deadline_guard': True, 'payment_hash': payment_hash,
                'payment_secret': invoice['payment_secret'], 'xbt_invoice': invoice['bolt11'],
                'btc_binding': binding, 'xbt_amount_msat': 200000000,
                'xbt_cli': swap_xbt['cli'], 'btc_cli': swap_btc['cli'],
                'route': [{'id': receiver['id'], 'channel': channel(swap_xbt)['short_channel_id'],
                           'amount_msat': 200000000, 'delay': 40}]})
    command = [sys.executable, str(Path(__file__).with_name('swap_controller.py')),
               '--state', str(path)]
    crashed = subprocess.run([*command, '--crash-after-sendpay'], text=True,
                             capture_output=True, timeout=60)
    if crashed.returncode != 88:
        raise AssertionError(f'controller missed submission crash: {crashed.stdout}\n{crashed.stderr}')
    before = path.read_bytes()
    if json.loads(before)['phase'] != 'outgoing_started' or 'preimage' in json.loads(before):
        raise AssertionError('invalid pending controller checkpoint')
    for node, state in ((swap_xbt, 'SENT_ADD_ACK_REVOCATION'), (receiver, 'RCVD_ADD_ACK_REVOCATION')):
        wait_until(lambda: any(h['payment_hash'] == payment_hash and h['state'] == state
                              for h in channel(node).get('htlcs', [])), node['proc'])

    watcher = None
    watch_log = lab.root / 'watcher.log'
    watch_command = [sys.executable, str(Path(__file__).with_name('swap_watch.py')),
                     '--state', str(path), '--interval', '0.2']

    def watch_events():
        return [json.loads(line) for line in watch_log.read_text().split('\n')[:-1] if line.strip()]

    def await_polls():
        count = len(watch_events())
        wait_until(lambda: len(watch_events()) >= count + 2, watcher, timeout=40)
        if any(e['event'] != 'reconciled' or e.get('outcome') != 'pending' for e in watch_events()):
            raise AssertionError('watcher did not reconcile pending swap: ' + watch_log.read_text())

    def assert_pending():
        if watch:
            await_polls()
        else:
            result = subprocess.run(command, text=True, capture_output=True, timeout=60)
            if result.returncode or json.loads(result.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}:
                raise AssertionError('controller did not preserve unresolved swap')
        for node in (payer, swap_xbt):
            payments = rpc(node, 'listsendpays')['payments']
            if len(payments) != 1 or payments[0]['status'] != 'pending' or payments[0].get('payment_preimage'):
                raise AssertionError('expected original pending payment without preimage')
        if path.read_bytes() != before or plugin.with_suffix('.quotes.json').read_bytes() != quote_before:
            raise AssertionError('pending recovery changed controller or quote state')

    remaining = incoming['expiry'] - rpc(btc, 'getblockcount')
    if remaining <= 30:
        raise AssertionError('insufficient initial BTC margin for deadline fixture')
    mine(remaining - 31)
    if watch:
        watcher = lab.start(watch_command, watch_log)
    assert_pending()
    if watch:
        watcher.kill()
        watcher.wait(timeout=10)
        if watcher.returncode != -9:
            raise AssertionError('watcher was not killed')
        if path.read_bytes() != before:
            raise AssertionError('watcher changed pending checkpoint')
        watcher = lab.start(watch_command, watch_log)
        assert_pending()
        print('PASS: watcher SIGKILL/restart preserved pending swap and original XBT attempt', flush=True)
    if channel(swap_btc)['state'] != 'CHANNELD_NORMAL':
        raise AssertionError('controller closed before threshold')
    print('PASS: controller leaves BTC channel open at 31 blocks remaining', flush=True)
    mine(1)
    if incoming['expiry'] - rpc(btc, 'getblockcount') != 30 or rpc(xbt, 'getblockcount') != xbt_height:
        raise AssertionError('unexpected chain heights after BTC deadline advancement')
    if watch:
        wait_until(lambda: 'btc_close_result' in json.loads(path.read_text()), watcher, timeout=60)
    else:
        triggered = subprocess.run(command, text=True, capture_output=True, timeout=60)
        if triggered.returncode or json.loads(triggered.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}:
            raise AssertionError(f'deadline controller failed: {triggered.stdout}\n{triggered.stderr}')
    protected = json.loads(path.read_text())
    if protected['btc_close_intent']['channel_id'] != incoming_channel['channel_id']:
        raise AssertionError('controller closed the wrong BTC channel')
    if protected['btc_close_result']['type'] != 'unilateral' or 'preimage' in protected:
        raise AssertionError('expected unilateral close without preimage')
    wait_until(lambda: channel(swap_btc)['state'] == 'AWAITING_UNILATERAL', swap_btc['proc'])
    before = path.read_bytes()
    assert_pending()
    print('PASS: controller closed bound BTC channel at 30 blocks; fresh controller preserved pending payments and close checkpoint', flush=True)
    outgoing_before = rpc(swap_xbt, 'listsendpays')['payments']

    def release_after_commitment():
        # run_claim has already checked the confirmed, unresolved BTC HTLC.
        assert_pending()
        print('PASS: BTC commitment confirmed while XBT remains pending; no preimage in controller checkpoint',
              flush=True)
        if rpc(receiver, 'xbt-continue', payment_hash)['continued'] != 1:
            raise AssertionError('expected one XBT hook to resume')
        completed = rpc(swap_xbt, 'waitsendpay', payment_hash, 10)
        if completed['status'] != 'complete':
            raise AssertionError('XBT did not settle after BTC went on-chain')
        if watch:
            watcher.wait(timeout=60)
            checkpoint = json.loads(path.read_text())
            if (watcher.returncode or checkpoint['phase'] != 'btc_released'
                    or checkpoint['preimage'] != completed['payment_preimage']):
                raise AssertionError('watcher failed to recover BTC release: ' + watch_log.read_text())
            if completed['payment_preimage'] in watch_log.read_text():
                raise AssertionError('watcher leaked preimage to log')
            print('PASS: watcher recovered XBT completion and released BTC without manual reconciliation', flush=True)
        else:
            result = subprocess.run(command, text=True, capture_output=True, timeout=60)
            if result.returncode:
                raise AssertionError(f'controller recovery failed: {result.stdout}\n{result.stderr}')
            expected = {'phase': 'btc_released', 'payment_preimage': completed['payment_preimage']}
            if json.loads(result.stdout) != expected:
                raise AssertionError('controller failed to recover XBT preimage and release BTC hook')
        return completed['payment_preimage']

    print('On-chain checks: Alice = BTC payer; Bob = BTC operator', flush=True)
    claim = run_claim(btc, payer, swap_btc, funding,
                      {'bolt11': btc_invoice, 'payment_hash': payment_hash}, None,
                      incoming['expiry'], mine, rpc, confirmed_outputs,
                      standalone=False, release=release_after_commitment,
                      close=protected['btc_close_result'])
    pay_process.wait(timeout=30)
    if pay_process.returncode:
        raise AssertionError('BTC payer failed: ' + pay_log.read_text())
    paid = json.loads(pay_log.read_text())
    if paid['status'] != 'complete' or paid['payment_preimage'] != claim['payment_preimage']:
        raise AssertionError('BTC payer did not settle with the XBT preimage learned on-chain')
    for node, delta in ((swap_xbt, -200000000), (receiver, 200000000)):
        def settled():
            c = channel(node)
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[node['id']] + delta)
        wait_until(settled, node['proc'])
    received = rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]
    if received['status'] != 'paid' or received['amount_received_msat'] != 200000000:
        raise AssertionError('XBT receiver did not receive quoted amount')
    outgoing = rpc(swap_xbt, 'listsendpays')['payments']
    if len(outgoing) != 1 or outgoing[0]['id'] != outgoing_before[0]['id'] or outgoing[0]['status'] != 'complete':
        raise AssertionError('XBT attempt was replaced or duplicated')
    expected = json.loads(quote_before)[payment_hash]
    expected.update(phase='resolved', preimage=claim['payment_preimage'])
    if json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash] != expected:
        raise AssertionError('quote terms or binding changed during BTC claim')
    if rpc(swap_btc, 'xbt-held')['held'] or rpc(xbt, 'getblockcount') != xbt_height:
        raise AssertionError('BTC still held or XBT chain advanced unexpectedly')
    again = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if again.returncode or json.loads(again.stdout) != {'phase': 'btc_released', 'payment_preimage': claim['payment_preimage']}:
        raise AssertionError('completed swap recovery failed')
    if rpc(swap_xbt, 'listsendpays')['payments'] != outgoing:
        raise AssertionError('repeat recovery changed XBT payment history')
    print('PASS: XBT settled off-chain; BTC operator claim and CSV sweep confirmed; no additional XBT attempt', flush=True)
    if watch:
        print('PASS: restarted watcher handled BTC deadline and settlement automatically', flush=True)
    print('BTC deadline swap test OK (controller-triggered BTC close; BTC fees apply; regtest only)', flush=True)
