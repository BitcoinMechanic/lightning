"""Funded customer/API/worker receiving test; regtest quote adapter, no live activation."""
import argparse
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

from customer_receive import workflow
from receive_service import process
from reverse_quote_api import Quotes, Server
from service_manager import private_load
from smoke_regtest import Lab, wait_until
from swap_controller import save
import swap_service as service
import swap_regtest


def demo(lab, payer, btc, xbt, receiver, initial, fail=False, selected_settings=None):
    rpc = lambda node, *args: lab.rpc(node['cli'], *args)
    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if node['id'] == xbt['id']:
            channels = [c for c in channels if c['peer_id'] == receiver['id']]
        assert len(channels) == 1
        return channels[0]
    config = dict(profile='live-market-v1', btc_cli=btc['cli'], xbt_cli=xbt['cli'], market=dict(
        btc_channel=channel(btc)['short_channel_id'], xbt_channel=channel(xbt)['short_channel_id'],
        xbt_peer=receiver['id'], max_btc_sats=3000, max_xbt_sats=400000, margin_bps=0))
    settings = dict(receive_config=config, btc_cli=btc['cli'], xbt_cli=xbt['cli'],
                    node_ids=[btc['id'], xbt['id']], receiver_id=receiver['id'], swap_root=str(lab.root))
    credential = dict(token='ab'*32, payer_id=receiver['id'])
    if selected_settings is not None:
        settings = selected_settings
        credential = dict(token='ab'*32, scope='receive')
    ordinary = dict(btc_cli=btc['cli'], xbt_cli=xbt['cli'])
    original_create, original_publish, original_identities = service.create, service.publish, service.identities

    # Only this harness adapts quote creation to regtest networks and a fixed
    # 1500-BTC-sat price. Production market preflights are covered by unit tests;
    # this fixture exercises real invoices, HTTP, HTLCs, controllers and balances.
    def create(config, invoice, price, directory):
        assert rpc(xbt, 'decode', invoice)['amount_msat'] == 200000000
        assert config['market']['max_btc_sats'] >= 1500
        original_create(ordinary, invoice, 1500, directory)
        q = private_load(directory/'quote.json'); q['config'] = config
        save(directory/'quote.json', q)

    def publish(directory):
        q = private_load(directory/'quote.json'); outer = q['config']
        q['config'] = ordinary; save(directory/'quote.json', q)
        original_publish(directory)
        q = private_load(directory/'quote.json'); q['config'] = outer
        q['terms']['btc_channel'] = config['market']['btc_channel']
        save(directory/'quote.json', q)

    def identities(config):
        return original_identities(ordinary)

    def wallet_rpc(cli, method, *args):
        assert cli == receiver['cli'], 'customer accessed operator wallet'
        value = lab.rpc(cli, method, *args)
        # The customer CLI has no regtest flag; translate only read-only network
        # labels in the fixture. Signed invoices and hashes remain real regtest.
        if method == 'getinfo':
            assert value['network'] == 'xbt-regtest'
            value['network'] = 'xbt'
        if method == 'decode':
            value['currency'] = {'xbtrt':'xbt', 'bcrt':'bc'}[value['currency']]
        return value

    hold = lab.root/'receive-hold.py'
    hold.write_text('#!'+sys.executable+'\n'+Path(__file__).with_name('hold_htlc.py').read_text())
    hold.chmod(0o700); rpc(receiver, 'plugin', 'start', str(hold))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    import receive_selection
    original_select = receive_selection.select
    def selector(config, invoice, cap, ids):
        def operator_rpc(cli, method, *args):
            assert cli in (btc['cli'], xbt['cli']), 'selection accessed customer RPC'
            value = lab.rpc(cli, method, *args)
            if method == 'getinfo':
                value['network'] = {'regtest':'bitcoin', 'xbt-regtest':'xbt'}[value['network']]
            if method == 'decode':
                assert value['currency'] == 'xbtrt'
                value['currency'] = 'xbt'
            return value
        return original_select(config, invoice, cap, ids, rpc=operator_rpc)
    with patch('swap_service.create', create), patch('swap_service.publish', publish), patch('swap_service.identities', identities), patch('receive_selection.select', selector):
        api = Quotes(settings, auto_process=True)
        server = Server(port, api, credential)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            directory = lab.root/('customer-receiving-'+receiver['data'].name if selected_settings else 'customer-receiving')
            args = (receiver['cli'],directory,credential,f'http://127.0.0.1:{port}',200000,1500)
            offer = workflow(*args,rpc=wallet_rpc)
            assert workflow(*args,rpc=wallet_rpc) == offer
            state = private_load(directory/'receive.json')
            swap = lab.root/('receive-api-'+state['request_id'])
            # A fresh API instance must return the original offer without creation.
            body=dict(request_id=state['request_id'],xbt_invoice=state['xbt_invoice'],max_btc_sats=1500)
            assert Quotes(settings,auto_process=True).receive.quote(body)['btc_invoice']==offer['btc_invoice']
            assert process(swap,settings)['outcome']=='waiting_for_btc'
            assert not (swap/'state.json').exists()
            print('PASS: customer-created XBT invoice; authenticated API returns one stable BTC offer; no spend before BTC',flush=True)
            paylog=lab.root/'receive-payer.log'
            paying=lab.start([*payer['cli'],'pay',offer['btc_invoice']],paylog)
            def submitted():
                try: process(swap,settings)
                except (subprocess.CalledProcessError,subprocess.TimeoutExpired): pass
                return (swap/'state.json').exists() and private_load(swap/'state.json')['phase']=='outgoing_started'
            wait_until(submitted,paying,timeout=60)
            before=(swap/'state.json').read_bytes()
            assert process(swap,settings)['outcome']=='pending'
            assert process(swap,settings)['outcome']=='pending'
            assert (swap/'state.json').read_bytes()==before
            payment_hash=rpc(xbt,'decode',state['xbt_invoice'])['payment_hash']
            attempts=[p for p in rpc(xbt,'listsendpays')['payments'] if p['payment_hash']==payment_hash]
            assert len(attempts)==1
            wait_until(lambda: bool(rpc(receiver,'xbt-held')['held']),receiver['proc'])
            print('PASS: worker submits one XBT attempt; fresh worker steps preserve pending payments without resend',flush=True)
            rpc(receiver,'xbt-fail' if fail else 'xbt-continue',payment_hash)
            terminal='btc_failed' if fail else 'btc_released'
            wait_until(lambda: process(swap,settings).get('phase')==terminal,timeout=60)
            assert process(swap,settings)['phase']==terminal
            paying.wait(timeout=60)
            assert (paying.returncode != 0) == fail
            if not fail:
                assert workflow(*args,rpc=wallet_rpc)==dict(outcome='paid',received_xbt_sats=200000)
            for node,delta in ((payer,-1500000),(btc,1500000),(xbt,-200000000),(receiver,200000000)):
                def settled():
                    c=channel(node)
                    return not c.get('htlcs') and c['to_us_msat']==initial[node['id']]+(0 if fail else delta)
                wait_until(settled,node['proc'])
            attempts=[p for p in rpc(xbt,'listsendpays')['payments'] if p['payment_hash']==payment_hash]
            assert len(attempts)==1 and attempts[0]['status']==('failed' if fail else 'complete')
            print('PASS: invoice outcome and all four balances verified; no pending HTLCs; repeated recovery does not resend',flush=True)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
    print('Receive API workflow OK ('+('XBT rejection' if fail else 'success')+'; regtest nodes; fixed-price adapter; live activation disabled)',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bitcoind',type=Path,required=True)
    p.add_argument('--bitcoin-cli',type=Path,required=True)
    p.add_argument('--fail-outgoing',action='store_true')
    p.add_argument('--work-dir',type=Path)
    a=p.parse_args()
    temporary = None
    if a.work_dir:
        root=a.work_dir.resolve(); root.mkdir(mode=0o700,parents=True,exist_ok=False)
    else:
        temporary=tempfile.TemporaryDirectory(prefix='cln-receive-api-')
        root=Path(temporary.name)
    lab=Lab(root,str(a.bitcoind.resolve()),str(a.bitcoin_cli.resolve()))
    print('Test directory: '+str(root),flush=True)
    try:
        import service_demo
        with patch.object(service_demo,'demo',lambda *args:demo(*args,fail=a.fail_outgoing)):
            swap_regtest.run(lab,service_demo=True)
    finally:
        lab.close()
        if temporary: temporary.cleanup()


if __name__=='__main__': main()
