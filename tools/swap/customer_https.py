"""Fixed, grant-scoped CLNrest client for the migrated regtest worker.

Only swap-customer-call is exposed. TLS uses the saved CA bundle and hostname;
no environment proxy, redirect, fallback transport or automatic retry exists.
Errors deliberately say nothing about a mutation's remote outcome.
"""
import copy
import http.client
import ipaddress
import json
import re
import ssl
import urllib.parse

import bounded_authority as ba
import customer_remote as remote
import held_gate as hg

LIMIT = 1048576
TIMEOUT = 15


class HttpsError(ValueError):
    pass


def endpoint(value):
    try:
        hg.need(type(value) is str and 1 <= len(value) <= 2048
                and all(33 <= ord(c) < 127 for c in value) and '\\' not in value, 'invalid_https_endpoint')
        p = urllib.parse.urlsplit(value)
        hg.need(p.scheme == 'https' and p.hostname and p.username is None and p.password is None
                and p.path in ('','/') and not p.query and not p.fragment
                and '?' not in value and '#' not in value and '%' not in value, 'invalid_https_endpoint')
        host = p.hostname; port = p.port or 443
        hg.need(p.port is None or 1 <= p.port <= 65535, 'invalid_https_endpoint')
        try: ipaddress.ip_address(host)
        except ValueError:
            hg.need(len(host) <= 253 and all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                    for label in host.split('.')), 'invalid_https_endpoint')
        authority = '['+host+']' if ':' in host else host
        return 'https://'+authority+(':'+str(port) if port != 443 else '')
    except (ValueError,TypeError,AttributeError):
        raise HttpsError('invalid_https_endpoint') from None


def configuration(value):
    hg.need(type(value) is dict and set(value) == {'url','ca_pem','rune','grant'}, 'https_configuration_invalid')
    hg.need(endpoint(value['url']) == value['url'], 'https_endpoint_not_canonical')
    pem = value['ca_pem']
    hg.need(type(pem) is str and len(pem) <= 65536 and re.fullmatch(
        r'(?:\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+',pem),
        'https_ca_invalid')
    hg.need(remote.base.token(value['rune']), 'https_credential_invalid')
    g = value['grant']; ba.grant(g,hg.binding(g['chain']))
    return value


def context(pem):
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_verify_locations(cadata=pem)
        return ctx
    except Exception:
        raise HttpsError('https_ca_invalid') from None


def info(value, grant):
    hg.need(type(value) is dict and set(value) == {'format','grant','paused','current','remaining','reserved_msat','regtest_only'}
        and value['format'] == remote.FORMAT and ba.equal(value['grant'],grant)
        and type(value['paused']) is bool and type(value['current']) is bool and value['regtest_only'] is True
        and hg.integer(value['remaining'],0,grant['limits']['max_swaps'])
        and hg.integer(value['reserved_msat'],0,grant['limits']['total_amount_msat']), 'https_grant_or_identity_changed')
    return value


def parameters(kind, op, body, grant=None):
    allowed = remote.operations(grant) if grant is not None else remote.OPERATIONS[kind]
    hg.need(type(op) is str and op in allowed and type(body) is dict, 'https_operation_refused')
    if op == 'inspect':
        fields = [{'entry_id'}, {'amount_msat'} if kind == 'incoming' else set()]
    else:
        fields = [set() if op == 'info' else {'invoice'} if op == 'plan'
            else {'data','confirmation'} if op == 'enroll' else
            {'entry_id'} | ({'preimage'} if op == 'release' else {'confirmation'} if op == 'retire' else set())]
    hg.need(set(body) in fields, 'https_parameters_invalid')
    for key in ('entry_id','preimage','confirmation'):
        if key in body: hg.need(ba.ci.hex_field(body[key],64), 'https_parameters_invalid')
    if 'amount_msat' in body: hg.need(hg.integer(body['amount_msat'],1,1000010000), 'https_parameters_invalid')
    if 'invoice' in body: hg.need(type(body['invoice']) is str and 100 <= len(body['invoice']) <= 16384, 'https_parameters_invalid')


class Client:
    def __init__(self, config):
        self.config = copy.deepcopy(configuration(config))
        self.grant = self.config['grant']; self.gid = ba.digest(self.grant)
        self.context = context(self.config['ca_pem'])

    def _request(self, op, body):
        # Called only after the public operation/schema checks. Never accept an
        # RPC method, URL or authentication header from an operation body.
        raw = json.dumps(dict(grant_id=self.gid,operation=op,body=body),allow_nan=False,separators=(',',':')).encode()
        hg.need(len(raw) <= LIMIT, 'https_request_too_large')
        p = urllib.parse.urlsplit(self.config['url']); conn = None
        try:
            conn = http.client.HTTPSConnection(p.hostname,p.port or 443,timeout=TIMEOUT,context=self.context)
            conn.request('POST','/v1/'+remote.CALL,body=raw,headers={
                'Content-Type':'application/json','Accept':'application/json','Rune':self.config['rune']})
            response = conn.getresponse()
            if response.status != 201 or response.getheader('Content-Type','').split(';')[0].strip().lower() != 'application/json':
                raise HttpsError('coordinator_reply_refused_or_unavailable')
            data = response.read(LIMIT+1)
            if len(data) > LIMIT: raise HttpsError('coordinator_reply_refused_or_unavailable')
            def invalid_constant(_): raise ValueError('invalid_json_constant')
            value = json.loads(data,object_pairs_hook=hg.unique_object,parse_constant=invalid_constant)
            if type(value) is not dict or 'error' in value: raise HttpsError('coordinator_reply_refused_or_unavailable')
            return value
        except Exception:
            raise HttpsError('coordinator_reply_refused_or_unavailable') from None
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass

    def call(self, op, body=None):
        body = {} if body is None else copy.deepcopy(body)
        parameters(self.grant['kind'],op,body,self.grant)
        if op == 'enroll':
            ba.payload(body['data'],self.grant)
            hg.need(body['confirmation'] == ba.digest(body['data']), 'exact_confirmation_required')
        observed = info(self._request('info',{}),self.grant)
        # Expired/paused grants remain usable for existing enrollment recovery.
        # Worker/node admission checks decide whether a new operation may start.
        return observed if op == 'info' else self._request(op,body)
