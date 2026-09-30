"""Foreground recovery watcher for one existing experimental regtest swap.

Restart with the same state path. This does not originate payments, install a
service, or verify final on-chain settlement. The controller's durable terminal
phases describe release/failure intent; CLN continues enforcing the contracts.
"""
import argparse
import json
import math
from pathlib import Path
import signal
import subprocess
import threading

from swap_controller import run


def watch(path, interval=1.0, stop=None, emit=None):
    if not math.isfinite(interval) or interval < 0.1 or interval > 60:
        raise ValueError('interval must be between 0.1 and 60 seconds')
    stop = stop if stop is not None else threading.Event()
    emit = emit if emit is not None else lambda value: print(json.dumps(value), flush=True)
    path = path.resolve()
    while not stop.is_set():
        # Refuse to originate a new payment. All valid controller transitions
        # from these phases only reconcile the original outgoing attempt.
        state = json.loads(path.read_text())
        if state['phase'] not in ('outgoing_started', 'xbt_paid', 'xbt_failed',
                                  'btc_released', 'btc_failed'):
            raise RuntimeError('watcher requires an already-started swap')
        try:
            result = run(path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # An RPC error never proves failure and never authorizes resend.
            # Avoid logging CLI arguments/output: they may contain a preimage.
            emit({'event': 'rpc_retry', 'error': type(exc).__name__})
        else:
            # Never emit the controller's payment_preimage.
            report = {k: result[k] for k in ('phase', 'outcome') if k in result}
            emit(dict(event='reconciled', **report))
            if result.get('phase') in ('btc_released', 'btc_failed'):
                return 0
            if result.get('outcome') not in ('pending', 'busy'):
                raise RuntimeError('unexpected watcher reconciliation result')
        stop.wait(interval)  # Signal-aware sleep; no lock held between polls.
    emit({'event': 'stopped'})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--interval', type=float, default=1.0)
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        return watch(args.state, args.interval, stop)
    except Exception as exc:
        # Invariant errors require inspection, not an endless retry loop.
        print(json.dumps({'event': 'fatal', 'error': type(exc).__name__}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
