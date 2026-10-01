"""Prepare user systemd units; capture private RPC settings without printing them."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

from smoke_regtest import Lab
from swap_controller import save
from swap_service import config_from, identities
import live_pilot as pilot

ROLES = {'btc': 'cln-btc-operator', 'xbt': 'cln-xbt-operator',
         'receiver': 'cln-xbt-receiver', 'recovery': 'cln-swap-recovery'}


def private_load(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private file must be owned by this user with mode 0600')
        return json.load(f)


def write_once(path, value, replace=False):
    if path.exists():
        previous = private_load(path)
        if previous == value:
            return
        if not replace:
            raise ValueError('existing settings differ; explicit --replace is required')
    save(path, value)
    path.chmod(0o600)


def credentials(directory, kind, replace=False):
    prefix = kind.upper() + '_RPC_'
    keys = [prefix + n for n in ('HOST', 'PORT', 'USER', 'PASSWORD')]
    if kind == 'btc':
        keys += ['BTC_RPC_CA', 'BTC_LN_HOST']
    values = {k: os.environ.get(k) for k in keys}
    if any(not v or '\n' in v or '\r' in v or '\0' in v for v in values.values()):
        raise ValueError('required exported variables are missing or contain invalid characters')
    if not 1 <= int(values[prefix+'PORT']) <= 65535:
        raise ValueError('invalid RPC port')
    if kind == 'btc':
        from live_btc_node import peer_options
        peer_options(values['BTC_LN_HOST'], 19735)
        ca = Path(values['BTC_RPC_CA']).expanduser().resolve()
        if not ca.is_file():
            raise ValueError('BTC CA file not found')
        values['BTC_RPC_CA'] = str(ca)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError('service settings directory must have mode 0700')
    write_once(directory/(kind+'-rpc.json'), values, replace)


def quote_arg(value):
    value = str(value)
    if any(c in value for c in ('\n', '\r', '\0')):
        raise ValueError('invalid path in unit')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def units(settings, directory):
    runtime = Path(__file__).with_name('service_runtime.py').resolve()
    root = settings['repo']
    result = {}
    for role, name in ROLES.items():
        argv = [settings['python'], str(runtime), '--directory', str(directory), '--role', role]
        after = ''
        if role == 'recovery':
            after = 'After=cln-btc-operator.service cln-xbt-operator.service cln-xbt-receiver.service\n'
        stop = ''
        if role != 'recovery':
            network = 'bitcoin' if role == 'btc' else 'xbt'
            stopargs = [settings['cli'], '--lightning-dir='+settings['roots'][role],
                        '--network='+network, 'stop']
            stop = 'ExecStop='+' '.join(map(quote_arg, stopargs))+'\n'
        result[name+'.service'] = (
            '[Unit]\nDescription=Experimental CLN '+role+'\nPartOf=cln-swaps.target\n'
            'StartLimitIntervalSec=0\n'+after+'\n[Service]\nType=simple\nUMask=0077\n'
            'WorkingDirectory='+str(root).replace('%', '%%')+'\n'
            'ExecStart='+' '.join(map(quote_arg, argv))+'\n'+stop+
            'Restart=on-failure\nRestartSec=10\nRestartPreventExitStatus=78\n'
            'TimeoutStopSec=120\nKillMode=mixed\nNoNewPrivileges=true\n')
    result['cln-swaps.target'] = ('[Unit]\nDescription=Experimental BTC-XBT swap nodes and recovery\n'
        'Wants='+' '.join(n+'.service' for n in ROLES.values())+'\n\n[Install]\nWantedBy=default.target\n')
    return result


def prepare(directory, config_path, bitcoin_cli, swap_root):
    if sys.prefix == sys.base_prefix:
        raise ValueError('run prepare with the project venv Python')
    config = config_from(config_path)
    if config['profile'] != pilot.PROFILE_MARKET:
        raise ValueError('requires market operator configuration')
    ids = identities(config)
    repo = Path(__file__).resolve().parents[2]
    roots = {'btc': str(Path.home()/'cln-btc-observe'),
             'xbt': str(Path.home()/'cln-xbt-observe'),
             'receiver': str(Path.home()/'cln-xbt-peer')}
    cli = str(repo/'cli/lightning-cli')
    expected = {role: [cli, '--lightning-dir='+roots[role], '--network='+network,
                        '--json', '--notifications=none']
                for role, network in (('btc', 'bitcoin'), ('xbt', 'xbt'))}
    if config['btc_cli'] != expected['btc'] or config['xbt_cli'] != expected['xbt']:
        raise ValueError('operator CLI paths differ from expected local deployment')
    receiver_cli = [cli, '--lightning-dir='+roots['receiver'], '--network=xbt', '--json', '--notifications=none']
    receiver = Lab.rpc(receiver_cli, 'getinfo')
    if receiver['network'] != 'xbt' or receiver['id'] != config['market']['xbt_peer']:
        raise ValueError('receiver identity mismatch')
    for role, marker in (('btc', 'btc-https-observer-v1'), ('xbt', 'xbt-observer-v1'), ('receiver', 'xbt-observer-v1')):
        if not (Path(roots[role])/marker).is_file():
            raise ValueError('existing node marker missing; refusing new node setup')
    if not bitcoin_cli.is_file() or not os.access(bitcoin_cli, os.X_OK):
        raise ValueError('bitcoin-cli executable not found')
    settings = dict(repo=str(repo), python=os.path.abspath(sys.executable), cli=cli,
                    bitcoin_cli=str(bitcoin_cli.resolve()), roots=roots,
                    btc_cli=expected['btc'], xbt_cli=expected['xbt'], receiver_cli=receiver_cli,
                    node_ids=ids, receiver_id=receiver['id'], swap_root=str(swap_root.resolve()))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError('service settings directory must have mode 0700')
    write_once(directory/'settings.json', settings)
    destination = Path.home()/'.config/systemd/user'
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    texts = units(settings, directory)
    # Refuse before writing any unit if an unrelated unit has this name.
    for name, value in texts.items():
        path = destination/name
        if path.exists() and path.read_text() != value:
            raise ValueError('existing unit differs; inspect locally before replacing it')
    for name, value in texts.items():
        path = destination/name
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(value)
    return {'prepared': True, 'units': len(texts), 'started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path.home()/'.config/cln-swaps')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--config', required=True, type=Path)
    p.add_argument('--bitcoin-cli', required=True, type=Path)
    p.add_argument('--swap-root', type=Path, default=Path.home()/'cln-live-pilot')
    p = sub.add_parser('credentials')
    p.add_argument('kind', choices=['btc', 'xbt'])
    p.add_argument('--replace', action='store_true')
    sub.add_parser('status')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = args.directory.resolve()
        if args.command == 'prepare':
            result = prepare(directory, args.config, args.bitcoin_cli.resolve(), args.swap_root)
        elif args.command == 'credentials':
            credentials(directory, args.kind, args.replace)
            result = {'credentials_saved': args.kind}
        else:
            result = private_load(directory/'health.json')
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'operation failed; private details withheld'
        print(json.dumps({'event': 'error', 'reason': message}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
