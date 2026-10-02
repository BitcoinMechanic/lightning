"""Two invoice-selected receivers; authenticated receive-only API; regtest only."""
import argparse
import copy
from pathlib import Path
import sys
import tempfile

from smoke_regtest import Lab, wait_until
from receive_api_regtest import demo
from service_manager import private_load


def run(lab, fail_second=False, any_btc=False):
    btc_backend = lab.node('knots-btc', False)
    xbt_backend = lab.node('knots-xbt', True)
    plugin = lab.root/'quote_plugin.py'
    plugin.write_text('#!'+sys.executable+'\n'+Path(__file__).with_name('quote_plugin.py').read_text())
    plugin.chmod(0o700)
    payer = lab.lightning('payer', 'regtest', btc_backend)
    payers = [payer, lab.lightning('payer-2', 'regtest', btc_backend)] if any_btc else [payer]
    btc = lab.lightning('swap-btc', 'regtest', btc_backend, plugins=(plugin,))
    xbt = lab.lightning('swap-xbt', 'xbt-regtest', xbt_backend)
    receivers = [lab.lightning('receiver-'+str(n), 'xbt-regtest', xbt_backend) for n in (1, 2)]
    rpc = lambda node, *args: lab.rpc(node['cli'], *args)

    def channel(node, peer):
        matches = [c for c in rpc(node, 'listpeerchannels')['channels'] if c['peer_id'] == peer['id']]
        assert len(matches) == 1
        return matches[0]

    def mine(backend, nodes, count):
        rpc(backend, 'generatetoaddress', count, rpc(backend, 'getnewaddress'))
        height = rpc(backend, 'getblockcount')
        for node in nodes:
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height, node['proc'], timeout=90)

    funding_pairs = [(btc_backend, p, btc, [*payers, btc]) for p in payers]
    funding_pairs += [(xbt_backend, xbt, r, [xbt, *receivers]) for r in receivers]
    for backend, sender, receiver, nodes in funding_pairs:
        deposit = rpc(backend, 'sendtoaddress', rpc(sender, 'newaddr', 'bech32')['bech32'], '0.02')
        mine(backend, nodes, 1)
        wait_until(lambda: any(o['txid'] == deposit and o['status'] == 'confirmed'
                              for o in rpc(sender, 'listfunds')['outputs']), sender['proc'])
        rpc(sender, 'connect', receiver['id'], '127.0.0.1', receiver['port'])
        funding = rpc(sender, 'fundchannel', receiver['id'], '1000000sat')
        wait_until(lambda: funding['txid'] in rpc(backend, 'getrawmempool'))
        mine(backend, nodes, 6)
        for node, peer in ((sender, receiver), (receiver, sender)):
            wait_until(lambda: channel(node, peer)['state'] == 'CHANNELD_NORMAL', node['proc'])
    settings = dict(btc_cli=btc['cli'], xbt_cli=xbt['cli'], node_ids=[btc['id'], xbt['id']],
                    swap_root=str(lab.root), receive_policy=dict(profile='invoice-direct-v1',
                    btc_cli=btc['cli'], xbt_cli=xbt['cli'], market=dict(
                        btc_channel=channel(btc, payer)['short_channel_id'], max_btc_sats=3000,
                        max_xbt_sats=400000, margin_bps=0)))
    if any_btc:
        settings['receive_policy']['profile'] = 'invoice-direct-v2'
        del settings['receive_policy']['market']['btc_channel']
    before = copy.deepcopy(settings)
    print('PASS: two independent receiver channels funded; policy contains no customer identity', flush=True)
    def restart(state):
        original = rpc(btc, 'xbt-quote-status', state['payment_hash'])
        lab.stop(btc['proc'])
        btc['log'].rename(btc['log'].with_name('before-restart-'+str(len(list(lab.root.glob('receive-api-*'))))+'.log'))
        old_id = btc['id']
        btc.update(lab.lightning('swap-btc', 'regtest', btc_backend, plugins=(plugin,)))
        assert btc['id'] == old_id
        for p in payers:
            rpc(p, 'connect', btc['id'], '127.0.0.1', btc['port'])
        wait_until(lambda: any(h['payment_hash'] == state['payment_hash']
                   for h in rpc(btc, 'xbt-held')['held']), btc['proc'])
        assert rpc(btc, 'xbt-quote-status', state['payment_hash']) == original
        from incoming_btc import pinned_channel
        pinned_channel(state, rpc(btc, 'listpeerchannels')['channels'])

    for index, receiver in enumerate(receivers):
        payer = payers[index] if any_btc else payers[0]
        unrelated_btc = channel(btc, payers[1-index])['to_us_msat'] if any_btc else None
        initial = {node['id']: channel(node, peer)['to_us_msat']
                   for node, peer in ((payer, btc), (btc, payer), (xbt, receiver), (receiver, xbt))}
        other = receivers[1-index]
        other_balance = channel(xbt, other)['to_us_msat']
        demo(lab, payer, btc, xbt, receiver, initial, fail=fail_second and index == 1,
             selected_settings=settings, restart=restart if any_btc else None)
        if any_btc:
            assert channel(btc, payers[1-index])['to_us_msat'] == unrelated_btc
        assert channel(xbt, other)['to_us_msat'] == other_balance
        assert settings == before
    selected = [private_load(p)['receive_selection'] for p in lab.root.glob('receive-api-*/quote.json')]
    assert len(selected) == 2
    assert {s['payee'] for s in selected} == {r['id'] for r in receivers}
    assert len({s['funding_txid'] for s in selected}) == 2
    assert len({s['channel'] for s in selected}) == 2
    print('PASS: receiver-specific invoice, channel and funding pins preserved; unrelated balances unchanged', flush=True)
    if any_btc:
        states = [private_load(p) for p in lab.root.glob('receive-api-*/state.json')]
        assert len({s['btc_incoming_pin']['funding_txid'] for s in states}) == 2
        assert {s['btc_binding'][0] for s in states} == {channel(btc,p)['short_channel_id'] for p in payers}
        assert all('btc_channel' not in private_load(p)['terms'] for p in lab.root.glob('receive-api-*/quote.json'))
        print('PASS: unbound BTC quotes settled through two different channels; exact incoming pins retained',flush=True)
    print('Invoice-selected receiving OK (two wallets; '+('second rejects' if fail_second else 'both paid')+
          '; fixed-price regtest adapter; live configuration unchanged)', flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bitcoind', type=Path, required=True)
    p.add_argument('--bitcoin-cli', type=Path, required=True)
    p.add_argument('--fail-second', action='store_true')
    p.add_argument('--any-btc', action='store_true', help='Exercise two incoming BTC channels and gate restarts.')
    p.add_argument('--work-dir', type=Path,
                   help='New retained directory supplied by the regression runner.')
    a = p.parse_args(argv)
    temporary = None
    if a.work_dir:
        root = a.work_dir.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        temporary = tempfile.TemporaryDirectory(prefix='cln-receive-multi-')
        root = Path(temporary.name)
    lab = Lab(root, str(a.bitcoind.resolve()), str(a.bitcoin_cli.resolve()))
    print('Test directory: '+str(root), flush=True)
    try:
        run(lab, a.fail_second, a.any_btc)
    finally:
        lab.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
