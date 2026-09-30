"""Single-swap BTC -> XBT regtest service commands; direct XBT channels only."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time

from smoke_regtest import Lab
from swap_controller import run as reconcile, save
from swap_invoice import unsigned_invoice
from swap_watch import watch


def emit(value):
    print(json.dumps(value), flush=True)


def config_from(path):
    config = json.loads(path.read_text())
    if set(config) != {'btc_cli', 'xbt_cli'}:
        raise ValueError('config needs btc_cli and xbt_cli only')
    for cli in config.values():
        if (not isinstance(cli, list) or not cli or
                any(not isinstance(arg, str) or not arg for arg in cli)
                or not Path(cli[0]).is_absolute()):
            raise ValueError('CLI must be an argument array with an absolute executable path')
    return config


def identities(config):
    btc = Lab.rpc(config['btc_cli'], 'getinfo')
    xbt = Lab.rpc(config['xbt_cli'], 'getinfo')
    if btc['network'] != 'regtest' or xbt['network'] != 'xbt-regtest':
        raise ValueError('requires BTC regtest and XBT regtest operator nodes')
    if btc['id'] == xbt['id']:
        raise ValueError('operator nodes must have distinct identities')
    return [btc['id'], xbt['id']]


def create(config, invoice, btc_sats, directory):
    if type(btc_sats) is not int or not 0 < btc_sats <= 2100000000000000:
        raise ValueError('BTC price must be a positive integer number of sats')
    ids = identities(config)
    decoded = Lab.rpc(config['xbt_cli'], 'decode', invoice)
    amount = decoded.get('amount_msat')
    if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
            or decoded.get('currency') != 'xbtrt' or not invoice.startswith('lnxbtrt')
            or type(amount) is not int or amount <= 0 or not decoded.get('payment_secret')):
        raise ValueError('requires a signed, fixed-amount XBT regtest BOLT11 invoice')
    now = int(time.time())
    # Allow time for submission after the BTC quote expires.
    expires = min(now + 600, decoded['created_at'] + decoded['expiry'] - 60)
    if expires < now + 30:
        raise ValueError('XBT invoice needs at least 90 seconds remaining')
    if decoded['min_final_cltv_expiry'] > 40:
        raise ValueError('receiver CLTV exceeds this experimental policy')
    channels = [c for c in Lab.rpc(config['xbt_cli'], 'listpeerchannels')['channels']
                if c['peer_id'] == decoded['payee'] and c['state'] == 'CHANNELD_NORMAL'
                and c.get('short_channel_id') and c.get('spendable_msat', 0) >= amount]
    if len(channels) != 1:
        raise ValueError('requires one usable direct XBT channel to invoice payee')
    attempts = Lab.rpc(config['xbt_cli'], 'listsendpays')['payments']
    if any(p['payment_hash'] == decoded['payment_hash'] for p in attempts):
        raise ValueError('XBT invoice already has an outgoing attempt')
    terms = {'payment_hash': decoded['payment_hash'], 'payment_secret': secrets.token_hex(32),
             'btc_amount_msat': btc_sats * 1000, 'xbt_amount_msat': amount,
             'xbt_invoice': invoice, 'expires_at': expires, 'min_cltv_delta': 100,
             'max_cltv_delta': 2000}
    template = dict(config, phase='prepared', quote_gate=True, btc_deadline_guard=True,
                    payment_hash=decoded['payment_hash'], payment_secret=decoded['payment_secret'],
                    xbt_invoice=invoice, xbt_amount_msat=amount,
                    route=[{'id': decoded['payee'], 'channel': channels[0]['short_channel_id'],
                            'amount_msat': amount, 'delay': 40}])
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Save everything needed to retry registration/signing before any mutation.
    save(directory / 'quote.json', {'config': config, 'node_ids': ids, 'terms': terms,
                                    'controller': template})


def publish(directory):
    path = directory / 'quote.json'
    data = json.loads(path.read_text())
    if identities(data['config']) != data['node_ids']:
        raise RuntimeError('configured operator identity changed')
    terms = data['terms']
    if terms['expires_at'] <= int(time.time()):
        raise RuntimeError('quote expired')
    if 'btc_invoice' not in data:
        cli = data['config']['btc_cli']
        if Lab.rpc(cli, 'xbt-register', json.dumps(terms)) != {'registered': True}:
            raise RuntimeError('quote registration failed')
        unsigned = unsigned_invoice(terms['payment_hash'], terms['payment_secret'],
                                    terms['btc_amount_msat'], terms['expires_at'] - int(time.time()))
        signed = Lab.rpc(cli, 'signinvoice', unsigned)['bolt11']
        decoded = Lab.rpc(cli, 'decode', signed)
        expected = {'valid': True, 'currency': 'bcrt', 'payee': data['node_ids'][0],
                    'payment_hash': terms['payment_hash'], 'payment_secret': terms['payment_secret'],
                    'amount_msat': terms['btc_amount_msat'], 'min_final_cltv_expiry': 120}
        if any(decoded.get(k) != v for k, v in expected.items()):
            raise RuntimeError('signed BTC invoice does not match quote')
        data['btc_invoice'] = signed
        save(path, data)
    return {'btc_invoice': data['btc_invoice'], 'payment_hash': terms['payment_hash'],
            'btc_sats': terms['btc_amount_msat'] // 1000,
            'xbt_msat': terms['xbt_amount_msat'], 'expires_at': terms['expires_at']}


def serve(directory, stop):
    data = json.loads((directory / 'quote.json').read_text())
    if identities(data['config']) != data['node_ids']:
        raise RuntimeError('configured operator identity changed')
    if 'btc_invoice' not in data:
        raise RuntimeError('finish quote publication first with the invoice command')
    path = directory / 'state.json'
    cli = data['config']['btc_cli']
    payment_hash = data['terms']['payment_hash']
    while not stop.is_set() and not path.exists():
        try:
            status = Lab.rpc(cli, 'xbt-quote-status', payment_hash)
            if status['payment_hash'] != payment_hash:
                raise RuntimeError('quote identity mismatch')
            if status['phase'] == 'held':
                binding = status['binding']
                channels = Lab.rpc(cli, 'listpeerchannels')['channels']
                committed = any(c.get('short_channel_id') == binding[0] and
                                any(h['id'] == binding[1] and h['direction'] == 'in'
                                    and h['payment_hash'] == payment_hash
                                    and h['state'] == 'RCVD_ADD_ACK_REVOCATION'
                                    for h in c.get('htlcs', [])) for c in channels)
                if committed:
                    outgoing = Lab.rpc(data['config']['xbt_cli'], 'listsendpays')['payments']
                    if any(p['payment_hash'] == payment_hash for p in outgoing):
                        raise RuntimeError('outgoing attempt exists without controller state; restore original state')
                    save(path, dict(data['controller'], btc_binding=binding))
                    break
            elif status['phase'] != 'quoted':
                raise RuntimeError('quote terminal without controller state; inspect nodes')
            elif data['terms']['expires_at'] <= int(time.time()):
                raise RuntimeError('unpaid quote expired')
            emit({'event': 'waiting_for_btc', 'payment_hash': payment_hash})
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            emit({'event': 'rpc_retry', 'error': type(exc).__name__})
        stop.wait(1)
    if stop.is_set():
        return 0
    # Only the original prepared checkpoint can submit. The controller saves
    # outgoing_started before sendpay; a lost reply is reconciled, never resent.
    if json.loads(path.read_text())['phase'] == 'prepared':
        try:
            result = reconcile(path)
            if result.get('outcome') == 'refused':
                emit(result)
                return 1  # No spend. Keep binding for deliberate inspection/cleanup.
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if json.loads(path.read_text())['phase'] == 'prepared':
                raise  # Pre-spend RPC failure; safe to restart run.
    return watch(path, stop=stop, emit=emit)


def status(directory):
    data = json.loads((directory / 'quote.json').read_text())
    path = directory / 'state.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    return {'payment_hash': data['terms']['payment_hash'],
            'phase': state.get('phase', 'waiting_for_btc' if 'btc_invoice' in data else 'unpublished'),
            'quote_expired': data['terms']['expires_at'] <= int(time.time()),
            'btc_close_requested': 'btc_close_intent' in state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('quote', 'invoice', 'run', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--directory', required=True, type=Path)
        if name == 'quote':
            command.add_argument('--config', required=True, type=Path)
            command.add_argument('--xbt-invoice', required=True)
            command.add_argument('--btc-sats', required=True, type=int)
    args = parser.parse_args()
    directory = args.directory.resolve()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        if args.command == 'quote':
            create(config_from(args.config), args.xbt_invoice, args.btc_sats, directory)
        if args.command == 'status':
            emit(status(directory))
            return 0
        # Stable per-directory lock serializes publication and service startup.
        fd = os.open(directory / 'service.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command in ('quote', 'invoice'):
                emit(publish(directory))
                return 0
            return serve(directory, stop)
        finally:
            os.close(fd)
    except BlockingIOError:
        emit({'event': 'busy'})
        return 1
    except Exception as exc:
        # RPC arguments/output may contain secrets; never dump those here.
        emit({'event': 'error', 'error': type(exc).__name__,
              'reason': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'inspect node logs'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
