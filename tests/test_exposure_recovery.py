"""GET-only recovery of a filled basket job halted by a stale post-fill exposure check,
and the drop_unused_auto regression that caused it (22 Oct 2026)."""
import copy
import unittest
from unittest.mock import Mock, patch
from trading_bot import Halt, Runner, TID


def fixture(action='sell'):
    legs = [{'exchangeId': i, 'side': 'no', 'action': action, 'quantity': 137,
             'price': p, 'tournamentId': TID} for i, p in [('982', .37), ('983', .63)]]
    held = [{'exchangeId': '500', 'quantity': -1000, 'costBasis': 900.0}]
    texas = [{'exchangeId': i, 'quantity': -137, 'costBasis': c} for i, c in [('982', 55.24), ('983', 78.05)]]
    if action == 'sell':
        before = {'cash': 2057.66, 'positions': held + texas, 'orders': []}
        after = {'cash': 2194.66, 'positions': held, 'orders': [], 'at': 'fresh'}
    else:
        before = {'cash': 2194.66, 'positions': held, 'orders': []}
        after = {'cash': 2057.66, 'positions': held + texas, 'orders': [], 'at': 'fresh'}
    fills = {str(n): {'orderId': n, 'exchangeId': l['exchangeId'], 'tournamentId': TID,
                      'data': [{'id': n, 'quantity': -137, 'price': l['price']}],
                      'coverage': {'complete': True}, 'pagination': {'hasMore': False}}
             for n, l in enumerate(legs, 1)}
    job = {'id': 'halted', 'status': 'halted', 'repairs': [],
           'plan': {'kind': 'basket_exit' if action == 'sell' else 'basket_entry', 'action': action},
           'request': {'legs': legs},
           'response': {'results': [{'index': n - 1, 'data': {'orderId': n}} for n in (1, 2)]},
           'fills': fills, 'before': before, 'after': after,
           'cash_change': 137.0, 'observed_extra_cost': 0.0}
    # Live risk profile (bot_config.json, 22 Oct 2026).
    cfg = {'costs': {'fee_per_share': 0}, 'baskets': [], 'kill_switch': '/nonexistent-stop',
           'risk_limits': {'committed': 100000, 'reserve': 2000, 'basket': 5000, 'clip': 5000,
                           'race': 3000, 'directional': 60000, 'factor': 60000}}
    api = Mock()
    api.state.return_value = copy.deepcopy(after)

    def get(path):
        oid = int(path.split('/')[2].split('?')[0]); leg = legs[oid - 1]
        if '/fills' in path:
            return copy.deepcopy(fills[str(oid)])
        return dict(leg, open=False, priceLimit=leg['price'])
    api.get.side_effect = get
    api.request.side_effect = AssertionError('Recovery must not POST or DELETE')
    return job, cfg, api


class ExposureRecoveryTests(unittest.TestCase):
    def recover(self, mutate=None, risk=(900, {}, 900), live=True, action='sell'):
        job, cfg, api = fixture(action)
        if mutate:
            mutate(job, api)
        journal = Mock()
        with patch('trading_bot.portfolio_risk', return_value=risk):
            result = Runner(api, journal, cfg, live=live).reconcile_halt(job)
        api.request.assert_not_called()
        return result, journal

    def test_clean_exit_is_marked_done_without_order_writes(self):
        result, journal = self.recover()
        self.assertEqual(result['orders_submitted'], 0)
        journal.save.assert_called_once()
        self.assertEqual(journal.save.call_args[0][1], 'done')

    def test_clean_entry_also_recovers(self):
        result, _ = self.recover(action='buy')
        self.assertEqual(result['orders_submitted'], 0)

    def test_dry_run_does_not_persist(self):
        _, journal = self.recover(live=False)
        journal.save.assert_not_called()

    def test_real_exposure_breach_still_halts(self):
        with self.assertRaises(Halt):
            self.recover(risk=(900, {}, 60979.8))

    def test_account_change_halts(self):
        def m(job, api):
            api.state.return_value = dict(job['after'], cash=2000.0)
        with self.assertRaises(Halt):
            self.recover(m)

    def test_open_order_or_changed_fills_halt(self):
        def open_order(job, api):
            api.state.return_value = dict(job['after'], orders=[{'priceLimit': .5, 'quantity': 1}])
        def partial(job, api):
            job['fills']['1']['data'][0]['quantity'] = -100
        for m in (open_order, partial):
            with self.subTest(m.__name__), self.assertRaises(Halt):
                self.recover(m)

    def test_repairs_or_mixed_actions_not_eligible(self):
        def repaired(job, api):
            job['repairs'] = [{'request': {}, 'fills': {}}]
        def mixed(job, api):
            job['request']['legs'][1]['action'] = 'buy'
        for m in (repaired, mixed):
            with self.subTest(m.__name__), self.assertRaises(Halt):
                self.recover(m)


class DropUnusedAutoTests(unittest.TestCase):
    def test_selected_basket_survives_later_cycle(self):
        cfg = {'baskets': [{'name': 'Fixed', 'legs': []},
                           {'name': 'SC', 'legs': [], '_prescreened': True},
                           {'name': 'Other', 'legs': [], '_prescreened': True}]}
        runner = Runner(Mock(), Mock(), cfg)
        runner.drop_unused_auto('SC')
        self.assertEqual([b['name'] for b in cfg['baskets']], ['Fixed', 'SC'])
        runner.drop_unused_auto('Texas Senate')  # next cycle selects a different basket
        self.assertEqual([b['name'] for b in cfg['baskets']], ['Fixed', 'SC'])


if __name__ == '__main__':
    unittest.main()
