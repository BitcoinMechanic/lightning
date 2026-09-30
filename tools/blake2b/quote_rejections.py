"""Live quote-gate rejection cases, sharing one disposable pair of channels.

Direct sendpay deliberately supplies invalid final-hop terms without wallet
invoice checks or randomized routing. No outgoing swap controller is launched:
this tests the acceptance boundary, not autonomous controller spend policy.
"""
import json
import secrets
import time

from smoke_regtest import wait_until


CASES = (
    ('wrong-secret', 'payment secret mismatch'),
    ('wrong-amount', 'incoming amount mismatch'),
    ('expired-quote', 'quote expired'),
    ('short-cltv', 'incoming CLTV outside quote bounds'),
)


def run_rejections(lab, payer, swap_btc, swap_xbt, receiver, plugin, initial):
    nodes = (payer, swap_btc, swap_xbt, receiver)

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected exactly one channel')
        return channels[0]

    def no_xbt_attempt():
        if rpc(swap_xbt, 'listsendpays')['payments']:
            raise AssertionError('quote rejection created an XBT payment attempt')

    for name, expected_reason in CASES:
        label = 'reject-' + name
        invoice = rpc(receiver, 'invoice', '200000000msat', label, 'Quote rejection test')
        payment_hash = invoice['payment_hash']
        secret = secrets.token_hex(32)
        quote = {'payment_hash': payment_hash, 'payment_secret': secret,
                 'btc_amount_msat': 100000000, 'xbt_amount_msat': 200000000,
                 'xbt_invoice': invoice['bolt11'],
                 'expires_at': int(time.time()) + (5 if name == 'expired-quote' else 3600),
                 'min_cltv_delta': 100, 'max_cltv_delta': 2000}
        if not rpc(swap_btc, 'xbt-register', json.dumps(quote))['registered']:
            raise AssertionError('quote registration failed')
        statefile = plugin.with_suffix('.quotes.json')
        expected_entry = {'terms': quote, 'phase': 'quoted'}
        if json.loads(statefile.read_text())[payment_hash] != expected_entry:
            raise AssertionError('registered quote not persisted unchanged')
        if name == 'expired-quote':
            # Register while valid, then let real wall time expire it. No disk
            # mutation or test-only clock override in the plugin.
            wait_until(lambda: int(time.time()) >= quote['expires_at'], timeout=10)

        amount = 99999000 if name == 'wrong-amount' else 100000000
        delay = 40 if name == 'short-cltv' else 120
        if name == 'wrong-secret':
            secret = ('00' if secret[:2] != '00' else '01') + secret[2:]
        route = [{'id': swap_btc['id'], 'channel': channel(payer)['short_channel_id'],
                  'amount_msat': amount, 'delay': delay}]
        log_offset = swap_btc['log'].stat().st_size
        lab.rpc([*payer['cli'], '-k'], 'sendpay', 'route=' + json.dumps(route),
                'payment_hash=' + payment_hash, 'payment_secret=' + secret)

        def rejected():
            no_xbt_attempt()
            if rpc(swap_btc, 'xbt-held')['held']:
                raise AssertionError(name + ': invalid quote became eligible for spending')
            if json.loads(statefile.read_text())[payment_hash] != expected_entry:
                raise AssertionError(name + ': rejected HTLC changed or bound the quote')
            payments = [p for p in rpc(payer, 'listsendpays')['payments']
                        if p['payment_hash'] == payment_hash]
            if len(payments) != 1:
                raise AssertionError(name + ': expected exactly one BTC payment attempt')
            payment = payments[0]
            if payment['status'] == 'complete' or payment.get('payment_preimage'):
                raise AssertionError(name + ': invalid BTC payment settled')
            return payment['status'] == 'failed'

        wait_until(rejected, payer['proc'])
        # A failed payment alone could mean a route/channel error. Require the
        # operator's hook to have rejected this attempt for the intended reason.
        def reason_logged():
            with swap_btc['log'].open('rb') as log:
                log.seek(log_offset)
                return ('Quote gate rejected HTLC: ' + expected_reason) in log.read().decode(errors='replace')

        wait_until(reason_logged, swap_btc['proc'])
        for node in nodes:
            def unchanged():
                c = channel(node)
                return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                        and c['to_us_msat'] == initial[node['id']])
            wait_until(unchanged, node['proc'])
        no_xbt_attempt()
        if rpc(receiver, 'listinvoices', label)['invoices'][0]['status'] != 'unpaid':
            raise AssertionError(name + ': XBT invoice did not remain unpaid')
        print(f'PASS: {name}: quote gate rejected BTC; no XBT attempt; '
              'no pending HTLCs; all four balances unchanged', flush=True)

    print('BTC -> XBT quote rejection tests OK (four cases; regtest only)', flush=True)
