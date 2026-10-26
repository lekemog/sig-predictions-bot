"""Hourly GitHub runner with synchronous, private-repository order checkpoints."""
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import arb_engine
import value_engine
from trading_bot import API, END, Halt, Journal, Runner, arb_policy, now, value_policy
from research_runner import ReadOnlyAPI, collect
from market_brain import BrainStore
from dip_monitor import scan as scan_dips

# The workflow job is killed at 20 minutes. Leave room for setup, the final
# checkpoint and the read-only universe scan; never START a cycle (which may
# submit an order) that is not expected to finish inside this budget.
PROCESS_STARTED = time.monotonic()
RUN_BUDGET_SECONDS = 17 * 60
CYCLE_SAFETY_FACTOR = 1.25


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


def cycle_screening(journal, start_rowid, baskets):
    """Count only this invocation's verified baskets, never restored history."""
    expected = {b['name'] for b in baskets}
    rows = journal.db.execute(
        "SELECT body FROM events WHERE rowid > ? AND kind = 'screened'",
        (start_rowid,))
    screened = {json.loads(body).get('basket') for (body,) in rows} & expected
    complete = bool(expected) and screened == expected
    return ('success' if complete else 'incomplete'), len(screened)


AUTO_KEYS = ('name', 'clip', 'auto', 'legs')


def prepare_universe(config, data):
    """Managed auto baskets always join config; unheld D/R races rotate per sweep."""
    policy = arb_policy(config)
    if policy is None:
        return
    taken = {str(l['exchange_id']) for b in config['baskets'] for l in b['legs']}
    # Jobs are checkpointed BEFORE any order is sent, so they also recover auto
    # baskets after a crash between submission and remember_auto_baskets.
    from_jobs = [{'name': j['plan']['name'], 'clip': 1000, 'auto': True, 'legs': j['plan']['legs']}
                 for j in data.get('jobs', [])
                 if str((j.get('plan') or {}).get('kind', '')).startswith('basket')
                 and j.get('status') != 'dry_run' and j['plan'].get('legs')]
    for b in data.get('auto_baskets', []) + from_jobs:
        ids = {str(l['exchange_id']) for l in b['legs']}
        if not ids & taken:
            config['baskets'].append({k: b[k] for k in AUTO_KEYS if k in b})
            taken |= ids
    catalog = (data.get('last_universe_scan') or {}).get('catalog') or {}
    universe = arb_engine.discover(catalog, config['baskets'])
    slot = int(now().timestamp() // 600)
    chunk = arb_engine.rotation(universe, int(policy['universe_races_per_sweep']), slot)
    # Races where we hold a directional position are always rechecked so value
    # exits ("sell high") never wait for the rotation to come back round.
    held = {str(p['exchangeId']) for p in (data.get('latest_portfolio_review') or {}).get('positions', [])
            if p.get('quantity')}
    pinned = [b for b in universe if held & {l['exchange_id'] for l in b['legs']} and b not in chunk]
    config['_universe'] = pinned + chunk


def refresh_fair(config, data, fetch=value_engine.fetch_fair):
    """Outside-venue fair bands for this cycle; failure disables value trades only."""
    if value_policy(config) is None:
        return
    try:
        mapping = json.loads(Path('value_map.json').read_text())
        fair = fetch(mapping, at=now().isoformat())
    except (OSError, ValueError, KeyError, TypeError) as error:
        config['_fair'] = {}
        data['latest_fair'] = {'at': now().isoformat(), 'status': 'failed', 'reason': str(error)}
        return
    config['_fair'] = fair
    data['latest_fair'] = {'at': fair['at'], 'status': 'ok', 'legs': len(fair['legs']),
                           'kalshi_quotes': fair['kalshi_quotes'], 'poly_quotes': fair['poly_quotes']}


def remember_auto_baskets(config, data):
    """Never forget an auto basket once selected: held legs must stay managed."""
    known = {b['name']: b for b in data.get('auto_baskets', [])}
    for b in config['baskets']:
        if b.get('auto'):
            known.setdefault(b['name'], {k: b[k] for k in AUTO_KEYS if k in b})
    data['auto_baskets'] = list(known.values())


def run_cycles(runner, config, remote, clock=time.monotonic, started=None):
    """Repeat full fresh cycles while each one completes a live trade.

    GitHub drops most scheduled runs, so one trade per run starves the arb.
    Every cycle re-reads the account, re-validates contracts and rebuilds prices;
    nothing carries over except the persisted journal and remembered baskets.
    """
    policy = arb_policy(config)
    limit = int(policy['max_jobs_per_run']) if policy else 1
    results = []
    started = PROCESS_STARTED if started is None else started
    longest = 0.
    for index in range(limit):
        elapsed = clock() - started
        if index and elapsed + longest * CYCLE_SAFETY_FACTOR > RUN_BUDGET_SECONDS:
            print(json.dumps({'event': 'cycle_budget_stop', 'cycles': index,
                              'elapsed_seconds': round(elapsed, 1),
                              'longest_cycle_seconds': round(longest, 1)}), flush=True)
            break
        cycle_start = clock()
        refresh_fair(config, remote.data)
        result = runner.cycle()
        results.extend(result)
        longest = max(longest, clock() - cycle_start)
        if runner.live:
            # Every journal save rewrites remote.data with a new dict, so always
            # write into the CURRENT object, never one captured before a trade.
            remember_auto_baskets(config, remote.data)
        if not (runner.live and result and isinstance(result[0], dict) and 'completed' in result[0]):
            break
    return results


def arb_summary(journal, start_rowid):
    rows = journal.db.execute(
        "SELECT kind, body FROM events WHERE rowid > ? AND kind IN ('universe_prescreen', 'entry_blocked')",
        (start_rowid,))
    edges, blocked = [], []
    for kind, body in rows:
        body = json.loads(body)
        if kind == 'universe_prescreen':
            edges.append(body)
        elif isinstance(body.get('reason'), dict) and body['reason'].get('event') in ('capital_blocked', 'arb_no_edge', 'value_candidate'):
            blocked.append(body['reason'])
    edges.sort(key=lambda e: e['edge_per_share'], reverse=True)
    return {'at': now().isoformat(), 'universe_top_edges': edges[:10],
            'capital_blocked': [b for b in blocked if b['event'] == 'capital_blocked'][:10],
            'value_candidates': [b for b in blocked if b['event'] == 'value_candidate'][:10],
            'universe_screened': len(edges)}


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
    prepare_universe(config, remote.data)
    journal = DurableJournal('private/hosted.sqlite', remote)
    runner = HostedRunner(API(os.environ.get('SIG_API_KEY')), journal, config,
                    live=os.environ.get('SIG_MODE', 'dry-run') == 'live')
    start_rowid = journal.db.execute('SELECT COALESCE(MAX(rowid), 0) FROM events').fetchone()[0]
    try:
        if not journal.pending():
            # The same executor owns all scheduling/checkpoints. Dip sampling
            # remains GET-only while entries are paused; never dispatch a second
            # runner or translate observations into orders.
            try:
                history, observation = scan_dips(
                    runner.api, runner.api.state(), config,
                    remote.data.get('dip_quote_history', {}))
                remote.data['dip_quote_history'] = history
                remote.data['latest_dip_scan'] = observation
                config['_dip_scan'] = observation
                journal.event('dip_scan', {k: v for k, v in observation.items() if k != 'baskets'})
                print(json.dumps({'dip_scan': observation}, allow_nan=False), flush=True)
            except (Halt, ValueError, KeyError, TypeError) as error:
                remote.data['dip_quote_history'] = {}
                remote.data['latest_dip_scan'] = {
                    'at': now().isoformat(), 'status': 'failed',
                    'reason': str(error), 'execution_enabled': False, 'orders_submitted': 0}
                journal.event('dip_scan_failed', remote.data['latest_dip_scan'])
        result = run_cycles(runner, config, remote)
        remote.data['latest_arb_scan'] = arb_summary(journal, start_rowid)
        status, screened_count = cycle_screening(journal, start_rowid, config['baskets'])
        record_sweep(remote, 'live' if runner.live else 'dry-run', status, screened_count)
        # Keep account and diagnostics independently of bounded event history.
        reviews = list(journal.db.execute("SELECT body FROM events WHERE rowid > ? AND kind = 'review' ORDER BY rowid DESC LIMIT 1", (start_rowid,)))
        if reviews:
            remote.data['latest_portfolio_review'] = json.loads(reviews[0][0])
        diagnostics = [json.loads(body) for (body,) in journal.db.execute(
            "SELECT body FROM events WHERE rowid > ? AND kind = 'entry_blocked'", (start_rowid,))]
        remote.data['latest_basket_diagnostics'] = [d['reason'] for d in diagnostics
            if isinstance(d.get('reason'), dict) and d['reason'].get('event') == 'held_basket_diagnostic']
        remote.data['latest_turnover_candidates'] = [d['reason'] for d in diagnostics
            if isinstance(d.get('reason'), dict) and d['reason'].get('event') == 'turnover_exit_candidate']
        journal.event('hosted_result', result)
        print(json.dumps(result, allow_nan=False), flush=True)
        # Same executor/concurrency lock; research never provides orders/cases.
        # Reconciliation always runs first. Pending-only cycles skip discovery.
        if status == 'success' and not journal.pending():
            try:
                store = BrainStore('private/universe.sqlite')
                snapshot = collect(ReadOnlyAPI(os.environ.get('SIG_API_KEY'), store), {}, 3)
                remote.data['last_universe_scan'] = {
                    'at': snapshot['at'], 'status': 'success',
                    'catalog_count': snapshot['catalog_count'],
                    'exchange_count': snapshot['exchange_count'],
                    'relationship_count': snapshot['relationship_count'],
                    'universe_discovery_complete': snapshot['universe_discovery_complete'],
                    'catalog': snapshot['exchange_catalog'],
                    'leads': snapshot['relations'], 'read_failures': snapshot['read_failures'],
                    'orders_submitted': 0, 'execution_authorized': False}
                journal.event('universe_screened', {
                    k: v for k, v in remote.data['last_universe_scan'].items()
                    if k not in ('catalog', 'leads')})
            except (Halt, ValueError, KeyError, TypeError) as error:
                remote.data['last_universe_scan'] = {
                    'at': now().isoformat(), 'status': 'incomplete',
                    'universe_discovery_complete': False, 'reason': str(error),
                    'orders_submitted': 0, 'execution_authorized': False}
                journal.event('universe_incomplete', remote.data['last_universe_scan'])
    except BaseException:
        # Fail closed. An unresolved job remains in the remote checkpoint.
        _, screened_count = cycle_screening(journal, start_rowid, config['baskets'])
        record_sweep(remote, 'live' if runner.live else 'dry-run', 'failure', screened_count)
        journal.event('hosted_failure', {'at': now().isoformat(), 'message': 'Run failed; review logs and reconcile'})
        raise
    finally:
        journal.checkpoint()


if __name__ == '__main__':
    main()
