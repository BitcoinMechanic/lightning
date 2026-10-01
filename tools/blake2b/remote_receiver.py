"""Bind a separate market configuration to a direct XBT receiver, using operator RPCs only."""
import argparse
import json
import os
from pathlib import Path
import re

import live_pilot as pilot
from market_policy import policy
from swap_rpc import RPC
from swap_service import config_from, identities


def bind(source, receiver_file, destination):
    config = config_from(source)
    if config.get('profile') != pilot.PROFILE_MARKET:
        raise ValueError('requires an existing live market configuration')
    with receiver_file.open('rb') as stream:
        raw = stream.read(129)
    if len(raw) > 128:
        raise ValueError('invalid receiver ID file')
    try:
        receiver = raw.decode('ascii').strip()
    except UnicodeDecodeError:
        raise ValueError('invalid receiver ID file') from None
    if not re.fullmatch(r'0[23][0-9a-f]{64}', receiver):
        raise ValueError('receiver ID must be a compressed public key')
    if source.resolve() == destination.resolve():
        raise ValueError('choose a separate destination configuration')
    if receiver in identities(config):
        raise ValueError('receiver must be distinct from the operators')
    pilot.require_reserves(config, RPC.call)
    p = policy(config)
    btc = [c for c in RPC.call(config['btc_cli'], 'listpeerchannels')['channels']
           if c.get('short_channel_id') == p['btc_channel']]
    xbt = [c for c in RPC.call(config['xbt_cli'], 'listpeerchannels')['channels']
           if c.get('peer_id') == receiver and c.get('state') == 'CHANNELD_NORMAL']
    if len(btc) != 1 or len(xbt) != 1:
        raise ValueError('need the bound BTC channel and exactly one normal receiver channel')
    for c in (btc[0], xbt[0]):
        if (c.get('state') != 'CHANNELD_NORMAL' or not c.get('peer_connected')
                or c.get('htlcs') or not c.get('short_channel_id')):
            raise ValueError('channels must be connected, normal and free of pending HTLCs')
    if btc[0].get('receivable_msat', 0) <= 0 or xbt[0].get('spendable_msat', 0) <= 0:
        raise ValueError('channels need incoming BTC and outgoing XBT liquidity')
    config['market'] = dict(p, xbt_peer=receiver, xbt_channel=xbt[0]['short_channel_id'])
    policy(config)
    # No mutation RPCs. Never replace a different configuration or old swap.
    if destination.exists():
        if json.loads(destination.read_text()) != config:
            raise ValueError('existing configuration differs; not overwritten')
    else:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return dict(config_ready=True, profile=pilot.PROFILE_MARKET,
                receiver_rpc_required=False, channel_funding_attempted=False,
                max_btc_sats=p['max_btc_sats'], max_xbt_sats=p['max_xbt_sats'],
                margin_bps=p['margin_bps'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-config', required=True, type=Path)
    parser.add_argument('--receiver-id-file', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(bind(args.source_config, args.receiver_id_file, args.config)))
        return 0
    except Exception as exc:
        reason = (str(exc) if type(exc) in (ValueError, RuntimeError)
                  else 'setup failed; private details withheld')
        print(json.dumps(dict(event='error', reason=reason)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
