import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_bot import Halt, Journal, Runner, basket_plan, directional_plan
from tests.test_trading_bot import FakeAPI, config


class EntryPauseTests(unittest.TestCase):
    def setUp(self):
        self.api, self.cfg = FakeAPI(), config()
        self.cfg['entries_paused'] = True
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(str(Path(self.tmp.name) / 'journal.sqlite'))
        self.addCleanup(self.journal.db.close)
        self.runner = Runner(self.api, self.journal, self.cfg, live=True)

    def test_entry_blocked_with_audit_reason_and_no_intent(self):
        reasons = []
        self.assertIsNone(basket_plan(self.api, self.api.state(), self.cfg,
                                     self.cfg['baskets'][0], reasons.append))
        self.assertEqual(reasons, ['New entries paused for drawdown review'])
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(self.api.calls, [])

    def test_matched_exit_remains_available(self):
        self.api.quantities = {'11': -100, '22': -100}
        with patch.object(self.api, 'book', side_effect=lambda i: {
                'exchangeId': i, '_received_at': self.api.state()['at'],
                'bids': [{'price': .48, 'quantity': 10000}],
                'asks': [{'price': .49, 'quantity': 10000}]}):
            plan = basket_plan(self.api, self.api.state(), self.cfg, self.cfg['baskets'][0])
            self.assertEqual(plan['kind'], 'basket_exit')
            self.assertGreaterEqual(plan['net_limit_proceeds'], plan['value_floor'])

    def test_directional_entry_blocked_before_reads(self):
        self.assertIsNone(directional_plan(self.api, {}, self.cfg, {}))
        self.assertEqual(self.api.calls, [])

    def test_direct_submission_blocked_before_journal_or_post(self):
        for kind in ('basket_entry', 'directional_entry', 'unknown'):
            with self.assertRaisesRegex(Halt, 'entries paused'):
                self.runner.submit({'kind': kind}, self.api.state())
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(self.api.calls, [])

    def test_pending_reconciliation_path_is_preserved(self):
        job = {'id': 'existing', 'status': 'accepted'}
        self.journal.save(job, 'accepted')
        with patch.object(self.runner, 'finish', return_value={'reconciled': True}) as finish:
            self.assertEqual(self.runner.cycle(), [{'reconciled': True}])
            finish.assert_called_once_with(job)

    def test_absent_flag_preserves_normal_entry(self):
        del self.cfg['entries_paused']
        self.assertEqual(basket_plan(self.api, self.api.state(), self.cfg,
                                    self.cfg['baskets'][0])['kind'], 'basket_entry')


if __name__ == '__main__':
    unittest.main()
