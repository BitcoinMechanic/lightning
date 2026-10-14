#!/usr/bin/env python3
"""Static regtest customer facade. Fresh journals; no StartOS activation."""
from pathlib import Path
import sys
import time
import threading

import authority_plugin as plugin
import bounded_authority as ba
import customer_invoice as invoice
import customer_remote as remote
import held_gate as hg

REMOTE_DIR = 'customer-remote-v1'
INVOICE_DIR = 'customer-invoices-v1'


def manifest():
    result = plugin.manifest()
    result['rpcmethods'] = [
        dict(name=remote.CALL, usage='grant_id operation body', description='Scoped regtest customer contract operations'),
        dict(name=remote.CREDENTIAL, usage='grant_id confirmed', description='Local customer credential setup')]
    return result


class RPC(plugin.RPC):
    methods = remote.METHODS


class Service(plugin.Service):
    def __init__(self, configuration, profile, rpc_factory=RPC, clock=time.time):
        self.serial = threading.RLock()
        super().__init__(configuration, profile, rpc_factory, clock)

    def protect_once(self):
        with self.serial: self._protect_once()

    def _protect_once(self):
        import deadline_protection as protection
        # Same nonblocking journal lock as RPC. A busy lock is retried by the
        # next tick; no cached authority survives a request or node restart.
        journal = hg.Journal(self.root/plugin.AUTHORITY_DIR)
        try:
            a = ba.Authority(journal, self.chain, self.rpc, self.clock)
            a.guard()
            for eid, e in list(a.record['entries'].items()):
                if 'protection' in e and 'terminal' not in e:
                    protection.protect(a, eid)
        finally: journal.close()

    def start_watchdog(self):
        def run():
            while True:
                try: self.protect_once()
                except Exception:
                    # Retry observations, never a saved close/send dispatch.
                    # Remote evidence retains ambiguity; no secret is logged.
                    pass
                time.sleep(1)
        threading.Thread(target=run, name='swap-deadline-watchdog', daemon=True).start()

    def invoke(self, method, params, profile=None):
        with self.serial: return self._invoke(method, params, profile)

    def _invoke(self, method, params, profile=None):
        journals = []
        try:
            for name in (plugin.AUTHORITY_DIR, REMOTE_DIR, INVOICE_DIR):
                journals.append(hg.Journal(self.root/name))
            chain = hg.binding(journals[0].read()['chain'])
            if profile is not None: hg.need(chain['profile'] == profile, 'profile_changed')
            else: ba.ci.same_binding(chain, self.chain)
            a = ba.Authority(journals[0], chain, self.rpc, self.clock)
            p = invoice.Publisher(a, journals[2]); r = remote.Remote(a, journals[1], self.rpc, p)
            r.guard()
            if method is None: return chain
            if method == remote.CREDENTIAL:
                hg.need(type(params) is dict and set(params) == {'grant_id', 'confirmed'}, 'local_setup_invalid')
                return r.enable(params['grant_id'], confirmed=params['confirmed'])
            hg.need(method == remote.CALL, 'remote_method_refused')
            return r.call(params)
        finally:
            for journal in reversed(journals): journal.close()


if __name__ == '__main__':
    try: plugin.main(RPC, Service, manifest)
    except Exception:
        print('customer facade startup or framing refused', file=sys.stderr)
        raise SystemExit(1) from None
