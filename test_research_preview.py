import hashlib
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from research_preview import digest, preview
from trading_bot import now


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.at = now()
        stamp = self.at.isoformat()
        self.config = dict(baskets=[], risk_limits=dict(committed=75000, reserve=25000),
                           kill_switch='private/STOP',
                           costs=dict(verified=True, source='test only', fee_per_share=0))
        root = {'test': 'settlement definition'}
        fingerprint = hashlib.sha256(json.dumps(root, sort_keys=True).encode()).hexdigest()
        case = dict(contract=dict(market_id='1', exchange_id='2', question='Test race?',
            settlement_rule='YES for winner, cancellation refunds', rule_url='https://official.example/rule',
            settles_at=(self.at + timedelta(days=10)).isoformat(), verified=True, nodes_sha256=fingerprint),
            forecast=dict(kind='probability_range', yes_probability=[.7, .8],
                validation_reference='TEST calibration only', assumptions=['test'],
                evidence=[dict(url='https://official.example/release', claim='Test evidence',
                               verified=True, independence_group='one', published_at=stamp)]),
            trade=dict(side='yes', quantity=100, minimum_edge=.05, objective='settlement',
                       thesis='test', falsification='test'))
        self.snapshot = dict(cases=[case], state=dict(at=stamp, cash=100000, positions=[], orders=[]),
            checkpoint=dict(enabled=True, updated_at=stamp, jobs=[]),
            markets={'1':dict(status='open', exchanges=[dict(id='2')])},
            nodes={'1':dict(root=root)},
            books={'2':dict(exchangeId='2', marketId='1', asOf=dict(at=stamp), _received_at=stamp,
                            bids=[dict(price=.59, quantity=1000)], asks=[dict(price=.60, quantity=1000)])},
            manifest=dict(reviewed=True, config_sha256=digest(self.config),
                          expires_at=(self.at+timedelta(hours=1)).isoformat(), cases={}))
        self.bind()

    def bind(self):
        case = self.snapshot['cases'][0]
        self.snapshot['manifest']['cases'] = {digest(case):dict(
            calibration_reviewed=True, settlement_reviewed=True,
            calibration_reference=case['forecast']['validation_reference'],
            forecast_sha256=digest(case['forecast']), nodes_sha256=case['contract']['nodes_sha256'])}

    def result(self):
        return preview(self.snapshot, self.config)

    def rejected(self):
        self.assertEqual(self.result()['candidates'], [])

    def test_valid_preview_cannot_submit_or_enable_live(self):
        result = self.result()
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['orders_submitted'], 0)
        self.assertFalse(result['live_enabled'])
        self.assertFalse(result['candidates'][0]['execution_authorized'])

    def test_pending_unknown_outcome_blocks(self):
        self.snapshot['checkpoint']['jobs'] = [dict(status='submitted')]
        self.rejected()

    def test_open_orders_block(self):
        self.snapshot['state']['orders'] = [dict(quantity=1)]
        self.rejected()

    def test_changed_forecast_invalidates_review(self):
        self.snapshot['cases'][0]['forecast']['yes_probability'] = [.9, .95]
        self.rejected()

    def test_unreviewed_calibration_blocks(self):
        next(iter(self.snapshot['manifest']['cases'].values()))['calibration_reviewed'] = False
        self.rejected()

    def test_changed_settlement_blocks(self):
        self.snapshot['nodes']['1']['root'] = {'changed': True}
        self.rejected()

    def test_future_and_stale_book_block(self):
        for seconds in (-61, 10):
            self.snapshot['books']['2']['asOf']['at'] = (self.at+timedelta(seconds=seconds)).isoformat()
            self.rejected()

    def test_depth_and_price_breaks_reject(self):
        for level in (dict(price=.60, quantity=1), dict(price=.68, quantity=1000)):
            self.snapshot['books']['2']['asks'] = [level]
            self.rejected()

    def test_existing_hedge_cannot_be_unmatched(self):
        self.config['baskets'] = [dict(name='test', legs=[dict(exchange_id='2'),dict(exchange_id='3')])]
        self.snapshot['manifest']['config_sha256'] = digest(self.config)
        self.rejected()

    def test_cash_reserve_and_directional_cap(self):
        self.snapshot['state']['cash'] = 25001
        self.rejected()
        self.snapshot['state']['cash'] = 100000
        self.snapshot['state']['positions'] = [dict(exchangeId='9', quantity=1, costBasis=490)]
        self.rejected()

    def test_final_clock_rechecks_freshness(self):
        ticks = iter([self.at, self.at, self.at, self.at+timedelta(seconds=61)])
        self.assertEqual(preview(self.snapshot, self.config, lambda: next(ticks))['candidates'], [])

    def test_stop_and_expiry(self):
        with patch('research_preview.Path.exists', return_value=True):
            self.rejected()
        self.snapshot['manifest']['expires_at'] = (self.at-timedelta(seconds=1)).isoformat()
        self.rejected()


if __name__ == '__main__':
    unittest.main()
