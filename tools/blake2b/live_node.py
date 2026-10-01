"""Start an experimental XBT node against an existing Knots RPC.

RPC credentials stay in the environment and the bitcoin-cli password pipe.
No channel connection, funding, or payment is requested by this launcher.
Defaults to offline; optionally accept peers on an explicit localhost port.
"""
import argparse
import os
from pathlib import Path
import sys


WRAPPER = '''import os
import subprocess
import sys

required = ("XBT_RPC_HOST", "XBT_RPC_PORT", "XBT_RPC_USER", "XBT_RPC_PASSWORD")
if any(not os.environ.get(k) for k in required):
    sys.exit("Missing XBT RPC environment variables")
if any("\\n" in os.environ[k] or "\\r" in os.environ[k] for k in required):
    sys.exit("RPC environment values must not contain newlines")
args = sys.argv[1:]
if any(a.startswith(("-rpcpassword", "-rpcuser", "-rpcconnect", "-rpcport", "-stdinrpcpass")) for a in args):
    sys.exit("Set RPC credentials and endpoint through XBT environment variables only")
payload = sys.stdin.buffer.read() if "-stdin" in args else b""
command = [BINARY, "-conf=/dev/null", "-datadir=" + DATADIR,
           "-rpcconnect=" + os.environ["XBT_RPC_HOST"],
           "-rpcport=" + os.environ["XBT_RPC_PORT"],
           "-rpcuser=" + os.environ["XBT_RPC_USER"], "-stdinrpcpass", *args]
result = subprocess.run(command, input=os.environ["XBT_RPC_PASSWORD"].encode() + b"\\n" + payload)
sys.exit(result.returncode)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lightning-dir', required=True, type=Path)
    parser.add_argument('--bitcoin-cli', required=True, type=Path)
    parser.add_argument('--local-peer-port', type=int,
                        help='Accept Lightning peers on 127.0.0.1 only, instead of offline mode.')
    parser.add_argument('--reverse-settings', type=Path, help='Private activated reverse-pilot settings for this operator only.')
    args = parser.parse_args()
    if args.local_peer_port is not None and not 1024 <= args.local_peer_port <= 65535:
        parser.error('local peer port must be between 1024 and 65535')
    required = ('XBT_RPC_HOST', 'XBT_RPC_PORT', 'XBT_RPC_USER', 'XBT_RPC_PASSWORD')
    if any(not os.environ.get(k) for k in required):
        parser.error('export all four XBT_RPC environment variables first')
    if any('\n' in os.environ[k] or '\r' in os.environ[k] for k in required):
        parser.error('RPC values must not contain newlines')
    try:
        port = int(os.environ['XBT_RPC_PORT'])
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        parser.error('invalid RPC port')
    binary = args.bitcoin_cli.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('bitcoin-cli is not executable')
    root = args.lightning_dir.resolve()
    marker = root / 'xbt-observer-v1'
    if root.exists() and not marker.is_file():
        parser.error('choose a new dedicated data directory')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.touch(mode=0o600)
    rpcdir = root / 'rpc-client'
    rpcdir.mkdir(mode=0o700, exist_ok=True)
    wrapper = root / 'knots-env-cli'
    wrapper.write_text('#!' + sys.executable + '\n' +
                       'BINARY = ' + repr(str(binary)) + '\n' +
                       'DATADIR = ' + repr(str(rpcdir)) + '\n' + WRAPPER)
    wrapper.chmod(0o700)
    lightningd = Path(__file__).resolve().parents[2] / 'lightningd/lightningd'
    command = [str(lightningd), '--conf=/dev/null', '--network=xbt',
               '--lightning-dir=' + str(root), '--bitcoin-cli=' + str(wrapper),
               '--log-file=' + str(root / 'lightning.log')]
    if args.local_peer_port is None:
        command.append('--offline')
        mode = 'offline'
    else:
        command.extend(['--autolisten=false', '--announce-addr-discovered=false',
                        '--autoconnect-seeker-peers=0',
                        '--bind-addr=127.0.0.1:' + str(args.local_peer_port)])
        mode = 'localhost listener'
    if args.reverse_settings is not None:
        from reverse_activation import plugin
        command.append('--plugin='+str(plugin(root, args.reverse_settings.expanduser().resolve())))
    print('Starting experimental XBT node (' + mode + '); log: ' + str(root / 'lightning.log'), flush=True)
    os.execv(str(lightningd), command)


if __name__ == '__main__':
    main()
