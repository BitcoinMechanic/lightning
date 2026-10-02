"""Run CLN nodes, recover swaps, and process explicitly authorized reverse quotes."""
import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

from service_manager import private_load
from swap_rpc import RPC
from swap_controller import run, save


def node_command(settings, directory, role):
    kind = 'btc' if role == 'btc' else 'xbt'
    values = private_load(directory/(kind+'-rpc.json'))
    keys = {kind.upper()+'_RPC_'+n for n in ('HOST', 'PORT', 'USER', 'PASSWORD')}
    if kind == 'btc':
        keys.update(('BTC_RPC_CA', 'BTC_LN_HOST'))
    if set(values) != keys or any(not isinstance(v, str) or not v for v in values.values()):
        raise ValueError('invalid private RPC settings')
    tools = Path(settings['repo'])/'tools/blake2b'
    root = Path(settings['roots'][role])
    marker = 'btc-https-observer-v1' if kind == 'btc' else 'xbt-observer-v1'
    if not (root/marker).is_file():
        raise ValueError('existing node directory missing; refusing new wallet')
    if kind == 'btc':
        args = [str(tools/'live_btc_node.py'), '--lightning-dir='+str(root),
                '--listen-host='+values['BTC_LN_HOST'], '--listen-port=19735', '--market-swaps']
    else:
        args = [str(tools/'live_node.py'), '--lightning-dir='+str(root),
                '--bitcoin-cli='+settings['bitcoin_cli'],
                '--local-peer-port='+('19835' if role == 'receiver' else '19836')]
    if role == 'xbt':
        from reverse_activation import configured
        if configured(settings):
            args.append('--reverse-settings='+str(directory/'settings.json'))
    return [settings['python'], *args], dict(os.environ, **values)


def tick(settings):
    health = {'checked_at': int(time.time()), 'nodes_ready': False, 'xbt_connected': False,
              'swaps': []}
    try:
        from reverse_live import PROFILE, networks
        net = networks(settings.get('reverse_profile', PROFILE))
        infos = [RPC.call(settings[key], 'getinfo') for key in ('btc_cli', 'xbt_cli')]
        if ([i['network'] for i in infos] != [net['btc'], net['xbt']]
                or [i['id'] for i in infos] != settings['node_ids']):
            health['error'] = 'node_identity_mismatch'
            return health
        health['operators_ready'] = True
        health['warning_present'] = any(k.startswith('warning_') for info in infos for k in info)
    except Exception:
        health['error'] = 'operator_rpc_unavailable'
        return health
    if settings.get('deployment') == 'operator-pair-v1':
        health['nodes_ready'] = True
        health['customer_wallet_managed'] = False
        # Peer connectivity is observable from the operator, not from a
        # customer RPC. Being offline must not block existing swap recovery.
        try:
            peers = RPC.call(settings['xbt_cli'], 'listpeers')['peers']
            health['xbt_connected'] = (any(p['connected'] for p in peers) if ('reverse_incoming_policy' in settings
                                       or settings.get('receive_policy', {}).get('profile') in ('routed-receive-regtest-v1', 'bounded-receive-regtest-v1', 'live-routed-receive-v1'))
                                       else any(p['id'] == settings['receiver_id'] and p['connected'] for p in peers))
        except Exception:
            health['peer_status_available'] = False
    else:
        try:
            receiver = RPC.call(settings['receiver_cli'], 'getinfo')
            if receiver['network'] != net['xbt'] or receiver['id'] != settings['receiver_id']:
                raise ValueError('receiver identity mismatch')
            health['nodes_ready'] = True
            peers = RPC.call(settings['xbt_cli'], 'listpeers')['peers']
            if not any(p['id'] == settings['receiver_id'] and p['connected'] for p in peers):
                RPC.call(settings['xbt_cli'], 'connect', settings['receiver_id'], '127.0.0.1', 19835)
            health['xbt_connected'] = True
        except Exception:
            # A receiver outage must not block reconciliation by the operators.
            health['error'] = 'receiver_rpc_or_connection_unavailable'
    root = Path(settings['swap_root']).resolve()
    quotes = sorted(set(root.glob('*/quote.json')) | set(root.glob('*/swap/quote.json')))
    for quote_path in quotes:
        label = str(quote_path.parent.relative_to(root))
        report = {'directory': label}
        try:
            if not quote_path.resolve().is_relative_to(root):
                raise ValueError('quote outside recovery root')
            quote = private_load(quote_path)
            config = quote['config']
            if (config['btc_cli'] != settings['btc_cli'] or config['xbt_cli'] != settings['xbt_cli']
                    or quote['node_ids'] != settings['node_ids']):
                raise ValueError('quote node binding mismatch')
            if (quote_path.parent/'receive-authorization.json').exists():
                from receive_service import process
                result = process(quote_path.parent, settings)
                if result.get('phase') not in ('btc_released', 'btc_failed'):
                    report.update(result)
                    health['swaps'].append(report)
                continue
            path = quote_path.parent/'state.json'
            if not path.exists():
                if 'btc_invoice' not in quote:
                    continue  # Never register, sign or publish a draft.
                status = RPC.call(settings['btc_cli'], 'xbt-quote-status', quote['terms']['payment_hash'])
                if status['payment_hash'] != quote['terms']['payment_hash']:
                    raise ValueError('gate identity mismatch')
                if status['phase'] == 'quoted':
                    continue  # No hook accepted; do not create controller state.
                report['outcome'] = 'needs_manual_resume'
            else:
                if not path.resolve().is_relative_to(root):
                    raise ValueError('state outside recovery root')
                state = private_load(path)
                if (state['btc_cli'] != settings['btc_cli'] or state['xbt_cli'] != settings['xbt_cli']
                        or state['node_ids'] != settings['node_ids']
                        or state['payment_hash'] != quote['terms']['payment_hash']):
                    raise ValueError('controller node or quote binding mismatch')
                if state['phase'] in ('btc_released', 'btc_failed'):
                    continue
                result = run(path, recover_only=True)
                report.update({k: result[k] for k in ('phase', 'outcome') if k in result})
            health['swaps'].append(report)
        except Exception:
            health['swaps'].append(dict(report, outcome='needs_inspection'))
    for quote_path in sorted(root.glob('*/reverse-quote.json')):
        report = {'directory': str(quote_path.parent.relative_to(root)), 'direction': 'xbt-to-btc'}
        try:
            if not quote_path.resolve().is_relative_to(root):
                raise ValueError('reverse quote outside recovery root')
            path = quote_path.parent/'reverse-state.json'
            if path.exists() and not path.resolve().is_relative_to(root):
                raise ValueError('reverse state outside recovery root')
            from reverse_authorize import process_record
            result = process_record(quote_path.parent, settings)
            if result.get('phase') not in ('xbt_released', 'xbt_failed'):
                report.update(result)
                health['swaps'].append(report)
        except Exception:
            health['swaps'].append(dict(report, outcome='needs_inspection'))
    return health


def monitor(settings, directory, stop):
    last = None
    while not stop.is_set():
        health = tick(settings)
        save(directory/'health.json', health)
        summary = dict(health)
        summary.pop('checked_at')
        if summary != last:
            print(json.dumps(summary), flush=True)
            last = summary
        stop.wait(10)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--role', choices=['btc', 'xbt', 'receiver', 'recovery'], required=True)
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.directory.resolve()
    try:
        settings = private_load(directory/'settings.json')
        if args.role != 'recovery':
            command, env = node_command(settings, directory, args.role)
            os.execve(command[0], command, env)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        return monitor(settings, directory, stop)
    except Exception:
        print(json.dumps({'event': 'service_configuration_error', 'details': 'withheld'}), flush=True)
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
