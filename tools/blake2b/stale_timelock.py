"""Advance only BTC between quote acceptance and first XBT submission."""
import json
from pathlib import Path
import subprocess
import sys

from smoke_regtest import wait_until
from swap_controller import check_spend, save


def run_stale(lab, payer, swap_btc, swap_xbt, receiver, btc, invoice,
              binding, quote, mine, initial, pay_process):
    payment_hash = invoice['payment_hash']

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    state = {'phase': 'prepared', 'payment_hash': payment_hash,
             'payment_secret': invoice['payment_secret'], 'quote_gate': True,
             'xbt_invoice': invoice['bolt11'],
             'btc_binding': binding, 'xbt_amount_msat': 200000000,
             'xbt_cli': swap_xbt['cli'], 'btc_cli': swap_btc['cli'],
             'route': [{'id': receiver['id'],
                        'channel': rpc(swap_xbt, 'listpeerchannels')['channels'][0]['short_channel_id'],
                        'amount_msat': 200000000, 'delay': 40}]}
    if check_spend(state) is not None:
        raise AssertionError('fresh accepted quote unexpectedly refused')
    info = rpc(swap_btc, 'xbt-spend-info', payment_hash)
    remaining = info['cltv_expiry'] - rpc(swap_btc, 'getinfo')['blockheight']
    xbt_height = rpc(swap_xbt, 'getinfo')['blockheight']
    # Leave 99 BTC blocks: below quote policy, far from actual HTLC timeout.
    count = remaining - quote['min_cltv_delta'] + 1
    if count <= 0:
        raise AssertionError('quote was already stale before mining')
    mine(btc, (payer, swap_btc), count)
    if rpc(swap_xbt, 'getinfo')['blockheight'] != xbt_height:
        raise AssertionError('XBT advanced in BTC-only timelock test')
    if info['cltv_expiry'] - rpc(swap_btc, 'getinfo')['blockheight'] != quote['min_cltv_delta'] - 1:
        raise AssertionError('unexpected remaining BTC margin')
    print('PASS: accepted quote initially met policy; BTC advanced to 99 blocks remaining; XBT did not advance',
          flush=True)
    path = lab.root / 'swap-state.json'
    save(path, state)
    before = path.read_bytes()
    for attempt in range(2):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('swap_controller.py')),
                                 '--state', str(path)], text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError(f'controller failed: {result.stdout}\n{result.stderr}')
        if json.loads(result.stdout) != {'phase': 'prepared', 'outcome': 'refused',
                                        'reason': 'insufficient_btc_cltv'}:
            raise AssertionError('controller did not refuse insufficient BTC margin')
        if path.read_bytes() != before or rpc(swap_xbt, 'listsendpays')['payments']:
            raise AssertionError('refused controller changed state or attempted XBT')
        payments = rpc(payer, 'listsendpays')['payments']
        if len(payments) != 1 or payments[0]['status'] != 'pending':
            raise AssertionError('refusal did not preserve pending BTC')
        held = rpc(swap_btc, 'xbt-held')['held']
        if len(held) != 1 or held[0]['payment_hash'] != payment_hash:
            raise AssertionError('BTC no longer held after refusal')
    print('PASS: two fresh controllers refused stale margin; no XBT attempt; BTC remained held', flush=True)
    # Test cleanup only, after proving no outgoing attempt occurred. Refusal
    # itself neither settles nor fails an HTLC with a potentially unknown outcome.
    if rpc(swap_btc, 'xbt-fail', payment_hash, json.dumps(binding))['failed'] != 1:
        raise AssertionError('cleanup did not fail bound BTC HTLC')
    wait_until(lambda: rpc(payer, 'listsendpays')['payments'][0]['status'] == 'failed', payer['proc'])
    pay_process.wait(timeout=30)
    if pay_process.returncode == 0:
        raise AssertionError('BTC pay succeeded despite refusal')
    for node in (payer, swap_btc, swap_xbt, receiver):
        def restored():
            c = rpc(node, 'listpeerchannels')['channels'][0]
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[node['id']])
        wait_until(restored, node['proc'])
    if rpc(receiver, 'listinvoices', 'swap-receive')['invoices'][0]['status'] != 'unpaid':
        raise AssertionError('XBT invoice unexpectedly paid')
    if rpc(swap_xbt, 'listsendpays')['payments']:
        raise AssertionError('unexpected XBT attempt after cleanup')
    print('PASS: harness failed unspent BTC HTLC; no pending HTLCs; all four balances restored', flush=True)
    print('BTC pre-spend timelock test OK (BTC-only block advancement; regtest only)', flush=True)
