"""Explicit, private, identity-bound activation of the bounded reverse pilot."""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import sys

from service_manager import private_load
from swap_controller import save
from swap_rpc import RPC

ACTIVE = ContextVar('reverse_live_pilot_active', default=False)
PROFILE = 'reverse-live-v1'


def record(settings):
    ids = settings['node_ids']
    if (len(ids) != 2 or len(set([*ids, settings['receiver_id']])) != 3
            or any(not re.fullmatch('0[23][0-9a-f]{64}', i) for i in [*ids, settings['receiver_id']])
            or settings.get('reverse_profile', PROFILE) != PROFILE):
        raise ValueError('invalid live service identities or profile')
    value = dict(profile=PROFILE, node_ids=ids, payer_id=settings['receiver_id'],
                btc_cli=settings['btc_cli'], xbt_cli=settings['xbt_cli'],
                xbt_root=settings['roots']['xbt'],
                swap_root=settings['swap_root'], btc_sats=1500, max_xbt_sats=500000,
                max_routing_fee_sats=30, margin_bps=100, max_delay=576)
    if settings.get('deployment') != 'operator-pair-v1':
        value['payer_cli'] = settings['receiver_cli']
    return value


def configured(settings):
    if 'reverse_live' not in settings:
        return False
    if settings['reverse_live'] != record(settings):
        raise ValueError('reverse activation binding changed')
    return True


@contextmanager
def activation(settings):
    token = ACTIVE.set(configured(settings))
    try:
        yield
    finally:
        ACTIVE.reset(token)


def directory_settings(directory, settings_path):
    settings = private_load(settings_path)
    if not configured(settings):
        raise ValueError('reverse live pilot has not been enabled')
    if directory.resolve().parent != Path(settings['swap_root']).resolve():
        raise ValueError('reverse directory outside monitored root')
    from reverse_service import binding
    quote = private_load(directory/'reverse-quote.json')
    if quote['config'] != binding(settings) or quote['terms']['profile'] != PROFILE:
        raise ValueError('reverse quote differs from activated service')
    return settings


def install(settings_path, rpc=RPC.call):
    settings = private_load(settings_path)
    expected = record(settings)
    if 'reverse_live' in settings and not configured(settings):
        raise ValueError('existing activation differs')
    root = Path(settings['roots']['xbt'])
    if not (root/'xbt-observer-v1').is_file() or not (root/'xbt/hsm_secret').is_file():
        raise ValueError('existing operator wallet required')
    for key, node, network in (('btc_cli', settings['node_ids'][0], 'bitcoin'),
                                ('xbt_cli', settings['node_ids'][1], 'xbt'),
                                ('receiver_cli', settings['receiver_id'], 'xbt')):
        if key == 'receiver_cli' and settings.get('deployment') == 'operator-pair-v1':
            continue
        info = rpc(settings[key], 'getinfo')
        if info['id'] != node or info['network'] != network or any(k.startswith('warning_') for k in info):
            raise ValueError('node identity or readiness mismatch')
        channels = rpc(settings[key], 'listpeerchannels')['channels']
        if any(c.get('htlcs') for c in channels):
            raise ValueError('pending HTLCs; activation postponed')
    from live_pilot import require_reserves
    require_reserves(settings, rpc)
    if settings.get('reverse_live') != expected:
        backup = settings_path.with_name('settings.before-reverse-live.json')
        if backup.exists():
            if private_load(backup) != settings:
                raise ValueError('existing settings backup differs')
        else:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(settings, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        settings['reverse_live'] = expected
        save(settings_path, settings)
    return dict(configured=True, btc_sats=1500, max_xbt_sats=500000,
                max_routing_fee_sats=30, payment_started=False, restart_required=True)


def plugin(root, settings_path):
    settings = private_load(settings_path)
    if not configured(settings) or Path(settings['roots']['xbt']).resolve() != root.resolve():
        raise ValueError('reverse gate requires activated operator directory')
    content = ('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\n'+
               f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'+
               'from reverse_activation import gate_main\n'+
               f'gate_main(Path({str(settings_path.resolve())!r}), Path({str(root.resolve())!r}))\n')
    path = root/'reverse-live-gate.py'
    if path.is_symlink():
        raise ValueError('gate wrapper cannot be a symlink')
    if path.exists():
        if path.read_text() != content:
            raise ValueError('existing gate wrapper differs')
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
    path.chmod(0o700)
    return path


def gate_main(settings_path, root):
    settings = private_load(settings_path)
    if not configured(settings) or Path(settings['roots']['xbt']).resolve() != root.resolve():
        raise ValueError('reverse gate operator binding mismatch')
    from reverse_gate import main
    with activation(settings):
        main(root/'reverse-live-gate.json', live=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'status'])
    parser.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        path = args.settings.expanduser().resolve()
        if args.command == 'install':
            result = install(path)
        else:
            settings = private_load(path)
            if not configured(settings):
                raise ValueError('reverse pilot not configured')
            info = RPC.call(settings['xbt_cli'], 'getinfo')
            if info['network'] != 'xbt' or info['id'] != settings['node_ids'][1]:
                raise ValueError('operator identity mismatch')
            gate = RPC.call(settings['xbt_cli'], 'reverse-pilot-info')
            if gate != dict(profile=PROFILE, gate_active=True):
                raise ValueError('active live gate unavailable')
            response = RPC.call(settings['xbt_cli'], 'xbt-held')
            result = dict(configured=True, gate_rpc_ready=True,
                          held_htlc_count=len(response['held']), payment_started=False)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps(dict(event='reverse_activation_error', details='withheld')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
