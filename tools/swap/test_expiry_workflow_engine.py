"""Disposable regtest only: exact native version declarations and unpaid-expiry regression."""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_engine,chain_identity as ci,test_cross_engine as ce
from test_engine import require
import cross_worker as cw
# Exact already-verified installed native builds, not a wildcard version relaxation.
ce.customer.CustomerLab.engine_versions={ci.BTC:('v26.06.10-suitbox1',),ci.PRIVKEYIO_XBT:('v26.06.9-blake2b.7-swap0094',)}

def unpaid(lab):
 receipts=[]
 for direction in ('forward','reverse'):
  topologies={label:ce.customer.topology(lab,direction+'-'+label,profile,grant_amount_msat=10000000) for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]}
  ia,oa=('btc','xbt') if direction=='forward' else ('xbt','btc')
  _,ins,ic=topologies[ia];_,outs,oc=topologies[oa]
  for nodes in (ins,outs):ce.routes.quiet(lab,nodes);ce.routes.public_graph(lab,nodes)
  incoming,outgoing=ic[1],oc[0]
  bolt,_=ce.recipient_invoice(lab,outs,'unpaid-'+direction)
  c=ce.contract(incoming,outgoing,bolt,direction)
  directory=lab.root/('cross-worker-unpaid-'+direction);cw.initialize_fixture(directory)
  driver=ce.Driver(lab,directory,dict(incoming=incoming,outgoing=outgoing))
  driver.cid=driver.run('prepare',contract=c)['contract_id'];driver.run('confirm',confirmation=driver.cid)
  for _ in range(3):driver.run('step')
  require(driver.run('status')['state']=='waiting_for_payment','invoice_not_published')
  before=outgoing.call('info')['remaining']
  # The incoming fixture alone observes its already-unpaid deadline passed.
  # The outgoing coordinator retains its real clock and still-valid invoice.
  (ce.customer.state_dir(incoming.node)/'customer-fixture-time').write_text(str(c['pay_until']+1))
  require(int(time.time())<c['outgoing']['expires_at'],'recipient_expired_before_test')
  driver.run('step',fault=dict(kind='outgoing',operation='retire'),crash=True)
  require(driver.run('step')['state']=='expired','unpaid_retirement_not_terminal')
  eid=cw.enrollment(c,'outgoing')
  require(outgoing.evidence(eid)['status']['state']=='retired','node_retirement_missing')
  outgoing.call('retire',dict(entry_id=eid,confirmation=eid))
  try:outgoing.call('send',dict(entry_id=eid))
  except Exception:pass
  else:raise RuntimeError('retired_enrollment_sent')
  require(outgoing.call('info')['remaining']==before,'retirement_refunded_budget')
  require(lab.rpc_named(outs[0]['cli'],'listsendpays',dict(payment_hash=c['outgoing']['payment_hash']))['payments']==[],'unpaid_sent')
  saved=driver.record();require('send' not in saved and saved['contract']==c,'old_contract_changed')
  receipts.append(dict(direction=direction,unpaid_expiry_retired=True,recipient_still_valid=True,retire_reply_SIGKILL_recovered=True,late_send_refused=True,budget_not_refunded=True,payment_attempts=0))
  print('PASS unpaid retirement: '+direction,flush=True)
  for _,nodes,_ in topologies.values():
   for node in reversed(nodes):lab.stop(node['proc'])
  for backend,_,_ in topologies.values():lab.stop(backend['proc'])
 (lab.root/'unpaid-result.json').write_text(json.dumps(receipts,indent=2)+'\n')
 print('UNPAID EXPIRY FIXTURE PASSED',flush=True)
if os.environ.get('EXPIRY_ONLY')=='1':ce.run=unpaid
ce.main()
