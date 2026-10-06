"""SIG virtual-currency execution runner. Standard library only; dry-run default."""
import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decision_engine import evaluate, executable, timestamp

BASE = 'https://sig.thesuper.market/api/v1'
TID = 'bda92870-621e-47b0-bc3c-3602c5c26f55'
SLUG = 'midterm-elections'
END = datetime(2026, 11, 4, 17, tzinfo=timezone.utc)
MAX_COMMITTED, RESERVE, BASKET_CAP = 25000, 75000, 5000
DIRECTIONAL_RACE, DIRECTIONAL_TOTAL, FACTOR_CAP = 500, 5000, 2000


class Halt(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc)


def tick(price):
    if not math.isfinite(price) or not .005 <= price <= .995 or abs(price * 200 - round(price * 200)) > 1e-7:
        raise Halt('Invalid limit price; market orders prohibited')
    return round(price, 3)


def qty(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2147483647:
        raise Halt('Quantity must be a positive integer')
    return value


class API:
    def __init__(self, key):
        if not key:
            raise Halt('SIG_API_KEY is missing')
        self.key = key
        self.last = {'GET': 0., 'WRITE': 0.}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, payload=None):
        if not path.startswith('/') or '://' in path:
            raise Halt('Only supported SIG /api/v1 paths permitted')
        bucket = 'GET' if method == 'GET' else 'WRITE'
        delay = max(0, self.last[bucket] + (0.8 if bucket == 'GET' else 2.2) - time.monotonic())
        if delay:
            time.sleep(delay)
        self.last[bucket] = time.monotonic()
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            # Never print headers, credential values, arbitrary server bodies or request objects.
            raise Halt(f'SIG HTTP {e.code}; reconcile before any placement retry') from None
        except (urllib.error.URLError, TimeoutError):
            raise Halt('SIG connection failed; placement outcome may be unknown') from None

    def get(self, path):
        return self.request('GET', path)

    def book(self, exchange):
        book = self.get(f'/exchanges/{exchange}/orderbook?depth=100&tournamentId={TID}')
        book['_received_at'] = now().isoformat()
        # asOf is engine's last change, not necessarily receipt time. Use fresh HTTP receipt
        # for unchanged books, while retaining asOf for audit; reject future versions.
        if str(book.get('exchangeId')) != str(exchange) or not book.get('asOf'):
            raise Halt('Book identity/version missing')
        if timestamp(book['asOf']['at']) > now() + timedelta(seconds=5):
            raise Halt('Future book timestamp')
        return book

    def all_orders(self):
        data, cursor = [], None
        for _ in range(10):
            path = f'/orders?status=open&limit=100&tournamentId={TID}'
            if cursor:
                path += '&cursor=' + urllib.parse.quote(cursor, safe='')
            r = self.get(path)
            if r.get('coverage', {}).get('complete') is not True:
                raise Halt('Order projection incomplete')
            data.extend(r['data'])
            if not r['pagination']['hasMore']:
                return data
            cursor = r['pagination']['nextCursor']
        raise Halt('Order pagination exceeds bounded review')

    def state(self):
        positions = self.get(f'/tournaments/{SLUG}/portfolio/positions')['positions']
        orders = self.all_orders()
        fills = self.get(f'/portfolio/fills?limit=100&tournamentId={TID}')
        if fills.get('coverage', {}).get('complete') is not True:
            raise Halt('Fill projection incomplete')
        # Tournament-scoped P&L total less positions gives scoped cash; /account may
        # represent organization/default context and is not used as sole scoped balance.
        pnl = self.get(f'/tournaments/{SLUG}/portfolio/pnl')
        cash = float(pnl['totalAccountValue']) - float(pnl['totalHoldingsValue'])
        return {'at': now().isoformat(), 'cash': round(cash, 6), 'positions': positions,
                'orders': orders, 'recent_fills': fills['data'], 'pnl': pnl}


class Journal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, body TEXT, updated TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (at TEXT, kind TEXT, body TEXT)')
        self.db.commit()

    def save(self, job, status):
        job['status'] = status
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)',
                            (job['id'], status, json.dumps(job, allow_nan=False), now().isoformat()))

    def pending(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM jobs WHERE status NOT IN ('done','dry_run')")]

    def event(self, kind, body):
        with self.db:
            self.db.execute('INSERT INTO events VALUES (?,?,?)', (now().isoformat(), kind, json.dumps(body, allow_nan=False)))


def signed_positions(state):
    return {str(p['exchangeId']): float(p['quantity']) for p in state['positions']}


def validate_contracts(api, basket):
    """Only Party Winner contracts for one exact race/stage/date; no exhaustive assumption."""
    identity, parties, records = None, set(), []
    if len(basket['legs']) < 2 or len(basket['legs']) > 10:
        raise Halt('Basket needs2–10 mutually exclusive party contracts')
    if len({str(l['exchange_id']) for l in basket['legs']}) != len(basket['legs']):
        raise Halt('Duplicate basket exchange')
    for leg in basket['legs']:
        market = api.get(f'/tournaments/{SLUG}/markets/{leg["market_id"]}')
        nodes = api.get(f'/markets/{leg["market_id"]}/nodes')
        root = nodes['root']
        d = root['contract_details']
        if market['status'] != 'open' or not market.get('settlementDate') or timestamp(market['settlementDate']) <= now():
            raise Halt('Market closed or past settlement')
        if str(leg['exchange_id']) not in {str(e['id']) for e in market['exchanges']}:
            raise Halt('Market/exchange mismatch')
        if d.get('resolutionType') != 'Party Winner' or root.get('contract_type') != 'Election Outcome':
            raise Halt('Unsupported resolution definition')
        context_ids = {c.get('tournament', {}).get('id') for c in nodes.get('contexts', [])}
        if TID not in context_ids:
            raise Halt('Contract not assigned to this tournament')
        fields = ('raceId', 'stageId', 'electionDate', 'raceStage')
        if not all(d.get(k) for k in fields) or d['raceStage'] != 'General':
            raise Halt('Missing or unsupported race metadata')
        current = tuple(d[k] for k in fields) + (root['settlement_date'],)
        if identity is not None and identity != current:
            raise Halt('Race/stage/date/settlement mismatch')
        identity = current
        if d.get('winnerName') != leg['party'] or leg['party'] in parties:
            raise Halt('Party definition mismatch or duplicate')
        parties.add(leg['party'])
        records.append({'market': market['id'], 'title': market['title'], 'contract': root})
    return records


def portfolio_risk(state, config):
    committed = sum(float(p['costBasis']) for p in state['positions'])
    for order in state['orders']:
        if order.get('priceLimit') is None:
            raise Halt('Unbounded outstanding order')
        # Treat all pending orders as commitments, including sells; conservative.
        committed += abs(float(order['quantity'])) * float(order['priceLimit'])
    known, hedged = set(), set()
    positions = {str(p['exchangeId']): p for p in state['positions']}
    race_cost = {}
    for b in config['baskets']:
        ids = [str(l['exchange_id']) for l in b['legs']]
        if known.intersection(ids):
            raise Halt('Overlapping configured baskets')
        known.update(ids)
        counts = [float(positions.get(i, {}).get('quantity', 0)) for i in ids]
        race_cost[b['name']] = sum(float(positions.get(i, {}).get('costBasis', 0)) for i in ids)
        if len(set(counts)) == 1 and counts[0] <= 0:
            hedged.update(ids)
        elif any(counts):
            raise Halt('Configured hedge is not matched; reconcile before new entries')
    directional = sum(float(p['costBasis']) for i, p in positions.items() if i not in hedged)
    # Unclassified directional exposure is charged in full to every proposed race/factor.
    return committed, race_cost, directional


def fresh_books(api, legs):
    books = {str(l['exchange_id']): api.book(str(l['exchange_id'])) for l in legs}
    if any((now() - timestamp(b['_received_at'])).total_seconds() > 60 for b in books.values()):
        raise Halt('Sequential book reads too old; no order')
    return books


def price_for(book, side, action, quantity):
    execution = executable(book, side, action, quantity)
    if not execution['complete']:
        raise Halt('Insufficient executable depth')
    levels = book['asks'] if (side, action) in [('yes', 'buy'), ('no', 'sell')] else book['bids']
    remaining, worst = quantity, None
    # API levels YES normalized, naturally ordered by best execution.
    for level in levels:
        take = min(remaining, level['quantity'])
        if take:
            worst = level['price'] if side == 'yes' else 1 - level['price']
            remaining -= take
        if remaining <= 0:
            break
    return tick(worst), execution


def cost_allowance(config):
    c = config['costs']
    if c.get('verified') is not True or not c.get('source'):
        raise Halt('Document fee schedule in config before enabling live placements')
    fee = float(c['fee_per_share'])
    if not math.isfinite(fee) or fee < 0:
        raise Halt('Invalid fee allowance')
    return fee + .005  # one extra tick per leg for implementation sensitivity


def basket_plan(api, state, config, basket):
    allowance = cost_allowance(config)
    contracts = validate_contracts(api, basket)
    committed, costs, directional = portfolio_risk(state, config)
    positions = signed_positions(state)
    held = -positions.get(str(basket['legs'][0]['exchange_id']), 0)
    books = fresh_books(api, basket['legs'])
    n = len(basket['legs'])
    if held > 0:
        # A thin book must not hide a profitable matched partial exit. Try the
        # complete holding first, then smaller balanced clips; preserve the hedge.
        exit_q = int(held)
        while exit_q > 0:
            try:
                limits = [price_for(books[str(l['exchange_id'])], 'no', 'sell', exit_q)[0]
                          for l in basket['legs']]
                proceeds = exit_q * (sum(limits) - n * allowance)
                if proceeds >= exit_q * (n - 1):
                    return {'kind': 'basket_exit', 'name': basket['name'], 'action': 'sell',
                            'quantity': exit_q, 'legs': basket['legs'], 'prices': limits,
                            'value_floor': exit_q * (n - 1), 'net_limit_proceeds': proceeds,
                            'contracts': contracts}
            except Halt:
                pass
            exit_q //= 2
    # Small default clip sized from displayed depth and surviving edge, not caps.
    q = min(int(basket.get('clip', 100)), 1000)
    while q > 0:
        try:
            limits = [price_for(books[str(l['exchange_id'])], 'no', 'buy', q)[0] for l in basket['legs']]
        except Halt:
            q //= 2
            continue
        spend = q * (sum(limits) + n * allowance)
        profit = q * (n - 1) - spend
        if profit < max(1., q * .01):
            return None
        if (committed + spend <= MAX_COMMITTED and costs[basket['name']] + spend <= BASKET_CAP
                and state['cash'] - spend >= RESERVE):
            return {'kind': 'basket_entry', 'name': basket['name'], 'action': 'buy', 'quantity': q,
                    'legs': basket['legs'], 'prices': limits, 'max_spend': spend,
                    'value_floor': q * (n - 1), 'robust_profit': profit, 'contracts': contracts,
                    'objective': 'normal-settlement value; no fast-exit guarantee',
                    'mechanism': 'Distinct party-winner claims in separate books; third-party win increases NO payout',
                    'falsification': 'Changed settlement/party definitions, incomplete fills, cancellation or N/A'}
        q //= 2
    return None


def directional_plan(api, state, config, supplied):
    c = copy.deepcopy(supplied)
    if c['trade']['side'] not in ('yes', 'no'):
        raise Halt('Invalid directional side')
    q = qty(c['trade']['quantity'])
    leg = {'market_id': str(c['contract']['market_id']), 'exchange_id': str(c['contract']['exchange_id'])}
    # Contract record must be reviewed against current source, not just a boolean.
    market = api.get(f'/tournaments/{SLUG}/markets/{leg["market_id"]}')
    nodes = api.get(f'/markets/{leg["market_id"]}/nodes')
    fingerprint = hashlib.sha256(json.dumps(nodes['root'], sort_keys=True).encode()).hexdigest()
    if c['contract'].get('nodes_sha256') != fingerprint or market['status'] != 'open':
        raise Halt('Directional contract fingerprint missing/changed')
    if leg['exchange_id'] not in {str(e['id']) for e in market['exchanges']}:
        raise Halt('Directional exchange mismatch')
    book = api.book(leg['exchange_id'])
    c['book'] = copy.deepcopy(book)
    c['book']['asOf']['at'] = book['_received_at']
    committed, _, exposure = portfolio_risk(state, config)
    # Never net a forecast-based trade against an intact configured hedge.
    if signed_positions(state).get(leg['exchange_id'], 0) != 0:
        raise Halt('Directional entries restricted to flat exchanges; preserve existing hedges')
    allowance = cost_allowance(config)
    c['costs'] = dict(config['costs'], slippage_per_share=.005)
    c['risk'] = {'cash': state['cash'], 'minimum_cash': RESERVE,
                 'race_exposure': exposure, 'factor_exposure': exposure, 'total_exposure': exposure,
                 'max_race_loss': DIRECTIONAL_RACE, 'max_factor_loss': FACTOR_CAP, 'max_total_loss': DIRECTIONAL_TOTAL}
    c['trade']['minimum_edge'] = max(.05, float(c['trade'].get('minimum_edge', .05)))
    decision = evaluate(c)
    limit, _ = price_for(book, c['trade']['side'], 'buy', q)
    worst_loss = q * (limit + allowance)
    conservative = decision['probability_range'][0] - limit - allowance
    if (decision['decision'] != 'CANDIDATE' or conservative < .05 or worst_loss + exposure > DIRECTIONAL_RACE
            or committed + worst_loss > MAX_COMMITTED or state['cash'] - worst_loss < RESERVE):
        return None
    return {'kind': 'directional_entry', 'name': c['contract']['question'], 'action': 'buy',
            'side': c['trade']['side'], 'quantity': q, 'legs': [leg], 'prices': [limit],
            'max_spend': worst_loss, 'decision': decision, 'case': c,
            'conservative_limit_edge': conservative}


class Runner:
    def __init__(self, api, journal, config, live=False):
        self.api, self.journal, self.config, self.live = api, journal, config, live

    def check_stop(self):
        if now() >= END or Path(self.config.get('kill_switch', 'private/STOP')).exists():
            raise Halt('Trading deadline or kill switch reached')

    def submit(self, plan, state):
        self.check_stop()
        expiration = min(now() + timedelta(seconds=90), END).isoformat()
        request = {'idempotencyKey': str(uuid.uuid4()), 'legs': [
            {'exchangeId': str(l['exchange_id']), 'side': plan.get('side', 'no'), 'action': plan['action'],
             'quantity': qty(plan['quantity']), 'price': tick(price), 'tournamentId': TID,
             'expirationDate': expiration} for l, price in zip(plan['legs'], plan['prices'])]}
        job = {'id': request['idempotencyKey'], 'request': request, 'plan': plan, 'before': state,
               'repairs': [], 'created_at': now().isoformat()}
        if not self.live:
            self.journal.save(job, 'dry_run')
            return {'dry_run': True, 'plan': plan}
        # Commit intent before network write. No automatic new-key retry.
        self.journal.save(job, 'prepared')
        return self.finish(job)

    def finish(self, job):
        self.check_stop()
        if job['status'] in ('halted', 'repairing'):
            raise Halt('Unfinished repair requires reconciliation; no new orders')
        if 'response' not in job:
            # Includes restart after unknown outcome: reconcile fills/positions FIRST,
            # then replay the identical persisted request/key (never reconstruct it).
            if job['status'] != 'prepared':
                self.api.state()
            self.journal.save(job, 'outcome_unknown')
            job['response'] = self.api.request('POST', '/orders/multi-leg', job['request'])
            self.journal.save(job, 'accepted')
        try:
            results = sorted(job['response']['results'], key=lambda r: r['index'])
            if len(results) != len(job['request']['legs']):
                raise Halt('Placement response incomplete')
            for result in results:
                d = result['data']
                if d.get('orderId') is None:
                    continue
                # Do not rely on displayed depth or response quantityTraded alone.
                self.api.get(f'/orders/{d["orderId"]}/fills?limit=100')
                order = self.api.get(f'/orders/{d["orderId"]}')
                order = order.get('data', order)
                if order['open']:
                    self.api.request('DELETE', f'/orders/{d["orderId"]}')
                final_order = self.api.get(f'/orders/{d["orderId"]}')
                final_order = final_order.get('data', final_order)
                if final_order['open']:
                    raise Halt('Unfilled remainder could not be cancelled')
                final_fills = self.api.get(f'/orders/{d["orderId"]}/fills?limit=100')
                if final_fills.get('coverage', {}).get('complete') is False:
                    raise Halt('Fill projection incomplete')
                if final_fills.get('pagination', {}).get('hasMore'):
                    raise Halt('Fill pagination needs reconciliation')
                job.setdefault('fills', {})[str(d['orderId'])] = final_fills
            after = self.api.state()
            if after['orders']:
                raise Halt('Outstanding orders after execution; no further entries')
            if job['plan']['kind'].startswith('basket'):
                after = self.repair(job, after)
            job['after'] = after
            job['cash_change'] = after['cash'] - job['before']['cash']
            expected_cash_change, filled_shares = 0., 0.
            for result, leg in zip(results, job['request']['legs']):
                oid = result['data'].get('orderId')
                for fill in job.get('fills', {}).get(str(oid), {}).get('data', []):
                    q = abs(float(fill['quantity']))
                    expected_cash_change += (1 if leg['action'] == 'sell' else -1) * q * float(fill['price'])
                    filled_shares += q
            for repair in job['repairs']:
                for fill in repair['fills']['data']:
                    q = abs(float(fill['quantity']))
                    expected_cash_change += (1 if repair['request']['action'] == 'sell' else -1) * q * float(fill['price'])
                    filled_shares += q
            job['observed_extra_cost'] = expected_cash_change - job['cash_change']
            if (job['observed_extra_cost'] < -.01 or job['observed_extra_cost'] > filled_shares * float(self.config['costs']['fee_per_share']) + .01):
                raise Halt('Fill/cash costs do not reconcile to verified fee allowance')
            if after['cash'] < RESERVE:
                raise Halt('Cash reserve breached after fills')
            committed, race_cost, directional = portfolio_risk(after, self.config)
            if committed > MAX_COMMITTED or any(c > BASKET_CAP for c in race_cost.values()) or directional > DIRECTIONAL_TOTAL:
                raise Halt('Post-fill exposure exceeds configured ceilings')
            self.journal.save(job, 'done')
            return {'completed': job['id'], 'cash': after['cash'], 'cash_change': job['cash_change'],
                    'repairs': len(job['repairs'])}
        except Exception:
            self.journal.save(job, 'halted')
            raise

    def repair(self, job, after):
        plan = job['plan']
        ids = [str(l['exchange_id']) for l in plan['legs']]
        before_q = signed_positions(job['before'])
        post_q = signed_positions(after)
        held = [-post_q.get(i, 0) for i in ids]
        if len(set(held)) == 1:
            return after
        if plan['action'] == 'buy':
            target = min(held)  # unwind only unmatched incremental entry shares
            action = 'sell'
        else:
            target = max(held)  # buy back unmatched exits; restore intact original hedge
            action = 'buy'
        budget = min(10., float(self.config.get('repair_loss_budget', 10)))
        total_stress = 0.
        for i, amount in zip(ids, held):
            q = int(abs(amount - target))
            if not q:
                continue
            if abs(post_q.get(i, 0) - before_q.get(i, 0)) < q:
                raise Halt('Repair would change pre-existing hedge')
            book = self.api.book(i)
            price, _ = price_for(book, 'no', action, q)
            original = job['request']['legs'][ids.index(i)]['price']
            # Include an extra fee/slippage tick for each repair leg.
            loss = q * (max(0, original - price) if action == 'sell' else max(0, price - original))
            total_stress += loss + q * cost_allowance(self.config)
            if total_stress > budget:
                raise Halt('Repair depth/cost exceeds10 SUSQies; manual reconciliation needed')
            if action == 'buy' and (after['cash'] - q * (price + cost_allowance(self.config)) < RESERVE):
                raise Halt('Repair would breach cash reserve')
            request = {'exchangeId': i, 'side': 'no', 'action': action, 'quantity': q, 'price': price,
                       'tournamentId': TID, 'expirationDate': min(now() + timedelta(seconds=60), END).isoformat(),
                       'idempotencyKey': str(uuid.uuid4())}
            repair = {'request': request}
            job['repairs'].append(repair)
            self.journal.save(job, 'repairing')
            self.check_stop()
            repair['response'] = self.api.request('POST', '/orders', request)
            self.journal.save(job, 'repairing')
            order_id = repair['response'].get('orderId')
            if not order_id:
                raise Halt('Repair produced no order')
            order = self.api.get(f'/orders/{order_id}')
            order = order.get('data', order)
            if order['open']:
                self.api.request('DELETE', f'/orders/{order_id}')
            repair['fills'] = self.api.get(f'/orders/{order_id}/fills?limit=100')
            after = self.api.state()
            post_q = signed_positions(after)
            if -post_q.get(i, 0) != target or after['orders']:
                raise Halt('Repair incomplete; halted with persisted evidence')
        if len({signed_positions(after).get(i, 0) for i in ids}) != 1:
            raise Halt('Hedge still unmatched')
        return after

    def cycle(self, cases=None):
        self.check_stop()
        pending = self.journal.pending()
        if pending:
            if not self.live:
                raise Halt('Pending live job exists; reconcile in live mode before further planning')
            return [self.finish(job) for job in pending]
        state = self.api.state()
        self.journal.event('review', state)
        if state['orders']:
            raise Halt('Existing resting orders found; no conflicting new placements')
        portfolio_risk(state, self.config)
        plans = []
        review_names = set(self.config.get('review_names', [b['name'] for b in self.config['baskets']]))
        for basket in self.config['baskets']:
            if basket['name'] not in review_names:
                continue
            self.journal.event('checking', {'basket': basket['name']})
            try:
                plan = basket_plan(self.api, state, self.config, basket)
                if plan:
                    plans.append(plan)
                self.journal.event('screened', {'basket': basket['name'], 'candidate': bool(plan)})
            except Halt as e:
                self.journal.event('abstain', {'basket': basket['name'], 'reason': str(e)})
        for case in (cases or [])[:3]:
            plan = directional_plan(self.api, state, self.config, case)
            if plan:
                plans.append(plan)
        # One job per cycle; always obtain a fresh state and prices on the next cycle.
        plans.sort(key=lambda p: (p['kind'] == 'basket_exit', p.get('robust_profit', p.get('conservative_limit_edge', 0))), reverse=True)
        if not plans:
            return [{'decision': 'NO_TRADE', 'cash': state['cash']}]
        # Long scans must not use stale limits/state to submit. Rebuild selected plan.
        state = self.api.state()
        selected = plans[0]
        if selected['kind'].startswith('basket'):
            b = next(b for b in self.config['baskets'] if b['name'] == selected['name'])
            selected = basket_plan(self.api, state, self.config, b)
        else:
            selected = directional_plan(self.api, state, self.config, selected['case'])
        return [self.submit(selected, state)] if selected else [{'decision': 'NO_TRADE_AFTER_REFRESH'}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='bot_config.json')
    parser.add_argument('--journal', default='private/execution.sqlite')
    parser.add_argument('--live', action='store_true', help='Enable API writes under configured limits')
    parser.add_argument('--loop', action='store_true', help='Repeat until stopped/deadline; not continuous book monitoring')
    parser.add_argument('--interval', type=int, default=3600)
    parser.add_argument('--cases', help='Private JSON list of researched directional cases')
    args = parser.parse_args()
    if args.interval < 3600:
        parser.error('Current authorization supports hourly reviews; interval must be>=3600')
    config = json.loads(Path(args.config).read_text())
    lock_path = Path(args.journal + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another bot instance holds this journal lock')
        runner = Runner(API(os.environ.get('SIG_API_KEY')), Journal(args.journal), config, args.live)
        while True:
            cases = json.loads(Path(args.cases).read_text()) if args.cases else []
            try:
                result = runner.cycle(cases)
                print(json.dumps(result, allow_nan=False), flush=True)
            except Halt as e:
                runner.journal.event('halt', {'reason': str(e)})
                raise SystemExit(str(e))
            if not args.loop:
                break
            due = time.monotonic() + args.interval
            while time.monotonic() < due:
                runner.check_stop()
                time.sleep(min(10, max(0, due - time.monotonic())))


if __name__ == '__main__':
    main()
