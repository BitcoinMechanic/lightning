"""Bounded forward quote requests and explicitly authorized worker steps."""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

from market_policy import policy
from quote_refusal import QuoteRefused
from service_manager import private_load
from swap_controller import save, run
from swap_rpc import RPC
import swap_service as service
import receive_selection as selection
import routed_receive_service as routed_service
import live_receive as live_service

FORMAT = 'btc-xbt-receive-offer-v1'
FIELDS = {'format', 'xbt_invoice_sha256', 'btc_invoice', 'btc_sats', 'xbt_sats', 'expires_at'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def configuration(settings):
    if 'receive_policy' in settings:
        if 'receive_config' in settings:
            raise ValueError('choose one receiving policy')
        if settings['receive_policy'].get('profile') == live_service.PROFILE:
            return live_service.configuration(settings)
        return (routed_service.configuration(settings)
                if routed_service.is_config(settings['receive_policy']) else selection.configuration(settings))
    config = copy.deepcopy(settings['receive_config'])
    if (set(config) != {'profile', 'btc_cli', 'xbt_cli', 'market'}
            or config['profile'] != 'live-market-v1'
            or config['btc_cli'] != settings['btc_cli']
            or config['xbt_cli'] != settings['xbt_cli']
            or policy(config)['xbt_peer'] != settings['receiver_id']):
        raise ValueError('receiving configuration differs from operator/customer')
    return config


def packet(quote):
    t = quote['terms']
    return dict(format=FORMAT, xbt_invoice_sha256=hashlib.sha256(t['xbt_invoice'].encode()).hexdigest(),
                btc_invoice=quote['btc_invoice'], btc_sats=t['btc_amount_msat']//1000,
                xbt_sats=t['xbt_amount_msat']//1000, expires_at=t['expires_at'])


class ReceiveQuotes:
    def __init__(self, settings, auto_process=False):
        self.settings = settings
        self.config = configuration(settings)
        self.backend = (live_service if self.config.get('profile') == live_service.PROFILE
                        else routed_service if routed_service.is_config(self.config) else service)
        self.auto_process = auto_process
        self.root = Path(settings['swap_root']).resolve()
        self.records = self.root/'receive-requests'
        if self.records.is_symlink():
            raise ValueError('request directory is a symlink')
        self.records.mkdir(mode=0o700, exist_ok=True)
        if self.records.stat().st_mode & 0o077:
            raise ValueError('request directory must be private')

    def quote(self, request):
        if not isinstance(request, dict):
            raise ValueError('invalid receiving request')
        request = dict(request)
        retry = request.pop('retry_refused', False)
        if (type(retry) is not bool
                or set(request) != {'request_id', 'xbt_invoice', 'max_btc_sats'}
                or not isinstance(request['request_id'], str)
                or not re.fullmatch('[0-9a-f]{32}', request['request_id'])
                or not isinstance(request['xbt_invoice'], str)
                or not request['xbt_invoice'].startswith('lnxbt')
                or len(request['xbt_invoice']) > 32768
                or any(c.isspace() for c in request['xbt_invoice'])
                or type(request['max_btc_sats']) is not int
                or not 0 < request['max_btc_sats'] <= 10000):
            raise ValueError('invalid receiving request')
        with lock(self.records/'requests.lock'):
            return self._quote(request, retry)

    def _quote(self, request, retry):
        key = request['request_id']
        path = self.records/(key+'.json')
        directory = self.root/('receive-api-'+key)
        bound = dict(config=self.config, node_ids=self.settings['node_ids'])
        # A second request ID must not register another quote for the same invoice.
        index = self.records/('invoice-'+hashlib.sha256(request['xbt_invoice'].encode()).hexdigest()+'.json')
        if index.exists() and private_load(index) != {'request_id': key}:
            raise ValueError('invoice already bound to a request')
        if path.exists():
            stored = private_load(path)
            if stored['request'] != request or stored['bound'] != bound:
                raise ValueError('existing request differs')
        else:
            if directory.exists():
                raise ValueError('quote without request record')
            stored = dict(request=request, bound=bound, phase='creating', auto_process=self.auto_process)
            save(path, stored)
        if not index.exists():
            save(index, {'request_id': key})
        if 'offer' in stored:
            return stored['offer']
        if stored['phase'] == 'refused':
            if directory.exists():
                raise ValueError('refusal conflicts with quote')
            if not retry:
                raise QuoteRefused(stored['reason'])
            stored.pop('attempted', None)
        if not directory.exists():
            if stored.get('attempted'):
                raise ValueError('uncertain creation requires inspection')
            stored.update(phase='creating', attempted=True)
            save(path, stored)
            config = copy.deepcopy(self.config)
            config['market']['max_btc_sats'] = min(config['market']['max_btc_sats'], request['max_btc_sats'])
            try:
                if selection.is_selected(self.config):
                    if 'selection' not in stored:
                        stored['selection'] = selection.select(self.config, request['xbt_invoice'],
                            request['max_btc_sats'], self.settings['node_ids'])
                        save(path, stored)
                    config = selection.validate(stored['selection'], self.config,
                                                request['xbt_invoice'], request['max_btc_sats'])
                    payment_index = self.records/('hash-'+stored['selection']['payment_hash']+'.json')
                    if payment_index.exists():
                        if private_load(payment_index) != {'request_id': key}:
                            raise ValueError('payment hash already bound to another request')
                    else:
                        save(payment_index, {'request_id': key})
                    selection.check_channel(stored['selection'])
                if self.backend.identities(config) != self.settings['node_ids']:
                    raise ValueError('operator identity changed')
                self.backend.create(config, request['xbt_invoice'], None, directory)
            except ValueError as error:
                code = error.reason if isinstance(error, QuoteRefused) else {
                    'oracle BTC amount exceeds operator cap': 'btc_price_cap',
                    'replacement cost exceeds slippage limit': 'market_slippage',
                    'limit ask differs from ticker beyond reference gap limit': 'market_reference_gap',
                    'insufficient limit-order depth': 'market_depth',
                    'no limit-order ask liquidity': 'market_depth',
                    'stale or future ticker timestamp': 'market_stale',
                    'crossed or excessive market spread': 'market_spread',
                    'inconsistent ticker and order book': 'market_inconsistent',
                }.get(str(error))
                if code and not directory.exists():
                    stored.update(phase='refused', reason=code)
                    save(path, stored)
                    raise QuoteRefused(code) from None
                if isinstance(error, QuoteRefused):
                    raise ValueError('quote creation outcome uncertain') from None
                raise
        with lock(directory/'service.lock'):
            quote = private_load(directory/'quote.json')
            expected = copy.deepcopy(self.config)
            expected['market']['max_btc_sats'] = min(expected['market']['max_btc_sats'], request['max_btc_sats'])
            if selection.is_selected(self.config):
                expected = selection.validate(stored['selection'], self.config,
                                              request['xbt_invoice'], request['max_btc_sats'])
                if quote['terms']['payment_hash'] != stored['selection']['payment_hash']:
                    raise ValueError('quote payment hash changed')
                if 'receive_selection' not in quote:
                    if 'btc_invoice' in quote:
                        raise ValueError('published quote lacks selection')
                    quote['receive_selection'] = stored['selection']
                    save(directory/'quote.json', quote)
                if quote['receive_selection'] != stored['selection']:
                    raise ValueError('quote selection changed')
            if (quote['config'] != expected or quote['node_ids'] != self.settings['node_ids']
                    or quote['terms']['xbt_invoice'] != request['xbt_invoice']):
                raise ValueError('saved quote differs from request')
            if 'btc_invoice' not in quote:
                self.backend.publish(directory)
                quote = private_load(directory/'quote.json')
            if stored['auto_process']:
                permit = directory/'receive-authorization.json'
                authorization = dict(format='receive-authorization-v1', quote_sha256=digest(quote),
                                     expires_at=quote['terms']['expires_at'])
                if permit.exists():
                    if private_load(permit) != authorization:
                        raise ValueError('existing authorization differs')
                else:
                    if authorization['expires_at'] <= int(time.time()):
                        raise ValueError('expired quote cannot be authorized')
                    save(permit, authorization)
            offer = packet(quote)
            stored.update(phase='quoted', offer=offer)
            save(path, stored)
            return offer


def process(directory, settings, rpc=RPC.call, controller=run, now=time.time):
    """One bounded worker step; recovery never requires a current permit."""
    with lock(directory/'service.lock'):
        quote = private_load(directory/'quote.json')
        if (directory.resolve().parent != Path(settings['swap_root']).resolve()
                or quote['config']['btc_cli'] != settings['btc_cli']
                or quote['config']['xbt_cli'] != settings['xbt_cli']
                or quote['node_ids'] != settings['node_ids']):
            raise ValueError('worker binding mismatch')
        path = directory/'state.json'
        state = private_load(path) if path.exists() else None
        if state is not None:
            if any(state.get(k) != v for k, v in quote['controller'].items() if k != 'phase'):
                raise ValueError('controller differs from quote')
            if state['phase'] in ('btc_released', 'btc_failed'):
                return {'phase': state['phase']}
            if state['phase'] != 'prepared':
                result = controller(path, recover_only=True)
                return {k: result[k] for k in ('phase', 'outcome') if k in result}
        permit_path = directory/'receive-authorization.json'
        if not permit_path.exists():
            return {'outcome': 'needs_manual_resume'}
        config = configuration(settings)
        expected = copy.deepcopy(config)
        expected['market']['max_btc_sats'] = quote['config']['market']['max_btc_sats']
        if selection.is_selected(config):
            selected = quote['receive_selection']
            expected = selection.validate(selected, config, quote['terms']['xbt_invoice'],
                                          quote['config']['market']['max_btc_sats'])
            if selected['payment_hash'] != quote['terms']['payment_hash']:
                raise ValueError('quote payment hash differs')
            selection.check_channel(selected, rpc)
        if (quote['config'] != expected or not 0 < expected['market']['max_btc_sats'] <= config['market']['max_btc_sats']
                or 'btc_invoice' not in quote):
            raise ValueError('quote outside current receive configuration')
        if private_load(permit_path) != dict(format='receive-authorization-v1', quote_sha256=digest(quote),
                                           expires_at=quote['terms']['expires_at']):
            raise ValueError('authorization differs')
        if quote['terms']['expires_at'] <= int(now()):
            return {'outcome': 'authorization_expired'}
        if config.get('profile') == live_service.PROFILE:
            live_service.preflight(quote, rpc)
        elif routed_service.is_config(config):
            routed_service.preflight(quote, rpc)
        if state is None:
            ph = quote['terms']['payment_hash']
            gate = rpc(settings['btc_cli'], 'xbt-quote-status', ph)
            if gate['payment_hash'] != ph:
                raise ValueError('gate identity mismatch')
            if gate['phase'] == 'quoted':
                return {'outcome': 'waiting_for_btc'}
            if gate['phase'] != 'held':
                raise ValueError('unexpected gate outcome')
            binding = gate['binding']
            from incoming_btc import enabled, bind
            dynamic = enabled(quote['controller'])
            if (not isinstance(binding, list) or len(binding) != 2
                    or (not dynamic and binding[0] != quote['terms']['btc_channel'])):
                raise ValueError('incoming channel differs')
            committed = any(c.get('short_channel_id') == binding[0] and
                any(h.get('id') == binding[1] and h.get('direction') == 'in'
                    and h.get('payment_hash') == ph and h.get('state') == 'RCVD_ADD_ACK_REVOCATION'
                    for h in c.get('htlcs', []))
                for c in rpc(settings['btc_cli'], 'listpeerchannels')['channels'])
            if not committed:
                return {'outcome': 'waiting_for_commitment'}
            if any(p['payment_hash'] == ph for p in rpc(settings['xbt_cli'], 'listsendpays')['payments']):
                raise ValueError('attempt without controller state')
            prepared = dict(quote['controller'], btc_binding=binding)
            if dynamic:
                prepared = bind(prepared, rpc)
            save(path, prepared)
        result = controller(path)
        return {k: result[k] for k in ('phase', 'outcome') if k in result}
