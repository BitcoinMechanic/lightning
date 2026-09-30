"""Start an unfunded, offline BTC CLN node using verified HTTPS RPC."""
import argparse
import hashlib
import ipaddress
import os
from pathlib import Path
import sys

from btc_https_cli import Client


def peer_options(host, port):
    if host is None:
        if port is not None:
            raise ValueError('listen-port requires listen-host')
        return ['--offline']
    address = ipaddress.IPv4Address(host)
    if (not address.is_private or address.is_unspecified
            or address.is_multicast or address.is_reserved
            or address.is_link_local or address.is_loopback):
        raise ValueError('Use a private LAN IPv4 address')
    port = 19735 if port is None else port
    if not 1024 <= port <= 65535:
        raise ValueError('Use a port between 1024 and 65535')
    return ['--autolisten=false', '--announce-addr-discovered=false',
            '--autoconnect-seeker-peers=0', f'--bind-addr={address}:{port}']


def check_backend(client):
    info = client.call('getblockchaininfo')
    if (info['chain'] != 'main' or info['initialblockdownload']
            or info['pruned'] or info['blocks'] < 961640):
        raise ValueError('Need synced unpruned mainnet backend')
    if client.call('getblockhash', [0]) != (
            '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'):
        raise ValueError('Wrong genesis')
    fork = client.call('getblockhash', [961640])
    if fork == '0000000000000050c1e5f69672f459293be14f46e5a494e7a8c8541396f18eeb':
        raise ValueError('XBT backend refused')
    for block_hash in (fork, info['bestblockhash']):
        header = bytes.fromhex(client.call('getblockheader', [block_hash, False]))
        calculated = hashlib.sha256(hashlib.sha256(header).digest()).digest()[::-1].hex()
        if len(header) != 80 or calculated != block_hash:
            raise ValueError('Not a SHA256d Bitcoin header')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lightning-dir', required=True, type=Path)
    parser.add_argument('--listen-host', help='Explicit private LAN IPv4 address')
    parser.add_argument('--listen-port', type=int, help='LAN peer port (default: 19735)')
    parser.add_argument('--live-pilot', action='store_true',
                        help='Persistently enable the one-quote BTC-to-XBT pilot gate')
    parser.add_argument('--market-swaps', action='store_true',
                        help='Persistently enable bounded oracle-priced swaps')
    args = parser.parse_args()
    try:
        peers = peer_options(args.listen_host, args.listen_port)
    except ValueError:
        parser.error('listener requires a private LAN IPv4 address and port 1024..65535')
    root = args.lightning_dir.expanduser().resolve()
    marker = root / 'btc-https-observer-v1'
    if root.exists() and not marker.is_file():
        parser.error('choose a new dedicated BTC directory')
    source = Path(__file__).resolve().parent / 'btc_https_cli.py'
    binary = source.parents[2] / 'lightningd/lightningd'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('build lightningd first')
    try:
        check_backend(Client())
    except Exception:
        parser.error('BTC HTTPS backend check failed; private details withheld')
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.touch(mode=0o600)
    wrapper = root / 'btc-https-cli'
    temporary = root / 'btc-https-cli.tmp'
    temporary.write_text('#!' + sys.executable + '\n' + source.read_text())
    temporary.chmod(0o700)
    temporary.replace(wrapper)
    command = [str(binary), '--conf=/dev/null', '--network=bitcoin',
               '--lightning-dir=' + str(root), '--bitcoin-cli=' + str(wrapper),
               '--log-file=' + str(root / 'lightning.log'), *peers]
    gate = root / 'live-swap-gate.py'
    # Once installed, reload on every restart: omitting the flag must not
    # accidentally drop held hooks. Never replace/delete its quotes database.
    market_marker = root / 'market-swaps-v1'
    if args.market_swaps:
        market_marker.touch(mode=0o600)
    if args.live_pilot or gate.exists() or market_marker.exists():
        temporary = root / 'live-swap-gate.tmp'
        temporary.write_text('#!' + sys.executable + '\n' +
                             source.with_name('quote_plugin.py').read_text())
        temporary.chmod(0o700)
        temporary.replace(gate)
        profile = 'live-market-v1' if market_marker.exists() else 'live-pilot-v2'
        command.extend(['--plugin=' + str(gate), '--xbt-live-pilot=' + profile])
    mode = 'offline' if args.listen_host is None else 'LAN listener'
    print('BTC backend verified; starting BTC node (' + mode + ').', flush=True)
    os.execv(str(binary), command)


if __name__ == '__main__':
    main()
