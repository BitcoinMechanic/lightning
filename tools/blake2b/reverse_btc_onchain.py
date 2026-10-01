"""Outgoing BTC on-chain outcome followed by bound XBT release or failure.

Only BTC advances. Receiver preimage is a harness-only fixture secret; the
controller obtains it from the BTC operator's completed payment record.
"""
import json
from pathlib import Path
import subprocess
import sys

from htlc_timeout import recover_timeout
from preimage_claim import run_claim
from smoke_regtest import wait_until
from swap_controller import save


def exercise_btc_onchain(lab, payer, incoming, outgoing, receiver, xbt, btc,
                         invoice, decoded, route, quote, plugin, initial,
                         paying, pay_log, receiver_preimage, timeout=False):
    payment_hash = invoice['payment_hash']
    active = [outgoing, receiver]

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected exactly one fixture channel')
        return channels[0]

    def mine(count):
        blocks = rpc(btc, 'generatetoaddress', count, rpc(btc, 'getnewaddress'))
        height = rpc(btc, 'getblockcount')
        for node in active:
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height, node['proc'], timeout=90)
        return blocks

    def confirmed_outputs(node, txid):
        return [o for o in rpc(node, 'listfunds')['outputs']
                if o['txid'] == txid and o['status'] == 'confirmed']

    def attempts():
        return [p for p in rpc(outgoing, 'listsendpays')['payments'] if p['payment_hash'] == payment_hash]

    if not timeout:
        deposit = rpc(btc, 'sendtoaddress', rpc(receiver, 'newaddr', 'bech32')['bech32'], '0.01')
        mine(1)
        wait_until(lambda: confirmed_outputs(receiver, deposit), receiver['proc'])
    elif receiver_preimage is not None:
        raise AssertionError('timeout harness must not know receiver preimage')
    c = channel(outgoing)
    funding = dict(txid=c['funding_txid'], outnum=c['funding_outnum'])
    gate = rpc(incoming, 'reverse-status', payment_hash)
    path = lab.root/'reverse-state.json'
    save(path, dict(profile='reverse-regtest-v1', phase='prepared', durable_gate=True,
                    reverse_quote=quote, xbt_cli=incoming['cli'], btc_cli=outgoing['cli'],
                    node_ids=[incoming['id'], outgoing['id']], payment_hash=payment_hash,
                    xbt_binding=gate['binding'], xbt_expiry=gate['cltv_expiry'],
                    xbt_amount_msat=200000000, btc_amount_msat=100000000,
                    btc_invoice=invoice['bolt11'], btc_secret=decoded['payment_secret'], route=route))
    command = [sys.executable, str(Path(__file__).with_name('reverse_controller.py')), '--state', str(path)]

    def controller(*flags):
        return subprocess.run([*command, *flags], capture_output=True, text=True, timeout=60)

    if controller('--crash-after-sendpay').returncode != 88:
        raise AssertionError('controller missed BTC submission crash point')
    before = path.read_bytes()
    if json.loads(before)['phase'] != 'outgoing_started' or 'preimage' in json.loads(before):
        raise AssertionError('invalid pending controller checkpoint')
    htlc = None
    for node, state in ((outgoing, 'SENT_ADD_ACK_REVOCATION'), (receiver, 'RCVD_ADD_ACK_REVOCATION')):
        held = wait_until(lambda: next((h for h in channel(node).get('htlcs', [])
                                       if h['payment_hash'] == payment_hash and h['state'] == state), None), node['proc'])
        if node is outgoing:
            htlc = held
    original = attempts()
    if len(original) != 1 or original[0]['status'] != 'pending':
        raise AssertionError('expected exactly one pending BTC attempt')
    attempt_id = tuple(original[0].get(k) for k in ('id', 'groupid', 'partid'))
    quote_before = plugin.with_suffix('.quotes.json').read_bytes()
    xbt_height = rpc(xbt, 'getblockcount')

    def assert_xbt_held():
        payments = rpc(payer, 'listsendpays')['payments']
        if len(payments) != 1 or payments[0]['payment_hash'] != payment_hash or payments[0]['status'] != 'pending':
            raise AssertionError('XBT did not remain held during BTC on-chain recovery')
        if (path.read_bytes() != before or plugin.with_suffix('.quotes.json').read_bytes() != quote_before
                or rpc(xbt, 'getblockcount') != xbt_height):
            raise AssertionError('controller, quote or XBT height changed before recovery')

    def assert_pending():
        for _ in range(2):
            result = controller()
            if result.returncode or json.loads(result.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}:
                raise AssertionError('controller resolved XBT before definitive BTC outcome')
            assert_xbt_held()
            if attempts() != original:
                raise AssertionError('controller replaced or changed pending BTC attempt')
        print('PASS: fresh controllers keep XBT held while BTC remains unresolved; no BTC resend', flush=True)

    assert_pending()
    print('On-chain checks: Alice = BTC operator; Bob = BTC receiver', flush=True)
    if timeout:
        if rpc(receiver, 'listinvoices', 'reverse-receive')['invoices'][0]['status'] != 'unpaid':
            raise AssertionError('BTC invoice paid before receiver shutdown')
        lab.stop(receiver['proc'])
        active.remove(receiver)
        recover_timeout(btc, outgoing, receiver, funding, htlc['expiry'], c['our_to_self_delay'],
                        mine, rpc, confirmed_outputs, amount_sat=100000, standalone=False,
                        before_timeout=assert_pending)
    else:
        def reveal():
            assert_pending()
            if rpc(receiver, 'xbt-release', receiver_preimage) != {'released': 1}:
                raise AssertionError('receiver did not release the original BTC hook')
            return receiver_preimage

        run_claim(btc, outgoing, receiver, funding, invoice, None, htlc['expiry'],
                  mine, rpc, confirmed_outputs, amount_sat=100000, standalone=False, release=reveal)
    assert_xbt_held()

    def terminal_btc():
        records = attempts()
        if (len(records) != 1 or tuple(records[0].get(k) for k in ('id', 'groupid', 'partid')) != attempt_id):
            raise AssertionError('original BTC attempt replaced or duplicated')
        record = records[0]
        if timeout:
            if record['status'] == 'complete' or record.get('payment_preimage'):
                raise AssertionError('BTC timeout unexpectedly completed or revealed preimage')
            return records if record['status'] == 'failed' else None
        if record['status'] != 'complete' or record.get('payment_preimage') != receiver_preimage:
            raise AssertionError('BTC operator did not recover the on-chain preimage')
        return records

    terminal = wait_until(terminal_btc, outgoing['proc'], timeout=90)
    print('PASS: original BTC attempt '+('definitively failed after timeout refund' if timeout else
          'completed with preimage extracted on-chain')+'; XBT still held', flush=True)
    # Lose controller progress after the durable XBT outcome, then reconcile
    # twice. No on-chain status is inferred merely from the controller phase.
    result = controller('--crash-after-xbt-resolution')
    if result.returncode != 89:
        raise AssertionError('controller missed durable XBT resolution crash point: '+result.stdout)
    phase = 'xbt_failed' if timeout else 'xbt_released'
    intermediate = json.loads(path.read_text())
    if intermediate['phase'] != ('btc_failed' if timeout else 'btc_paid'):
        raise AssertionError('controller lost original BTC outcome checkpoint')
    if timeout and 'preimage' in intermediate:
        raise AssertionError('timeout checkpoint unexpectedly contains a preimage')
    for _ in range(2):
        result = controller()
        if result.returncode or json.loads(result.stdout) != {'phase': phase}:
            raise AssertionError('controller could not reconcile durable XBT resolution')
    paying.wait(timeout=30)
    if (paying.returncode == 0) == timeout:
        raise AssertionError('unexpected XBT payer outcome')
    if not timeout:
        paid = json.loads(pay_log.read_text())
        if (paid['status'] != 'complete' or paid['payment_preimage'] != receiver_preimage
                or paid['amount_msat'] != 200000000 or paid['amount_sent_msat'] != 200000000):
            raise AssertionError('XBT receipt differs from expected on-chain BTC preimage settlement')
    for node, delta in ((payer, 0 if timeout else -200000000), (incoming, 0 if timeout else 200000000)):
        def settled():
            c = channel(node)
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[node['id']] + delta)
        wait_until(settled, node['proc'])
    payments = rpc(payer, 'listsendpays')['payments']
    if len(payments) != 1 or payments[0]['status'] != ('failed' if timeout else 'complete'):
        raise AssertionError('XBT payer record differs from settled outcome')
    expected = json.loads(quote_before)[payment_hash]
    expected['phase'] = 'failed' if timeout else 'resolved'
    if not timeout:
        expected['preimage'] = receiver_preimage
    if (json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash] != expected
            or rpc(incoming, 'xbt-held')['held'] or attempts() != terminal
            or rpc(xbt, 'getblockcount') != xbt_height):
        raise AssertionError('quote binding changed, XBT still held or BTC history changed')
    print('PASS: release-crash recovery repeated safely; XBT balances '+('restored' if timeout else 'match')+
          '; no pending XBT HTLCs; no additional BTC attempt', flush=True)
    mode = 'timeout refund' if timeout else 'preimage claim'
    print(f'XBT -> BTC outgoing on-chain {mode} test OK (BTC sweep confirmed; BTC fees apply; regtest only)', flush=True)
