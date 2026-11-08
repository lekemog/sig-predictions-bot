"""Regression for the Oct 20 CO-03 halt: value entry filled in full, then fill reads 503'd.

Recovery must be GET-only and clear the job only when the live account equals the
pre-trade snapshot plus exactly the verified fills.
"""
import copy
import unittest
from unittest.mock import Mock, patch
from trading_bot import Halt, Runner, TID


def fixture():
    leg = {'exchangeId': '894', 'side': 'no', 'action': 'buy', 'quantity': 2410,
           'price': 0.645, 'tournamentId': TID}
    before = {'cash': 3554.6, 'orders': [],
              'positions': [{'exchangeId': '846', 'quantity': -1000}]}
    after = {'cash': 2012.05, 'orders': [], 'at': 'fresh',
             'positions': [{'exchangeId': '846', 'quantity': -1000},
                           {'exchangeId': '894', 'quantity': -2410}]}
    fills = {'orderId': 51118866, 'exchangeId': '894', 'tournamentId': TID,
             # SIG reported totalCost 1542.55 (< 2410 * 0.645): fills priced inside the limit.
             'data': [{'id': 1, 'quantity': -2380, 'price': 0.64},
                      {'id': 2, 'quantity': -30, 'price': 0.645}],
             'coverage': {'complete': True}, 'pagination': {'hasMore': False}}
    job = {'id': 'co3', 'status': 'halted', 'repairs': [], 'created_at': 'x',
           'plan': {'kind': 'value_entry'}, 'request': {'legs': [leg]}, 'before': before,
           'response': {'results': [{'index': 0, 'data': {
               'orderId': 51118866, 'exchangeId': '894', 'open': False, 'remainingQuantity': 0,
               'quantity': 2410, 'quantityTraded': 2410, 'price': 0.645, 'totalCost': 1542.55}}]}}
    cfg = {'costs': {'fee_per_share': 0}, 'baskets': [], 'kill_switch': '/nonexistent-stop',
           'risk_limits': {'committed': 100000, 'reserve': 2000, 'clip': 5000, 'race': 3000,
                           'directional': 60000, 'factor': 60000}}
    api = Mock()
    api.state.return_value = copy.deepcopy(after)
    api.fills = fills

    def get(path):
        if path == '/orders/51118866/fills?limit=100':
            return copy.deepcopy(api.fills)
        if path == '/orders/51118866':
            return {'data': dict(leg, open=False, priceLimit=0.645)}
        raise AssertionError(path)
    api.get.side_effect = get
    api.request.side_effect = AssertionError('Recovery must not POST or DELETE')
    return job, cfg, api


class ValueReadRecoveryTests(unittest.TestCase):
    def run_recovery(self, mutate=None, live=True, via='finish'):
        job, cfg, api = fixture()
        journal = Mock()
        if mutate:
            mutate(job, api)
        runner = Runner(api, journal, cfg, live=live)
        with patch('trading_bot.portfolio_risk', return_value=(0, {}, 50000)):
            result = runner.finish(job) if via == 'finish' else runner.reconcile_halt(job)
        api.request.assert_not_called()
        return result, journal, job

    def test_current_code_halts_forever_on_this_job(self):
        job, cfg, api = fixture()
        with self.assertRaises(Halt):
            Runner(api, Mock(), cfg, live=True).reconcile_rounding_halt(job)

    def test_live_recovery_marks_done_with_verified_snapshot(self):
        result, journal, job = self.run_recovery()
        self.assertEqual(result['orders_submitted'], 0)
        self.assertEqual(journal.save.call_args.args[1], 'done')
        self.assertEqual(job['after']['cash'], 2012.05)
        self.assertAlmostEqual(job['cash_change'], -1542.55, places=6)
        self.assertIn('51118866', job['fills'])

    def test_dry_run_verifies_but_does_not_clear_halt(self):
        result, journal, job = self.run_recovery(live=False, via='reconcile_halt')
        self.assertTrue(result['dry_run'])
        journal.save.assert_not_called()
        self.assertEqual(job['status'], 'halted')

    def test_any_mismatch_fails_closed(self):
        changes = {
            'cash moved': lambda j, a: a.state.return_value.update(cash=2000.00),
            'open order': lambda j, a: a.state.return_value.update(orders=[{'id': 9}]),
            'other position changed': lambda j, a: a.state.return_value['positions'][0].update(quantity=-900),
            'short fill': lambda j, a: a.fills['data'][1].update(quantity=-29),
            'fills incomplete': lambda j, a: a.fills['coverage'].update(complete=False),
            'more pages': lambda j, a: a.fills['pagination'].update(hasMore=True),
            'response partial': lambda j, a: j['response']['results'][0]['data'].update(quantityTraded=2000),
            'response open': lambda j, a: j['response']['results'][0]['data'].update(open=True),
            'has repairs': lambda j, a: j.update(repairs=[{}]),
            'has after': lambda j, a: j.update(after={}),
            'basket kind': lambda j, a: j['plan'].update(kind='basket_entry'),
            'yes side': lambda j, a: j['request']['legs'][0].update(side='yes'),
            'two legs': lambda j, a: j['request']['legs'].append(dict(j['request']['legs'][0])),
        }
        for name, change in changes.items():
            with self.subTest(name), self.assertRaises(Halt):
                self.run_recovery(change)

    def test_exposure_breach_fails_closed(self):
        job, cfg, api = fixture()
        with patch('trading_bot.portfolio_risk', return_value=(0, {}, 10**9)), self.assertRaises(Halt):
            Runner(api, Mock(), cfg, live=True).finish(job)


if __name__ == '__main__':
    unittest.main()
