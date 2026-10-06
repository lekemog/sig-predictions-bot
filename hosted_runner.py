"""Hourly GitHub runner with synchronous, private-repository order checkpoints."""
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from trading_bot import API, END, Halt, Journal, Runner, now


class RepositoryState:
    def __init__(self):
        repo = os.environ.get('GITHUB_REPOSITORY')
        if repo != 'lekemog/sig-predictions-bot':
            raise Halt('Unexpected repository')
        token = os.environ.get('GITHUB_TOKEN')
        if not token:
            raise Halt('Repository checkpoint token missing')
        self.url = f'https://api.github.com/repos/{repo}/contents/.bot/state.json'
        self.headers = {'Authorization': 'Bearer ' + token,
                        'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28',
                        'Content-Type': 'application/json'}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.opener = urllib.request.build_opener(NoRedirect)
        response = self.request('GET')
        self.sha = response['sha']
        self.data = json.loads(base64.b64decode(response['content']))
        if self.data.get('version') != 1 or not isinstance(self.data.get('jobs'), list):
            raise Halt('Missing or invalid durable checkpoint')

    def request(self, method, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url, data=body, headers=self.headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError):
            raise Halt('Durable checkpoint unavailable; trading halted') from None

    def write(self, data):
        encoded = base64.b64encode(json.dumps(data, allow_nan=False).encode()).decode()
        result = self.request('PUT', {'message': 'Checkpoint SIG execution state',
                                     'content': encoded, 'sha': self.sha, 'branch': 'main'})
        self.sha = result['content']['sha']
        self.data = data


class DurableJournal(Journal):
    def __init__(self, path, remote):
        self.remote = remote
        super().__init__(path)
        for job in remote.data['jobs']:
            Journal.save(self, job, job['status'])
        for event in remote.data.get('events', []):
            self.db.execute('INSERT INTO events VALUES (?,?,?)',
                            (event['at'], event['kind'], json.dumps(event['body'])))
        self.db.commit()

    def checkpoint(self):
        jobs = [json.loads(r[0]) for r in self.db.execute('SELECT body FROM jobs ORDER BY updated')]
        pending = [j for j in jobs if j['status'] not in ('done', 'dry_run')]
        receipt_fields = ('id', 'status', 'request', 'plan', 'fills', 'created_at',
                          'cash_change', 'observed_extra_cost')
        complete = [{k: v for k, v in j.items() if k in receipt_fields}
                    for j in jobs if j['status'] in ('done', 'dry_run')][-30:]
        rows = list(self.db.execute('SELECT at,kind,body FROM events ORDER BY rowid DESC LIMIT 50'))
        events = [{'at': a, 'kind': k, 'body': json.loads(b)} for a, k, b in reversed(rows)]
        data = dict(self.remote.data, jobs=complete + pending, events=events, updated_at=now().isoformat())
        self.remote.write(data)

    def save(self, job, status):
        super().save(job, status)
        # Failure aborts before the following SIG write. Persisted unknown outcomes
        # retain their exact request and idempotency key for the next invocation.
        self.checkpoint()


class HostedRunner(Runner):
    def finish(self, job):
        if 'response' not in job and job['status'] == 'prepared':
            self.api.state()
        return super().finish(job)


def record_sweep(remote, mode, status, basket_count):
    marker = {'at': now().isoformat(), 'mode': mode, 'status': status,
              'basket_count': basket_count}
    remote.data['last_run'] = marker
    if status == 'success':
        remote.data['last_successful_sweep'] = marker
        if mode == 'live':
            remote.data['last_successful_live_sweep'] = marker


def main():
    if now() >= END:
        print('Trading cutoff reached; no API orders or further reviews.')
        return
    remote = RepositoryState()
    if remote.data.get('enabled') is not True:
        print('Repository kill switch is disabled; no orders.')
        return
    config = json.loads(Path('bot_config.json').read_text())
    config['kill_switch'] = 'private/STOP'
    config['costs'] = {
        'verified': True, 'fee_per_share': 0,
        'source': 'SIG markets-and-trading documentation: no participant trading fees; '
                  'complete prior account ledger reconciled without extra fees. '
                  'Every new execution must independently reconcile fills and cash.'}
    # Check every existing basket, but limit additions to one fresh clip per run.
    config.pop('review_names', None)
    journal = DurableJournal('private/hosted.sqlite', remote)
    runner = HostedRunner(API(os.environ.get('SIG_API_KEY')), journal, config,
                    live=os.environ.get('SIG_MODE', 'dry-run') == 'live')
    try:
        result = runner.cycle()
        record_sweep(remote, 'live' if runner.live else 'dry-run', 'success', len(config['baskets']))
        journal.event('hosted_result', result)
        print(json.dumps(result, allow_nan=False), flush=True)
    except BaseException:
        # Fail closed. An unresolved job remains in the remote checkpoint.
        record_sweep(remote, 'live' if runner.live else 'dry-run', 'failure', len(config['baskets']))
        journal.event('hosted_failure', {'at': now().isoformat(), 'message': 'Run failed; review logs and reconcile'})
        raise
    finally:
        journal.checkpoint()


if __name__ == '__main__':
    main()
