import tempfile
import unittest
from pathlib import Path
from hosted_runner import DurableJournal, record_sweep
from trading_bot import Halt


class Remote:
    def __init__(self, jobs=None):
        self.data = {'version': 1, 'enabled': True, 'jobs': jobs or [], 'events': []}
        self.writes = []
        self.fail = False

    def write(self, data):
        if self.fail:
            raise Halt('Checkpoint unavailable')
        self.data = data
        self.writes.append(data)


class PersistenceTests(unittest.TestCase):
    def test_unknown_request_survives_fresh_runner(self):
        remote = Remote()
        with tempfile.TemporaryDirectory() as directory:
            first = DurableJournal(str(Path(directory) / 'first.sqlite'), remote)
            job = {'id': 'stable-key', 'request': {'idempotencyKey': 'stable-key', 'legs': [1]}}
            first.save(job, 'outcome_unknown')
            second = DurableJournal(str(Path(directory) / 'second.sqlite'), remote)
            self.assertEqual(second.pending()[0]['request'], job['request'])
            self.assertEqual(second.pending()[0]['status'], 'outcome_unknown')

    def test_checkpoint_failure_blocks_caller(self):
        remote = Remote()
        with tempfile.TemporaryDirectory() as directory:
            journal = DurableJournal(str(Path(directory) / 'journal.sqlite'), remote)
            remote.fail = True
            with self.assertRaises(Halt):
                journal.save({'id': 'no-send', 'request': {}}, 'prepared')
            self.assertEqual(remote.writes, [])

    def test_pending_jobs_are_retained_beyond_history_limit(self):
        jobs = [{'id': str(i), 'status': 'done', 'request': {}} for i in range(40)]
        jobs.append({'id': 'unresolved', 'status': 'accepted', 'request': {}})
        remote = Remote(jobs)
        with tempfile.TemporaryDirectory() as directory:
            journal = DurableJournal(str(Path(directory) / 'journal.sqlite'), remote)
            journal.checkpoint()
            self.assertEqual(len(remote.data['jobs']), 31)
            self.assertEqual(remote.data['jobs'][-1]['id'], 'unresolved')


class HeartbeatTests(unittest.TestCase):
    def test_dry_run_cannot_claim_successful_live_management(self):
        remote = Remote()
        record_sweep(remote, 'dry-run', 'success', 12)
        self.assertNotIn('last_successful_live_sweep', remote.data)
        self.assertEqual(remote.data['last_successful_sweep']['mode'], 'dry-run')

    def test_failure_preserves_previous_successful_live_timestamp(self):
        remote = Remote()
        record_sweep(remote, 'live', 'success', 12)
        previous = dict(remote.data['last_successful_live_sweep'])
        record_sweep(remote, 'live', 'failure', 12)
        self.assertEqual(remote.data['last_successful_live_sweep'], previous)
        self.assertEqual(remote.data['last_run']['status'], 'failure')
