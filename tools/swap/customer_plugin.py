#!/usr/bin/env python3
"""Static regtest customer facade. Fresh journals; no StartOS activation."""
from pathlib import Path
import sys
import time

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
        super().__init__(configuration, profile, rpc_factory, clock)

    def invoke(self, method, params, profile=None):
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
