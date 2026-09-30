#!/usr/bin/env python3
"""Disposable XBT channel/payment/close test; standard library only."""
import argparse
from decimal import Decimal
from pathlib import Path
import subprocess
import tempfile

from smoke_regtest import Lab, wait_until


def run(lab, force_close=False, htlc_timeout=False, preimage_claim=False):
    backend = lab.node('knots-xbt', True)
    alice = lab.lightning('alice', 'xbt-regtest', backend)
    bob = lab.lightning('bob', 'xbt-regtest', backend)
    active_nodes = [alice, bob]

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    mining_address = rpc(backend, 'getnewaddress')

    def mine(count):
        blocks = rpc(backend, 'generatetoaddress', count, mining_address)
        height = rpc(backend, 'getblockcount')
        for node in active_nodes:
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height,
                       node['proc'], timeout=90)
        return blocks

    def confirmed_outputs(node, txid):
        return [o for o in rpc(node, 'listfunds')['outputs']
                if o['txid'] == txid and o['status'] == 'confirmed']

    mine(1)
    address = rpc(alice, 'newaddr', 'bech32')['bech32']
    deposit = rpc(backend, 'sendtoaddress', address, '0.02')
    mine(1)
    wait_until(lambda: confirmed_outputs(alice, deposit), alice['proc'])
    print('PASS: Alice received confirmed XBT test coins', flush=True)

    rpc(alice, 'connect', bob['id'], '127.0.0.1', bob['port'])
    funding = rpc(alice, 'fundchannel', bob['id'], '1000000sat')
    wait_until(lambda: funding['txid'] in rpc(backend, 'getrawmempool'))
    mine(6)
    for node in (alice, bob):
        wait_until(lambda: any(
            c['funding_txid'] == funding['txid'] and c['state'] == 'CHANNELD_NORMAL'
            for c in rpc(node, 'listpeerchannels')['channels']), node['proc'])
    if not rpc(backend, 'gettxout', funding['txid'], funding['outnum']):
        raise AssertionError('funding output is not unspent')
    print('PASS: 1,000,000-sat XBT channel confirmed and ready at both ends', flush=True)

    if htlc_timeout or preimage_claim:
        from htlc_timeout import run_timeout
        run_timeout(lab, backend, alice, bob, funding, mine, active_nodes, rpc,
                    confirmed_outputs, preimage_claim)
        return

    invoice = rpc(bob, 'invoice', '100000000msat', 'funded-smoke',
                  'XBT funded channel test')
    if not invoice['bolt11'].startswith('lnxbtrt'):
        raise AssertionError('unexpected invoice prefix')
    payment = rpc(alice, 'pay', invoice['bolt11'])
    if payment['status'] != 'complete' or payment['payment_hash'] != invoice['payment_hash']:
        raise AssertionError(payment)
    received = rpc(bob, 'listinvoices', 'funded-smoke')['invoices']
    if len(received) != 1 or received[0]['status'] != 'paid':
        raise AssertionError(received)
    if received[0]['amount_received_msat'] != 100000000:
        raise AssertionError(received)
    print('PASS: Alice paid 100,000 XBT sats; Bob marked the invoice paid', flush=True)

    if force_close:
        # Payment completion can precede the last commitment acknowledgement.
        for node in active_nodes:
            wait_until(lambda: all(not c.get('htlcs') for c in
                       rpc(node, 'listpeerchannels')['channels']), node['proc'])
        channel = rpc(alice, 'listpeerchannels')['channels'][0]
        delay = channel['our_to_self_delay']
        if not 1 <= delay <= 2016:
            raise AssertionError(f'unexpected regtest CSV delay: {delay}')
        lab.stop(bob['proc'])
        active_nodes.remove(bob)
        print(f'PASS: Bob stopped; Alice must wait {delay} blocks to recover', flush=True)
        close = rpc(alice, 'close', bob['id'], 1)
        if close['type'] != 'unilateral' or not close['txids']:
            raise AssertionError(close)
        close_ids = set(close['txids'])
        wait_until(lambda: bool(close_ids.intersection(rpc(backend, 'getrawmempool'))))
        blocks = mine(1)
        block = rpc(backend, 'getblock', blocks[0], 2)
        commitments = [tx for tx in block['tx'] if tx['txid'] in close_ids and any(
            vin.get('txid') == funding['txid'] and vin.get('vout') == funding['outnum']
            for vin in tx['vin'])]
        if len(commitments) != 1:
            raise AssertionError('no unique confirmed commitment spending funding output')
        commitment = commitments[0]
        close_height = block['height']
        # In this fixture Alice owns ~900k sats, Bob 100k, anchors 330 each.
        # Select the large P2WSH output, then verify its actual sweep below.
        delayed = [o for o in commitment['vout']
                   if Decimal('0.008') < Decimal(str(o['value'])) < Decimal('0.009')
                   and o['scriptPubKey']['type'] == 'witness_v0_scripthash']
        if len(delayed) != 1:
            raise AssertionError('cannot identify Alice delayed commitment output')
        outnum = delayed[0]['n']
        if rpc(backend, 'gettxout', funding['txid'], funding['outnum']) is not None:
            raise AssertionError('funding output still unspent')
        # At depth delay-1, the next block is still too early for this CSV spend.
        if delay > 2:
            mine(delay - 2)
        if not rpc(backend, 'gettxout', commitment['txid'], outnum, 'false'):
            raise AssertionError('delayed output spent before maturity')
        print('PASS: unilateral commitment mined; delayed output remains unspent before maturity', flush=True)
        mine(2)

        def find_sweep():
            for txid in rpc(backend, 'getrawmempool'):
                tx = rpc(backend, 'getrawtransaction', txid, 'true')
                for vin in tx['vin']:
                    if vin.get('txid') == commitment['txid'] and vin.get('vout') == outnum:
                        sequence = vin['sequence']
                        if sequence & ((1 << 31) | (1 << 22)) or (sequence & 0xffff) < delay:
                            raise AssertionError('sweep does not use expected block-based CSV delay')
                        return txid
            return None

        sweep_id = wait_until(find_sweep, alice['proc'], timeout=90)
        blocks = mine(1)
        sweep_block = rpc(backend, 'getblock', blocks[0], 2)
        if sweep_id not in [tx['txid'] for tx in sweep_block['tx']]:
            raise AssertionError('recovery transaction not mined')
        if sweep_block['height'] - close_height < delay:
            raise AssertionError('recovery confirmed before CSV maturity')
        outputs = wait_until(lambda: confirmed_outputs(alice, sweep_id), alice['proc'])
        for output in outputs:
            utxo = rpc(backend, 'gettxout', sweep_id, output['output'])
            if not utxo or utxo['confirmations'] < 1:
                raise AssertionError('recovery output not confirmed and unspent')
        if rpc(backend, 'gettxout', commitment['txid'], outnum) is not None:
            raise AssertionError('delayed output still unspent after recovery')
        print('PASS: Alice recovery transaction mined after CSV delay; wallet output confirmed and unspent', flush=True)
        print('XBT forced-close test OK (Bob offline; no pending HTLCs; regtest coins only)', flush=True)
        return

    # Disable the unilateral fallback: this test must exercise mutual close.
    close = rpc(alice, 'close', bob['id'], 0)
    if close['type'] != 'mutual' or not close['txids']:
        raise AssertionError(close)
    close_ids = set(close['txids'])
    wait_until(lambda: bool(close_ids.intersection(rpc(backend, 'getrawmempool'))))
    blocks = mine(1)
    block = rpc(backend, 'getblock', blocks[0], 2)
    closing = [tx for tx in block['tx'] if tx['txid'] in close_ids and any(
        vin.get('txid') == funding['txid'] and vin.get('vout') == funding['outnum']
        for vin in tx['vin'])]
    if len(closing) != 1:
        raise AssertionError('no unique confirmed mutual spend of funding output')
    close_id = closing[0]['txid']
    if rpc(backend, 'gettxout', funding['txid'], funding['outnum']) is not None:
        raise AssertionError('funding output remains unspent after close')
    for node in (alice, bob):
        outputs = wait_until(lambda: confirmed_outputs(node, close_id), node['proc'])
        for output in outputs:
            utxo = rpc(backend, 'gettxout', close_id, output['output'])
            if not utxo or utxo['confirmations'] < 1:
                raise AssertionError('wallet close output not confirmed and unspent in Knots')
    print('PASS: mutual close mined; both wallets have confirmed unspent close outputs', flush=True)
    print('XBT funded-channel test OK (regtest coins only)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', required=True, type=Path)
    parser.add_argument('--bitcoin-cli', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--force-close', action='store_true',
                        help='Stop Bob and verify Alice unilateral close and delayed recovery.')
    mode.add_argument('--htlc-timeout', action='store_true',
                      help='Hold an HTLC, stop Bob, and verify on-chain timeout and refund.')
    mode.add_argument('--preimage-claim', action='store_true',
                      help='Bob claims a held HTLC on-chain; Alice learns its preimage.')
    parser.add_argument('--work-dir', type=Path,
                        help='New directory to retain logs/data; use a short path.')
    args = parser.parse_args()
    temporary = None
    if args.work_dir:
        root = args.work_dir.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        temporary = tempfile.TemporaryDirectory(prefix='cln-xbt-funded-')
        root = Path(temporary.name)
    lab = Lab(root, str(args.bitcoind.resolve()), str(args.bitcoin_cli.resolve()))
    print(f'Test directory: {root}', flush=True)
    try:
        run(lab, args.force_close, args.htlc_timeout, args.preimage_claim)
    except Exception as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print(f'RPC stdout: {exc.stdout}\nRPC stderr: {exc.stderr}', flush=True)
        for log in root.glob('*/console.log'):
            print(f'\n--- {log.parent.name}: last log lines ---', flush=True)
            print('\n'.join(log.read_text(errors='replace').splitlines()[-30:]), flush=True)
        raise
    finally:
        lab.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
