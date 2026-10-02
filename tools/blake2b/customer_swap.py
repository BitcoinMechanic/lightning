"""Request, review and confirm a BTC payment funded from your own XBT wallet."""
import argparse
import json
import os
from pathlib import Path

from service_manager import private_load
from customer_errors import CustomerError
from quote_refusal import QuoteRefused
from reverse_check import private_invoice
from reverse_request import request_quote
from reverse_customer import locked, review, validate, pay, result, routing_budget, fee_summary


def workflow(invoice, cli, directory, token_path, url, max_xbt_sats,
             max_delay=2016, confirm=input, emit=print, retry_quote=False,
             max_xbt_routing_fee_sats=None):
    routing_budget(max_xbt_sats, max_xbt_routing_fee_sats)
    routing = ({} if max_xbt_routing_fee_sats is None else
               dict(max_xbt_routing_fee_sats=max_xbt_routing_fee_sats))
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('private customer directory required')
    with locked(directory):
        wallet = directory/'wallet'
        state_path = wallet/'customer.json'
        if state_path.exists():
            state = private_load(state_path)
            if (state['btc_invoice'] != invoice or state['cli'] != cli
                    or state['max_xbt_sats'] != max_xbt_sats
                    or state.get('max_xbt_routing_fee_sats') != max_xbt_routing_fee_sats
                    or state['max_delay'] != max_delay or state['network'] != 'xbt'):
                raise ValueError('existing customer intent differs')
            if state['phase'] == 'submitted':
                return result(state)  # No quote request, prompt or second pay.
            if state['phase'] != 'reviewed':
                raise ValueError('unexpected customer phase')
            checked = validate(state['offer'], invoice, cli, max_xbt_sats, max_delay, **routing)
            if any(state[k] != v for k, v in checked.items()):
                raise ValueError('customer binding changed')
            offer = state['offer']
            reviewed = dict(btc_sats=offer['btc_sats'], xbt_sats=offer['xbt_sats'],
                            final_cltv=checked['final_cltv'], expires_at=offer['expires_at'],
                            **fee_summary(offer, max_xbt_routing_fee_sats))
        else:
            request = directory/'request'
            options = {'retry_refused': True} if retry_quote else {}
            request_quote(invoice, private_load(token_path), url, request, max_xbt_sats, **options)
            reviewed = review(private_load(request/'offer.json'), invoice, cli, wallet,
                              max_xbt_sats, max_delay, **routing)
        emit(json.dumps({k: reviewed[k] for k in
                         ('btc_sats', 'xbt_sats', 'final_cltv', 'expires_at',
                          'max_xbt_routing_fee_sats', 'max_total_xbt_sats') if k in reviewed}))
        if confirm('Type PAY to accept this quote and send XBT: ').strip() != 'PAY':
            return dict(outcome='not_submitted', payment_started=False)
        return pay(wallet)  # Existing single-submission and fresh validation guards.


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--invoice-file', type=Path, required=True)
    p.add_argument('--lightning-dir', type=Path, required=True)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--token-file', type=Path, default=Path.home()/'.config/cln-swaps/customer-api.json')
    p.add_argument('--url', default='http://127.0.0.1:19840')
    p.add_argument('--max-xbt-sats', type=int, required=True)
    p.add_argument('--max-delay', type=int, default=2016)
    p.add_argument('--max-xbt-routing-fee-sats', type=int, default=None,
                   help='Opt into routing; fee cap included in --max-xbt-sats (0..1000).')
    p.add_argument('--retry-quote', action='store_true',
                   help='Retry only a recorded definite pre-creation refusal.')
    args = p.parse_args()
    os.umask(0o077)
    try:
        cli = [str(Path(__file__).resolve().parents[2]/'cli/lightning-cli'),
               '--lightning-dir='+str(args.lightning_dir.expanduser().resolve()),
               '--network=xbt', '--json', '--notifications=none']
        answer = workflow(private_invoice(args.invoice_file.expanduser()), cli,
                          args.directory.expanduser().absolute(), args.token_file.expanduser(),
                          args.url, args.max_xbt_sats, args.max_delay, retry_quote=args.retry_quote,
                          max_xbt_routing_fee_sats=args.max_xbt_routing_fee_sats)
        print(json.dumps(answer))
        return 0
    except CustomerError as error:
        print(json.dumps(error.public()))
        return 1
    except QuoteRefused as error:
        print(json.dumps(dict(error.public(), message=str(error),
                             next_step='Correct the cause, then rerun the same command with --retry-quote.')))
        return 1
    except (Exception, KeyboardInterrupt):
        print(json.dumps(dict(event='customer_workflow_interrupted', details='withheld',
                             automatic_resubmission=False,
                             next_step='Rerun with the same directory and arguments to inspect or resume.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
