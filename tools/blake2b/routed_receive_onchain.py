"""Routed receive API on-chain outcomes; disposable regtest only.

Claim: the relay closes its coordinator-facing channel, then learns the
receiver's preimage off-chain and claims on-chain. The coordinator extracts
that preimage. Timeout: the receiver stops, the relay closes the final channel,
recovers its HTLC on-chain and propagates definitive failure to the coordinator.
BTC height is fixed throughout; this does not test relative chain speeds.
"""
import json

from htlc_timeout import recover_timeout
from preimage_claim import run_claim
from service_manager import private_load
from smoke_regtest import wait_until


def finish(lab, workflow, payer, incoming, outgoing, relay, receiver,
           btc, xbt, invoice, initial, paying, gate, mode):
    assert mode in ('preimage', 'timeout')
    ph = invoice['payment_hash']
    path = workflow.directory/'state.json'
    checkpoint = path.read_bytes()
    original = private_load(path)
    assert original['phase'] == 'outgoing_started' and 'preimage' not in original
    rpc = lambda node, *args: lab.rpc(node['cli'], *args)
    active = [outgoing, relay, receiver]
    def channel(node, peer):
        rows = [c for c in rpc(node,'listpeerchannels')['channels'] if c['peer_id'] == peer['id']]
        assert len(rows) == 1
        return rows[0]
    def htlc(node, peer, direction):
        expected = 'SENT_ADD_ACK_REVOCATION' if direction == 'out' else 'RCVD_ADD_ACK_REVOCATION'
        rows = [h for h in channel(node,peer).get('htlcs',[]) if h['payment_hash'] == ph
                and h['direction'] == direction and h['state'] == expected]
        return rows[0] if len(rows) == 1 else None
    first = wait_until(lambda: htlc(outgoing,relay,'out'),outgoing['proc'])
    final = wait_until(lambda: htlc(relay,receiver,'out'),relay['proc'])
    assert first['expiry'] - final['expiry'] == 30
    assert first['amount_msat'] == 100005000 and final['amount_msat'] == 100000000
    # Thirty blocks separate the two XBT expiries so the final-hop timeout and
    # sweep can finish without forcing the healthy upstream channel on-chain.
    btc_height = rpc(btc,'getblockcount')
    quote_path = gate.with_suffix('.quotes.json')
    quote_before = quote_path.read_bytes()
    attempts = rpc(outgoing,'listsendpays')['payments']
    assert len(attempts) == 1 and attempts[0]['status'] == 'pending'
    attempt_id = tuple(attempts[0].get(k) for k in ('id','groupid','partid'))

    def mine(count):
        blocks = rpc(xbt,'generatetoaddress',count,rpc(xbt,'getnewaddress'))
        height = rpc(xbt,'getblockcount')
        for node in active:
            wait_until(lambda: rpc(node,'getinfo')['blockheight'] >= height,node['proc'],timeout=90)
        return blocks

    def confirmed_outputs(node, txid):
        return [o for o in rpc(node,'listfunds')['outputs'] if o['txid'] == txid and o['status'] == 'confirmed']

    def held():
        assert path.read_bytes() == checkpoint
        assert quote_path.read_bytes() == quote_before
        assert rpc(incoming,'xbt-quote-status',ph)['phase'] == 'held'
        pays = rpc(payer,'listsendpays')['payments']
        assert len(pays) == 1 and pays[0]['status'] == 'pending'
        assert not pays[0].get('payment_preimage')
        assert rpc(btc,'getblockcount') == btc_height

    def pending():
        for _ in range(2):
            result = workflow.step_result()
            assert result.get('outcome') == 'pending', result
            held()
        print('PASS: fresh API workers keep BTC held while routed XBT remains unresolved on-chain',flush=True)

    pending()
    if mode == 'preimage':
        c = channel(outgoing,relay)
        funding = dict(txid=c['funding_txid'],outnum=c['funding_outnum'])
        # The shared claim helper inspects Bob's channel. Relay has two, so
        # expose only the exact closing channel for that one read operation.
        def claim_rpc(node, method, *args):
            if node is relay and method == 'listpeerchannels':
                return {'channels':[channel(relay,outgoing)]}
            return rpc(node,method,*args)
        def reveal():
            pending()  # Commitment confirmed; controller still has no preimage.
            assert rpc(receiver,'xbt-continue',ph)['continued'] == 1
            paid = wait_until(lambda: next((i for i in rpc(receiver,'listinvoices','routed-receive')['invoices']
                                            if i['status'] == 'paid'),None),receiver['proc'])
            assert paid['amount_received_msat'] == 100000000
            return paid['payment_preimage']
        print('On-chain checks: Alice = XBT coordinator; Bob = XBT relay',flush=True)
        claim = run_claim(xbt,outgoing,relay,funding,invoice,None,first['expiry'],mine,
                          claim_rpc,confirmed_outputs,amount_sat=100005,standalone=False,release=reveal)
        expected_proof = claim['payment_preimage']
        preserved = ((relay,receiver,-100000000),(receiver,relay,100000000))
        print('PASS: relay claim and CSV sweep confirmed; coordinator extracted the receiver preimage on-chain',flush=True)
    else:
        assert rpc(receiver,'listinvoices','routed-receive')['invoices'][0]['status'] == 'unpaid'
        c = channel(relay,receiver)
        funding = dict(txid=c['funding_txid'],outnum=c['funding_outnum'])
        lab.stop(receiver['proc']); active.remove(receiver)
        print('On-chain checks: Alice = XBT relay; Bob = offline XBT receiver',flush=True)
        recover_timeout(xbt,relay,receiver,funding,final['expiry'],c['our_to_self_delay'],mine,rpc,
                        confirmed_outputs,amount_sat=100000,standalone=False,before_timeout=pending)
        expected_proof = None
        preserved = ((outgoing,relay,0),(relay,outgoing,0))
        print('PASS: relay timeout refund and CSV sweep confirmed; waiting for original coordinator attempt to fail',flush=True)

    terminal = 'complete' if mode == 'preimage' else 'failed'
    def outcome():
        rows = rpc(outgoing,'listsendpays')['payments']
        assert len(rows) == 1 and tuple(rows[0].get(k) for k in ('id','groupid','partid')) == attempt_id
        assert rows[0]['payment_hash'] == ph
        assert rows[0]['status'] in ('pending',terminal)
        if expected_proof is None: assert not rows[0].get('payment_preimage')
        return rows if rows[0]['status'] == terminal else None
    resolved = wait_until(outcome,outgoing['proc'],timeout=90)
    if expected_proof is not None: assert resolved[0]['payment_preimage'] == expected_proof
    held()  # BTC is still held until a fresh worker reads the definite outcome.
    for _ in range(2):
        result = workflow.step_result()
        assert result.get('phase') == ('btc_released' if mode == 'preimage' else 'btc_failed'), result
    paying.wait(timeout=40)
    assert (paying.returncode == 0) == (mode == 'preimage')
    assert rpc(outgoing,'listsendpays')['payments'] == resolved
    assert rpc(btc,'getblockcount') == btc_height
    if expected_proof is not None:
        paid = rpc(payer,'listpays',workflow.offer['btc_invoice'])['pays'][0]
        assert paid['status'] == 'complete' and paid['preimage'] == expected_proof
    btc_amount = workflow.offer['btc_sats']*1000 if mode == 'preimage' else 0
    for node, peer, delta in ((payer,incoming,-btc_amount),(incoming,payer,btc_amount),*preserved):
        def settled():
            c = channel(node,peer)
            return (c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                    and c['to_us_msat'] == initial[(node['id'],c['short_channel_id'])]+delta)
        wait_until(settled,node['proc'])
    before = json.loads(quote_before)[ph]
    after = json.loads(quote_path.read_text())[ph]
    before['phase'] = 'resolved' if mode == 'preimage' else 'failed'
    if expected_proof is not None: before['preimage'] = expected_proof
    assert after == before and not rpc(incoming,'xbt-held')['held']
    print('PASS: worker reconciled original XBT outcome; BTC and surviving XBT channel balances verified; no resend',flush=True)
    print('Routed receive API on-chain '+mode+' OK (confirmed sweep; XBT on-chain fees apply; BTC height fixed; regtest only)',flush=True)
