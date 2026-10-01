"""Reverse swap XBT claim after BTC completion; harness or controller deadline close."""
import json
from pathlib import Path
import subprocess
import sys

from preimage_claim import run_claim
from smoke_regtest import wait_until
from swap_controller import save


def exercise_onchain(lab, payer, incoming, outgoing, receiver, xbt, btc,
                     invoice, decoded, xbt_invoice, route, quote, plugin,
                     initial, paying, pay_log, deadline=False):
    payment_hash = invoice['payment_hash']

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected exactly one fixture channel')
        return channels[0]

    def mine(count):
        blocks = rpc(xbt, 'generatetoaddress', count, rpc(xbt, 'getnewaddress'))
        height = rpc(xbt, 'getblockcount')
        for node in (payer, incoming):
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height, node['proc'], timeout=90)
        return blocks

    def confirmed_outputs(node, txid):
        return [o for o in rpc(node, 'listfunds')['outputs']
                if o['txid'] == txid and o['status'] == 'confirmed']

    # Onchaind needs wallet inputs to fund anchor HTLC-success fees.
    deposit = rpc(xbt, 'sendtoaddress', rpc(incoming, 'newaddr', 'bech32')['bech32'], '0.01')
    mine(1)
    wait_until(lambda: confirmed_outputs(incoming, deposit), incoming['proc'])
    c = channel(incoming)
    funding = dict(txid=c['funding_txid'], outnum=c['funding_outnum'])
    gate = rpc(incoming, 'reverse-status', payment_hash)
    path = lab.root/'reverse-state.json'
    save(path, dict(profile='reverse-regtest-v1', phase='prepared', durable_gate=True,
                    xbt_onchain_claim=True, xbt_deadline_guard=deadline, reverse_quote=quote,
                    xbt_cli=incoming['cli'], btc_cli=outgoing['cli'],
                    node_ids=[incoming['id'], outgoing['id']], payment_hash=payment_hash,
                    xbt_binding=gate['binding'], xbt_expiry=gate['cltv_expiry'],
                    xbt_amount_msat=200000000, btc_amount_msat=100000000,
                    btc_invoice=invoice['bolt11'], btc_secret=decoded['payment_secret'], route=route))
    command = [sys.executable, str(Path(__file__).with_name('reverse_controller.py')), '--state', str(path)]

    def controller(*flags):
        return subprocess.run([*command, *flags], capture_output=True, text=True, timeout=60)

    if controller('--crash-after-sendpay').returncode != 88:
        raise AssertionError('controller missed submission crash point')
    before = path.read_bytes()
    saved = json.loads(before)
    if (saved['phase'] != 'outgoing_started' or 'preimage' in saved
            or saved['incoming_channel']['channel_id'] != c['channel_id']):
        raise AssertionError('missing pending checkpoint or original channel pin')
    for node, state in ((outgoing, 'SENT_ADD_ACK_REVOCATION'), (receiver, 'RCVD_ADD_ACK_REVOCATION')):
        wait_until(lambda: any(h['payment_hash'] == payment_hash and h['state'] == state
                              for h in channel(node).get('htlcs', [])), node['proc'])
    original = rpc(outgoing, 'listsendpays')['payments']
    if len(original) != 1 or original[0]['status'] != 'pending':
        raise AssertionError('expected original pending BTC attempt')
    attempt_id = tuple(original[0].get(k) for k in ('id', 'groupid', 'partid'))
    quote_before = plugin.with_suffix('.quotes.json').read_bytes()
    btc_height = rpc(btc, 'getblockcount')

    def assert_pending():
        result = controller()
        if (result.returncode or json.loads(result.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}
                or path.read_bytes() != before or plugin.with_suffix('.quotes.json').read_bytes() != quote_before
                or rpc(outgoing, 'listsendpays')['payments'] != original):
            raise AssertionError('pending on-chain reconciliation changed payment or quote')

    assert_pending()
    print('PASS: controller crashed with both HTLCs pending; original XBT channel pinned before BTC send', flush=True)

    def release_after_commitment():
        assert_pending()
        if channel(incoming)['state'] != 'ONCHAIN':
            raise AssertionError('XBT channel not on-chain before BTC completion')
        print('PASS: XBT commitment confirmed while BTC remains pending; controller has no preimage', flush=True)
        if rpc(receiver, 'xbt-continue', payment_hash) != {'continued': 1}:
            raise AssertionError('BTC receiver did not resume original HTLC')
        completed = rpc(outgoing, 'waitsendpay', payment_hash, 10)
        if completed['status'] != 'complete':
            raise AssertionError('original BTC payment did not complete')
        # Exercise lost controller progress after the gate has durably released
        # the hook to onchaind, before the controller terminal checkpoint.
        crashed = controller('--crash-after-xbt-resolution')
        if crashed.returncode != 89:
            raise AssertionError('controller did not release pinned on-chain XBT hook: '+crashed.stdout)
        checkpoint = json.loads(path.read_text())
        if checkpoint['phase'] != 'btc_paid' or checkpoint['preimage'] != completed['payment_preimage']:
            raise AssertionError('BTC completion checkpoint missing after release crash')
        for _ in range(2):
            result = controller()
            if result.returncode or json.loads(result.stdout) != {'phase': 'xbt_released'}:
                raise AssertionError('on-chain XBT release reconciliation failed')
        print('PASS: BTC preimage recovered; bound XBT hook released on-chain; release-crash recovery repeated safely', flush=True)
        return completed['payment_preimage']

    close = None
    if deadline:
        remaining = gate['cltv_expiry'] - rpc(xbt, 'getblockcount')
        if remaining <= 31:
            raise AssertionError('insufficient starting XBT deadline margin')
        mine(remaining - 31)
        assert_pending()
        if channel(incoming)['state'] != 'CHANNELD_NORMAL':
            raise AssertionError('XBT channel closed before threshold')
        print('PASS: controller keeps XBT channel open at 31 blocks remaining; BTC still pending', flush=True)
        mine(1)
        if gate['cltv_expiry'] - rpc(xbt, 'getblockcount') != 30:
            raise AssertionError('incorrect XBT deadline height')
        result = controller()
        if result.returncode or json.loads(result.stdout) != {'phase': 'outgoing_started', 'outcome': 'pending'}:
            raise AssertionError('XBT deadline controller failed: '+result.stdout)
        protected = json.loads(path.read_text())
        if (protected['xbt_close_intent']['channel'] != saved['incoming_channel']
                or protected['xbt_close_result']['type'] != 'unilateral' or 'preimage' in protected):
            raise AssertionError('deadline did not close original XBT channel without preimage')
        close = protected['xbt_close_result']
        wait_until(lambda: channel(incoming)['state'] == 'AWAITING_UNILATERAL', incoming['proc'])
        before = path.read_bytes()
        assert_pending()
        if rpc(btc, 'getblockcount') != btc_height:
            raise AssertionError('BTC advanced while testing XBT deadline')
        print('PASS: controller closed pinned XBT channel at 30 blocks; fresh recovery preserved both payments and close intent', flush=True)
    print('On-chain checks: Alice = XBT payer; Bob = XBT operator', flush=True)
    claim = run_claim(xbt, payer, incoming, funding,
                      dict(bolt11=xbt_invoice, payment_hash=payment_hash), None,
                      gate['cltv_expiry'], mine, rpc, confirmed_outputs,
                      amount_sat=200000, standalone=False, release=release_after_commitment, close=close)
    paying.wait(timeout=30)
    paid = json.loads(pay_log.read_text())
    if (paying.returncode or paid['status'] != 'complete'
            or paid['payment_preimage'] != claim['payment_preimage']
            or paid['amount_msat'] != 200000000 or paid['amount_sent_msat'] != 200000000):
        raise AssertionError('XBT payer did not settle with on-chain preimage')
    for node, delta in ((outgoing, -100000000), (receiver, 100000000)):
        def settled():
            c = channel(node)
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[node['id']] + delta)
        wait_until(settled, node['proc'])
    received = rpc(receiver, 'listinvoices', 'reverse-receive')['invoices'][0]
    if received['status'] != 'paid' or received['amount_received_msat'] != 100000000:
        raise AssertionError('BTC invoice not paid in full')
    attempts = rpc(outgoing, 'listsendpays')['payments']
    if (len(attempts) != 1 or attempts[0]['status'] != 'complete'
            or tuple(attempts[0].get(k) for k in ('id', 'groupid', 'partid')) != attempt_id):
        raise AssertionError('original BTC attempt replaced or duplicated')
    expected = json.loads(quote_before)[payment_hash]
    expected.update(phase='resolved', preimage=claim['payment_preimage'])
    if (json.loads(plugin.with_suffix('.quotes.json').read_text())[payment_hash] != expected
            or rpc(incoming, 'xbt-held')['held'] or rpc(btc, 'getblockcount') != btc_height):
        raise AssertionError('quote changed, hook still held or BTC chain advanced')
    checkpoint = path.read_bytes()
    result = controller()
    if (result.returncode or json.loads(result.stdout) != {'phase': 'xbt_released'}
            or path.read_bytes() != checkpoint or rpc(outgoing, 'listsendpays')['payments'] != attempts):
        raise AssertionError('post-sweep recovery changed controller state or BTC history')
    print('PASS: BTC receiver paid; XBT operator claim and CSV sweep confirmed; original BTC attempt only', flush=True)
    trigger = 'controller deadline' if deadline else 'harness'
    print(f'XBT -> BTC on-chain claim test OK ({trigger}-triggered XBT close; XBT fees apply; regtest only)', flush=True)
