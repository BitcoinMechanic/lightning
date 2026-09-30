"""Unresolved outgoing HTLC timeout and delayed sweep checks for funded_regtest."""
from decimal import Decimal
import json
from pathlib import Path
import sys
import secrets

from smoke_regtest import wait_until


def run_timeout(lab, backend, alice, bob, funding, mine, active_nodes, rpc,
                confirmed_outputs, preimage_claim=False):
    plugin = lab.root / 'hold_htlc.py'
    plugin.write_text(f'#!{sys.executable}\n' +
                      Path(__file__).with_name('hold_htlc.py').read_text())
    plugin.chmod(0o700)
    rpc(bob, 'plugin', 'start', plugin)
    preimage = None
    if preimage_claim:
        # Anchor HTLC-success transactions may need wallet inputs for fees.
        deposit = rpc(backend, 'sendtoaddress', rpc(bob, 'newaddr', 'bech32')['bech32'], '0.01')
        mine(1)
        wait_until(lambda: confirmed_outputs(bob, deposit), bob['proc'])
        preimage = secrets.token_hex(32)
        invoice = lab.rpc([*bob['cli'], '-k'], 'invoice', 'amount_msat=100000000',
                          'label=held-smoke', 'description=XBT onchain preimage claim',
                          'preimage=' + preimage)
    else:
        invoice = rpc(bob, 'invoice', '100000000msat', 'held-smoke', 'XBT HTLC timeout')
    channel = rpc(alice, 'listpeerchannels')['channels'][0]
    delay = channel['our_to_self_delay']
    if not 1 <= delay <= 2016:
        raise AssertionError(f'unexpected CSV delay {delay}')
    route = [{'id': bob['id'], 'channel': channel['short_channel_id'],
              'amount_msat': 100000000, 'delay': 40}]
    # sendpay returns while the HTLC is pending; pay would wait for settlement.
    lab.rpc([*alice['cli'], '-k'], 'sendpay', 'route=' + json.dumps(route),
            'payment_hash=' + invoice['payment_hash'],
            'payment_secret=' + invoice['payment_secret'])
    wait_until(lambda: any(h['payment_hash'] == invoice['payment_hash']
                          for h in rpc(bob, 'xbt-held')['held']), bob['proc'])

    def committed(node, state):
        htlcs = rpc(node, 'listpeerchannels')['channels'][0].get('htlcs', [])
        return next((h for h in htlcs if h['payment_hash'] == invoice['payment_hash']
                     and h['state'] == state), None)

    htlc = wait_until(lambda: committed(alice, 'SENT_ADD_ACK_REVOCATION'), alice['proc'])
    wait_until(lambda: committed(bob, 'RCVD_ADD_ACK_REVOCATION'), bob['proc'])
    expiry = htlc['expiry']
    if rpc(bob, 'listinvoices', 'held-smoke')['invoices'][0]['status'] != 'unpaid':
        raise AssertionError('held invoice unexpectedly paid')
    print(f'PASS: 100,000-sat HTLC committed at both ends and held; expiry {expiry}', flush=True)
    if preimage_claim:
        from preimage_claim import run_claim
        run_claim(backend, alice, bob, funding, invoice, preimage, expiry,
                  mine, rpc, confirmed_outputs)
        return
    lab.stop(bob['proc'])
    active_nodes.remove(bob)
    recover_timeout(backend, alice, bob, funding, expiry, delay, mine, rpc, confirmed_outputs)


def recover_timeout(backend, alice, bob, funding, expiry, delay, mine, rpc,
                    confirmed_outputs, amount_sat=100000, standalone=True,
                    before_timeout=None):
    """Recover a committed outgoing HTLC with the receiver already offline."""
    if not 1 <= delay <= 2016:
        raise AssertionError(f'unexpected CSV delay {delay}')
    amount_btc = Decimal(amount_sat) / Decimal(100000000)
    close = rpc(alice, 'close', bob['id'], 1)
    if close['type'] != 'unilateral':
        raise AssertionError(close)
    wait_until(lambda: set(close['txids']).intersection(rpc(backend, 'getrawmempool')))

    def spends(tx, txid, outnum):
        return any(v.get('txid') == txid and v.get('vout') == outnum for v in tx['vin'])

    block = rpc(backend, 'getblock', mine(1)[0], 2)
    commitments = [tx for tx in block['tx'] if tx['txid'] in close['txids']
                   and spends(tx, funding['txid'], funding['outnum'])]
    if len(commitments) != 1:
        raise AssertionError('no unique confirmed commitment')
    commitment = commitments[0]
    outputs = [o for o in commitment['vout'] if Decimal(str(o['value'])) == amount_btc
               and o['scriptPubKey']['type'] == 'witness_v0_scripthash']
    if len(outputs) != 1:
        raise AssertionError(f'cannot identify untrimmed {amount_sat}-sat HTLC output')
    outnum = outputs[0]['n']
    height = rpc(backend, 'getblockcount')
    if height >= expiry:
        raise AssertionError('HTLC already expired before pre-expiry check')
    if expiry - 1 > height:
        mine(expiry - 1 - height)
    if not rpc(backend, 'gettxout', commitment['txid'], outnum, 'false'):
        raise AssertionError('HTLC output spent before expiry')
    print('PASS: commitment confirmed; HTLC output remains unspent before expiry', flush=True)
    if before_timeout is not None:
        before_timeout()
    mine(2)

    def mempool_spend(txid, index):
        for candidate in rpc(backend, 'getrawmempool'):
            tx = rpc(backend, 'getrawtransaction', candidate, 'true')
            if spends(tx, txid, index):
                return tx
        return None

    timeout_tx = wait_until(lambda: mempool_spend(commitment['txid'], outnum),
                            alice['proc'], timeout=90)
    if not expiry <= timeout_tx['locktime'] < 500000000:
        raise AssertionError('HTLC spend lacks expected height locktime')
    block = rpc(backend, 'getblock', mine(1)[0], 2)
    mined = [tx for tx in block['tx'] if spends(tx, commitment['txid'], outnum)]
    if len(mined) != 1 or mined[0]['locktime'] < expiry or block['height'] <= expiry:
        raise AssertionError('HTLC timeout did not confirm after expiry')
    timeout_tx = mined[0]
    timeout_height = block['height']
    delayed = [o for o in timeout_tx['vout']
               if o['scriptPubKey']['type'] == 'witness_v0_scripthash'
               and amount_btc * Decimal('0.8') < Decimal(str(o['value'])) <= amount_btc]
    if len(delayed) != 1:
        raise AssertionError('cannot identify HTLC timeout delayed output')
    index = delayed[0]['n']
    if delay > 2:
        mine(delay - 2)
    if not rpc(backend, 'gettxout', timeout_tx['txid'], index, 'false'):
        raise AssertionError('HTLC timeout output spent before CSV maturity')
    print('PASS: HTLC timeout mined after expiry; refund still subject to CSV delay', flush=True)
    mine(2)
    sweep = wait_until(lambda: mempool_spend(timeout_tx['txid'], index),
                       alice['proc'], timeout=90)
    vin = next(v for v in sweep['vin'] if v.get('txid') == timeout_tx['txid']
               and v.get('vout') == index)
    if vin['sequence'] & ((1 << 31) | (1 << 22)) or (vin['sequence'] & 0xffff) < delay:
        raise AssertionError('HTLC sweep lacks expected block-based CSV delay')
    block = rpc(backend, 'getblock', mine(1)[0], 2)
    mined = [tx for tx in block['tx'] if spends(tx, timeout_tx['txid'], index)]
    if len(mined) != 1 or block['height'] - timeout_height < delay:
        raise AssertionError('HTLC recovery not confirmed after CSV maturity')
    recovered = wait_until(lambda: confirmed_outputs(alice, mined[0]['txid']), alice['proc'])
    for output in recovered:
        utxo = rpc(backend, 'gettxout', mined[0]['txid'], output['output'])
        if not utxo or utxo['confirmations'] < 1:
            raise AssertionError('HTLC recovery wallet output not confirmed and unspent')
    for txid, n in ((commitment['txid'], outnum), (timeout_tx['txid'], index)):
        if rpc(backend, 'gettxout', txid, n) is not None:
            raise AssertionError('recovered HTLC ancestor remains unspent')
    print('PASS: HTLC refund swept after CSV delay; Alice wallet output confirmed and unspent', flush=True)
    if standalone:
        print('XBT HTLC-timeout test OK (Bob offline; regtest coins only)', flush=True)
    return {'timeout_txid': timeout_tx['txid'], 'refund_sweep_txid': mined[0]['txid']}
