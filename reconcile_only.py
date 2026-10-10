"""Read-only SIG maintenance: verify the legacy cent-boundary halt and checkpoint it."""
import json
import os
from pathlib import Path
from hosted_runner import DurableJournal, RepositoryState, record_sweep
from trading_bot import API, Halt, Runner


class ReadOnlyAPI(API):
    def request(self, method, path, payload=None):
        if method != 'GET' or payload is not None:
            raise Halt('Read-only reconciliation prohibits order writes')
        return super().request(method, path, payload)


def reconcile_pending(api, journal, config):
    runner = Runner(api, journal, config, live=True)
    # Never enter cycle(), submit(), finish() or repair(). This routine verifies
    # closed original fills and writes only repository checkpoint metadata.
    return [runner.reconcile_rounding_halt(job) for job in journal.pending()]


def main():
    remote = RepositoryState()
    if remote.data.get('enabled') is not True:
        raise Halt('Durable kill switch disabled')
    journal = DurableJournal('private/reconciliation.sqlite', remote)
    config = json.loads(Path('bot_config.json').read_text())
    results = reconcile_pending(ReadOnlyAPI(os.environ.get('SIG_API_KEY')), journal, config)
    # A reconciliation receipt is never a successful live sweep.
    record_sweep(remote, 'reconcile-only', 'incomplete', 0)
    journal.event('readonly_checkpoint_recovery', results)
    journal.checkpoint()
    print(json.dumps({'mode': 'reconcile-only', 'orders_submitted': 0, 'results': results}), flush=True)


if __name__ == '__main__':
    main()
