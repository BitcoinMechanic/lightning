#!/usr/bin/env python3
"""Private, disposable XBT regtest startup/isolation test. Standard library only.

Requires the pinned Knots release and a built CLN checkout. Never uses default
node data directories. No channels or payments are funded by this test.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]


def wait_until(fn, proc=None, timeout=40):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f'process exited with status {proc.returncode}')
        try:
            value = fn()
            if value:
                return value
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f'timed out waiting for service: {last}')


class Lab:
    def __init__(self, root, bitcoind, bitcoin_cli):
        self.root = root
        self.bitcoind = bitcoind
        self.bitcoin_cli = bitcoin_cli
        self.procs = []
        self.logs = []
        self.ports = set()

    def port(self):
        registry = os.environ.get('CLN_BLAKE2B_TEST_PORTS')
        if registry:
            # Parallel regression workers must not reuse a port between its
            # ephemeral bind probe and daemon startup. Reserve for the run.
            with open(registry, 'a+') as ports:
                fcntl.flock(ports, fcntl.LOCK_EX)
                ports.seek(0)
                self.ports.update(int(line) for line in ports if line.strip())
                port = self._port()
                ports.write(str(port) + '\n')
                ports.flush()
                return port
        return self._port()

    def _port(self):
        while True:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            if port not in self.ports:
                self.ports.add(port)
                return port

    def start(self, args, logfile, new_session=False):
        log = logfile.open('w')
        self.logs.append(log)
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=new_session)
        self.procs.append(proc)
        return proc

    @staticmethod
    def rpc(args, *command):
        result = subprocess.run([*args, *map(str, command)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=20)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, result.args,
                                                result.stdout, result.stderr)
        # bitcoin-cli prints nothing for JSON null (e.g. a spent gettxout).
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()

    def node(self, name, fork):
        data = self.root / name
        data.mkdir(mode=0o700)
        rpcport = self.port()
        args = [self.bitcoind, f'-datadir={data}', '-conf=/dev/null', '-regtest',
                '-server=1', '-listen=0', '-dnsseed=0', '-discover=0', '-connect=0',
                '-rpcbind=127.0.0.1', '-rpcallowip=127.0.0.1', f'-rpcport={rpcport}',
                '-fallbackfee=0.00001']
        if fork:
            args += ['-testactivationheight=blake2b@1', '-rdtsexpiry=2147483647',
                     '-blake2b_headline=CLN XBT isolated regtest']
        proc = self.start(args, data / 'console.log')
        cli = [self.bitcoin_cli, f'-datadir={data}', '-conf=/dev/null',
               '-regtest', f'-rpcport={rpcport}']
        wait_until(lambda: self.rpc(cli, 'getblockchaininfo'), proc)
        self.rpc(cli, 'createwallet', 'smoke')
        address = self.rpc(cli, 'getnewaddress')
        self.rpc(cli, 'generatetoaddress', 101, address)
        return {'data': data, 'port': rpcport, 'cli': cli, 'proc': proc}

    def lightning(self, name, network, backend, expect_success=True, plugins=()):
        data = self.root / name
        data.mkdir(mode=0o700, exist_ok=True)
        port = self.port()
        logfile = data / 'console.log'
        proc = self.start([
            str(ROOT / 'lightningd/lightningd'), f'--lightning-dir={data}',
            f'--network={network}', f'--bitcoin-cli={self.bitcoin_cli}',
            f'--bitcoin-datadir={backend["data"]}',
            f'--bitcoin-rpcport={backend["port"]}',
            '--developer', '--dev-bitcoind-poll=1',
            f'--bind-addr=127.0.0.1:{port}', '--autolisten=false', '--disable-dns',
            '--autoconnect-seeker-peers=0', '--log-level=debug',
            *[f'--plugin={plugin}' for plugin in plugins],
        ], logfile, new_session=True)
        cli = [str(ROOT / 'cli/lightning-cli'), '--json', '--notifications=none',
               f'--lightning-dir={data}',
               f'--network={network}']
        if expect_success:
            info = wait_until(lambda: self.rpc(cli, 'getinfo'), proc)
            if info['network'] != network:
                raise AssertionError(info)
            return {'data': data, 'port': port, 'proc': proc, 'cli': cli,
                    'id': info['id'], 'log': logfile}
        # Failure must be a clean refusal, not a crash or indefinite startup.
        proc.wait(timeout=40)
        if proc.returncode <= 0:
            raise AssertionError(f'expected startup refusal, got {proc.returncode}')
        return logfile.read_text()

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def close(self):
        for proc in reversed(self.procs):
            self.stop(proc)
        for log in self.logs:
            log.close()


def run(lab, backend_only=False):
    xbt_backend = lab.node('knots-xbt', True)
    btc_backend = lab.node('knots-btc', False)
    print('PASS: isolated Knots backends mined 101 blocks each', flush=True)
    tip = lab.rpc(xbt_backend['cli'], 'getbestblockhash')
    rawblock = lab.rpc(xbt_backend['cli'], 'getblock', tip, 0)
    rawfile = lab.root / 'live-block.hex'
    rawfile.write_text(rawblock + '\n')
    subprocess.run([str(ROOT / 'bitcoin/test/run-xbt-chainparams'),
                    str(rawfile), tip], check=True)
    refusal = lab.lightning('wrong-backend', 'xbt-regtest', btc_backend, False)
    if 'requires Knots with -testactivationheight=blake2b@1' not in refusal:
        raise AssertionError('wrong-backend failed for an unexpected reason:\n' + refusal[-4000:])
    print('PASS: ordinary BTC regtest backend rejected', flush=True)
    if backend_only:
        print('Backend-only tests OK; peer/invoice/wallet smoke tests NOT RUN', flush=True)
        return
    a = lab.lightning('alice', 'xbt-regtest', xbt_backend)
    b = lab.lightning('bob', 'xbt-regtest', xbt_backend)
    btc = lab.lightning('btc-ln', 'regtest', btc_backend)
    lab.rpc(a['cli'], 'connect', b['id'], '127.0.0.1', b['port'])
    print('PASS: two XBT nodes start and connect', flush=True)
    log_offset = a['log'].stat().st_size
    try:
        lab.rpc(a['cli'], 'connect', btc['id'], '127.0.0.1', btc['port'])
    except subprocess.CalledProcessError:
        # connectd logs the chain mismatch locally; the RPC can report only
        # a generic init-exchange failure. Require fresh, peer-specific proof
        # so an unrelated connection failure cannot pass this check.
        with a['log'].open('rb') as log:
            log.seek(log_offset)
            rejection = log.read().decode(errors='replace')
        expected = f"{btc['id']}-connectd: No common chain with this peer"
        if expected not in rejection:
            raise
    else:
        raise AssertionError('XBT connected to BTC Lightning')
    print('PASS: BTC Lightning peer rejected', flush=True)
    invoice = lab.rpc(b['cli'], 'invoice', '1000msat', 'smoke', 'XBT isolation')['bolt11']
    if not invoice.startswith('lnxbtrt'):
        raise AssertionError(invoice)
    try:
        lab.rpc(btc['cli'], 'pay', invoice)
    except subprocess.CalledProcessError as exc:
        if 'Prefix xbtrt is not for regtest' not in exc.stderr + exc.stdout:
            raise
    else:
        raise AssertionError('BTC accepted the XBT invoice')
    print('PASS: XBT invoice prefix and BTC payment rejection', flush=True)
    lab.stop(btc['proc'])
    wrong = lab.root / 'wrong-wallet' / 'xbt-regtest'
    wrong.mkdir(parents=True, mode=0o700)
    for filename in ('lightningd.sqlite3', 'hsm_secret'):
        shutil.copy2(btc['data'] / 'regtest' / filename, wrong / filename)
    refusal = lab.lightning('wrong-wallet', 'xbt-regtest', xbt_backend, False)
    if 'Wallet blockchain hash does not match' not in refusal:
        raise AssertionError('wrong-wallet failed for an unexpected reason:\n' + refusal[-4000:])
    print('PASS: inherited-genesis BTC wallet database rejected', flush=True)
    print('XBT regtest smoke tests OK (no channels funded)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', required=True, type=Path)
    parser.add_argument('--bitcoin-cli', required=True, type=Path)
    parser.add_argument('--work-dir', type=Path,
                        help='New empty directory to retain logs/data for inspection.')
    parser.add_argument('--backend-only', action='store_true',
                        help='Only mining, live-block parsing/hash and wrong-backend rejection.')
    args = parser.parse_args()
    temporary = None
    if args.work_dir:
        root = args.work_dir.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        temporary = tempfile.TemporaryDirectory(prefix='cln-xbt-')
        root = Path(temporary.name)
    lab = Lab(root, str(args.bitcoind.resolve()), str(args.bitcoin_cli.resolve()))
    print(f'Test directory: {root}', flush=True)
    try:
        run(lab, args.backend_only)
    except Exception:
        for log in root.glob('*/console.log'):
            print(f'\n--- {log.parent.name}: last log lines ---', flush=True)
            print('\n'.join(log.read_text(errors='replace').splitlines()[-15:]), flush=True)
        raise
    finally:
        lab.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
