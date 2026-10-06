import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from trading_bot import (END, Halt, Journal, Runner, basket_plan, now,
                         portfolio_risk, tick, validate_contracts)


def config():
    return {'costs': {'verified': True, 'source': 'test fee schedule', 'fee_per_share': 0},
            'kill_switch': '/nonexistent-test-stop', 'repair_loss_budget': 10,
            'baskets': [{'name': 'test', 'clip': 100, 'legs': [
                {'market_id': '1', 'exchange_id': '11', 'party': 'Democratic Party'},
                {'market_id': '2', 'exchange_id': '22', 'party': 'Republican Party'}]}]}


class FakeAPI:
    def __init__(self, fill=(100, 100)):
        self.fill = fill
        self.cash = 100000
        self.quantities = {'11': 0, '22': 0}
        self.calls = []
        self.orders = {}
        self.responses = {}
        self.lose_response_once = False
        self.repair_partial = False

    def state(self):
        return {'at': now().isoformat(), 'cash': self.cash,
                'positions': [{'exchangeId': i, 'quantity': q, 'costBasis': abs(q) * .49}
                              for i, q in self.quantities.items() if q],
                'orders': [], 'recent_fills': [], 'pnl': {}}

    def book(self, i):
        return {'exchangeId': i, 'asOf': {'at': now().isoformat()}, '_received_at': now().isoformat(),
                'bids': [{'price': .52, 'quantity': 10000}], 'asks': [{'price': .53, 'quantity': 10000}]}

    def get(self, path):
        if '/nodes' in path:
            i = path.split('/')[2]
            return {'contexts': [{'tournament': {'id': 'bda92870-621e-47b0-bc3c-3602c5c26f55'}}],
                    'root': {'contract_type': 'Election Outcome', 'settlement_date': END.isoformat(),
                             'contract_details': {'raceId': 'r', 'stageId': 's', 'electionDate': '2026-11-03',
                              'raceStage': 'General', 'resolutionType': 'Party Winner',
                              'winnerName': 'Democratic Party' if i == '1' else 'Republican Party'}}}
        if '/markets/' in path:
            i = path.split('/')[-1]
            return {'id': i, 'title': 'test', 'status': 'open', 'settlementDate': END.isoformat(),
                    'exchanges': [{'id': '11' if i == '1' else '22'}]}
        if path.startswith('/orders/'):
            i = int(path.split('/')[2])
            if '/fills' in path:
                return {'data': [{'quantity': self.orders[i]['quantityFilled']}], 'pagination': {'hasMore': False}}
            return self.orders[i]
        raise AssertionError(path)

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == 'DELETE':
            self.orders[int(path.split('/')[-1])]['open'] = False
            return {}
        key = payload['idempotencyKey']
        if key in self.responses:
            return self.responses[key]
        legs = payload['legs'] if path.endswith('multi-leg') else [payload]
        results = []
        for idx, leg in enumerate(legs):
            if path.endswith('multi-leg'):
                q = min(leg['quantity'], self.fill[idx])
            else:
                q = 0 if self.repair_partial else leg['quantity']
            sign = -1 if leg['action'] == 'buy' else 1
            self.quantities[leg['exchangeId']] += sign * q
            self.cash += sign * q * leg['price']
            oid = len(self.orders) + 1
            self.orders[oid] = {'open': q < leg['quantity'], 'quantityFilled': q}
            results.append({'index': idx, 'data': {'orderId': oid, 'quantityTraded': q}})
        response = {'results': results} if path.endswith('multi-leg') else results[0]['data']
        self.responses[key] = response
        if self.lose_response_once:
            self.lose_response_once = False
            raise Halt('simulated response loss AFTER accepted fills')
        return response


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(str(Path(self.tmp.name) / 'journal.sqlite'))
        self.addCleanup(self.journal.db.close)
        self.api, self.cfg = FakeAPI(), config()

    def plan(self):
        return basket_plan(self.api, self.api.state(), self.cfg, self.cfg['baskets'][0])

    def test_tick_rejects_market_offtick_nan(self):
        for p in [0, 1, .501, float('nan')]:
            with self.assertRaises(Halt): tick(p)
        self.assertEqual(tick(.505), .505)

    def test_entry_prices_use_NO_complements(self):
        p = self.plan()
        self.assertEqual(p['prices'], [.48, .48])
        self.assertAlmostEqual(p['robust_profit'], 3)

    def test_default_dry_run_never_writes(self):
        runner = Runner(self.api, self.journal, self.cfg)
        result = runner.submit(self.plan(), self.api.state())
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.journal.pending(), [])

    def test_live_complete_fills_reconciled(self):
        runner = Runner(self.api, self.journal, self.cfg, True)
        result = runner.submit(self.plan(), self.api.state())
        self.assertEqual(result['cash'], 99904)
        self.assertEqual(self.api.quantities, {'11': -100, '22': -100})
        self.assertEqual(self.journal.pending(), [])

    def test_partial_fill_unwinds_excess_not_original_hedge(self):
        self.api.quantities = {'11': -1000, '22': -1000}
        self.api.cash = 99000
        self.api.fill = (100, 30)
        runner = Runner(self.api, self.journal, self.cfg, True)
        result = runner.submit(self.plan(), self.api.state())
        self.assertEqual(self.api.quantities, {'11': -1030, '22': -1030})
        self.assertEqual(result['repairs'], 1)
        self.assertTrue(any(c[0] == 'DELETE' for c in self.api.calls))

    def test_unknown_outcome_reuses_identical_key_without_double_fill(self):
        self.api.lose_response_once = True
        runner = Runner(self.api, self.journal, self.cfg, True)
        with self.assertRaises(Halt): runner.submit(self.plan(), self.api.state())
        job = self.journal.pending()[0]
        runner.finish(job)
        writes = [c[2] for c in self.api.calls if c[1] == '/orders/multi-leg']
        self.assertEqual(writes[0], writes[1])
        self.assertEqual(self.api.quantities, {'11': -100, '22': -100})

    def test_failed_repair_halts_further_placements(self):
        self.api.fill = (100, 30)
        self.api.repair_partial = True
        runner = Runner(self.api, self.journal, self.cfg, True)
        with self.assertRaises(Halt): runner.submit(self.plan(), self.api.state())
        calls = len(self.api.calls)
        with self.assertRaises(Halt): runner.cycle()
        self.assertEqual(len(self.api.calls), calls)

    def test_missing_fee_verification_abstains(self):
        self.cfg['costs']['verified'] = False
        with self.assertRaises(Halt): self.plan()

    def test_cash_reserve_blocks_entry(self):
        self.api.cash = 75000
        self.assertIsNone(self.plan())

    def test_unmatched_existing_hedge_blocks_entry(self):
        self.api.quantities = {'11': -100, '22': -99}
        with self.assertRaises(Halt): portfolio_risk(self.api.state(), self.cfg)

    def test_contract_race_mismatch_blocks(self):
        original = self.api.get
        def get(path):
            d = original(path)
            if path == '/markets/2/nodes': d['root']['contract_details']['raceId'] = 'other'
            return d
        self.api.get = get
        with self.assertRaises(Halt): validate_contracts(self.api, self.cfg['baskets'][0])

    def test_deadline_prevents_writes(self):
        plan = self.plan()
        with patch('trading_bot.now', return_value=END):
            with self.assertRaises(Halt): Runner(self.api, self.journal, self.cfg, True).submit(plan, self.api.state())
        self.assertEqual(self.api.calls, [])


if __name__ == '__main__': unittest.main()
