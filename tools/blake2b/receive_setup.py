"""Explicitly enable bounded receiving through the existing authenticated API."""
import argparse
import json
import os
from pathlib import Path

from receive_service import configuration
from service_manager import private_load
from swap_controller import save
from swap_service import config_from, identities


def setup(settings_path, config_path):
    settings = private_load(settings_path)
    config = config_from(config_path)
    candidate = dict(settings, receive_config=config)
    configuration(candidate)
    if identities(config) != settings['node_ids']:
        raise ValueError('operator identity differs')
    if 'receive_config' in settings and settings['receive_config'] != config:
        raise ValueError('existing receiving configuration differs')
    from swap_rpc import RPC
    import live_pilot as pilot
    pilot.require_reserves(config, RPC.call)
    for role, key in (('btc', 'btc_channel'), ('xbt', 'xbt_channel')):
        matches = [c for c in RPC.call(config[role+'_cli'], 'listpeerchannels')['channels']
                   if c.get('short_channel_id') == config['market'][key]]
        if (len(matches) != 1 or matches[0]['state'] != 'CHANNELD_NORMAL'
                or not matches[0].get('peer_connected') or matches[0].get('htlcs')
                or (role == 'xbt' and matches[0]['peer_id'] != config['market']['xbt_peer'])):
            raise ValueError('configured channel not ready')
    if 'receive_config' not in settings:
        save(settings_path, candidate)
    return dict(receiving_enabled=True, max_btc_sats=config['market']['max_btc_sats'],
                max_xbt_sats=config['market']['max_xbt_sats'], services_restarted=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    p.add_argument('--config', type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(setup(a.settings.expanduser(), a.config.expanduser())))
        return 0
    except Exception:
        print(json.dumps(dict(event='receive_setup_failed', details='withheld')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
