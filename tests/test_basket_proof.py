import copy
import hashlib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_trading_bot import FakeAPI, config
from trading_bot import (Halt, Journal, Runner, basket_plan, contract_proof_hash,
                         entry_proof_reason, max_simultaneous_yes, now,
                         validate_contracts)


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.api, self.cfg = FakeAPI(), config()
        self.basket = self.cfg['baskets'][0]
        self.basket['name'] = 'Nebraska Senate'
        self.contracts = validate_contracts(self.api, self.basket)
        self.at = now()
        text = 'Synthetic TEST rules: distinct nominees; other winners match zero.'
        self.proof = {
            'schema_version': 1,
            'reviewer': 'synthetic-test-fixture',
            'contract_hash': contract_proof_hash(self.basket, self.contracts),
            'reviewed_at': self.at.isoformat(),
            'expires_at': (self.at + timedelta(hours=72)).isoformat(),
            'enumeration_complete': True,
            'certified_candidates': ['person_d', 'person_r', 'person_i1', 'person_i2'],
            'candidate_rows': {'person_d': ['11'], 'person_r': ['22'],
                               'person_i1': [], 'person_i2': [], 'write_in_other': []},
            'rules_review': {'complete': True, 'other_matches_zero': True,
                             'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                             'source_url': 'https://example.invalid/test-rules'},
            'sources': [{'tier': 'A', 'fetched': True, 'official': True,
                         'kind': 'certification', 'url': 'https://example.invalid/test-only',
                         'sha256': 'a' * 64, 'source_date': '2026-10-11',
                         'fetched_at': self.at.isoformat(),
                         'candidate_ids': ['person_d', 'person_r', 'person_i1', 'person_i2']}]}
        self.basket['entry_proof'] = self.proof

    def reason(self):
        return entry_proof_reason(self.basket, self.contracts, self.at)

    def test_valid_complete_proof_allows_entry(self):
        self.assertIsNone(self.reason())
        self.assertEqual(basket_plan(self.api, self.api.state(), self.cfg,
                                     self.basket)['kind'], 'basket_entry')

    def test_one_person_on_two_labels_blocks_entry(self):
        self.proof['candidate_rows']['person_d'] = ['11', '22']
        self.assertEqual(max_simultaneous_yes(self.proof['candidate_rows'], ['11', '22']), 2)
        self.assertIn('more than one', self.reason())
        self.assertIsNone(basket_plan(self.api, self.api.state(), self.cfg, self.basket))

    def test_multi_candidate_category_checks_each_person(self):
        rows = {'a': ['11'], 'b': ['22'], 'i1': ['33'], 'i2': ['33'], 'write_in_other': []}
        self.assertEqual(max_simultaneous_yes(rows, ['11', '22', '33']), 1)
        rows['i2'].append('11')
        self.assertEqual(max_simultaneous_yes(rows, ['11', '22', '33']), 2)

    def test_missing_and_incomplete_enumeration_block(self):
        del self.proof['candidate_rows']['person_i2']
        self.assertIn('incomplete', self.reason())
        self.proof['enumeration_complete'] = False
        self.assertIsNotNone(self.reason())

    def test_stale_future_and_excessive_lifetime_block(self):
        for reviewed, expires in [(-73, -1), (1, 72), (0, 73)]:
            with self.subTest(reviewed=reviewed):
                self.proof['reviewed_at'] = (self.at + timedelta(hours=reviewed)).isoformat()
                self.proof['expires_at'] = (self.at + timedelta(hours=expires)).isoformat()
                self.assertIn('lifetime', self.reason())

    def test_rule_or_leg_change_invalidates_fingerprint(self):
        changed = copy.deepcopy(self.contracts)
        changed[0]['contract']['contract_details']['winnerName'] = 'Changed'
        self.assertIsNotNone(entry_proof_reason(self.basket, changed, self.at))
        self.basket['legs'][0]['exchange_id'] = '99'
        self.assertIsNotNone(self.reason())

    def test_b_source_and_missing_rules_and_bad_artifact_block(self):
        original = copy.deepcopy(self.proof)
        for change in ('tier', 'rules', 'digest', 'coverage', 'stale_fetch'):
            with self.subTest(change=change):
                self.proof = copy.deepcopy(original)
                self.basket['entry_proof'] = self.proof
                if change == 'tier': self.proof['sources'][0]['tier'] = 'B'
                if change == 'rules': self.proof['rules_review']['complete'] = False
                if change == 'digest': self.proof['sources'][0]['sha256'] = 'z' * 64
                if change == 'coverage': self.proof['sources'][0]['candidate_ids'].pop()
                if change == 'stale_fetch':
                    self.proof['sources'][0]['fetched_at'] = (self.at - timedelta(hours=73)).isoformat()
                self.assertIsNotNone(self.reason())

    def test_missing_other_row_unknown_exchange_and_malformed_block(self):
        for rows in ({'a': ['11']}, {'a': ['99'], 'write_in_other': []},
                     {'a': ['11', '11'], 'write_in_other': []},
                     {'a': ['11'], 'write_in_other': ['22']}):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                max_simultaneous_yes(rows, ['11', '22'])
        self.basket['entry_proof'] = {'schema_version': 1}
        self.assertIsNotNone(self.reason())

    def test_all_five_quarantined_without_proof_and_rename_cannot_bypass(self):
        del self.basket['entry_proof']
        for name in ('Nebraska Senate', 'Montana Senate', 'Kansas Senate',
                     'PA Governor', 'Alaska Governor'):
            self.basket['name'] = name
            self.assertIsNotNone(self.reason())
        self.basket['name'] = 'renamed'
        self.basket['legs'][0]['exchange_id'] = '968'
        self.assertIsNotNone(self.reason())

    def test_missing_proof_leaves_profitable_matched_exit_available(self):
        del self.basket['entry_proof']
        self.api.quantities = {'11': -100, '22': -100}
        book = self.api.book
        def profitable(i):
            b = book(i)
            b['asks'] = [{'price': .48, 'quantity': 10000}]
            return b
        self.api.book = profitable
        plan = basket_plan(self.api, self.api.state(), self.cfg, self.basket)
        self.assertEqual(plan['kind'], 'basket_exit')
        self.assertEqual(plan['quantity'], 100)

    def test_submission_rechecks_expiry_before_journal_or_write(self):
        plan = basket_plan(self.api, self.api.state(), self.cfg, self.basket)
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(str(Path(directory) / 'test.sqlite'))
            try:
                runner = Runner(self.api, journal, self.cfg, live=True)
                with patch('trading_bot.now', return_value=self.at + timedelta(hours=73)):
                    with self.assertRaises(Halt): runner.submit(plan, self.api.state())
                self.assertEqual(self.api.calls, [])
                self.assertEqual(journal.pending(), [])
            finally:
                journal.db.close()

    def test_missing_proof_does_not_halt_held_screening(self):
        del self.basket['entry_proof']
        self.api.quantities = {'11': -100, '22': -100}
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(str(Path(directory) / 'test.sqlite'))
            try:
                Runner(self.api, journal, self.cfg, live=False).cycle()
                self.assertEqual(self.api.calls, [])
                self.assertEqual(journal.pending(), [])
            finally:
                journal.db.close()


if __name__ == '__main__':
    unittest.main()
