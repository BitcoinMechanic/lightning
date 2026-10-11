#!/usr/bin/env python3
"""Disposable fixture fault injection. Never installed as a service plugin.

Pause after the durable terminal write, before either hook or command reply.
0068a restarts the whole node. The separate 0070 harness kills this process and
checks the engine's behavior; the PID marker is only for that disposable test.
"""
import json
import os
import sys

import gate_plugin
import held_gate

original_write = held_gate.Journal.write
original_init = held_gate.Journal.__init__


def fixture_init(self, directory):
    original_init(self, directory)
    (self.directory/'fixture-process.json').write_text(json.dumps(
        dict(pid=os.getpid(), parent=os.getppid())))


def pause_until_parent_closes_input():
    # CLN's ordinary shutdown destroys the plugin's pipe connections; it does
    # not promise to SIGKILL the process. Continue consuming this test-only
    # input, without dispatching or replying, until the pipe reaches EOF.
    # SystemExit bypasses Gate.commit's ordinary-error handler and unwinds
    # gate_plugin.main's finally block, which releases the lifetime lock.
    while os.read(sys.stdin.fileno(), 65536):
        pass
    raise SystemExit(0)


def write_and_pause(self, record):
    original_write(self, record)
    arm = self.directory/'fixture-arm.json'
    if not arm.is_file():
        return
    expected = json.loads(arm.read_text())
    entry = record['quotes'].get(expected['payment_hash'], {})
    if entry.get('phase') != expected['phase']:
        return
    arm.unlink()
    (self.directory/'fixture-paused.json').write_text(json.dumps(expected))
    # No reply has left this process. Keep the lock until the whole node closes
    # this pipe during shutdown, then exit without sending either response.
    pause_until_parent_closes_input()


if __name__ == '__main__':
    held_gate.Journal.__init__ = fixture_init
    held_gate.Journal.write = write_and_pause
    try:
        gate_plugin.main()
    except Exception:
        print('swap gate startup or framing refused', file=sys.stderr)
        raise SystemExit(1) from None
