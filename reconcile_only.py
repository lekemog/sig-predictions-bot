"""Read-only SIG maintenance: verify a halted, already-filled job and checkpoint it."""
import json
import os
from pathlib import Path
from hosted_runner import DurableJournal, RepositoryState, record_sweep, prepare_universe
from trading_bot import API, Halt, Runner, signed_positions


class ReadOnlyAPI(API):
    def state(self):
        state = super().state()
        # Whitelisted account evidence only; never print transport exceptions,
        # headers, credentials or arbitrary API responses.
        print(json.dumps({'event': 'readonly_account_snapshot', 'at': state['at'],
                          'cash': state['cash'], 'positions': signed_positions(state),
                          'open_orders': len(state['orders']),
                          'recent_fill_ids': [f.get('id') for f in state['recent_fills']]}), flush=True)
        return state

    def request(self, method, path, payload=None):
        if method != 'GET' or payload is not None:
            raise Halt('Read-only reconciliation prohibits order writes')
        return super().request(method, path, payload)


def reconcile_pending(api, journal, config):
    runner = Runner(api, journal, config, live=True)
    # Never enter cycle(), submit(), finish() or repair(). This routine verifies
    # closed original fills and writes only repository checkpoint metadata.
    return [runner.reconcile_halt(job) for job in journal.pending()]


def main():
    remote = RepositoryState()
    if remote.data.get('enabled') is not True:
        raise Halt('Durable kill switch disabled')
    journal = DurableJournal('private/reconciliation.sqlite', remote)
    config = json.loads(Path('bot_config.json').read_text())
    # Match the existing hosted executor's effective fee policy exactly; the
    # on-disk config is intentionally an unverified local-use template.
    config['kill_switch'] = 'private/STOP'
    config['costs'] = {
        'verified': True, 'fee_per_share': 0,
        'source': 'SIG markets-and-trading documentation: no participant trading fees; '
                  'complete prior account ledger reconciled without extra fees. '
                  'Every new execution must independently reconcile fills and cash.'}
    prepare_universe(config, remote.data)
    api = ReadOnlyAPI(os.environ.get('SIG_API_KEY'))
    api.state()
    for job in journal.pending():
        for result in job.get('response', {}).get('results', []):
            oid = result.get('data', {}).get('orderId')
            if oid is None:
                continue
            order = api.get(f'/orders/{oid}')
            order = order.get('data', order)
            fills = api.get(f'/orders/{oid}/fills?limit=100')
            print(json.dumps({'event': 'readonly_order_evidence', 'order_id': oid,
                              'order': {k: order.get(k) for k in
                                        ('open', 'exchangeId', 'side', 'action', 'quantity',
                                         'quantityTraded', 'priceLimit')},
                              'fills': fills.get('data'), 'coverage': fills.get('coverage'),
                              'pagination': fills.get('pagination')}), flush=True)
    results = reconcile_pending(api, journal, config)
    # A reconciliation receipt is never a successful live sweep.
    record_sweep(remote, 'reconcile-only', 'incomplete', 0)
    journal.event('readonly_checkpoint_recovery', results)
    journal.checkpoint()
    print(json.dumps({'mode': 'reconcile-only', 'orders_submitted': 0, 'results': results}), flush=True)


if __name__ == '__main__':
    main()

