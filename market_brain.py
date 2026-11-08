"""Shared research state and strategy evaluation. This module cannot place orders.

Quote and turnover models are assumption sensitivities, never inferred fills.
Every scanner uses the same portfolio snapshot and reservation ledger.
"""
import copy
import hashlib
import itertools
import json
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from decision_engine import executable, forecast, interval, number, timestamp

CUTOFF = datetime(2026, 11, 4, 17, tzinfo=timezone.utc)
LIMITS = dict(committed=25000, reserve=75000, basket=5000,
              clip=1000, race=500, directional=5000, factor=2000)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def shares(value, maximum=1000):
    q = number(value, 1, maximum)
    if q != int(q):
        raise ValueError("Whole shares required")
    return int(q)


def current(value, now, seconds):
    age = (now - timestamp(value)).total_seconds()
    return 0 <= age <= seconds


class BrainStore:
    """SQLite transactions coordinate scanners/processes on ONE shared disk.

    This is not a distributed lock and must not be used on independent replicas.
    Reserved plans never expire into reusable cash: only reconciliation releases.
    """
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None,
                                  check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS reads(at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,
              snapshot TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        ''')

    def take_read(self, at, ceiling=80):
        if isinstance(ceiling, bool) or not 1 <= ceiling <= 80:
            raise ValueError('Research read ceiling must leave room for the execution owner')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('DELETE FROM reads WHERE at <= ?', (at - 60,))
                count = self.db.execute('SELECT COUNT(*) FROM reads').fetchone()[0]
                if count >= ceiling:
                    raise ValueError('Shared research read budget exhausted')
                self.db.execute('INSERT INTO reads VALUES (?)', (at,))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def add_evidence(self, item):
        """Ingest facts/leads, not instructions or automatic sentiment forecasts."""
        e = copy.deepcopy(item)
        u = urlsplit(e['url'])
        if u.scheme != 'https' or not u.netloc or u.username or u.password or u.query:
            raise ValueError('Use an HTTPS source URL without credentials or query tokens')
        for field in ('claim', 'published_at', 'observed_at', 'source_kind', 'event_id'):
            if not e.get(field):
                raise ValueError('Evidence missing ' + field)
        timestamp(e['published_at']); timestamp(e['observed_at'])
        if e['source_kind'] not in ('official', 'original_poll', 'reporting', 'social'):
            raise ValueError('Unknown evidence source kind')
        if e['source_kind'] == 'social':
            e['verified'] = False
        if e.get('verified') is True and not e.get('verification_reference'):
            raise ValueError('Verified evidence needs a corroboration reference')
        e['id'] = digest([e['url'], e['claim'], e['published_at']])
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?)',
                            (e['id'], json.dumps(e, allow_nan=False)))
        return e['id']

    def evidence(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM evidence')]

    def observe(self, row):
        """Record actual snapshots only. No paper fills from touched prices."""
        key = digest(row)
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO observations VALUES (?,?)',
                            (key, json.dumps(row, allow_nan=False)))
        return key

    def reserve(self, plan, snapshot, limits=None, now=None):
        limits = checked_limits(limits)
        now = now or datetime.now(timezone.utc)
        if now >= CUTOFF or not current(snapshot['at'], now, 30):
            raise ValueError('Cutoff or stale portfolio')
        if snapshot.get('complete') is not True or snapshot.get('unresolved'):
            raise ValueError('Incomplete reconciliation or unresolved execution')
        if plan.get('eligible') is not True or not current(plan['observed_at'], now, 30):
            raise ValueError('Ineligible or stale plan')
        q = number(plan['quantity'], 1, limits['clip'])
        if q != int(q):
            raise ValueError('Whole shares required')
        # No estimated sale proceeds/offset credits fund another planned entry.
        spend = number(plan['cash_required'], 0)
        risk = number(plan['risk_charge'], 0)
        if risk < spend:
            raise ValueError('Risk charge cannot discount unfilled cash commitment')
        if len(set(plan['exchange_ids'])) != len(plan['exchange_ids']):
            raise ValueError('Duplicate exchange in a plan')
        if not plan.get('exchange_ids') or not plan.get('race') or not plan.get('factor'):
            raise ValueError('Plan identity and exposure mapping required')
        key = digest(plan['identity'])
        sid = digest(snapshot)
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if self.db.execute('SELECT 1 FROM reservations WHERE id=?', (key,)).fetchone():
                    raise ValueError('Duplicate logical plan')
                reserved = [json.loads(r[0]) for r in self.db.execute(
                    "SELECT body FROM reservations WHERE status='reserved'")]
                if any(set(p['exchange_ids']) & set(plan['exchange_ids']) for p in reserved):
                    raise ValueError('Conflicting scanner plan on an exchange')
                if set(plan['exchange_ids']) & set(snapshot.get('protected_hedge_exchanges', [])):
                    raise ValueError('Separate quote/entry would overlap a protected held basket')
                pending_cash = sum(p['cash_required'] for p in reserved)
                pending_risk = sum(p['risk_charge'] for p in reserved)
                if number(snapshot['cash'], 0) - pending_cash - spend < limits['reserve']:
                    raise ValueError('Cash reserve exceeded')
                if number(snapshot['committed'], 0) + pending_risk + risk > limits['committed']:
                    raise ValueError('Total committed cap exceeded')
                category = plan['risk_kind']
                if category not in ('basket', 'directional'):
                    raise ValueError('Unknown risk kind')
                for field, cap, label in (('race', limits['basket'] if category == 'basket' else limits['race'], 'race'),
                                          ('factor', limits['factor'], 'factor')):
                    if field == 'factor' and category == 'basket':
                        continue
                    extra = sum(p['risk_charge'] for p in reserved
                                if p[field] == plan[field] and p['risk_kind'] == category)
                    existing = number(snapshot.get(label + '_exposure', {}).get(plan[field], 0), 0)
                    if existing + extra + risk > cap:
                        raise ValueError(label + ' cap exceeded')
                if category == 'directional':
                    extra = sum(p['risk_charge'] for p in reserved if p['risk_kind'] == 'directional')
                    if number(snapshot['directional'], 0) + extra + risk > limits['directional']:
                        raise ValueError('Directional total exceeded')
                self.db.execute('INSERT INTO reservations VALUES (?,?,?,?)',
                                (key, sid, json.dumps(plan, allow_nan=False), 'reserved'))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return key

    def reconcile_reservation(self, key, receipt):
        """Requires owner reconciliation evidence; a timeout is never a receipt."""
        if receipt.get('complete') is not True or not receipt.get('owner_request_id'):
            raise ValueError('Authoritative execution receipt required')
        if receipt.get('status') not in ('done', 'cancelled_without_fill', 'rejected_without_order'):
            raise ValueError('Unresolved execution cannot release budget')
        with self.lock:
            self.db.execute("UPDATE reservations SET status='reconciled' WHERE id=?", (key,))


def checked_limits(values=None):
    result = dict(LIMITS, **(values or {}))
    if set(result) != set(LIMITS):
        raise ValueError('Unknown risk limit')
    for k, v in result.items():
        number(v, 0)
        if (k == 'reserve' and v < LIMITS[k]) or (k != 'reserve' and v > LIMITS[k]):
            raise ValueError('Higher risk profile needs explicit numeric authorization')
    return result


class BookCache:
    """One versioned cache. Any delivery gap blocks decisions until REST resync."""
    def __init__(self):
        self.books, self.revisions, self.synced = {}, {}, set()
        self.lock = threading.RLock()

    def resync(self, market, books, received_at, revision=None):
        with self.lock:
            ids = [str(b['exchangeId']) for b in books]
            if not books or len(set(ids)) != len(ids):
                raise ValueError('Invalid REST market snapshot')
            for b in books:
                if str(b['marketId']) != str(market) or not b.get('asOf'):
                    raise ValueError('Book scope/version missing')
                self._apply(b, received_at)
            self.revisions[str(market)] = revision
            self.synced.add(str(market))

    def _apply(self, book, received_at):
        b = copy.deepcopy(book)
        version = b['asOf']
        number(version['sequence'], 0); timestamp(version['at'])
        if timestamp(version['at']) > timestamp(received_at):
            raise ValueError('Future engine version')
        key = str(b['exchangeId'])
        old = self.books.get(key)
        if old:
            v = old['asOf']
            if (version['sequence'], timestamp(version['at'])) < (v['sequence'], timestamp(v['at'])):
                return
        b['_received_at'] = received_at
        self.books[key] = b

    def invalidate(self, market=None):
        with self.lock:
            if market is None:
                self.synced.clear()
            else:
                self.synced.discard(str(market))

    def batch(self, market, payload, received_at):
        market = str(market)
        with self.lock:
            last = self.revisions.get(market)
            delivery = payload['delivery']
            revision = int(number(delivery['revision'], 0))
            previous = int(number(delivery['previousRevision'], 0))
            gap = last is not None and previous > last
            bad = payload.get('resyncRequired') or gap or any(
                t.get('sequence') is None for t in payload.get('trades', []))
            if bad or market not in self.synced:
                self.synced.discard(market)
                self.revisions[market] = max(last or 0, revision)
                return 'RESYNC_REQUIRED'
            # Books may have newer versions even in a duplicated topic revision.
            for book in payload.get('books', []):
                if str(book['marketId']) != market:
                    self.synced.discard(market)
                    raise ValueError('Realtime scope mismatch')
                self._apply(book, received_at)
            if last is not None and revision <= last:
                return 'DUPLICATE'
            self.revisions[market] = revision
            return 'APPLIED'

    def get(self, exchange, now, max_age=15):
        with self.lock:
            b = self.books[str(exchange)]
            if str(b['marketId']) not in self.synced or not current(b['_received_at'], now, max_age):
                raise ValueError('Book requires authoritative resynchronization')
            if b.get('nextExpiryAt') and timestamp(b['nextExpiryAt']) <= now:
                raise ValueError('Resting depth expired; refetch')
            return copy.deepcopy(b)


def scenarios(relation):
    """Enumerate at most 10 verified binary claims; never infer exhaustiveness."""
    nodes = relation['nodes']
    ids = [str(n['exchangeId']) for n in nodes]
    if relation['status'] != 'active' or len(ids) < 2 or len(ids) > 10 or len(set(ids)) != len(ids):
        raise ValueError('Invalid active relationship')
    kind = relation['type']
    if kind not in ('mutually_exclusive', 'complementary', 'implication', 'boolean_not'):
        raise ValueError('Relationship type needs a dedicated verified payoff compiler')
    frm = relation.get('direction', {}).get('fromExchangeIds', [])
    to = relation.get('direction', {}).get('toExchangeIds', [])
    if kind == 'implication' and (len(frm) != 1 or len(to) != 1 or set(frm + to) != set(ids)):
        raise ValueError('Ambiguous implication direction')
    rows = []
    for values in itertools.product((0, 1), repeat=len(ids)):
        mapping = dict(zip(ids, values))
        valid = ((sum(values) <= 1 and (not relation.get('isExhaustive') or sum(values) == 1))
                 if kind == 'mutually_exclusive' else
                 (len(ids) == 2 and sum(values) == 1) if kind in ('complementary', 'boolean_not') else
                 mapping[frm[0]] <= mapping[to[0]])
        if valid:
            rows.append(mapping)
    if not rows:
        raise ValueError('Empty scenario set')
    return rows


def relation_candidates(relation, books, proof, now, clip=1000, costs=.005):
    """Generate long-only YES/NO baskets from executable depth and exact rules.

    Proof hashes bind a reviewed relationship version plus cancellation rules.
    Cancellation/N/A payout is UNKNOWN; the floor is conditional normal payout.
    """
    if proof.get('relation_hash') != digest(relation) or proof.get('verified') is not True:
        return []
    if not proof.get('rule_sources') or not proof.get('cancellation_rules'):
        return []
    if not current(proof['reviewed_at'], now, 86400):
        return []
    if not proof.get('settles_at') or timestamp(proof['settles_at']) <= now:
        return []
    rows = scenarios(relation)
    ids = [str(n['exchangeId']) for n in relation['nodes']]
    markets = {str(n['exchangeId']): str(n['marketId']) for n in relation['nodes']}
    for i in ids:
        b = books[i]
        if (str(b.get('exchangeId')) != i or str(b.get('marketId')) != markets[i]
                or not b.get('asOf') or not current(b['_received_at'], now, 15)):
            return []
    costs = number(costs, 0)
    clip = shares(clip)
    out = []
    for sides in itertools.product(('yes', 'no'), repeat=len(ids)):
        payout = min(sum(row[i] if s == 'yes' else 1 - row[i]
                         for i, s in zip(ids, sides)) for row in rows)
        if not payout:
            continue
        q = clip
        while q:
            entries = [executable(books[i], side, 'buy', q) for i, side in zip(ids, sides)]
            if not all(x['complete'] for x in entries):
                q //= 2; continue
            spend = sum(x['notional'] for x in entries) + q * len(ids) * costs
            profit = q * payout - spend
            if profit >= max(1, q * .01):
                out.append(dict(strategy='relationship', relation_id=relation['id'],
                    exchange_ids=ids, sides=sides, quantity=q, cash_required=spend,
                    normal_payout_floor=q*payout, conditional_profit=profit,
                    cancellation_payout='UNKNOWN', turnover_hours=None,
                    observed_at=now.isoformat(), eligible=True,
                    identity=['relationship', digest(relation), list(sides), q]))
                break
            q //= 2
    return out


def whole_basket_exit(legs, books, quantity, normal_floor_per_clip, now, costs=.005):
    if any(not current(books[str(l['exchange_id'])]['_received_at'],now,15) for l in legs):
        return None
    q = min(int(number(quantity, 1)), 1000)
    while q:
        fills = [executable(books[str(l['exchange_id'])], l['side'], 'sell', q) for l in legs]
        if all(f['complete'] for f in fills):
            proceeds = sum(f['notional'] for f in fills) - q*len(legs)*number(costs, 0)
            floor = q * number(normal_floor_per_clip, 0)
            if proceeds >= floor:
                return dict(strategy='basket_exit', quantity=q, net_proceeds=proceeds,
                            normal_payout_floor=floor, eligible=True, observed_at=now.isoformat())
        q //= 2
    return None


def turnover(case, now):
    """Exit-probability scenarios are validated separately from win probability."""
    issues = []
    book = case['book']
    if not current(book['_received_at'], now, 15):
        issues.append('Stale executable book')
    model = case['exit_model']
    for field in ('validation_reference', 'assumptions', 'falsification'):
        if not model.get(field):
            issues.append('Missing exit model ' + field)
    if not current(model['validated_at'], now, 86400):
        issues.append('Stale exit model')
    if case.get('contract_verified') is not True or case.get('costs_verified') is not True:
        issues.append('Unverified rules or costs')
    q = shares(case['quantity'])
    fee = number(case['round_trip_cost_per_share'], 0)
    entry = executable(book, case['side'], 'buy', q)
    exit_now = executable(book, case['side'], 'sell', q)
    if not entry['complete'] or not exit_now['complete']:
        issues.append('Insufficient entry or emergency exit depth')
    net_outcomes, hours = [], []
    states = model['outcomes']
    if not states or len({s['name'] for s in states}) != len(states):
        raise ValueError('Distinct exit scenarios required')
    for s in states:
        if number(s['exit_depth'], 0) < q:
            issues.append('Exit scenario lacks depth')
        net_outcomes.append(q*number(s['exit_price'], 0, 1) - entry['notional'] - q*fee)
        hours.append(number(s['hours_to_exit'], .001))
    expectations, durations = [], []
    for weights in model['probability_scenarios']:
        if len(weights) != len(states) or abs(sum(number(w, 0, 1) for w in weights)-1) > 1e-8:
            raise ValueError('Exit probabilities must sum to one')
        expectations.append(sum(w*p for w,p in zip(weights,net_outcomes)))
        durations.append(sum(w*h for w,h in zip(weights,hours)))
    if not expectations:
        issues.append('No exit-probability sensitivities')
    lower = min(expectations) if expectations else None
    eligible = not issues and lower/q >= .05
    return dict(strategy='turnover', eligible=eligible, issues=issues,
                quantity=q, cash_required=entry['notional']+q*fee,
                conservative_profit=lower, worst_scenario_pnl=min(net_outcomes),
                expected_hours_range=[min(durations),max(durations)] if durations else None,
                profit_per_capital_hour=(lower/(entry['notional']*max(durations))
                    if eligible and entry['notional'] else None),
                observed_at=now.isoformat(), paper_fills='UNKNOWN')


def quote_proposal(book, model, inventory, now, clip=100):
    """Inventory-skewed paper quotes, no claim of HJB optimality or real fills."""
    issues = []
    bounds, _, evidence_issues = forecast(model['forecast'], now)
    issues.extend(evidence_issues)
    if not current(book['_received_at'], now, 15):
        issues.append('Stale book')
    for field in ('flow_validation_reference', 'adverse_selection_bound', 'fees_per_share',
                  'volatility_buffer', 'inventory_penalty', 'max_inventory', 'valid_until'):
        if field not in model:
            issues.append('Missing quote assumption ' + field)
    if issues:
        return dict(strategy='maker', eligible=False, issues=issues)
    if timestamp(model['valid_until']) <= now:
        return dict(strategy='maker', eligible=False, issues=['Expired quote model'])
    exposure = number(inventory)
    cap = number(model['max_inventory'], 1)
    buffer = (number(model['adverse_selection_bound'], 0)+number(model['fees_per_share'], 0)
              + number(model['volatility_buffer'], 0))
    skew = number(model['inventory_penalty'], 0) * exposure/cap
    bid = math.floor((bounds[0]-buffer-skew)*200)/200
    ask = math.ceil((bounds[1]+buffer-skew)*200)/200
    best_bid = max((number(x['price'],0,1) for x in book['bids']), default=0)
    best_ask = min((number(x['price'],0,1) for x in book['asks']), default=1)
    bid = min(bid, best_ask-.005)
    ask = max(ask, best_bid+.005)
    q = shares(clip)
    quotes = []
    if .005 <= bid <= .995 and exposure+q <= cap:
        quotes.append(dict(side='yes', action='buy', price=round(bid,3), quantity=q))
    if .005 <= ask <= .995 and exposure-q >= -cap:
        quotes.append(dict(side='no', action='buy', price=round(1-ask,3), quantity=q))
    # Full two-sided commitment is reserved; never assume one offsets the other.
    return dict(strategy='maker', eligible=bool(quotes), issues=[], quotes=quotes,
                cash_required=sum(x['price']*q for x in quotes)+len(quotes)*q*number(model['fees_per_share'],0),
                observed_at=now.isoformat(), paper_fills='UNKNOWN',
                limitation='Queue position, fills and realized spread remain unobserved')


def scan_parallel(jobs, workers=4):
    """Jobs receive immutable snapshots; order-writing clients are never supplied."""
    workers = int(number(workers, 1, 8))
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in jobs]
        for name, future in futures:
            try:
                results.append(dict(scanner=name, result=future.result()))
            except (ValueError, KeyError, TypeError):
                errors.append(dict(scanner=name, category='INVALID_OR_INCOMPLETE_DATA'))
    return dict(results=results, errors=errors, complete=not errors)
