#!/usr/bin/env python3
"""Fixture-only reply loss, mutation counts and admission clock."""
from pathlib import Path
import json
import re
import sys
import time
import authority_plugin as base
import customer_plugin as plugin


class RPC(plugin.RPC):
    def __init__(self, configuration):
        super().__init__(configuration); self.root = Path(configuration['lightning-dir'])

    def __call__(self, method, **params):
        if method == 'close':
            with (self.root/'customer-mutations.log').open('a') as stream: stream.write(method+'\n')
        result = super().__call__(method, **params)
        if method in ('sendpay', 'signinvoice', 'createrune'):
            with (self.root/'customer-mutations.log').open('a') as stream: stream.write(method+'\n')
        arm = self.root/'customer-drop-reply'
        if arm.is_file() and arm.read_text().strip() == method:
            arm.unlink(); raise TimeoutError('synthetic lost local reply')
        return result


class Service(plugin.Service):
    def __init__(self, configuration, profile, rpc_factory):
        root = Path(configuration['lightning-dir'])
        def clock():
            path = root/'customer-fixture-time'
            return int(path.read_text()) if path.is_file() else time.time()
        super().__init__(configuration, profile, rpc_factory, clock)

    def _protect_once(self):
        try: return super()._protect_once()
        except Exception as error:
            reason = str(error)
            safe = reason if re.fullmatch(r'[a-z]+(?:_[a-z]+)+', reason) else None
            with (self.root/'watchdog-errors.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(error_type=type(error).__name__, reason=safe,
                    rpc_code=getattr(error,'code',None)))+'\n')
            raise


if __name__ == '__main__':
    try: base.main(RPC, Service, plugin.manifest)
    except Exception:
        print('fixture customer facade startup or framing refused', file=sys.stderr)
        raise SystemExit(1) from None
