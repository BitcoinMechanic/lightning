#!/usr/bin/env python3
"""Run the BLAKE2b regression suite with retained logs and an exit-code summary.

Uses the current Python interpreter (activate your venv first). Builds nothing.
Every live case creates independent disposable regtest backends and wallets.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


@dataclass(frozen=True)
class Case:
    name: str
    command: tuple
    live: bool = False


def cases():
    result = [Case('headers', (sys.executable, str(HERE / 'check_headers.py')))]
    for name in ('service_manager', 'receive_workflow', 'market_quotes', 'market_check', 'neoxa_oracle', 'quote_plugin', 'quote_replay', 'live_pilot', 'btc_https_cli', 'btc_listener', 'controller_recovery', 'spend_guard', 'controller_lock', 'regression_runner', 'deadline_guard', 'swap_watch', 'swap_service'):
        result.append(Case('unit-' + name.replace('_', '-'),
                           (sys.executable, str(HERE / ('test_' + name + '.py')), '-v')))
    for name in ('run-block_blake2b', 'run-bitcoin_block_from_hex'):
        result.append(Case(name, (str(ROOT / 'bitcoin/test' / name),)))
    result.append(Case('run-xbt-maturity', (str(ROOT / 'common/test/run-xbt-maturity'),)))
    result.append(Case('isolation', (sys.executable, str(HERE / 'smoke_regtest.py')), True))
    for mode in ('', 'force-close', 'htlc-timeout', 'preimage-claim'):
        result.append(Case('funded-' + (mode or 'mutual-close'),
                           (sys.executable, str(HERE / 'funded_regtest.py')) +
                           (('--' + mode,) if mode else ()), True))
    for mode in ('', 'fail-outgoing', 'crash-after-xbt', 'restart-operators', 'invoice',
                 'quoted-invoice', 'quoted-restart', 'reject-quotes', 'crash-after-btc',
                 'crash-while-pending', 'pending-failure', 'pending-restart',
                 'pending-restart-failure', 'pending-kill', 'pending-kill-failure',
                 'stale-timelock', 'concurrent', 'outgoing-binding', 'onchain-preimage', 'onchain-timeout',
                 'btc-deadline', 'watch-deadline', 'service-demo'):
        result.append(Case('swap-' + (mode or 'direct'),
                           (sys.executable, str(HERE / 'swap_regtest.py')) +
                           (('--' + mode,) if mode else ()), True))
    return result


class Runner:
    def __init__(self, output, backend_args):
        self.output = output
        self.backend_args = backend_args
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.active = set()

    def cancel(self):
        self.stop.set()
        with self.lock:
            for proc in self.active:
                if proc.poll() is None:
                    try:
                        # Python harnesses handle KeyboardInterrupt with their
                        # existing finally: Lab.close(), including isolated CLN.
                        proc.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass

    def run_case(self, index, case):
        log = self.output / (case.name + '.log')
        command = list(case.command)
        if case.live:
            command += self.backend_args + ['--work-dir', str(self.output / f'c{index:02d}')]
        start = time.monotonic()
        record = {'name': case.name, 'command': command, 'log': str(log)}
        if self.stop.is_set():
            return dict(record, status='NOT RUN', returncode=None, seconds=0)
        env = dict(os.environ, PYTHONUNBUFFERED='1',
                   CLN_BLAKE2B_TEST_PORTS=str(self.output / 'ports.txt'))
        proc = None
        try:
            with log.open('w') as stream:
                with self.lock:
                    if self.stop.is_set():
                        return dict(record, status='NOT RUN', returncode=None, seconds=0)
                    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                    self.active.add(proc)
                code = proc.wait()
            status = 'PASS' if code == 0 else 'FAIL'
        except OSError as exc:
            with log.open('a') as stream:
                stream.write(str(exc) + '\n')
            code, status = None, 'FAIL'
        finally:
            if proc is not None:
                with self.lock:
                    self.active.discard(proc)
        return dict(record, status=status, returncode=code,
                    seconds=round(time.monotonic() - start, 2))


def main():
    catalog = cases()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', type=Path)
    parser.add_argument('--bitcoin-cli', type=Path)
    parser.add_argument('--jobs', type=int, default=1, help='Parallel cases (1-8; default 1).')
    parser.add_argument('--only', action='append', choices=[c.name for c in catalog],
                        help='Run only this case; repeat to select more.')
    parser.add_argument('--list', action='store_true', help='List available cases and exit.')
    parser.add_argument('--output', type=Path, help='New short absolute output directory; default /tmp/cxr-*.')
    args = parser.parse_args()
    if args.list:
        print('\n'.join(c.name for c in catalog))
        return 0
    if not 1 <= args.jobs <= 8:
        parser.error('--jobs must be between 1 and 8')
    selected = [c for c in catalog if args.only is None or c.name in args.only]
    if any(c.live for c in selected) and (args.bitcoind is None or args.bitcoin_cli is None):
        parser.error('live cases require --bitcoind and --bitcoin-cli')
    backend_args = []
    for flag, path in (('--bitcoind', args.bitcoind), ('--bitcoin-cli', args.bitcoin_cli)):
        if path is not None:
            path = path.resolve()
            if not path.is_file() or not os.access(path, os.X_OK):
                parser.error(f'{flag} is not an executable file: {path}')
            backend_args += [flag, str(path)]
    if args.output:
        output = args.output.resolve()
        if len(os.fsencode(str(output))) > 50:
            parser.error('--output must be short (at most 50 bytes) for CLN Unix sockets')
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
    else:
        output = Path(tempfile.mkdtemp(prefix='cxr-'))
    runner = Runner(output, backend_args)
    records = []
    print(f'Regression output: {output}\nRunning {len(selected)} cases; jobs={args.jobs}', flush=True)
    previous = {sig: signal.signal(sig, lambda *_: runner.cancel())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(runner.run_case, i, case) for i, case in enumerate(selected)]
            for future in as_completed(futures):
                record = future.result()
                records.append(record)
                print(f"{record['status']:7} {record['name']} ({record['seconds']:.1f}s)", flush=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    order = {case.name: i for i, case in enumerate(selected)}
    records.sort(key=lambda r: order[r['name']])
    passed = sum(r['status'] == 'PASS' for r in records)
    summary = {'passed': passed, 'total': len(selected), 'interrupted': runner.stop.is_set(),
               'cases': records}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f"\n{passed}/{len(selected)} passed. Summary: {output / 'summary.json'}", flush=True)
    for record in records:
        if record['status'] != 'PASS':
            print(f"{record['status']}: {record['name']} — {record['log']}", flush=True)
    return 130 if runner.stop.is_set() else (0 if passed == len(selected) else 1)


if __name__ == '__main__':
    sys.exit(main())
