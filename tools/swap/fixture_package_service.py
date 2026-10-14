#!/usr/bin/python3 -I
"""External regtest price feed. This file is never in a runtime image.

Run the real sealed entry point. Only the read-only market reader is substituted;
there are no worker, transport, RPC, clock, journal or lifecycle substitutions.
"""
import json
from pathlib import Path
import runpy
import sys
import time
sys.dont_write_bytecode=True
runtime=Path(sys.argv.pop(1)); sys.path.insert(0,str(runtime))
import service_market
original=service_market.make

def reader(kind):
    value=dict(success=True,pair='BTCB2_BTC')
    if kind=='ticker':value['ticker']=dict(bestBid='0.49',bestAsk='0.5',computedAt=int(time.time()*1000))
    else:value.update(bids=[dict(price='0.49',quantity='1')],asks=[dict(price='0.5',quantity='1')])
    return value

def make(direction,route,markup_bps,*,clock):
    return original(direction,route,markup_bps,clock=clock,reader=reader)
service_market.make=make
runpy.run_path(str(runtime/'package_service.py'),run_name='__main__')
