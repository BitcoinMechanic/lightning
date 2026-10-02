"""HTTP quote and fresh background-worker steps for the routed receive fixture."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from reverse_quote_api import Quotes, Server
from reverse_request import transport
from service_manager import private_load
from swap_controller import save
import routed_receive_service as service


class Workflow:
    def __init__(self, lab, incoming, outgoing, invoice):
        self.lab = lab
        self.config = dict(profile=service.PROFILE, btc_cli=incoming['cli'], xbt_cli=outgoing['cli'],
                           market=dict(max_btc_sats=2000, max_xbt_sats=100010, margin_bps=0),
                           max_xbt_routing_fee_msat=10000)
        self.settings = dict(receive_policy=self.config, btc_cli=incoming['cli'], xbt_cli=outgoing['cli'],
                             node_ids=[incoming['id'], outgoing['id']], swap_root=str(lab.root),
                             deployment='operator-pair-v1', reverse_profile='reverse-service-regtest-v1')
        self.settings_path = lab.root/'worker-settings.json'
        save(self.settings_path, self.settings)
        self.body = dict(request_id=secrets.token_hex(16), xbt_invoice=invoice, max_btc_sats=2000)
        self.directory = lab.root/('receive-api-'+self.body['request_id'])
        self.token = secrets.token_hex(32)
        self.server = Server(lab.port(), Quotes(self.settings, auto_process=True), dict(token=self.token, scope='receive'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        try:
            # Only the exchange response is a fixture. The real estimate,
            # invoice validation, planning, HTTP and registration run unchanged.
            def market(kind):
                base = dict(success=True, pair='BTCB2_BTC')
                if kind == 'ticker':
                    return dict(base, ticker=dict(bestBid='0.015', bestAsk='0.015', computedAt=int(time.time()*1000)))
                assert kind == 'orderbook'
                return dict(base, asks=[dict(price='0.015', quantity='1', isAmm=False)])
            original = service.RPC.call
            def operator_rpc(cli, method, *args):
                assert cli in (incoming['cli'], outgoing['cli'], [*outgoing['cli'], '-k']), 'customer RPC accessed'
                return original(cli, method, *args)
            with patch.object(service.oracle, 'fetch', side_effect=market), patch.object(service.RPC, 'call', side_effect=operator_rpc):
                url = 'http://127.0.0.1:'+str(self.server.server_address[1])
                self.offer = transport(url, self.token, self.body, '/v1/receive')
                assert transport(url, self.token, self.body, '/v1/receive') == self.offer
            with patch.object(service.oracle, 'fetch', side_effect=AssertionError('repeated quote repriced')):
                assert Quotes(self.settings, auto_process=True).receive.quote(self.body) == self.offer
            self.quote = private_load(self.directory/'quote.json')
            audit = self.quote['oracle']
            assert self.offer['xbt_sats'] == 100000 and self.offer['btc_sats'] == 1501
            assert audit['xbt_sats'] == 100010 and audit['max_xbt_routing_fee_msat'] == 10000
            assert self.quote['controller']['route'][0]['amount_msat'] == 100005000
            decoded = lab.rpc(incoming['cli'], 'decode', self.offer['btc_invoice'])
            assert decoded['currency'] == 'bcrt' and decoded['amount_msat'] == 1501000
            assert decoded['payment_hash'] == self.quote['terms']['payment_hash']
            assert self.step_result()['outcome'] == 'waiting_for_btc'
            assert not (self.directory/'state.json').exists()
            assert not lab.rpc(outgoing['cli'], 'listsendpays')['payments']
            print('PASS: authenticated API prices 100,000 XBT sats plus 10-sat allowance at 1,501 BTC sats; stable offer; no spend', flush=True)
        finally:
            self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)

    def step_result(self):
        # Each invocation is a fresh worker process, with no oracle fixture and
        # no customer RPC command present in its configuration.
        code = ('import json,sys; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[1]); '
                'from service_manager import private_load; from service_runtime import tick; '
                'print(json.dumps(tick(private_load(Path(sys.argv[2])))))')
        result = subprocess.run([sys.executable, '-c', code, str(Path(__file__).resolve().parent), str(self.settings_path)],
                                capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stderr
        health = json.loads(result.stdout)
        assert health.get('nodes_ready') and health.get('operators_ready') and not health.get('warning_present'), health
        reports = [r for r in health['swaps'] if r['directory'] == self.directory.name]
        if reports:
            assert len(reports) == 1
            return reports[0]
        state = private_load(self.directory/'state.json')
        assert state['phase'] in ('btc_released', 'btc_failed')
        return {'phase': state['phase']}

    def controller(self, *args):
        assert not args
        result = self.step_result()
        return subprocess.CompletedProcess(['worker'], 0, json.dumps(result), '')
