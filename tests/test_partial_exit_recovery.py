import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from trading_bot import Halt, Journal, Runner, TID, exit_repair_preflight


def fixture():
    legs = [{'exchangeId': i, 'side': 'no', 'action': 'sell', 'quantity': 100,
             'price': .5, 'tournamentId': TID} for i in ('11', '22')]
    plan = {'kind': 'basket_exit', 'action': 'sell', 'name': 'test', 'quantity': 100,
            'legs': [{'exchange_id': i} for i in ('11', '22')], 'prices': [.5, .5]}
    fills = {str(n): {'orderId': n, 'exchangeId': leg['exchangeId'], 'tournamentId': TID,
                     'coverage': {'complete': True}, 'pagination': {'hasMore': False},
                     'data': [{'id': n, 'quantity': -q, 'price': .5}]}
             for n, leg, q in zip((1, 2), legs, (100, 20))}
    held = {'exchangeId': '500', 'quantity': -10000, 'costBasis': 7000}
    before = {'cash': 1000, 'positions': [held] + [
        {'exchangeId': i, 'quantity': -100, 'costBasis': 50} for i in ('11', '22')], 'orders': []}
    after = {'at': 'fresh', 'cash': 1060, 'positions': [held,
             {'exchangeId': '22', 'quantity': -80, 'costBasis': 40}], 'orders': []}
    job = {'id': 'original', 'status': 'halted', 'before': before, 'repairs': [],
           'plan': plan, 'request': {'legs': legs}, 'fills': fills,
           'response': {'results': [{'index': n-1, 'data': {'orderId': n}} for n in (1, 2)]}}
    cfg = {'baskets': [{'name': 'test', 'legs': plan['legs']}],
           'kill_switch': '/nonexistent-stop',
           'costs': {'fee_per_share': 0, 'verified': True, 'source': 'test fee schedule'},
           'risk_limits': {'committed': 100000, 'reserve': 1000, 'clip': 5000,
                           'race': 5000, 'directional': 6000, 'factor': 6000}}
    api = Mock()
    api.state.return_value = copy.deepcopy(after)
    def get(path):
        oid = int(path.split('/')[2].split('?')[0])
        if '/fills' in path:
            return copy.deepcopy(fills[str(oid)])
        return dict(legs[oid-1] if oid < 3 else legs[1], open=False, priceLimit=.5)
    api.get.side_effect = get
    return job, cfg, api, fills


class PartialExitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.journal = Journal(str(Path(tmp.name) / 'journal.sqlite'))
        self.addCleanup(self.journal.db.close)
        self.contracts = patch('trading_bot.validate_contracts', return_value=[])
        self.contracts.start(); self.addCleanup(self.contracts.stop)

    def test_readonly_stages_but_never_trades_or_marks_done(self):
        j, c, api, _ = fixture()
        r = Runner(api, self.journal, c, live=True).reconcile_halt(j)
        self.assertTrue(r['recovery_ready'])
        self.assertEqual(j['status'], 'exit_recovery_ready')
        self.assertEqual(len(self.journal.pending()), 1)
        api.request.assert_not_called()

    def test_changed_cash_positions_fills_open_orders_or_live_order_blocks(self):
        for change in ('cash', 'positions', 'fills', 'orders', 'live'):
            with self.subTest(change=change):
                j, c, api, fills = fixture()
                if change == 'cash': api.state.return_value['cash'] += .02
                if change == 'positions': api.state.return_value['positions'][0]['quantity'] -= 1
                if change == 'fills': fills['2']['data'][0]['quantity'] = -21
                if change == 'orders': api.state.return_value['orders'] = [{'id': 5}]
                if change == 'live': api.get.side_effect = lambda path: {'open': True}
                with self.assertRaises(Halt):
                    Runner(api, self.journal, c, live=True).reconcile_halt(j)
                api.request.assert_not_called()

    def test_complete_only_remaining_leg_original_limit_preserves_other_positions(self):
        j, c, api, fills = fixture()
        runner = Runner(api, self.journal, c, live=True)
        runner.reconcile_halt(j)
        def place(method, path, request):
            self.assertEqual((method, path), ('POST', '/orders'))
            self.assertEqual((request['exchangeId'], request['quantity'], request['price']), ('22', 80, .5))
            api.state.return_value['cash'] = 1100
            api.state.return_value['positions'] = api.state.return_value['positions'][:1]
            fills['3'] = dict(fills['2'], orderId=3,
                              data=[{'id': 3, 'quantity': -80, 'price': .5}])
            return {'orderId': 3}
        api.request.side_effect = place
        with patch('trading_bot.price_for', return_value=(.5, .5)):
            result = runner.finish(j)
        self.assertEqual(result['completed'], 'original')
        self.assertEqual(j['status'], 'done')
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(api.request.call_count, 1)
        self.assertEqual(j['cash_change'], 100)

    def test_worse_depth_waits_without_new_request_or_key(self):
        j, c, api, _ = fixture()
        runner = Runner(api, self.journal, c, live=True)
        runner.reconcile_halt(j)
        with patch('trading_bot.price_for', return_value=(.495, .495)), self.assertRaises(Halt):
            runner.finish(j)
        self.assertEqual(j['status'], 'exit_recovery_ready')
        self.assertEqual(j['repairs'], [])
        api.request.assert_not_called()

    def test_unknown_recovery_write_never_replays_original_or_creates_new_key(self):
        j, c, api, _ = fixture()
        runner = Runner(api, self.journal, c, live=True)
        runner.reconcile_halt(j)
        api.request.side_effect = Halt('lost response')
        with patch('trading_bot.price_for', return_value=(.5, .5)), self.assertRaises(Halt):
            runner.finish(j)
        original = copy.deepcopy(j['repairs'][0]['request'])
        with self.assertRaises(Halt): runner.finish(j)
        self.assertEqual(api.request.call_count, 1)
        self.assertEqual(j['repairs'][0]['request'], original)

    def test_inherited_exposure_never_permits_new_entry(self):
        j, c, api, _ = fixture()
        with self.assertRaisesRegex(Halt, 'exits only'):
            Runner(api, self.journal, c, live=True).submit(
                {'kind': 'value_entry'}, j['before'])
        api.request.assert_not_called()

    def test_existing_breach_can_reduce_but_not_grow(self):
        j, c, api, _ = fixture()
        j['after'] = copy.deepcopy(j['before'])
        j['after']['positions'][0]['quantity'] = -9999
        j['after']['positions'][0]['costBasis'] = 6999
        r = Runner(api, self.journal, c)
        r.check_post_fill_risk(j)
        j['after']['positions'][0]['quantity'] = -10001
        with self.assertRaises(Halt): r.check_post_fill_risk(j)


class ExitPreflightTests(unittest.TestCase):
    def test_affordable_expensive_missing_depth_fees_and_tighter_budget(self):
        for price, fee, budget, blocked in [(.51, 0, 10, False), (.56, 0, 10, True),
                                            (.51, 0, 1, True), (.5, .06, 10, True)]:
            j, c, api, _ = fixture()
            c['costs']['fee_per_share'] = fee; c['repair_loss_budget'] = budget
            with self.subTest(price=price, fee=fee, budget=budget):
                with patch('trading_bot.price_for', return_value=(price, price)):
                    if blocked:
                        with self.assertRaises(Halt): exit_repair_preflight(api, c, j['plan'])
                    else: exit_repair_preflight(api, c, j['plan'])
                api.request.assert_not_called()
        j, c, api, _ = fixture()
        with patch('trading_bot.price_for', side_effect=Halt('Insufficient depth')), self.assertRaises(Halt):
            exit_repair_preflight(api, c, j['plan'])


if __name__ == '__main__': unittest.main()
