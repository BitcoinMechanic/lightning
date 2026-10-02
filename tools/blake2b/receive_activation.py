"""Explicit, resumable activation of bounded live routed receiving."""
import argparse
import copy
import json
import os
from pathlib import Path
import time

import live_receive as live
from live_pilot import require_reserves
from receive_service import lock
from service_manager import private_load
from swap_controller import save
from swap_rpc import RPC
from switch_customer import stopped


class Blocked(ValueError):
    pass


def candidate(settings, btc=1650, xbt=500000, fee=10, margin=100, delay=288):
    if settings.get('deployment') != 'operator-pair-v1':
        raise Blocked('operator_pair_required')
    if type(fee) is not int:
        raise Blocked('invalid_limits')
    config = dict(profile=live.PROFILE, btc_cli=settings['btc_cli'],
                  xbt_cli=settings['xbt_cli'], node_ids=settings['node_ids'],
                  market=dict(max_btc_sats=btc, max_xbt_sats=xbt, margin_bps=margin),
                  max_xbt_routing_fee_msat=fee*1000, max_delay=delay)
    live.validate_config(config)
    result = copy.deepcopy(settings)
    result.pop('receive_config', None)
    result['receive_policy'] = config
    live.configuration(result)
    return result


def launcher_profile(path, root):
    settings = private_load(path)
    live.configuration(settings)
    if Path(settings['roots']['btc']).resolve() != root.resolve():
        raise Blocked('btc_root_binding_changed')
    return live.PROFILE


def quiescent(settings, rpc, now):
    for i, (key, network) in enumerate((('btc_cli','bitcoin'),('xbt_cli','xbt'))):
        info = rpc(settings[key], 'getinfo')
        if (info['id'] != settings['node_ids'][i] or info['network'] != network
                or any(k.startswith('warning_') for k in info)):
            raise Blocked('operator_identity_or_readiness')
        channels = rpc(settings[key], 'listpeerchannels')['channels']
        if any(c.get('htlcs') for c in channels):
            raise Blocked('pending_htlcs')
        if not any(c['state']=='CHANNELD_NORMAL' and c.get('peer_connected') for c in channels):
            raise Blocked('connected_channel_required')
        if rpc(settings[key], 'xbt-held')['held']:
            raise Blocked('held_hooks')
    require_reserves(settings, rpc)
    gate_profile = rpc(settings['btc_cli'], 'xbt-pilot-info')['profile']
    if gate_profile not in ('live-pilot-v2','live-market-v1','live-market-v2',live.PROFILE):
        raise Blocked('unexpected_btc_gate_profile')
    root = Path(settings['swap_root'])
    if not root.is_dir() or root.is_symlink():
        raise Blocked('invalid_swap_root')
    paths = set(root.glob('*/quote.json')) | set(root.glob('*/swap/quote.json')) | set(root.glob('*/reverse-quote.json'))
    for path in sorted(paths):
        if not path.resolve().is_relative_to(root.resolve()):
            raise Blocked('quote_outside_swap_root')
        quote = private_load(path)
        reverse = path.name=='reverse-quote.json'
        config = quote['config']
        ids = config['node_ids'] if reverse else quote['node_ids']
        if (any(config[k]!=settings[k] for k in ('btc_cli','xbt_cli'))
                or ids!=(list(reversed(settings['node_ids'])) if reverse else settings['node_ids'])):
            raise Blocked('historical_operator_binding_changed')
        state_path = path.parent/('reverse-state.json' if reverse else 'state.json')
        terms = quote['terms'];payment_hash = terms['payment_hash']
        cli = settings['xbt_cli' if reverse else 'btc_cli']
        gate = rpc(cli, 'reverse-status' if reverse else 'xbt-quote-status', payment_hash)
        if gate.get('payment_hash',payment_hash)!=payment_hash:
            raise Blocked('quote_identity_changed')
        if state_path.exists():
            state = private_load(state_path)
            terminal = {'xbt_released':'resolved','xbt_failed':'failed'} if reverse else {'btc_released':'resolved','btc_failed':'failed'}
            phase = state.get('phase')
            if (phase not in terminal or gate['phase']!=terminal[phase]
                    or state['payment_hash']!=payment_hash
                    or gate['binding']!=state['xbt_binding' if reverse else 'btc_binding']):
                raise Blocked('unfinished_swap')
        elif gate['phase']!='quoted' or terms['expires_at']>now:
            raise Blocked('unexpired_or_unresolved_quote')


def install(path, limits, rpc=RPC.call, check_stopped=stopped, now=time.time):
    path = Path(path)
    check_stopped()
    with lock(path.parent/'receive-activation.lock'):
        current = private_load(path)
        target = candidate(current, **limits)
        plan_path = path.parent/'routed-receive-activation.json'
        if plan_path.exists():
            plan = private_load(plan_path)
            if current not in (plan['old'],plan['new']) or target!=plan['new']:
                raise Blocked('activation_plan_changed')
        else:
            if current.get('receive_policy',{}).get('profile')==live.PROFILE:
                raise Blocked('existing_activation_without_plan')
            quiescent(current,rpc,int(now()))
            plan = dict(old=current,new=target)
            save(plan_path,plan)
        quiescent(current,rpc,int(now()))
        if current!=plan['new']:
            save(path,plan['new'])
    return dict(configured=True,profile=live.PROFILE,
                max_btc_sats=target['receive_policy']['market']['max_btc_sats'],
                max_xbt_sats=target['receive_policy']['market']['max_xbt_sats'],
                max_xbt_routing_fee_sats=target['receive_policy']['max_xbt_routing_fee_msat']//1000,
                payment_started=False,services_restarted=False,btc_restart_required=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['check','install','status'])
    p.add_argument('--settings',type=Path,default=Path.home()/'.config/cln-swaps/settings.json')
    for name,value in (('max-btc-sats',1650),('max-xbt-sats',500000),('max-xbt-routing-fee-sats',10),('margin-bps',100),('max-delay',288)):
        p.add_argument('--'+name,type=int,default=value)
    a=p.parse_args();os.umask(0o077)
    limits=dict(btc=a.max_btc_sats,xbt=a.max_xbt_sats,fee=a.max_xbt_routing_fee_sats,margin=a.margin_bps,delay=a.max_delay)
    try:
        path=a.settings.expanduser()
        if a.command=='install':
            result=install(path,limits)
        else:
            settings=private_load(path)
            if a.command=='check':
                target=candidate(settings,**limits)
                quiescent(settings,RPC.call,int(time.time()))
                result=dict(read_only=True,ready=True,**target['receive_policy']['market'],
                            max_xbt_routing_fee_sats=a.max_xbt_routing_fee_sats,payment_started=False)
            else:
                live.identities(live.configuration(settings));live.gate_ready(settings['receive_policy'],RPC.call)
                result=dict(configured=True,gate_rpc_ready=True,profile=live.PROFILE,payment_started=False)
        print(json.dumps(result));return 0
    except Exception as e:
        print(json.dumps(dict(event='receive_activation_blocked',reason=str(e) if isinstance(e,Blocked) else 'private_details_withheld',
                              services_restarted=False)))
        return 1


if __name__=='__main__':raise SystemExit(main())
