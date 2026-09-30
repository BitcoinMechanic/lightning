"""XBT on-chain HTLC claim or timeout followed by BTC swap recovery.

The receiver fixture knows its chosen preimage; the controller only learns it
from the XBT operator's completed payment after on-chain extraction. The BTC
chain stays fixed. This does not test cross-chain deadline or reorg policy.
"""
import json
from pathlib import Path
import subprocess
import sys

from preimage_claim import run_claim
from htlc_timeout import recover_timeout
from smoke_regtest import wait_until
from swap_controller import save


def run_onchain(lab, payer, swap_btc, swap_xbt, receiver, btc, xbt, invoice,
                receiver_preimage, binding, plugin, initial, pay_process, pay_log,
                timeout=False):
    payment_hash = invoice['payment_hash']
    active_nodes = [swap_xbt, receiver]

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected exactly one channel')
        return channels[0]

    def mine(count):
        blocks = rpc(xbt, 'generatetoaddress', count, rpc(xbt, 'getnewaddress'))
        height = rpc(xbt, 'getblockcount')
        for node in active_nodes:
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height, node['proc'], timeout=90)
        return blocks

    def confirmed_outputs(node, txid):
        return [o for o in rpc(node, 'listfunds')['outputs']
                if o['txid'] == txid and o['status'] == 'confirmed']

    # The receiver needs wallet inputs for anchor HTLC-success transaction fees.
    if not timeout:
        deposit = rpc(xbt, 'sendtoaddress', rpc(receiver, 'newaddr', 'bech32')['bech32'], '0.01')
        mine(1)
        wait_until(lambda: confirmed_outputs(receiver, deposit), receiver['proc'])
    outgoing_channel = channel(swap_xbt)
    funding = {'txid': outgoing_channel['funding_txid'], 'outnum': outgoing_channel['funding_outnum']}
    btc_height = rpc(btc, 'getblockcount')
    quote_before = plugin.with_suffix('.quotes.json').read_bytes()
    path = lab.root / 'swap-state.json'
    save(path, {'phase': 'prepared', 'quote_gate': True, 'payment_hash': payment_hash,
                'payment_secret': invoice['payment_secret'], 'xbt_invoice': invoice['bolt11'],
                'btc_binding': binding, 'xbt_amount_msat': 200000000,
                'xbt_cli': swap_xbt['cli'], 'btc_cli': swap_btc['cli'],
                'route': [{'id': receiver['id'], 'channel': outgoing_channel['short_channel_id'],
                           'amount_msat': 200000000, 'delay': 40}]})
    command = [sys.executable, str(Path(__file__).with_name('swap_controller.py')),
               '--state', str(path)]
    crashed = subprocess.run([*command, '--crash-after-sendpay'], text=True,
                             capture_output=True, timeout=60)
    if crashed.returncode != 88:
        raise AssertionError(f'controller missed crash point: {crashed.stdout}\n{crashed.stderr}')
    before = path.read_bytes()
    if json.loads(before)['phase'] != 'outgoing_started' or 'preimage' in json.loads(before):
        raise AssertionError('controller learned preimage before on-chain reveal')

    def committed(node, state):
        htlcs = channel(node).get('htlcs', [])
        return next((h for h in htlcs if h['payment_hash'] == payment_hash and h['state'] == state), None)

    htlc = wait_until(lambda: committed(swap_xbt, 'SENT_ADD_ACK_REVOCATION'), swap_xbt['proc'])
    wait_until(lambda: committed(receiver, 'RCVD_ADD_ACK_REVOCATION'), receiver['proc'])
    pending = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if pending.returncode or json.loads(pending.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}:
        raise AssertionError('controller did not preserve pending outgoing payment')
    print('PASS: XBT HTLC committed and held; controller checkpoint has no preimage', flush=True)

    # Reuse the tested funded-channel claim sequence: identify the commitment
    # HTLC output, verify the confirmed preimage witness and operator onchaind
    # extraction, then verify CSV maturity and the receiver's confirmed sweep.
    # In its diagnostics Alice means swap-xbt; Bob means receiver.
    print('On-chain checks: Alice = XBT operator; Bob = XBT receiver', flush=True)
    if timeout:
        if receiver_preimage is not None:
            raise AssertionError('timeout fixture must not know receiver preimage')
        if rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]['status'] != 'unpaid':
            raise AssertionError('receiver invoice unexpectedly paid before shutdown')
        original_attempts = rpc(swap_xbt, 'listsendpays')['payments']
        if len(original_attempts) != 1 or original_attempts[0]['status'] != 'pending':
            raise AssertionError('expected one pending XBT attempt before timeout')
        lab.stop(receiver['proc'])
        active_nodes.remove(receiver)

        def before_timeout():
            pending = subprocess.run(command, text=True, capture_output=True, timeout=60)
            if pending.returncode or json.loads(pending.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}:
                raise AssertionError('controller failed BTC before definitive on-chain failure')
            if path.read_bytes() != before or plugin.with_suffix('.quotes.json').read_bytes() != quote_before:
                raise AssertionError('pre-timeout reconciliation changed durable state')
            payments = rpc(payer, 'listsendpays')['payments']
            if len(payments) != 1 or payments[0]['status'] != 'pending':
                raise AssertionError('BTC not held before XBT timeout')
            print('PASS: commitment on-chain but HTLC not yet timed out; controller keeps BTC held', flush=True)

        recover_timeout(xbt, swap_xbt, receiver, funding, htlc['expiry'],
                        outgoing_channel['our_to_self_delay'], mine, rpc, confirmed_outputs,
                        amount_sat=200000, standalone=False, before_timeout=before_timeout)
    else:
        claim = run_claim(xbt, swap_xbt, receiver, funding, invoice, receiver_preimage,
                          htlc['expiry'], mine, rpc, confirmed_outputs,
                          amount_sat=200000, standalone=False)
    if path.read_bytes() != before:
        raise AssertionError('controller checkpoint changed while controller was offline')
    if rpc(btc, 'getblockcount') != btc_height:
        raise AssertionError('BTC chain advanced during XBT on-chain claim')
    btc_payments = rpc(payer, 'listsendpays')['payments']
    if len(btc_payments) != 1 or btc_payments[0]['status'] != 'pending':
        raise AssertionError('BTC did not remain held through on-chain XBT recovery')
    if plugin.with_suffix('.quotes.json').read_bytes() != quote_before:
        raise AssertionError('BTC quote changed before controller recovery')
    if timeout:
        def definitive_failure():
            payments = rpc(swap_xbt, 'listsendpays')['payments']
            if (len(payments) != 1 or payments[0]['id'] != original_attempts[0]['id']
                    or payments[0]['payment_hash'] != payment_hash
                    or payments[0]['status'] == 'complete' or payments[0].get('payment_preimage')):
                raise AssertionError('unexpected outgoing outcome after on-chain timeout')
            return payments if payments[0]['status'] == 'failed' else None

        outgoing = wait_until(definitive_failure, swap_xbt['proc'], timeout=90)
        print('PASS: XBT timeout refund swept; CLN reports original outgoing attempt failed without preimage',
              flush=True)
        for attempt in range(2):
            resumed = subprocess.run(command, text=True, capture_output=True, timeout=60)
            if resumed.returncode or json.loads(resumed.stdout) != {'phase': 'btc_failed', 'outcome': 'failed'}:
                raise AssertionError(f'BTC failure recovery failed: {resumed.stdout}\n{resumed.stderr}')
        pay_process.wait(timeout=30)
        if pay_process.returncode == 0:
            raise AssertionError('BTC pay unexpectedly succeeded after on-chain XBT timeout')

        def btc_failed():
            payments = rpc(payer, 'listsendpays')['payments']
            if len(payments) != 1 or payments[0]['payment_hash'] != payment_hash:
                raise AssertionError('unexpected BTC attempt after timeout')
            if payments[0]['status'] == 'complete' or payments[0].get('payment_preimage'):
                raise AssertionError('BTC unexpectedly settled after timeout')
            return payments[0]['status'] == 'failed'

        wait_until(btc_failed, payer['proc'])
        for node in (payer, swap_btc):
            def restored():
                c = channel(node)
                return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                        and c['to_us_msat'] == initial[node['id']])
            wait_until(restored, node['proc'])
        expected = json.loads(quote_before)[payment_hash]
        expected['phase'] = 'failed'
        if json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash] != expected:
            raise AssertionError('BTC failure changed original terms or binding')
        checkpoint = json.loads(path.read_text())
        if checkpoint['phase'] != 'btc_failed' or 'preimage' in checkpoint:
            raise AssertionError('invalid timeout recovery checkpoint')
        if rpc(swap_btc, 'xbt-held')['held'] or rpc(swap_xbt, 'listsendpays')['payments'] != outgoing:
            raise AssertionError('BTC still held or XBT history changed during recovery')
        print('PASS: controller failed original BTC HTLC; repeat recovery safe; BTC balances restored; no XBT resend',
              flush=True)
        print('BTC -> XBT on-chain timeout swap test OK (operator refund swept; XBT fees apply; regtest only)',
              flush=True)
        return
    outgoing = rpc(swap_xbt, 'listsendpays')['payments']
    if (len(outgoing) != 1 or outgoing[0]['status'] != 'complete'
            or outgoing[0]['payment_hash'] != payment_hash
            or outgoing[0]['payment_preimage'] != claim['payment_preimage']):
        raise AssertionError('XBT payment did not complete with on-chain preimage')
    resumed = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if resumed.returncode:
        raise AssertionError(f'on-chain swap recovery failed: {resumed.stdout}\n{resumed.stderr}')
    if json.loads(resumed.stdout) != {'phase': 'btc_released', 'payment_preimage': claim['payment_preimage']}:
        raise AssertionError('controller did not recover the on-chain preimage')
    pay_process.wait(timeout=30)
    if pay_process.returncode:
        raise AssertionError('BTC pay failed: ' + pay_log.read_text())
    paid = json.loads(pay_log.read_text())
    if paid['status'] != 'complete' or paid['payment_preimage'] != claim['payment_preimage']:
        raise AssertionError('BTC did not settle with the on-chain preimage')
    for node, delta in ((payer, -100000000), (swap_btc, 100000000)):
        def settled():
            c = channel(node)
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[node['id']] + delta)
        wait_until(settled, node['proc'])
    stored = json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash]
    expected = json.loads(quote_before)[payment_hash]
    expected.update(phase='resolved', preimage=claim['payment_preimage'])
    if stored != expected or rpc(swap_btc, 'xbt-held')['held']:
        raise AssertionError('BTC quote did not settle with original terms and binding')
    if rpc(swap_xbt, 'listsendpays')['payments'] != outgoing:
        raise AssertionError('controller recovery changed outgoing payment history')
    if json.loads(path.read_text())['phase'] != 'btc_released':
        raise AssertionError('controller did not checkpoint BTC release')
    print('PASS: controller recovered on-chain preimage; BTC settled off-chain; '
          'BTC balances match; no additional XBT attempt', flush=True)
    print('BTC -> XBT on-chain preimage swap test OK (receiver sweep confirmed; XBT fees apply; regtest only)',
          flush=True)
