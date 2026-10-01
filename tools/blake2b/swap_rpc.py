"""CLI-backed RPC transport for optional swap services, independent of regtest.

Command arrays and the historical result/exception contract are preserved.
Never log raw RPC arguments, stdout, stderr or exception representations:
these can contain invoices, payment secrets, preimages or private identifiers.
This transport deliberately does not retry commands, especially mutations.
"""
import json
import subprocess
import time


class RPC:
    @staticmethod
    def call(args, *command):
        result = subprocess.run([*args, *map(str, command)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=20)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, result.args,
                                                result.stdout, result.stderr)
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()


def wait_until(fn, proc=None, timeout=40):
    """Bounded condition polling used by the controller's crash-test mode.

RPC failures may be retried only as condition reads; never use this helper
for payment submission. Error text deliberately omits RPC details.
"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError('process exited while waiting')
        try:
            value = fn()
            if value:
                return value
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise RuntimeError('timed out waiting for service')
