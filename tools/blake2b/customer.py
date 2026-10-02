"""One customer command: send, receive, resume, and read-only status."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import customer_receive
import customer_swap
from customer_errors import CustomerError
from quote_refusal import QuoteRefused
from reverse_check import private_invoice
from reverse_customer import locked, result
from service_manager import private_load
from swap_controller import save
from swap_rpc import RPC


def private_directory(path, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError('private directory required')
    return path


def identifier(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', value):
        raise ValueError('invalid attempt name')
    return value


def identity(cli, rpc):
    info = rpc(cli, 'getinfo')
    if info['network'] != 'xbt' or any(k.startswith('warning_') for k in info):
        raise ValueError('wallet network or warning')
    return info['id']


def start(root, intent, rpc=RPC.call, emit=print, confirm=input, retry=False):
    """Save immutable intent before entering either existing workflow."""
    private_directory(root, create=True)
    with locked(root):
        name = identifier(intent['id'])
        directory = private_directory(root/name, create=True)
        path = directory/'intent.json'
        if path.exists():
            if private_load(path) != intent:
                raise ValueError('saved intent differs; preserve original attempt')
        else:
            if list(directory.iterdir()):
                raise ValueError('unrecognized attempt directory')
            save(path, intent)
        emit(json.dumps(dict(attempt=name, direction=intent['direction'])))
        return execute(directory, intent, rpc, emit, confirm, retry)


def execute(directory, intent, rpc, emit, confirm, retry):
    if identity(intent['cli'], rpc) != intent['customer_id']:
        raise ValueError('wallet identity changed')
    work = directory/'workflow'
    if intent['direction'] == 'send':
        return customer_swap.workflow(
            intent['invoice'], intent['cli'], work, Path(intent['token_file']),
            intent['url'], intent['max_xbt_sats'], intent['max_delay'],
            confirm=confirm, emit=emit, retry_quote=retry)
    if intent['direction'] != 'receive':
        raise ValueError('unknown direction')
    return customer_receive.workflow(
        intent['cli'], work, private_load(Path(intent['token_file'])), intent['url'],
        intent['xbt_sats'], intent['max_btc_sats'], retry_quote=retry)


def resume(root, name, rpc=RPC.call, emit=print, confirm=input, retry=False):
    private_directory(root)
    with locked(root):
        directory = private_directory(root/identifier(name))
        intent = private_load(directory/'intent.json')
        if intent['id'] != name:
            raise ValueError('attempt identity mismatch')
        emit(json.dumps(dict(attempt=name, direction=intent['direction'])))
        return execute(directory, intent, rpc, emit, confirm, retry)


def inspect(directory, rpc=RPC.call, now=time.time):
    """Read a managed workflow or a pre-existing customer workflow directly."""
    private_directory(directory)
    send_path = directory/'wallet/customer.json'
    receive_path = directory/'receive.json'
    if send_path.exists() and receive_path.exists():
        raise ValueError('ambiguous workflow')
    if send_path.exists():
        return result(private_load(send_path), rpc)
    if receive_path.exists():
        state = private_load(receive_path)
        if identity(state['cli'], rpc) != state['customer_id']:
            raise ValueError('wallet identity changed')
        rows = rpc(state['cli'], 'listinvoices', state['label'])['invoices']
        if not rows:
            return dict(outcome='wallet_invoice_missing', automatic_requote=False)
        if (len(rows) != 1 or rows[0]['amount_msat'] != state['xbt_sats']*1000
                or ('xbt_invoice' in state and rows[0]['bolt11'] != state['xbt_invoice'])):
            raise ValueError('wallet invoice changed')
        row = rows[0]
        if row['status'] == 'paid':
            return dict(outcome='paid', received_xbt_sats=row['amount_received_msat']//1000)
        if row['status'] != 'unpaid' or row['expires_at'] <= int(now()):
            return dict(outcome='invoice_expired', automatic_requote=False)
        offer = state.get('offer')
        if not offer:
            return dict(outcome='offer_not_saved', automatic_requote=False)
        return dict(outcome='quote_expired' if offer['expires_at'] <= int(now()) else 'awaiting_btc',
                    xbt_sats=state['xbt_sats'], btc_sats=offer['btc_sats'],
                    expires_at=offer['expires_at'], automatic_requote=False)
    return dict(outcome='wallet_state_not_created', automatic_resubmission=False)


def status(root, name=None, rpc=RPC.call):
    if not root.exists() and not root.is_symlink():
        if name:
            raise ValueError('unknown attempt')
        return dict(read_only=True, attempts=[])
    private_directory(root)
    directories = [root/identifier(name)] if name else sorted(
        p for p in root.iterdir() if p.name != 'customer.lock')
    answers = []
    for directory in directories:
        identifier(directory.name)
        private_directory(directory)
        intent = private_load(directory/'intent.json')
        if intent['id'] != directory.name:
            raise ValueError('attempt identity mismatch')
        work = directory/'workflow'
        answer = inspect(work, rpc) if work.exists() or work.is_symlink() else dict(outcome='not_started')
        answers.append(dict(attempt=directory.name, direction=intent['direction'], **answer))
    return dict(read_only=True, attempts=answers)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path.home()/'cln-customer-swaps/managed')
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('send', 'receive'):
        s = sub.add_parser(command)
        s.add_argument('--lightning-dir', type=Path, default=Path.home()/'cln-xbt-customer')
        s.add_argument('--token-file', type=Path, default=Path.home()/'.config/cln-swaps/customer-api.json')
        s.add_argument('--url', default='http://127.0.0.1:19840')
        s.add_argument('--retry-quote', action='store_true')
        if command == 'send':
            s.add_argument('--invoice-file', type=Path, required=True)
            s.add_argument('--max-xbt-sats', type=int, required=True)
            s.add_argument('--max-delay', type=int, default=2016)
        else:
            s.add_argument('name', help='Unique short name; repeat it to reuse the same invoice.')
            s.add_argument('--xbt-sats', type=int, required=True)
            s.add_argument('--max-btc-sats', type=int, required=True)
    s = sub.add_parser('resume')
    s.add_argument('attempt')
    s.add_argument('--retry-quote', action='store_true')
    s = sub.add_parser('status')
    group = s.add_mutually_exclusive_group()
    group.add_argument('--attempt')
    group.add_argument('--directory', type=Path, help='Read an older customer workflow without importing it.')
    a = p.parse_args(argv)
    os.umask(0o077)
    try:
        root = a.root.expanduser().absolute()
        if a.command == 'status':
            answer = (dict(read_only=True, **inspect(a.directory.expanduser().absolute()))
                      if a.directory else status(root, a.attempt))
        elif a.command == 'resume':
            answer = resume(root, a.attempt, retry=a.retry_quote)
        else:
            cli = [str(Path(__file__).resolve().parents[2]/'cli/lightning-cli'),
                   '--lightning-dir='+str(a.lightning_dir.expanduser().resolve()),
                   '--network=xbt', '--json', '--notifications=none']
            intent = dict(format='customer-command-v1', direction=a.command, cli=cli,
                          customer_id=identity(cli, RPC.call),
                          token_file=str(a.token_file.expanduser().absolute()), url=a.url)
            if a.command == 'send':
                invoice = private_invoice(a.invoice_file.expanduser())
                if not 0 < a.max_xbt_sats <= 500000 or not 0 < a.max_delay <= 2016:
                    raise ValueError('invalid send limits')
                intent.update(id='send-'+hashlib.sha256(invoice.encode()).hexdigest(),
                              invoice=invoice, max_xbt_sats=a.max_xbt_sats, max_delay=a.max_delay)
            else:
                if not 0 < a.xbt_sats <= 500000 or not 0 < a.max_btc_sats <= 10000:
                    raise ValueError('invalid receiving limits')
                intent.update(id=identifier('receive-'+a.name), xbt_sats=a.xbt_sats,
                              max_btc_sats=a.max_btc_sats)
            answer = start(root, intent, retry=a.retry_quote)
        print(json.dumps(answer))
        return 0
    except CustomerError as error:
        print(json.dumps(error.public()))
    except QuoteRefused as error:
        print(json.dumps(dict(error.public(), message=str(error),
                             next_step='Correct the cause and resume the same attempt with --retry-quote.')))
    except (Exception, KeyboardInterrupt):
        print(json.dumps(dict(event='customer_command_interrupted', details='withheld',
                             automatic_resubmission=False,
                             next_step='Use status, then resume the saved attempt. Preserve all records.')))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
