"""Held HTLC success transaction and preimage extraction test on XBT regtest."""
from decimal import Decimal
import hashlib

from smoke_regtest import wait_until


def run_claim(backend, alice, bob, funding, invoice, preimage, expiry,
              mine, rpc, confirmed_outputs):
    if hashlib.sha256(bytes.fromhex(preimage)).hexdigest() != invoice['payment_hash']:
        raise AssertionError('invoice does not use the chosen preimage')
    delay = rpc(bob, 'listpeerchannels')['channels'][0]['our_to_self_delay']
    if not 1 <= delay <= 2016:
        raise AssertionError(f'unexpected CSV delay {delay}')
    # An unresolved HTLC prevents mutual closing. Bob publishes his commitment.
    close = rpc(bob, 'close', alice['id'], 1)
    if close['type'] != 'unilateral':
        raise AssertionError(close)
    wait_until(lambda: set(close['txids']).intersection(rpc(backend, 'getrawmempool')))

    def spends(tx, txid, n):
        return any(v.get('txid') == txid and v.get('vout') == n for v in tx['vin'])

    block = rpc(backend, 'getblock', mine(1)[0], 2)
    commits = [t for t in block['tx'] if t['txid'] in close['txids']
               and spends(t, funding['txid'], funding['outnum'])]
    if len(commits) != 1:
        raise AssertionError('no unique confirmed Bob commitment')
    commit = commits[0]
    outputs = [o for o in commit['vout'] if Decimal(str(o['value'])) == Decimal('0.001')
               and o['scriptPubKey']['type'] == 'witness_v0_scripthash']
    if len(outputs) != 1:
        raise AssertionError('cannot identify held HTLC output')
    n = outputs[0]['n']
    for node in (alice, bob):
        wait_until(lambda: any(c['state'] == 'ONCHAIN' for c in
                   rpc(node, 'listpeerchannels')['channels']), node['proc'])
    payments = rpc(alice, 'listsendpays', invoice['bolt11'])['payments']
    if len(payments) != 1 or payments[0]['status'] != 'pending':
        raise AssertionError('payment not pending before on-chain preimage reveal')
    print('PASS: Bob commitment confirmed with unresolved HTLC; Alice payment still pending', flush=True)
    log_offset = alice['log'].stat().st_size
    if rpc(bob, 'xbt-release', preimage)['released'] != 1:
        raise AssertionError('expected exactly one held HTLC release')

    def mempool_spend(txid, index):
        for candidate in rpc(backend, 'getrawmempool'):
            tx = rpc(backend, 'getrawtransaction', candidate, 'true')
            if spends(tx, txid, index):
                return tx
        return None

    wait_until(lambda: mempool_spend(commit['txid'], n), bob['proc'], timeout=90)
    block = rpc(backend, 'getblock', mine(1)[0], 2)
    successes = [t for t in block['tx'] if spends(t, commit['txid'], n)]
    if len(successes) != 1 or block['height'] >= expiry:
        raise AssertionError('HTLC-success transaction did not confirm before expiry')
    success = successes[0]
    vin = next(v for v in success['vin'] if v.get('txid') == commit['txid'] and v.get('vout') == n)
    if preimage not in vin.get('txinwitness', []):
        raise AssertionError('confirmed HTLC witness does not reveal the expected preimage')
    payment = rpc(alice, 'waitsendpay', invoice['payment_hash'], 10)
    if payment['status'] != 'complete' or payment['payment_preimage'] != preimage:
        raise AssertionError('Alice did not settle with the on-chain preimage')

    def learned_onchain():
        with alice['log'].open('rb') as log:
            log.seek(log_offset)
            fresh = log.read().decode(errors='replace')
        return 'THEIR_UNILATERAL/OUR_HTLC gave us preimage' in fresh

    wait_until(learned_onchain, alice['proc'])
    print('PASS: confirmed witness reveals preimage; Alice learns it on-chain and settles payment', flush=True)
    delayed = [o for o in success['vout']
               if o['scriptPubKey']['type'] == 'witness_v0_scripthash'
               and Decimal('0.0008') < Decimal(str(o['value'])) <= Decimal('0.001')]
    if len(delayed) != 1:
        raise AssertionError('cannot identify Bob delayed HTLC-success output')
    index = delayed[0]['n']
    success_height = block['height']
    if delay > 2:
        mine(delay - 2)
    if not rpc(backend, 'gettxout', success['txid'], index, 'false'):
        raise AssertionError('success output spent before CSV maturity')
    mine(2)
    sweep = wait_until(lambda: mempool_spend(success['txid'], index), bob['proc'], timeout=90)
    vin = next(v for v in sweep['vin'] if v.get('txid') == success['txid'] and v.get('vout') == index)
    if vin['sequence'] & ((1 << 31) | (1 << 22)) or (vin['sequence'] & 0xffff) < delay:
        raise AssertionError('success sweep lacks expected CSV delay')
    block = rpc(backend, 'getblock', mine(1)[0], 2)
    sweeps = [t for t in block['tx'] if spends(t, success['txid'], index)]
    if len(sweeps) != 1 or block['height'] - success_height < delay:
        raise AssertionError('Bob recovery did not confirm after CSV maturity')
    outputs = wait_until(lambda: confirmed_outputs(bob, sweeps[0]['txid']), bob['proc'])
    for output in outputs:
        utxo = rpc(backend, 'gettxout', sweeps[0]['txid'], output['output'])
        if not utxo or utxo['confirmations'] < 1:
            raise AssertionError('Bob recovery output not confirmed and unspent')
    for txid, outnum in ((commit['txid'], n), (success['txid'], index)):
        if rpc(backend, 'gettxout', txid, outnum) is not None:
            raise AssertionError('claimed HTLC ancestor remains unspent')
    print('PASS: Bob sweeps HTLC-success output after CSV delay into confirmed wallet funds', flush=True)
    print('XBT preimage-claim test OK (preimage learned on-chain; regtest coins only)', flush=True)
