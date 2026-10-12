#!/usr/bin/env python3
"""Disposable service process harness. Faults and synthetic prices live here only."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

import customer_https as https
import held_gate as hg
import market_quote as market
import service
import service_cli


def price(direction,route,markup_bps,*,clock):
    now=int(clock())
    return market.quote(dict(schema=1,source='neoxa.exchange',pair='BTCB2_BTC',direction=direction,
        markup_bps=markup_bps,fetched_at_ms=now*1000,ticker_at_ms=now*1000,expires_at=now+120,
        bid='0.49',ask='0.5',levels=[dict(price='0.5' if direction=='forward' else '0.49',quantity='1')],
        recipient_msat=route[-1]['amount_msat'],routing_fee_msat=route[0]['amount_msat']-route[-1]['amount_msat'],
        btc_msat=0,xbt_msat=0),compute=True)


def execute(r):
    directory=Path(r['directory'])
    hg.need(directory.is_absolute() and directory.name.startswith('service-worker-'),'fixture_service_path_refused')
    mode=r['mode']
    if mode=='init':service.initialize(directory);return dict(initialized=True)
    if mode=='action':return service_cli.execute(directory,r['request'],pricer=price)
    hg.need(mode=='run','fixture_mode_refused');fault=r.get('fault');stop=threading.Event()
    class Client(https.Client):
        def call(self,op,body=None):
            value=super().call(op,body)
            if fault==dict(kind=self.grant['kind'],operation=op):
                (directory/'fault-consumed').write_text(self.grant['kind']+' '+op+'\n')
                os.kill(os.getpid(),signal.SIGKILL)
            return value
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *a:stop.set())
    service_cli.run(directory,factory=Client,interval=.2,stop=stop)
    return dict(stopped=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--disposable-regtest',required=True,action='store_true');p.parse_args()
    os.umask(0o077)
    try:
        raw=sys.stdin.read(hg.MAX_BYTES+1);hg.need(len(raw)<=hg.MAX_BYTES,'fixture_request_invalid')
        result=execute(json.loads(raw,object_pairs_hook=hg.unique_object));print(json.dumps(dict(result=result)));return 0
    except Exception as e:
        print(json.dumps(dict(reason='service_busy' if isinstance(e,BlockingIOError) else
            str(e) if isinstance(e,ValueError) and str(e) in service_cli.SAFE else 'service_request_refused')));return 1


if __name__=='__main__':raise SystemExit(main())
