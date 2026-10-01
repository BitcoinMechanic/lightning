"""Detach the existing customer wallet from the operator target without stopping it.

Rewrites private configuration and known generated unit files only. No node RPCs,
payments, wallet moves, service stops or restarts. Repeatable after interruption.
"""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile

from service_manager import private_load, units, quote_arg
from swap_controller import save
from reverse_activation import configured, record


def write_text(path, text):
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def separate(directory, unit_directory):
    fd = os.open(directory/'customer-separation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _separate(directory, unit_directory)
    finally:
        os.close(fd)


def _separate(directory, unit_directory):
    path = directory/'settings.json'
    backup = directory/'settings.before-customer-separation.json'
    current = private_load(path)
    original = private_load(backup) if backup.exists() else current
    if original.get('deployment') is not None:
        raise ValueError('original managed-customer settings required')
    if len(set(original['roots'].values())) != 3:
        raise ValueError('three distinct existing node directories required')
    activated = configured(original)
    updated = copy.deepcopy(original)
    updated['deployment'] = 'operator-pair-v1'
    del updated['receiver_cli']
    del updated['roots']['receiver']
    if activated:
        updated['reverse_live'] = record(updated)
    if current not in (original, updated):
        raise ValueError('settings changed since separation began')

    customer_dir = directory/'customer-wallet'
    customer_settings = {k: original[k] for k in ('repo', 'python', 'bitcoin_cli')}
    customer_settings['roots'] = {'receiver': original['roots']['receiver']}
    credentials = private_load(directory/'xbt-rpc.json')
    expected_customer = {'settings.json': customer_settings, 'xbt-rpc.json': credentials}
    if customer_dir.is_symlink():
        raise ValueError('customer settings directory cannot be a symlink')
    if customer_dir.exists() and customer_dir.stat().st_mode & 0o077:
        raise ValueError('customer settings directory must be private')
    for name, value in expected_customer.items():
        if (customer_dir/name).exists() and private_load(customer_dir/name) != value:
            raise ValueError('existing customer settings differ')

    before = units(original, directory)
    after = units(updated, directory)
    receiver_unit = before['cln-xbt-receiver.service']
    receiver_unit = receiver_unit.replace('PartOf=cln-swaps.target\n', '')
    old_arg = quote_arg('--directory')+' '+quote_arg(directory)
    if receiver_unit.count(old_arg) != 1:
        raise ValueError('unexpected customer service command')
    receiver_unit = receiver_unit.replace(old_arg, quote_arg('--directory')+' '+quote_arg(customer_dir))
    receiver_unit += '\n[Install]\nWantedBy=default.target\n'
    after['cln-xbt-receiver.service'] = receiver_unit
    # Refuse all unrelated changes before writing configuration or any unit.
    for name, value in before.items():
        unit = unit_directory/name
        if unit.is_symlink() or not unit.is_file() or unit.read_text() not in (value, after[name]):
            raise ValueError('generated service unit differs; inspect locally')
    if not backup.exists():
        save(backup, original)
    customer_dir.mkdir(mode=0o700, exist_ok=True)
    for name, value in expected_customer.items():
        if not (customer_dir/name).exists():
            save(customer_dir/name, value)
    for name, value in after.items():
        if (unit_directory/name).read_text() != value:
            write_text(unit_directory/name, value)
    if current != updated:
        save(path, updated)
    return dict(separated=True, operator_services=3, customer_wallet_independent=True,
                wallet_data_moved=False, services_restarted=False,
                daemon_reload_required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path.home()/'.config/cln-swaps')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = separate(args.directory.expanduser().resolve(), Path.home()/'.config/systemd/user')
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps(dict(event='customer_separation_error', details='withheld')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
