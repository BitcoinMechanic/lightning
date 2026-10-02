"""Opt-in invoice-selected direct receiving; each quote pins one funding output."""
import copy
import hashlib
import re
import time

from customer_errors import channel_reason
from market_policy import policy
from quote_refusal import QuoteRefused
from swap_rpc import RPC

PROFILE = 'invoice-direct-v1'
PIN_FIELDS = ('channel_id', 'funding_txid', 'funding_outnum')


def is_selected(config):
    return config.get('profile') == PROFILE


def configuration(settings):
    config = copy.deepcopy(settings['receive_policy'])
    if (set(config) != {'profile', 'btc_cli', 'xbt_cli', 'market'}
            or not is_selected(config) or config['btc_cli'] != settings['btc_cli']
            or config['xbt_cli'] != settings['xbt_cli']
            or set(config['market']) != {'btc_channel', 'max_btc_sats', 'max_xbt_sats', 'margin_bps'}):
        raise ValueError('invalid invoice-selected receive policy')
    policy(dict(market=dict(config['market'], xbt_peer='selected', xbt_channel='selected')))
    return config


def validate(selected, config, invoice, cap):
    expected = copy.deepcopy(config)
    expected['profile'] = 'live-market-v1'
    expected['market'].update(xbt_peer=selected['payee'], xbt_channel=selected['channel'])
    expected['market']['max_btc_sats'] = min(cap, expected['market']['max_btc_sats'])
    if (selected['config'] != expected or selected['policy'] != config
            or selected['invoice_sha256'] != hashlib.sha256(invoice.encode()).hexdigest()
            or not re.fullmatch(r'0[23][0-9a-f]{64}', selected['payee'])
            or not re.fullmatch(r'[0-9a-f]{64}', selected['payment_hash'])
            or not re.fullmatch(r'[0-9a-f]{64}', selected['channel_id'])
            or not re.fullmatch(r'[0-9a-f]{64}', selected['funding_txid'])
            or type(selected['funding_outnum']) is not int or selected['funding_outnum'] < 0
            or not isinstance(selected['channel'], str) or not selected['channel']):
        raise ValueError('selected quote binding differs')
    return expected


def select(config, invoice, cap, node_ids, rpc=RPC.call, now=time.time):
    if not isinstance(node_ids, list) or len(node_ids) != 2 or node_ids[0] == node_ids[1]:
        raise ValueError('two distinct operator identities required')
    for role, network, node in zip(('btc', 'xbt'), ('bitcoin', 'xbt'), node_ids):
        info = rpc(config[role+'_cli'], 'getinfo')
        if (info['network'] != network or info['id'] != node
                or any(k.startswith('warning_') for k in info)):
            raise ValueError('operator identity or network differs')
    d = rpc(config['xbt_cli'], 'decode', invoice)
    amount = d.get('amount_msat')
    if (d.get('valid') is not True or d.get('type') != 'bolt11 invoice' or d.get('currency') != 'xbt'
            or type(amount) is not int or amount % 1000 or not 0 < amount <= config['market']['max_xbt_sats']*1000
            or not re.fullmatch(r'0[23][0-9a-f]{64}', d.get('payee', '')) or d['payee'] in node_ids
            or not re.fullmatch(r'[0-9a-f]{64}', d.get('payment_hash', ''))
            or not re.fullmatch(r'[0-9a-f]{64}', d.get('payment_secret', ''))
            or type(d.get('created_at')) is not int or type(d.get('expiry')) is not int):
        raise ValueError('unsupported receiving invoice')
    if d['created_at']+d['expiry']-int(now()) < 90:
        raise QuoteRefused('invoice_expiring')
    channels = [c for c in rpc(config['xbt_cli'], 'listpeerchannels')['channels']
                if c.get('peer_id') == d['payee'] and c.get('state') == 'CHANNELD_NORMAL']
    reason = channel_reason(channels, 'xbt', amount)
    if reason:
        raise QuoteRefused(reason)
    c = channels[0]
    concrete = copy.deepcopy(config)
    concrete['profile'] = 'live-market-v1'
    concrete['market'].update(xbt_peer=d['payee'], xbt_channel=c['short_channel_id'])
    concrete['market']['max_btc_sats'] = min(cap, concrete['market']['max_btc_sats'])
    selected = dict(policy=config, config=concrete, payee=d['payee'], channel=c['short_channel_id'],
                    payment_hash=d['payment_hash'], invoice_sha256=hashlib.sha256(invoice.encode()).hexdigest(),
                    **{k:c[k] for k in PIN_FIELDS})
    validate(selected, config, invoice, cap)
    return selected


def check_channel(selected, rpc=RPC.call):
    matches = [c for c in rpc(selected['config']['xbt_cli'], 'listpeerchannels')['channels']
               if c.get('peer_id') == selected['payee'] and c.get('short_channel_id') == selected['channel']]
    if len(matches) != 1 or any(matches[0].get(k) != selected[k] for k in PIN_FIELDS):
        raise ValueError('selected funding output changed')
