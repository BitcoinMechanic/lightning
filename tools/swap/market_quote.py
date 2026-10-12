"""Exact Neoxa quote arithmetic ported unchanged from the installed controller.

Only quote validation/arithmetic is shared. No legacy contract, gate, credential
or profile is accepted. No network or clock reads occur in this module.
"""
from decimal import Decimal, InvalidOperation
from fractions import Fraction

CAPS = {'btc': 10000000, 'xbt': 500000000}

def require(ok, reason='market_data_invalid'):
    if not ok: raise ValueError(reason)

def integer(v, low, high): return type(v) is int and low <= v <= high

def number(v):
    require(type(v) is str and len(v) <= 80)
    try: d=Decimal(v)
    except InvalidOperation: raise ValueError('market_data_invalid') from None
    require(d.is_finite() and Decimal('1e-18') <= d <= Decimal('1e18'))
    return Fraction(d)

def ceil(v): return -(-v.numerator // v.denominator)

def quote(q, *, compute=False, details=False):
    require(type(q) is dict and set(q)=={'schema','source','pair','direction','markup_bps','fetched_at_ms','ticker_at_ms',
        'expires_at','bid','ask','levels','recipient_msat','routing_fee_msat','btc_msat','xbt_msat'})
    require(q['schema']==1 and type(q['schema']) is int and q['source']=='neoxa.exchange' and q['pair']=='BTCB2_BTC')
    require(q['direction'] in ('forward','reverse') and integer(q['markup_bps'],0,500))
    require(integer(q['fetched_at_ms'],0,2**53-1) and integer(q['ticker_at_ms'],0,q['fetched_at_ms'])
        and q['fetched_at_ms']-q['ticker_at_ms']<=30000)
    require(type(q['expires_at']) is int and q['expires_at']==q['fetched_at_ms']//1000+120)
    require(integer(q['recipient_msat'],1000,CAPS['xbt' if q['direction']=='forward' else 'btc']) and q['recipient_msat']%1000==0)
    require(integer(q['routing_fee_msat'],0,10000))
    bid,ask=number(q['bid']),number(q['ask'])
    require(ask>=bid and (ask-bid)*10000<=bid*500)
    require(type(q['levels']) is list and 1<=len(q['levels'])<=50)
    levels=[]
    for row in q['levels']:
        require(type(row) is dict and set(row)=={'price','quantity'})
        price=number(row['price']);capacity=number(row['quantity'])*100000000
        capacity=capacity.numerator//capacity.denominator
        if capacity: levels.append((price,capacity))
    require(bool(levels),'market_depth_unavailable')
    forward=q['direction']=='forward';levels.sort(reverse=not forward)
    best=levels[0][0];reference=ask if forward else bid
    require((best>=bid if forward else best<=ask) and abs(best-reference)*10000<=reference*200,'market_depth_unavailable')
    budget=(q['recipient_msat']+q['routing_fee_msat']+999)//1000
    factor=Fraction(10000+q['markup_bps'],10000)
    remaining=Fraction(budget) if forward else budget*factor
    quantity=0;value=Fraction(0)
    for price,capacity in levels:
        take=min(capacity,int(remaining) if forward else ceil(remaining/price))
        quantity+=take;value+=take*price
        remaining-=take if forward else take*price
        if remaining<=0:break
    require(remaining<=0,'market_depth_unavailable')
    average=value/quantity
    require((average-best)*10000<=best*100 if forward else (best-average)*10000<=best*100,'market_depth_unavailable')
    btc=ceil(value*factor)*1000 if forward else q['recipient_msat']
    xbt=q['recipient_msat'] if forward else quantity*1000
    if compute: q=dict(q,btc_msat=btc,xbt_msat=xbt)
    for k,v in [('btc',btc),('xbt',xbt)]:
        require(integer(q[k+'_msat'],1000,CAPS[k]) and q[k+'_msat']==v,'market_amount_limit')
    return (q,average) if details else q
