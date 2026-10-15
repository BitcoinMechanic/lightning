#!/usr/bin/env python3
"""0100: compiled StartOS SDK action handlers around the accepted 0098 runtimes.

The OS effects boundary is supplied by a disposable subprocess adapter. This
does not claim StartOS host installation, lifecycle or mainnet acceptance.
"""
import copy,json,subprocess,time
from pathlib import Path
import test_package_engine as base
from test_engine import require
import test_customer_engine as customer

BRIDGE='/opt/actions0100/migration-action-fixture.cjs'
COUNTS={}

def action(lab,asset,directory,aid=None,input=None,lifecycle=False):
    request=dict(disposable_regtest=True,asset=asset,generation=lab.generation,directory=str(directory),
                 id=aid,input=input or {},lifecycle=lifecycle)
    until=time.monotonic()+120
    while True:
        p=subprocess.run(['/usr/local/bin/node','--no-deprecation',BRIDGE],input=json.dumps(request),capture_output=True,text=True,timeout=180)
        require(not p.stderr,'StartOS_action_stderr_'+str(aid));result=json.loads(p.stdout)
        if p.returncode==0:break
        if result.get('reason')=='service_busy' and time.monotonic()<until:time.sleep(.05);continue
        raise RuntimeError('StartOS_action_failed_'+str(aid))
    if lifecycle:return result['command']
    COUNTS[aid]=COUNTS.get(aid,0)+1
    return result['result']


class Driver(base.Driver):
    def command(self,mode,request=None):
        if mode=='init':return action(self.lab,'controller',self.directory,'migrated-initialize',dict(confirmed=True))
        require(mode=='action','fixture_mode_invalid')
        a=request['action'];fields={k:v for k,v in request.items() if k!='action'}
        if a=='pair':
            binding=fields['binding'];self.direction=binding['direction'];inputs=dict(confirmed=fields['confirmed'])
            for kind,n in binding['nodes'].items():
                inputs[kind+'Url']=n['url'];inputs[kind+'Ca']=n['ca_pem']
                inputs[kind+'Credential']=json.dumps(dict(grant=n['grant'],rune=n['rune']))
            aid='migrated-pair-'+self.direction
        elif a=='prepare':
            self.direction=fields['direction'];aid='migrated-new-'+self.direction;inputs=dict(invoice=fields['invoice'])
        elif a in ('confirm','cancel'):
            aid='migrated-'+a+'-'+self.direction;inputs=dict(cid=fields['cid'],confirmed=True)
        elif a=='configure':
            aid='migrated-settings';inputs=dict(paused=fields['paused'],markupBps=fields['markup_bps'])
        else:
            aid='migrated-'+a;inputs=fields
        return action(self.lab,'controller',self.directory,aid,inputs)
    def argv(self,mode):
        require(mode=='run','fixture_daemon_mode_invalid')
        return action(self.lab,'controller',self.directory,lifecycle=True)


def admin_call(lab,node,**request):
    a=request['action'];asset='btc' if node['profile']==base.ci.BTC else 'xbt';root=customer.state_dir(node)
    if a=='initialize':aid='migrated-admin-init';fields=dict(confirmed=True)
    elif a=='review':
        l=request['limits'];aid='migrated-review-'+request['kind']
        fields=dict(maxSwaps=l['max_swaps'],maxAmount=l['max_amount_msat'],totalAmount=l['total_amount_msat'],lifetime=request['lifetime_seconds'])
        fields.update(dict(minDelay=l['min_cltv_delta'],maxDelay=l['max_cltv_delta']) if request['kind']=='incoming'
            else dict(maxFee=l['max_fee_msat'],maxDelay=l['max_delay_blocks'],maxHops=l['max_hops']))
    elif a=='confirm':aid='migrated-grant-confirm';fields=dict(confirmation=request['confirmation'],confirmed=request['confirmed'])
    else:raise RuntimeError('fixture_admin_action_unmapped')
    # The assertions in base.grant_setup verify review isolation, exact bounds,
    # repeated confirmation and the durable one-grant result independently.
    return action(lab,asset,root,aid,fields)


class Client(base.Client):
    def setup(self):
        asset='btc' if self.node['profile']==base.ci.BTC else 'xbt'
        args=(self.lab,asset,customer.state_dir(self.node),'migrated-grant-credential',dict(confirmation=self.gid,confirmed=True))
        bundle=action(*args)['credential'];require(action(*args)['credential']==bundle,'action_credential_replaced')
        credential=self.lab.rpc_named(self.node['cli'],base.remote.CREDENTIAL,dict(grant_id=self.gid,confirmed=True))
        require(credential['rune']==bundle['rune'],'action_credential_changed')
        self.credential=credential


original_scenario=base.scenario
def scenario(lab,topologies,direction,name):
    r=original_scenario(lab,topologies,direction,name)
    receipt=dict(schema=1,fixture='startos-sdk-actions-0100',startos_host_acceptance=False,regtest_only=True,
        actual_sdk_forms=True,compiled_handlers=True,unchanged_runtime=True,closed_channels=0,installer=False,
        counts=copy.deepcopy(COUNTS))
    (lab.root/'action-result.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return r


if __name__=='__main__':
    base.Driver=Driver;base.admin_call=admin_call;base.Client=Client;base.scenario=scenario
    base.main()
