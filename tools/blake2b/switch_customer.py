"""Switch a quiescent operator to a new customer with resumable private backups."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

from reverse_activation import configured, record
from service_manager import private_load
from swap_controller import save
from swap_rpc import RPC


def recovery_settings(settings, quote):
    from reverse_service import binding
    if quote['config'] == binding(settings):
        return settings
    matches = []
    for old in settings.get('reverse_previous_customers', []):
        if any(old[k] != settings[k] for k in ('btc_cli', 'xbt_cli', 'node_ids', 'swap_root')):
            raise ValueError('historical operator binding differs')
        if old['roots']['xbt'] != settings['roots']['xbt'] or not configured(old):
            raise ValueError('historical activation differs')
        if quote['config'] == binding(old):
            matches.append(old)
    if len(matches) != 1:
        raise ValueError('historical customer binding unavailable or ambiguous')
    return matches[0]


def stopped():
    for name in ('cln-swap-quotes', 'cln-swap-recovery'):
        p = subprocess.run(['systemctl', '--user', 'is-active', name],
                           capture_output=True, text=True, timeout=10)
        if p.stdout.strip() not in ('inactive', 'failed'):
            raise ValueError('stop quote API and recovery worker first')


def ready(settings, new_id, rpc):
    for i, (key, network) in enumerate((('btc_cli', 'bitcoin'), ('xbt_cli', 'xbt'))):
        info = rpc(settings[key], 'getinfo')
        if (info['id'] != settings['node_ids'][i] or info['network'] != network
                or any(k.startswith('warning_') for k in info)):
            raise ValueError('operator identity or readiness mismatch')
        channels = rpc(settings[key], 'listpeerchannels')['channels']
        if any(c.get('htlcs') for c in channels):
            raise ValueError('pending HTLCs')
        if key == 'xbt_cli':
            selected = [c for c in channels if c['peer_id'] == new_id and c['state'] == 'CHANNELD_NORMAL']
            if len(selected) != 1 or not selected[0].get('peer_connected'):
                raise ValueError('new customer channel unavailable')
    if rpc(settings['xbt_cli'], 'xbt-held')['held']:
        raise ValueError('held XBT hooks')
    root = Path(settings['swap_root'])
    for quote_path in root.glob('*/reverse-quote.json'):
        if not quote_path.resolve().is_relative_to(root.resolve()):
            raise ValueError('quote outside root')
        quote = private_load(quote_path)
        recovery_settings(settings, quote)
        state = private_load(quote_path.parent/'reverse-state.json')
        gate = rpc(settings['xbt_cli'], 'reverse-status', quote['terms']['payment_hash'])
        phase = state['phase']
        if (phase not in ('xbt_released', 'xbt_failed')
                or state['reverse_quote'] != quote['terms']
                or state['payment_hash'] != quote['terms']['payment_hash']
                or gate['phase'] != ('resolved' if phase == 'xbt_released' else 'failed')
                or gate['binding'] != state['xbt_binding']):
            raise ValueError('historical reverse swap not terminal')
    for path in set(root.glob('*/quote.json')) | set(root.glob('*/swap/quote.json')):
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('forward quote outside root')
        state_path = path.parent/'state.json'
        if state_path.exists():
            state = private_load(state_path)
            if state['phase'] not in ('btc_released', 'btc_failed'):
                raise ValueError('historical forward swap not terminal')
        else:
            quote = private_load(path)
            gate = rpc(settings['btc_cli'], 'xbt-quote-status', quote['terms']['payment_hash'])
            if (gate['phase'] != 'quoted' or gate['payment_hash'] != quote['terms']['payment_hash']
                    or quote['terms']['expires_at'] > int(time.time())):
                raise ValueError('unstarted forward quote requires inspection')


def migrate(settings_path, identity_path, rpc=RPC.call, check_stopped=stopped):
    check_stopped()
    settings_path = Path(settings_path)
    directory = settings_path.parent
    fd = os.open(directory/'customer-migration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = private_load(identity_path)['customer_id']
        path = directory/'vm-customer-migration.json'
        credential_path = directory/'customer-api.json'
        current = private_load(settings_path)
        credential = private_load(credential_path)
        if path.exists():
            plan = private_load(path)
            if plan['new']['receiver_id'] != target:
                raise ValueError('migration target differs')
            if current not in (plan['old'], plan['new']) or credential not in (plan['old_token'], plan['new_token']):
                raise ValueError('configuration changed outside migration')
        else:
            if (current.get('deployment') != 'operator-pair-v1' or not configured(current)
                    or target == current['receiver_id'] or credential['payer_id'] != current['receiver_id']):
                raise ValueError('unexpected customer or activation')
            new = copy.deepcopy(current)
            previous = copy.deepcopy(current)
            previous.pop('reverse_previous_customers', None)
            new['reverse_previous_customers'] = [*current.get('reverse_previous_customers', []), previous]
            new['receiver_id'] = target
            new['reverse_live'] = record(new)
            plan = dict(old=current, new=new, old_token=credential,
                        new_token=dict(token=secrets.token_hex(32), payer_id=target))
            ready(current, target, rpc)
            save(path, plan)  # Preserve exact old and new values before replacing either file.
        if current != plan['new'] or credential != plan['new_token']:
            ready(plan['old'], target, rpc)
            save(settings_path, plan['new'])
            save(credential_path, plan['new_token'])
        return dict(customer_switched=True, token_rotated=True, historical_records_preserved=True,
                    services_started=False)
    finally:
        os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    p.add_argument('--identity-file', type=Path, default=Path.home()/'.config/cln-swaps/vm-customer.json')
    args = p.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(migrate(args.settings.expanduser(), args.identity_file.expanduser())))
        return 0
    except Exception:
        print(json.dumps(dict(event='customer_migration_blocked', details='withheld',
                             next_step='Keep quote API stopped; inspect before restarting services.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
