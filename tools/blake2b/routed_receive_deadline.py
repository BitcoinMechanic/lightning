"""Bounded API worker's BTC close and claim; independent XBT height held fixed."""
import json

from preimage_claim import run_claim
from service_manager import private_load
from smoke_regtest import wait_until


def finish(lab, workflow, payer, incoming, outgoing, relay, receiver,
           btc, xbt, invoice, initial, paying):
    rpc=lambda node,*args:lab.rpc(node['cli'],*args)
    path=workflow.directory/'state.json'
    state=private_load(path);ph=invoice['payment_hash']
    def channel(node,peer):
        rows=[c for c in rpc(node,'listpeerchannels')['channels'] if c['peer_id']==peer['id']]
        assert len(rows)==1
        return rows[0]
    def mine(count):
        blocks=rpc(btc,'generatetoaddress',count,rpc(btc,'getnewaddress'))
        height=rpc(btc,'getblockcount')
        for node in (payer,incoming):
            wait_until(lambda:rpc(node,'getinfo')['blockheight']>=height,node['proc'],timeout=90)
        return blocks
    def confirmed(node,txid):
        return [o for o in rpc(node,'listfunds')['outputs'] if o['txid']==txid and o['status']=='confirmed']
    height=rpc(xbt,'getblockcount')
    terms_before=(workflow.directory/'quote.json').read_bytes()
    original=rpc(outgoing,'listsendpays')['payments']
    assert len(original)==1 and original[0]['status']=='pending'
    expiry=state['btc_incoming_pin']['expiry']
    mine(expiry-rpc(btc,'getblockcount')-73)
    before=path.read_bytes()
    assert workflow.step_result()['outcome']=='pending'
    assert path.read_bytes()==before and channel(incoming,payer)['state']=='CHANNELD_NORMAL'
    mine(1)
    assert workflow.step_result()['outcome']=='pending'
    protected=private_load(path)
    assert protected['btc_close_intent']['channel_id']==state['btc_incoming_pin']['channel_id']
    assert protected['btc_close_result']['type']=='unilateral' and 'preimage' not in protected
    wait_until(lambda:channel(incoming,payer)['state']=='AWAITING_UNILATERAL',incoming['proc'])
    for _ in range(2):assert workflow.step_result()['outcome']=='pending'
    assert rpc(outgoing,'listsendpays')['payments']==original
    print('PASS: worker kept BTC open at 73 blocks and closed exact pinned channel at 72; fresh workers did not resend',flush=True)
    def release():
        assert workflow.step_result()['outcome']=='pending'
        assert rpc(receiver,'xbt-continue',ph)['continued']==1
        payment=rpc(outgoing,'waitsendpay',ph,10)
        assert payment['status']=='complete'
        assert workflow.step_result()['phase']=='btc_released'
        return payment['payment_preimage']
    print('On-chain checks: Alice = BTC payer; Bob = BTC coordinator',flush=True)
    result=run_claim(btc,payer,incoming,
                     dict(txid=state['btc_incoming_pin']['funding_txid'],outnum=state['btc_incoming_pin']['funding_outnum']),
                     dict(bolt11=workflow.offer['btc_invoice'],payment_hash=ph),None,expiry,
                     mine,rpc,confirmed,amount_sat=workflow.offer['btc_sats'],standalone=False,
                     release=release,close=protected['btc_close_result'])
    paying.wait(timeout=40);assert paying.returncode==0
    paid=rpc(receiver,'listinvoices','routed-receive')['invoices'][0]
    assert paid['status']=='paid' and paid['payment_preimage']==result['payment_preimage']
    deltas={k:0 for k in initial}
    for a,b,amount in ((outgoing,relay,100005000),(relay,receiver,100000000)):
        scid=channel(a,b)['short_channel_id'];deltas[(a['id'],scid)]-=amount;deltas[(b['id'],scid)]+=amount
    for node in (outgoing,relay,receiver):
        def settled():
            return all(c['state']=='CHANNELD_NORMAL' and not c.get('htlcs')
                       and c['to_us_msat']==initial[(node['id'],c['short_channel_id'])]+deltas[(node['id'],c['short_channel_id'])]
                       for c in rpc(node,'listpeerchannels')['channels'])
        wait_until(settled,node['proc'])
    for _ in range(2):assert workflow.step_result()['phase']=='btc_released'
    after=rpc(outgoing,'listsendpays')['payments']
    assert len(after)==1 and after[0]['id']==original[0]['id'] and after[0]['status']=='complete'
    assert (workflow.directory/'quote.json').read_bytes()==terms_before
    assert rpc(xbt,'getblockcount')==height and not rpc(incoming,'xbt-held')['held']
    print('PASS: XBT settled through relay; BTC preimage claim and CSV sweep confirmed; original route and attempt preserved',flush=True)
    print('Bounded routed receive deadline OK (72-block close; BTC fees apply; XBT height fixed; regtest only)',flush=True)
