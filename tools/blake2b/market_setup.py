"""Create a private bounded market configuration from the completed pilot."""
import argparse
import json
import os
from pathlib import Path

import live_pilot as pilot
from market_policy import policy
from smoke_regtest import Lab


def setup(previous, destination, max_btc_sats, max_xbt_sats, margin_bps):
    old = json.loads((previous / 'quote.json').read_text())
    state = json.loads((previous / 'state.json').read_text())
    if (state['phase'] != 'btc_released' or state['payment_hash'] != old['terms']['payment_hash']
            or old['config']['profile'] != pilot.PROFILE_V2):
        raise ValueError('setup requires the completed v2 pilot')
    config = {key: old['config'][key] for key in ('btc_cli', 'xbt_cli')}
    pilot.verify_nodes(dict(config, node_ids=old['node_ids']), Lab.rpc)
    pilot.require_reserves(config, Lab.rpc)
    status = Lab.rpc(config['btc_cli'], 'xbt-quote-status', state['payment_hash'])
    if (status['payment_hash'] != state['payment_hash'] or status['phase'] != 'resolved'
            or status['binding'] != state['btc_binding']):
        raise ValueError('original gate binding has not resolved')
    btc = [c for c in Lab.rpc(config['btc_cli'], 'listpeerchannels')['channels']
           if c.get('short_channel_id') == old['terms']['btc_channel']]
    receiver = old['controller']['route'][0]['id']
    xbt = [c for c in Lab.rpc(config['xbt_cli'], 'listpeerchannels')['channels']
           if c['peer_id'] == receiver and c['state'] == 'CHANNELD_NORMAL']
    if len(btc) != 1 or len(xbt) != 1:
        raise ValueError('need exactly one original BTC and one active receiver XBT channel')
    for c in (btc[0], xbt[0]):
        if c['state'] != 'CHANNELD_NORMAL' or not c['peer_connected'] or c.get('htlcs'):
            raise ValueError('channels must be normal, connected and free of pending HTLCs')
    config.update(profile=pilot.PROFILE_MARKET, market=dict(
        btc_channel=btc[0]['short_channel_id'], xbt_channel=xbt[0]['short_channel_id'],
        xbt_peer=receiver, max_btc_sats=max_btc_sats, max_xbt_sats=max_xbt_sats,
        margin_bps=margin_bps))
    policy(config)
    if destination.exists():
        if json.loads(destination.read_text()) != config:
            raise ValueError('existing configuration differs; not overwritten')
    else:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return dict(config_ready=True, profile=pilot.PROFILE_MARKET,
                max_btc_sats=max_btc_sats, max_xbt_sats=max_xbt_sats, margin_bps=margin_bps)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-directory', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--max-btc-sats', required=True, type=int)
    parser.add_argument('--max-xbt-sats', required=True, type=int)
    parser.add_argument('--margin-bps', required=True, type=int)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(setup(args.previous_directory.resolve(), args.config.resolve(),
                               args.max_btc_sats, args.max_xbt_sats, args.margin_bps)))
        return 0
    except Exception as exc:
        print(json.dumps({'event': 'error', 'reason': str(exc) if isinstance(exc, ValueError)
                          and not isinstance(exc, json.JSONDecodeError) else 'setup failed; private details withheld'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
