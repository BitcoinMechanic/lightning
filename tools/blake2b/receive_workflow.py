"""Private foreground receiving workflow and single-attempt channel repayment."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time

import live_pilot as pilot
from market_policy import policy
from smoke_regtest import Lab
from swap_controller import save
import swap_service as service


def emit(value):
    # Deliberate whitelist: never print invoices, hashes, secrets or node IDs.
    allowed = ('event', 'phase', 'outcome', 'reason', 'btc_sats', 'xbt_sats',
               'invoice_file', 'expires_at', 'receiver_paid', 'received_xbt_sats',
               'operator_paid', 'returned_xbt_sats')
    print(json.dumps({k: v for k, v in value.items() if k in allowed}), flush=True)


@contextmanager
def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def receiver_cli(config, directory):
    return [config['xbt_cli'][0], '--lightning-dir=' + str(directory.resolve()),
            '--network=xbt', '--json', '--notifications=none']


def verify_receiver(config, cli):
    info = Lab.rpc(cli, 'getinfo')
    if info['network'] != 'xbt' or info['id'] != policy(config)['xbt_peer']:
        raise ValueError('receiver node differs from configured peer')
    if any(k.startswith('warning_') for k in info):
        raise ValueError('receiver node reports a warning')


def get_invoice(cli, label, amount):
    rows = Lab.rpc(cli, 'listinvoices', label)['invoices']
    if not rows:
        Lab.rpc(cli, 'invoice', str(amount)+'msat', label, 'XBT channel workflow', 1200)
        rows = Lab.rpc(cli, 'listinvoices', label)['invoices']
    if len(rows) != 1 or rows[0]['amount_msat'] != amount:
        raise ValueError('saved invoice label or amount mismatch')
    return rows[0]


def private_invoice(path, value):
    # Invoice is public to its payer, but kept private on disk by default.
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(value + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def receive(config, receiver, directory, sats, stop):
    if config.get('profile') != pilot.PROFILE_MARKET:
        raise ValueError('receive requires the market profile')
    p = policy(config)
    if type(sats) is not int or not 0 < sats <= p['max_xbt_sats']:
        raise ValueError('requested XBT exceeds configured cap')
    verify_receiver(config, receiver)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock(directory/'receive.lock'):
        path = directory/'request.json'
        expected = dict(config=config, receiver_cli=receiver, xbt_sats=sats,
                        directory=str(directory.resolve()))
        if path.exists():
            request = json.loads(path.read_text())
            if any(request.get(k) != v for k,v in expected.items()):
                raise ValueError('saved receiving request differs; use original arguments')
        else:
            if any(x.name != 'receive.lock' for x in directory.iterdir()):
                raise ValueError('new receive directory must be empty')
            request = dict(expected, label='xbt-receive-' + secrets.token_hex(16))
            save(path, request)  # Before invoice RPC: lost reply recovers by label.
        swap = directory/'swap'
        quote_path = swap/'quote.json'
        if not quote_path.exists():
            invoice = get_invoice(receiver, request['label'], sats*1000)
            if invoice['status'] != 'unpaid':
                raise ValueError('receiver invoice is paid or expired; no new quote created')
            service.create(config, invoice['bolt11'], None, swap)
        with lock(swap/'service.lock'):
            data = json.loads(quote_path.read_text())
            if (data['config'] != config or data['terms']['xbt_amount_msat'] != sats*1000):
                raise ValueError('saved quote differs from receiving request')
            rows = Lab.rpc(receiver, 'listinvoices', request['label'])['invoices']
            if (len(rows) != 1 or rows[0]['payment_hash'] != data['terms']['payment_hash']
                    or rows[0]['bolt11'] != data['terms']['xbt_invoice']):
                raise ValueError('receiver invoice differs from saved quote')
            # Already published quotes and recovery must not need a fresh oracle
            # or an unexpired invoice. The service reconciles accepted payments.
            if 'btc_invoice' not in data:
                service.publish(swap)
                data = json.loads(quote_path.read_text())
            invoice_path = directory/'btc-invoice.txt'
            private_invoice(invoice_path, data['btc_invoice'])
            if not (swap/'state.json').exists() and data['terms']['expires_at'] > int(time.time()):
                emit(dict(event='invoice_ready', btc_sats=data['terms']['btc_amount_msat']//1000,
                          xbt_sats=sats, invoice_file=str(invoice_path), expires_at=data['terms']['expires_at']))
            last_report = None
            def report(value):
                nonlocal last_report
                summary = {k: value[k] for k in ('event', 'phase', 'outcome', 'reason') if k in value}
                if summary != last_report:
                    emit(summary)
                    last_report = summary
            code = service.serve(swap, stop, report=report)
            state_path = swap/'state.json'
            if state_path.exists() and json.loads(state_path.read_text())['phase'] == 'btc_released':
                rows = Lab.rpc(receiver, 'listinvoices', request['label'])['invoices']
                paid = (len(rows) == 1 and rows[0]['status'] == 'paid'
                        and rows[0]['payment_hash'] == data['terms']['payment_hash']
                        and rows[0]['amount_received_msat'] == sats*1000)
                emit(dict(event='receipt_checked', receiver_paid=paid,
                          received_xbt_sats=sats if paid else 0))
                if not paid:
                    raise ValueError('receiver settlement not confirmed; preserve records')
            return code


def repay(directory, receiver):
    data = json.loads((directory/'quote.json').read_text())
    original = json.loads((directory/'state.json').read_text())
    config = data['config']
    if config.get('profile') != pilot.PROFILE_MARKET or original['phase'] != 'btc_released':
        raise ValueError('repay requires the completed market swap directory')
    if original['payment_hash'] != data['terms']['payment_hash']:
        raise ValueError('original swap identity mismatch')
    verify_receiver(config, receiver)
    pilot.verify_nodes(dict(config, node_ids=data['node_ids']), Lab.rpc)
    pilot.verify_state(original, Lab.rpc)
    original_amount = data['terms']['xbt_amount_msat']
    if (type(original_amount) is not int or not 0 < original_amount <= 500000000
            or original_amount != original['xbt_amount_msat']):
        raise ValueError('repayment amount outside bounds')
    operator = config['xbt_cli']
    with lock(directory/'repayment.lock'):
        path = directory/'repayment.json'
        expected = dict(receiver_cli=receiver, operator_cli=operator, original_amount_msat=original_amount,
                        original_hash=original['payment_hash'], directory=str(directory.resolve()))
        if path.exists():
            state = json.loads(path.read_text())
            if any(state.get(k) != v for k,v in expected.items()):
                raise ValueError('repayment record differs; do not reset it')
        else:
            candidates = [c for c in Lab.rpc(receiver, 'listpeerchannels')['channels']
                          if c.get('short_channel_id') == config['market']['xbt_channel']
                          and c['peer_id'] == data['node_ids'][1]]
            if len(candidates) != 1:
                raise ValueError('repayment channel not found')
            available = candidates[0]['spendable_msat']
            if type(available) is not int or available < 1000:
                raise ValueError('no spendable receiver balance')
            amount = min(original_amount, available // 1000 * 1000)
            state = dict(expected, amount_msat=amount, phase='invoice', label='xbt-return-' + secrets.token_hex(16))
            save(path, state)
        amount = state['amount_msat']
        if type(amount) is not int or not 0 < amount <= original_amount:
            raise ValueError('saved repayment amount outside original receipt')
        if state['phase'] == 'invoice':
            # Original receipt must be present before returning its amount.
            received = [i for i in Lab.rpc(receiver, 'listinvoices')['invoices']
                        if i['payment_hash'] == original['payment_hash']]
            if (len(received) != 1 or received[0]['status'] != 'paid'
                    or received[0]['amount_received_msat'] < amount):
                raise ValueError('original receiver payment not confirmed')
            channel = [c for c in Lab.rpc(receiver, 'listpeerchannels')['channels']
                       if c.get('short_channel_id') == config['market']['xbt_channel']
                       and c['peer_id'] == data['node_ids'][1]]
            if (len(channel) != 1 or channel[0]['state'] != 'CHANNELD_NORMAL'
                    or not channel[0]['peer_connected'] or channel[0].get('htlcs')
                    or channel[0]['spendable_msat'] < amount):
                raise ValueError('receiver channel cannot return the full amount yet')
            invoice = get_invoice(operator, state['label'], amount)
            if invoice['status'] != 'unpaid':
                raise ValueError('repayment invoice is not unpaid; inspect original record')
            decoded = Lab.rpc(receiver, 'decode', invoice['bolt11'])
            if (decoded.get('valid') is not True or decoded.get('currency') != 'xbt'
                    or decoded['payee'] != data['node_ids'][1] or decoded['amount_msat'] != amount
                    or decoded['payment_hash'] != invoice['payment_hash']
                    or decoded['min_final_cltv_expiry'] > 40
                    or decoded['created_at']+decoded['expiry'] <= int(time.time())+30):
                raise ValueError('repayment invoice validation failed')
            attempts = Lab.rpc(receiver, 'listsendpays')['payments']
            if any(p['payment_hash'] == invoice['payment_hash'] for p in attempts):
                raise ValueError('unexpected previous repayment attempt; inspect records')
            route = [dict(id=data['node_ids'][1], channel=channel[0]['short_channel_id'],
                          amount_msat=amount, delay=40)]
            state.update(phase='outgoing_started', payment_hash=invoice['payment_hash'])
            save(path, state)  # A lost reply never permits another sendpay.
            Lab.rpc([*receiver, '-k'], 'sendpay', 'route='+json.dumps(route),
                    'payment_hash='+invoice['payment_hash'], 'payment_secret='+decoded['payment_secret'])
            Lab.rpc(receiver, 'waitsendpay', invoice['payment_hash'], 10)
        attempts = [p for p in Lab.rpc(receiver, 'listsendpays')['payments']
                    if p['payment_hash'] == state['payment_hash']]
        if len(attempts) != 1:
            raise ValueError('repayment outcome unknown; no automatic resend')
        payment = attempts[0]
        if payment['status'] != 'complete':
            emit(dict(event='repayment_status', phase=payment['status']))
            return 0
        rows = Lab.rpc(operator, 'listinvoices', state['label'])['invoices']
        if (payment['amount_msat'] != amount or len(rows) != 1 or rows[0]['status'] != 'paid'
                or rows[0]['payment_hash'] != state['payment_hash']
                or rows[0]['amount_received_msat'] != amount
                or hashlib.sha256(bytes.fromhex(payment['payment_preimage'])).hexdigest() != state['payment_hash']):
            raise ValueError('repayment receipt mismatch')
        state['phase'] = 'complete'
        save(path, state)
        emit(dict(event='repayment_complete', operator_paid=True, returned_xbt_sats=amount//1000))
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('receive', 'repay'):
        command = commands.add_parser(name)
        command.add_argument('--directory', required=True, type=Path)
        command.add_argument('--receiver-dir', required=True, type=Path)
        if name == 'receive':
            command.add_argument('--config', required=True, type=Path)
            command.add_argument('--xbt-sats', required=True, type=int)
    args = parser.parse_args()
    os.umask(0o077)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        directory = args.directory.resolve()
        config = (service.config_from(args.config) if args.command == 'receive' else
                  json.loads((directory/'quote.json').read_text())['config'])
        cli = receiver_cli(config, args.receiver_dir)
        if args.command == 'repay':
            return repay(directory, cli)
        return receive(config, cli, directory, args.xbt_sats, stop)
    except BlockingIOError:
        emit(dict(event='busy', reason='another process is using this workflow'))
        return 1
    except (ValueError, RuntimeError) as exc:
        emit(dict(event='error', reason='invalid local JSON record' if isinstance(exc, json.JSONDecodeError) else str(exc)))
        return 1
    except Exception:
        # RPC exceptions can contain invoice/preimage arguments. Never print them.
        emit(dict(event='error', reason='workflow stopped; preserve records for inspection'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
