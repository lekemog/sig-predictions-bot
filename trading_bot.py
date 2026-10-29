"""SIG virtual-currency execution runner. Standard library only; dry-run default."""
import argparse
import copy
import fcntl
import hashlib
import http.client
import json
import math
import os
import sqlite3
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import arb_engine
import value_engine
from decision_engine import evaluate, executable, timestamp
from turnover import dip_entry_reason, profit_exit_target

BASE = 'https://sig.thesuper.market/api/v1'
TID = 'bda92870-621e-47b0-bc3c-3602c5c26f55'
SLUG = 'midterm-elections'
END = datetime(2026, 11, 4, 17, tzinfo=timezone.utc)
MAX_COMMITTED, RESERVE, BASKET_CAP = 25000, 75000, 5000
DIRECTIONAL_RACE, DIRECTIONAL_TOTAL, FACTOR_CAP = 500, 5000, 2000
# Entry quarantine from the settlement audit. Names and IDs prevent a renamed
# basket from accidentally dropping the requirement. Exits/recovery are exempt.
UNPROVEN_BASKETS = frozenset({'Nebraska Senate', 'Montana Senate', 'Kansas Senate',
                             'PA Governor', 'Alaska Governor'})
UNPROVEN_EXCHANGES = frozenset({'968', '969', '970', '963', '964', '965',
                               '1045', '1046', '878', '879', '1057', '1058'})


POST_TRADE_READS = 10  # ~4 minutes of backoff before an accepted order halts


class Halt(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc)


def contract_proof_hash(basket, contracts):
    """Pin current API definitions AND the configured exchange/party mapping.

    This is a metadata fingerprint, not proof that the API supplies full rules.
    Full settlement-rule interpretation must also be reviewed in the manifest.
    """
    body = {'legs': basket['legs'], 'contracts': contracts}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def max_simultaneous_yes(candidate_rows, exchange_ids):
    """Count YES legs per possible winner, including explicit other/write-in.

    A category may contain several candidates; each gets a separate row. Stable
    candidate IDs must identify people, not party labels or endorsements.
    """
    if not isinstance(candidate_rows, dict) or 'write_in_other' not in candidate_rows:
        raise ValueError('Missing candidate matrix or other/write-in row')
    allowed = set(exchange_ids)
    counts = []
    for candidate, matches in candidate_rows.items():
        if (not isinstance(candidate, str) or not candidate
                or not isinstance(matches, list)
                or any(not isinstance(i, str) or i not in allowed for i in matches)
                or len(set(matches)) != len(matches)):
            raise ValueError('Invalid candidate-to-leg mapping')
        counts.append(len(matches))
    # A zero row is safe only with reviewed rules that exclude all unlisted
    # winners from every configured category (checked by the proof gate).
    if candidate_rows['write_in_other']:
        raise ValueError('Other/write-in settlement mapping is unresolved')
    return max(counts, default=0)


def entry_proof_required(basket):
    return (basket.get('entry_proof_required') is True
            or basket['name'] in UNPROVEN_BASKETS
            or any(str(l['exchange_id']) in UNPROVEN_EXCHANGES for l in basket['legs'])
            or 'entry_proof' in basket)


def entry_proof_reason(basket, contracts, at=None):
    """Return a blocking reason; never halt held-basket exit/reconciliation.

    Evidence is a reviewed repository input, not an automatically inferred
    probability. Certification/completeness attestations require human research.
    """
    if not entry_proof_required(basket):
        return None
    try:
        p = basket.get('entry_proof')
        if not isinstance(p, dict) or p.get('schema_version') != 1:
            return 'Missing reviewed candidate settlement proof'
        if not isinstance(p.get('reviewer'), str) or not p['reviewer'].strip():
            return 'Candidate settlement proof needs an identified reviewer'
        if p.get('contract_hash') != contract_proof_hash(basket, contracts):
            return 'Contract definitions or leg mapping changed'
        current = at or now()
        reviewed, expires = timestamp(p['reviewed_at']), timestamp(p['expires_at'])
        if not reviewed <= current < expires <= reviewed + timedelta(hours=72):
            return 'Candidate settlement proof expired or has invalid lifetime'
        rules = p['rules_review']
        if (rules.get('complete') is not True or rules.get('other_matches_zero') is not True
                or not isinstance(rules.get('text'), str) or not rules['text'].strip()
                or rules.get('sha256') != hashlib.sha256(rules['text'].encode()).hexdigest()
                or not str(rules.get('source_url', '')).startswith('https://')):
            return 'Full settlement rules and category mapping not reviewed'
        rows = p['candidate_rows']
        ids = [str(l['exchange_id']) for l in basket['legs']]
        if max_simultaneous_yes(rows, ids) > 1:
            return 'Candidate can resolve more than one leg YES'
        candidates = p['certified_candidates']
        if (p.get('enumeration_complete') is not True or not isinstance(candidates, list)
                or not candidates or any(not isinstance(i, str) or not i for i in candidates)
                or len(set(candidates)) != len(candidates)
                or set(rows) != set(candidates) | {'write_in_other'}
                or 'write_in_other' in candidates):
            return 'Certified candidate enumeration is incomplete'
        sources = p['sources']
        if not isinstance(sources, list) or not sources:
            return 'Official certification missing'
        covered = set()
        for s in sources:
            if (s.get('tier') != 'A' or s.get('fetched') is not True
                    or s.get('official') is not True or s.get('kind') != 'certification'
                    or not str(s.get('url', '')).startswith('https://')
                    or len(s.get('sha256', '')) != 64
                    or any(c not in '0123456789abcdef' for c in s['sha256'])
                    or not s.get('source_date')):
                return 'Only fetched official certification supports new entries'
            fetched = timestamp(s['fetched_at'])
            if not current - timedelta(hours=72) <= fetched <= reviewed:
                return 'Official certification fetch is stale or unreviewed'
            source_date = datetime.strptime(s['source_date'], '%Y-%m-%d').date()
            if source_date > fetched.date():
                return 'Certification publication date is after its fetch'
            if not isinstance(s.get('candidate_ids'), list):
                return 'Certification does not enumerate candidates'
            covered.update(s['candidate_ids'])
        if covered != set(candidates):
            return 'Certification coverage differs from candidate matrix'
        return None
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, Halt):
        return 'Malformed candidate settlement proof'


def tick(price):
    if not math.isfinite(price) or not .005 <= price <= .995 or abs(price * 200 - round(price * 200)) > 1e-7:
        raise Halt('Invalid limit price; market orders prohibited')
    return round(price, 3)


def qty(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2147483647:
        raise Halt('Quantity must be a positive integer')
    return value


def risk_limits(config):
    """Explicit profile; absent configuration preserves legacy authorization."""
    legacy = dict(committed=MAX_COMMITTED, reserve=RESERVE, basket=BASKET_CAP,
                  clip=1000, race=DIRECTIONAL_RACE, directional=DIRECTIONAL_TOTAL,
                  factor=FACTOR_CAP)
    supplied = config.get('risk_limits', {})
    if not isinstance(supplied, dict) or set(supplied) - set(legacy):
        raise Halt('Invalid risk profile keys')
    limits = dict(legacy, **supplied)
    # Owner authorized deploying the full 100k account (18 Oct 2026).
    ceilings = dict(committed=100000, basket=15000, clip=5000, race=5000,
                    directional=75000, factor=75000)
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise Halt('Risk limits must be positive integers')
        if key in ceilings and value > ceilings[key]:
            raise Halt('Risk profile exceeds delegated staged ceiling')
    if (limits['reserve'] < 1000 or limits['committed'] > 100000
            or not limits['race'] <= limits['factor'] <= limits['directional'] <= limits['committed']
            or limits['basket'] > limits['committed']):
        raise Halt('Inconsistent risk profile')
    return limits


def connection_category(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, TimeoutError):
        return 'timeout'
    if isinstance(reason, socket.gaierror):
        return 'dns'
    if isinstance(reason, ssl.SSLError):
        return 'tls'
    if isinstance(reason, ConnectionError):
        return 'disconnected'
    if isinstance(reason, http.client.HTTPException):
        return 'http_transport'
    return 'transport'


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

    def request(self, method, path, payload=None, attempts=None):
        if not path.startswith('/') or '://' in path:
            raise Halt('Only supported SIG /api/v1 paths permitted')
        bucket = 'GET' if method == 'GET' else 'WRITE'
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        # Only reads can retry here. Writes retain their durable reconciliation path.
        attempts = 1 if method != 'GET' else (attempts or 3)
        for attempt in range(attempts):
            delay = max(0, self.last[bucket] + (1.2 if bucket == 'GET' else 2.2) - time.monotonic())
            if delay:
                time.sleep(delay)
            self.last[bucket] = time.monotonic()
            started = time.monotonic()
            phase = 'connect_or_headers'
            category = 'unknown'
            try:
                with self.opener.open(req, timeout=20) as response:
                    phase = 'response_body'
                    return json.load(response)
            except urllib.error.HTTPError as e:
                code = e.code
                category = f'http_{code}'
                e.close()
                retryable = code in (429, 500, 502, 503, 504)
                failure = f'SIG {method} HTTP {code}'
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
                category = connection_category(error)
                retryable = True
                failure = f'SIG {method} connection failed'
            except (ValueError, UnicodeError):
                category = 'invalid_json'
                retryable = False
                failure = f'SIG {method} invalid JSON response'
            # Log only fixed categories and timing; never URLs, headers or exception text.
            print(json.dumps({'event': 'sig_request_failure', 'operation': bucket,
                              'phase': phase, 'category': category, 'attempt': attempt + 1,
                              'elapsed_seconds': round(time.monotonic() - started, 3)}), flush=True)
            # Never expose credentials, headers, server bodies or exception strings.
            if method == 'GET' and retryable and attempt + 1 < attempts:
                # SIG's 429 window outlasts a 1-2s pause; back off for real.
                time.sleep(min(30, 8 * (attempt + 1)) if category in ('http_429', 'http_503')
                           else min(30, 2 ** attempt))
                continue
            if method == 'GET':
                raise Halt(f'{failure}; read failed after {attempt + 1} attempt(s)') from None
            raise Halt(f'{failure}; write outcome may be unknown; reconcile before retry') from None

    def get(self, path, attempts=None):
        # Subclasses (read-only reconciliation) keep the plain request signature.
        if attempts is None:
            return self.request('GET', path)
        return self.request('GET', path, attempts=attempts)

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

    def jobs(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM jobs')]

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


def verified_fee(config):
    c = config['costs']
    if c.get('verified') is not True or not c.get('source'):
        raise Halt('Document fee schedule in config before enabling live placements')
    fee = float(c['fee_per_share'])
    if not math.isfinite(fee) or fee < 0:
        raise Halt('Invalid fee allowance')
    return fee


def cost_allowance(config):
    return verified_fee(config) + .005  # one extra tick per leg for implementation sensitivity


REDEPLOY_ROI_CAP = .10


def arb_policy(config):
    """Validated probability-aware arb settings, or None for legacy behaviour."""
    p = config.get('arb')
    if p is None or (isinstance(p, dict) and p.get('enabled') is False):
        return None
    if not isinstance(p, dict) or p.get('enabled') is not True:
        raise Halt('Invalid arb policy')
    out = {'min_edge_per_share': p.get('min_edge_per_share', .005),
           'min_profit': p.get('min_profit', 1),
           'p_double_yes': p.get('p_double_yes', .002),
           'leg_buffer': p.get('leg_buffer', 'empirical'),
           'redeploy_haircut': p.get('redeploy_haircut', .5),
           'universe_races_per_sweep': p.get('universe_races_per_sweep', 0),
           'max_jobs_per_run': p.get('max_jobs_per_run', 1)}
    bounds = {'min_edge_per_share': (0, .05), 'min_profit': (0, 100), 'p_double_yes': (0, .05),
              'redeploy_haircut': (0, 1), 'universe_races_per_sweep': (0, 200),
              'max_jobs_per_run': (1, 5)}
    for k, (lo, hi) in bounds.items():
        v = out[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise Halt('Invalid arb policy value: ' + k)
    b = out['leg_buffer']
    if b != 'empirical' and (isinstance(b, bool) or not isinstance(b, (int, float))
                             or not math.isfinite(b) or not 0 <= b <= .02):
        raise Halt('Invalid arb leg buffer')
    return out


def arb_charges(config, policy, n):
    """(per-share charge, fixed per-job charge, leg-risk detail) for an n-leg job."""
    fee = verified_fee(config)
    if policy['leg_buffer'] == 'empirical':
        risk = arb_engine.leg_risk(config.get('_execution_stats', {}),
                                   min(10., float(config.get('repair_loss_budget', 10))))
        return n * fee, risk['expected_loss_per_job'], risk
    return n * (fee + policy['leg_buffer']), 0., {'flat_per_leg': policy['leg_buffer']}


def defensive_entry_reason(state, config, basket, contracts):
    """Entry-only controls; held exits and recovery remain independent."""
    policy = config.get('defensive_entries')
    if policy is None:
        return None
    if not isinstance(policy, dict) or policy.get('enabled') is not True:
        return 'Defensive entry policy missing or disabled'
    required = ('account_value_floor', 'max_entry_clip', 'max_new_basket_cost',
                'min_settlement_roi', 'max_round_trip_loss_fraction')
    if any(isinstance(policy.get(k), bool) or not isinstance(policy.get(k), (int, float))
           or not math.isfinite(policy[k]) or policy[k] <= 0 for k in required):
        return 'Invalid defensive entry budgets'
    if (not isinstance(policy['max_entry_clip'], int) or policy['max_entry_clip'] > 250
            or policy['max_new_basket_cost'] > 1000 or policy['min_settlement_roi'] < .03
            or policy['max_round_trip_loss_fraction'] > .01):
        return 'Defensive policy exceeds tightened ceilings'
    pnl = state.get('pnl', {})
    value = pnl.get('totalAccountValue')
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < policy['account_value_floor']):
        return 'Account value missing or below defensive entry floor'
    ids = {str(l['exchange_id']) for l in basket['legs']}
    if any(str(p['exchangeId']) in ids and p['quantity'] for p in state['positions']):
        return 'Defensive policy prohibits adding to a held basket'
    reviewed = dict(basket, entry_proof_required=True)
    return entry_proof_reason(reviewed, contracts)


def defensive_price_reason(config, quantity, spend, floor, liquidation):
    policy = config.get('defensive_entries')
    if policy is None:
        return None
    if quantity > policy['max_entry_clip'] or spend > policy['max_new_basket_cost']:
        return 'Defensive initial position limit exceeded'
    if floor - spend < spend * policy['min_settlement_roi'] - 1e-8:
        return 'Conditional settlement margin below defensive minimum'
    if spend - liquidation > spend * policy['max_round_trip_loss_fraction'] + 1e-8:
        return 'Executable round-trip loss exceeds defensive budget'
    return None


def value_policy(config):
    """Validated Kalshi/Polymarket-anchored settings, or None when disabled."""
    p = config.get('value')
    if p is None or (isinstance(p, dict) and p.get('enabled') is False):
        return None
    if not isinstance(p, dict) or p.get('enabled') is not True:
        raise Halt('Invalid value policy')
    out = {'min_edge': p.get('min_edge', .03), 'exit_edge': p.get('exit_edge', .01),
           'race_cap': p.get('race_cap', 3000), 'min_profit': p.get('min_profit', 5),
           'max_fair_age_seconds': p.get('max_fair_age_seconds', 900),
           'hold_edge': p.get('hold_edge', None)}
    bounds = {'min_edge': (.01, .5), 'exit_edge': (0, .5), 'race_cap': (1, 5000),
              'min_profit': (0, 1000), 'max_fair_age_seconds': (30, 3600)}
    if out['hold_edge'] is not None:
        bounds['hold_edge'] = (0, .2)
    for k, (lo, hi) in bounds.items():
        v = out[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise Halt('Invalid value policy value: ' + k)
    return out


def value_plan(api, state, config, race, books=None, entry_observer=None):
    """Trade one clean D/R race against the outside-venue fair band.

    Exits first: sell a held side when SIG bids above the band by exit_edge.
    Entries: buy the side SIG offers below the band by min_edge, walking depth
    within the race cap, directional cap, committed limit and cash reserve.
    """
    policy = value_policy(config)
    if policy is None:
        return None
    observe = entry_observer or (lambda reason: None)
    fair_all = config.get('_fair') or {}
    try:
        age = (now() - timestamp(fair_all['at'])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= age <= policy['max_fair_age_seconds']:
        return None
    fair = fair_all.get('legs', {})
    ids = [str(l['exchange_id']) for l in race['legs']]
    if not any(i in fair for i in ids):
        return None
    if books is None or any((now() - timestamp(b['_received_at'])).total_seconds() > 60
                            for b in books.values()):
        books = fresh_books(api, race['legs'])
    risk = risk_limits(config)
    positions = signed_positions(state)
    rows = {str(p['exchangeId']): p for p in state['positions']}
    race_cost = sum(float(rows[i]['costBasis']) for i in ids if i in rows)
    committed, _, directional = portfolio_risk(state, config)
    best = blocked = None
    for leg in race['legs']:
        i, f, pos = str(leg['exchange_id']), fair.get(str(leg['exchange_id'])), positions.get(str(leg['exchange_id']), 0)
        if not f or not pos:
            continue
        side = 'yes' if pos > 0 else 'no'
        _, mid, sell_value = value_engine.side_fair(f, side)
        # Sell when SIG overpays (above the fair band), or - if hold_edge is set -
        # as soon as holding is worth less than hold_edge/share more than the bid.
        # Outside venues reprice on news first, so a thesis that news has broken
        # is exited at roughly fair instead of being held to settlement.
        floor = sell_value + policy['exit_edge']
        reason = 'overpriced'
        if policy['hold_edge'] is not None and mid - policy['hold_edge'] < floor:
            floor, reason = mid - policy['hold_edge'], 'edge_gone'
        q, proceeds, worst = value_engine.sell_walk(arb_engine.ladder(books[i], side, 'sell'),
                                                    floor, min(int(abs(pos)), risk['clip']))
        if q and q * worst < policy['min_profit']:
            q = 0  # no dust sales
        gain = proceeds - q * mid
        if q and (best is None or gain > best['robust_profit']):
            if reason == 'edge_gone' and worst >= sell_value + policy['exit_edge']:
                reason = 'overpriced'
            best = {'kind': 'value_exit', 'name': race['name'], 'action': 'sell', 'side': side,
                    'quantity': q, 'legs': [leg], 'prices': [tick(worst)], 'robust_profit': gain,
                    'expected_profit': gain, 'fair': f, 'race': race, 'exit_reason': reason}
    if best is None:
        if config.get('entries_paused', False) is not False:
            return None
        # room: caps that selling other holdings cannot lift (race cap, directional).
        # budget additionally respects cash and committed, which an exit DOES free.
        room = min(policy['race_cap'] - race_cost, risk['directional'] - directional)
        budget = min(room, risk['committed'] - committed, state['cash'] - risk['reserve'])
        for leg in race['legs']:
            i = str(leg['exchange_id'])
            f, pos = fair.get(i), positions.get(i, 0)
            if not f or room <= 0:
                continue
            for side in ('yes', 'no'):
                if pos and (pos > 0) != (side == 'yes'):
                    continue  # never net against an opposite holding
                buy_value, mid, _ = value_engine.side_fair(f, side)
                ladder = arb_engine.ladder(books[i], side, 'buy')
                limit = buy_value - policy['min_edge']
                q, cost, worst = value_engine.buy_walk(ladder, limit, max(budget, 0), risk['clip'])
                if q and q * worst > budget:
                    q = int(budget // worst)
                    cost = min(cost, q * worst)
                robust, expected = q * buy_value - cost, q * mid - cost
                if not q or robust < policy['min_profit']:
                    if budget < room:
                        # Cash/committed-bound: report the edge so a lower-ROI arb
                        # basket can be released to fund it (opportunity-cost exit).
                        uq, ucost, _ = value_engine.buy_walk(ladder, limit, room, risk['clip'])
                        urobust = uq * buy_value - ucost
                        if uq and urobust >= policy['min_profit'] and (
                                blocked is None or urobust / ucost > blocked['roi']):
                            blocked = {'event': 'capital_blocked', 'basket': race['name'],
                                       'source': 'value', 'side': side, 'roi': urobust / ucost,
                                       'quantity': uq, 'robust_profit': urobust,
                                       'capacity': budget, 'shortfall': ucost - max(budget, 0)}
                    continue
                if best is None or expected > best['expected_profit']:
                    best = {'kind': 'value_entry', 'name': race['name'], 'action': 'buy',
                            'side': side, 'quantity': q, 'legs': [leg], 'prices': [tick(worst)],
                            'max_spend': q * worst, 'robust_profit': robust,
                            'expected_profit': expected, 'fair': f, 'race': race}
    if best is None:
        if blocked is not None:
            observe(blocked)
        return None
    best['contracts'] = validate_contracts(api, race)
    observe({'event': 'value_candidate', 'basket': race['name'],
             **{k: best[k] for k in ('kind', 'side', 'quantity', 'prices', 'robust_profit', 'expected_profit', 'fair')},
             'exit_reason': best.get('exit_reason')})
    best['objective'] = 'buy below / sell above Kalshi-Polymarket fair band; directional settlement risk'
    return best


def arb_basket_plan(api, state, config, basket, policy, entry_observer=None):
    """Depth-optimal, probability-aware version of basket_plan.

    Entry: maximise q * (E[payout] - sum(worst NO ask) - fees) - E[execution loss]
    over every depth breakpoint, with E[payout] = (n-1) - P(double YES).
    Exit: sell only if worst-level proceeds beat the settlement floor discounted
    at the best capital-blocked arb ROI (zero when capital is not binding).
    """
    observe = entry_observer or (lambda reason: None)
    risk = risk_limits(config)
    contracts = validate_contracts(api, basket)
    committed, costs, directional = portfolio_risk(state, config)
    positions = signed_positions(state)
    ids = [str(l['exchange_id']) for l in basket['legs']]
    held = -positions.get(ids[0], 0)
    books = fresh_books(api, basket['legs'])
    n = len(ids)
    per_share, fixed, leg_risk = arb_charges(config, policy, n)
    settlement_tier = arb_engine.tier(basket)
    fair = arb_engine.fair_value(books)
    if held > 0:
        rows = [p for p in state['positions'] if str(p['exchangeId']) in ids]
        redeploy = float(config.get('_redeploy_roi', 0) or 0)
        hold_value = arb_engine.hold_threshold(n - 1, redeploy, policy['redeploy_haircut'])
        cap = min(int(held), risk['clip'])
        need = float(config.get('_redeploy_need', 0) or 0)
        if redeploy > 0 and need > 0 and hold_value > 0:
            # Release only what the blocked trade needs (10% slack, 250-share
            # floor so the fixed charge is covered); surplus cash would only be
            # redeployed at a lower ROI than the one that justified the discount.
            cap = min(cap, max(250, math.ceil(1.1 * need / hold_value)))
        exit_ = arb_engine.optimal_exit([arb_engine.ladder(books[i], 'no', 'sell') for i in ids],
                                        hold_value, per_share, cap)
        if exit_ is not None and exit_['surplus'] < fixed:
            exit_ = None
        observe({'event': 'held_basket_diagnostic', 'basket': basket['name'],
                 'account_at': state['at'], 'matched_shares': held,
                 'cost_basis': sum(float(p['costBasis']) for p in rows),
                 'marked_value': sum(float(p.get('marketValue', 0)) for p in rows),
                 'conditional_floor': held * (n - 1), 'hold_value_per_share': hold_value,
                 'redeploy_roi': redeploy, 'best_exit': exit_, 'fair_value': fair,
                 'floor_is_conditional_not_realized_profit': True})
        if exit_ is not None:
            q = exit_['quantity']
            return {'kind': 'basket_exit', 'name': basket['name'], 'action': 'sell',
                    'quantity': q, 'legs': basket['legs'],
                    'prices': [tick(x) for x in exit_['limits']],
                    'value_floor': q * hold_value, 'normal_settlement_floor': q * (n - 1),
                    'net_limit_proceeds': exit_['net_proceeds'], 'contracts': contracts,
                    'robust_profit': exit_['surplus'],
                    'exit_method': 'opportunity_cost' if redeploy > 0 else 'settlement_floor',
                    'redeploy_roi': redeploy}
    if config.get('entries_paused', False) is not False:
        observe('New entries paused for drawdown review')
        return None
    needs_proof = entry_proof_required(basket) or settlement_tier != 'standard'
    reason = (defensive_entry_reason(state, config, basket, contracts)
              or (entry_proof_reason(dict(basket, entry_proof_required=True), contracts)
                  if needs_proof else None))
    if reason is not None:
        observe(reason)
        return None
    payout = arb_engine.expected_payout(n, policy['p_double_yes'])
    ladders = [arb_engine.ladder(books[i], 'no', 'buy') for i in ids]
    max_q = min(qty(basket.get('clip', 100)), risk['clip'])

    def size(limit_q):
        return arb_engine.optimal_entry(ladders, payout, per_share, fixed, limit_q,
                                        policy['min_edge_per_share'], policy['min_profit'])
    best = size(max_q)
    if best is None:
        tops = [l[0][0] if l else None for l in ladders]
        observe({'event': 'arb_no_edge', 'basket': basket['name'], 'best_no_asks': tops,
                 'expected_payout': payout, 'fair_value': fair})
        return None
    capacity = min(risk['committed'] - committed, risk['basket'] - costs.get(basket['name'], 0),
                   state['cash'] - risk['reserve'])
    unconstrained = best
    while best is not None and best['robust_spend'] > capacity + 1e-9:
        smaller = int(capacity // (best['robust_spend'] / best['quantity']))
        best = size(smaller) if 1 <= smaller < best['quantity'] else None
    if best is None:
        observe({'event': 'capital_blocked', 'basket': basket['name'],
                 'roi': unconstrained['roi'], 'quantity': unconstrained['quantity'],
                 'robust_profit': unconstrained['robust_profit'], 'capacity': capacity,
                 'shortfall': unconstrained['robust_spend'] - max(capacity, 0)})
        return None
    q = best['quantity']
    return {'kind': 'basket_entry', 'name': basket['name'], 'action': 'buy', 'quantity': q,
            'legs': basket['legs'], 'prices': [tick(x) for x in best['limits']],
            'max_spend': best['robust_spend'], 'value_floor': q * (n - 1),
            'robust_profit': best['robust_profit'], 'expected_profit': best['expected_profit'],
            'roi': best['roi'], 'settlement_tier': settlement_tier,
            'p_double_yes': policy['p_double_yes'], 'leg_risk': leg_risk, 'fair_value': fair,
            'contracts': contracts,
            'objective': 'normal-settlement value; no fast-exit guarantee',
            'mechanism': 'Distinct party-winner claims in separate books; third-party win increases NO payout',
            'falsification': 'Changed settlement/party definitions, incomplete fills, cancellation or N/A'}


def basket_plan(api, state, config, basket, entry_observer=None):
    policy = arb_policy(config)
    if policy is not None:
        return arb_basket_plan(api, state, config, basket, policy, entry_observer)
    risk = risk_limits(config)
    allowance = cost_allowance(config)
    contracts = validate_contracts(api, basket)
    committed, costs, directional = portfolio_risk(state, config)
    positions = signed_positions(state)
    held = -positions.get(str(basket['legs'][0]['exchange_id']), 0)
    books = fresh_books(api, basket['legs'])
    n = len(basket['legs'])
    if entry_observer is not None and held > 0:
        ids = {str(l['exchange_id']) for l in basket['legs']}
        rows = [p for p in state['positions'] if str(p['exchangeId']) in ids]
        clip = min(int(held), risk['clip'])
        try:
            liquidation = clip * (sum(price_for(books[str(l['exchange_id'])], 'no', 'sell', clip)[0]
                                      for l in basket['legs']) - n * allowance)
        except Halt:
            liquidation = None
        entry_observer({'event': 'held_basket_diagnostic', 'basket': basket['name'],
                        'account_at': state['at'], 'matched_shares': held,
                        'cost_basis': sum(float(p['costBasis']) for p in rows),
                        'marked_value': sum(float(p.get('marketValue', 0)) for p in rows),
                        'conditional_floor': held * (n - 1), 'tested_exit_clip': clip,
                        'net_exit_limit_proceeds': liquidation,
                        'exit_floor': clip * (n - 1),
                        'floor_is_conditional_not_realized_profit': True})
    if held > 0:
        # A thin book must not hide a profitable matched partial exit. Try the
        # complete holding first, then smaller balanced clips; preserve the hedge.
        exit_q = min(int(held), risk['clip'])
        while exit_q > 0:
            try:
                limits = [price_for(books[str(l['exchange_id'])], 'no', 'sell', exit_q)[0]
                          for l in basket['legs']]
                proceeds = exit_q * (sum(limits) - n * allowance)
                turnover = config.get('turnover', {})
                target = profit_exit_target(state, basket, exit_q, turnover)
                if target and proceeds >= target['net_proceeds_target']:
                    if entry_observer is not None:
                        entry_observer({'event': 'turnover_exit_candidate', 'basket': basket['name'],
                                        'quantity': exit_q, 'net_proceeds': proceeds, **target,
                                        'execution_enabled': turnover.get('mode') == 'live'})
                    if turnover.get('mode') == 'live':
                        return {'kind': 'basket_exit', 'name': basket['name'], 'action': 'sell',
                                'quantity': exit_q, 'legs': basket['legs'], 'prices': limits,
                                'value_floor': target['net_proceeds_target'],
                                'normal_settlement_floor': exit_q * (n - 1),
                                'net_limit_proceeds': proceeds, 'contracts': contracts,
                                'exit_method': 'cost_based_profit_take', **target}
                if proceeds >= exit_q * (n - 1):
                    return {'kind': 'basket_exit', 'name': basket['name'], 'action': 'sell',
                            'quantity': exit_q, 'legs': basket['legs'], 'prices': limits,
                            'value_floor': exit_q * (n - 1), 'net_limit_proceeds': proceeds,
                            'contracts': contracts}
            except Halt:
                pass
            exit_q //= 2
    if config.get('entries_paused', False) is not False:
        if entry_observer is not None:
            entry_observer('New entries paused for drawdown review')
        return None  # Keep held-contract checks and matched exits above active.
    proof_reason = (defensive_entry_reason(state, config, basket, contracts)
                    or entry_proof_reason(basket, contracts))
    if proof_reason is not None:
        if entry_observer is not None:
            entry_observer(proof_reason)
        return None  # Entry quarantine must not suppress matched exits above.
    # Small default clip sized from displayed depth and surviving edge, not caps.
    q = min(qty(basket.get('clip', 100)), risk['clip'])
    if config.get('defensive_entries') is not None:
        q = min(q, qty(config['defensive_entries']['max_entry_clip']))
    while q > 0:
        try:
            limits = [price_for(books[str(l['exchange_id'])], 'no', 'buy', q)[0] for l in basket['legs']]
        except Halt:
            q //= 2
            continue
        spend = q * (sum(limits) + n * allowance)
        profit = q * (n - 1) - spend
        if config.get('defensive_entries') is not None:
            try:
                sell_limits = [price_for(books[str(l['exchange_id'])], 'no', 'sell', q)[0]
                               for l in basket['legs']]
                liquidation = q * (sum(sell_limits) - n * allowance)
            except Halt:
                liquidation = -float('inf')
            reason = defensive_price_reason(config, q, spend, q * (n - 1), liquidation)
            if reason:
                if entry_observer is not None:
                    entry_observer(reason)
                q //= 2
                continue
        if profit < max(1., q * .01):
            q //= 2
            continue
        dip_reason = dip_entry_reason(config, basket, contract_proof_hash(basket, contracts), q, spend, now())
        if dip_reason:
            if entry_observer is not None:
                entry_observer(dip_reason)
            return None
        if (committed + spend <= risk['committed'] and costs[basket['name']] + spend <= risk['basket']
                and state['cash'] - spend >= risk['reserve']):
            return {'kind': 'basket_entry', 'name': basket['name'], 'action': 'buy', 'quantity': q,
                    'legs': basket['legs'], 'prices': limits, 'max_spend': spend,
                    'value_floor': q * (n - 1), 'robust_profit': profit, 'contracts': contracts,
                    'objective': 'normal-settlement value; no fast-exit guarantee',
                    'mechanism': 'Distinct party-winner claims in separate books; third-party win increases NO payout',
                    'falsification': 'Changed settlement/party definitions, incomplete fills, cancellation or N/A'}
        q //= 2
    return None


def directional_plan(api, state, config, supplied):
    if config.get('defensive_entries') is not None:
        return None  # No validated directional adapter under defensive operation.
    if config.get('entries_paused', False) is not False:
        return None
    risk = risk_limits(config)
    c = copy.deepcopy(supplied)
    if c['trade']['side'] not in ('yes', 'no'):
        raise Halt('Invalid directional side')
    q = qty(c['trade']['quantity'])
    leg = {'market_id': str(c['contract']['market_id']), 'exchange_id': str(c['contract']['exchange_id'])}
    if leg['exchange_id'] in {str(l['exchange_id']) for b in config['baskets'] for l in b['legs']}:
        raise Halt('Directional entry would make a configured basket unmatched')
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
    c['risk'] = {'cash': state['cash'], 'minimum_cash': risk['reserve'],
                 'race_exposure': exposure, 'factor_exposure': exposure, 'total_exposure': exposure,
                 'max_race_loss': risk['race'], 'max_factor_loss': risk['factor'], 'max_total_loss': risk['directional']}
    c['trade']['minimum_edge'] = max(.05, float(c['trade'].get('minimum_edge', .05)))
    decision = evaluate(c)
    limit, _ = price_for(book, c['trade']['side'], 'buy', q)
    worst_loss = q * (limit + allowance)
    conservative = decision['probability_range'][0] - limit - allowance
    if (decision['decision'] != 'CANDIDATE' or conservative < .05 or worst_loss + exposure > risk['race']
            or committed + worst_loss > risk['committed'] or state['cash'] - worst_loss < risk['reserve']):
        return None
    return {'kind': 'directional_entry', 'name': c['contract']['question'], 'action': 'buy',
            'side': c['trade']['side'], 'quantity': q, 'legs': [leg], 'prices': [limit],
            'max_spend': worst_loss, 'decision': decision, 'case': c,
            'conservative_limit_edge': conservative}


def reconcile_cash(job, config):
    """Use decimal API amounts; preserve the existing one-cent allowance exactly."""
    def amount(value):
        result = Decimal(str(value))
        if not result.is_finite():
            raise Halt('Non-finite reconciliation amount')
        return result
    expected, shares = Decimal('0'), Decimal('0')
    results = sorted(job['response']['results'], key=lambda r: r['index'])
    pairs = [(leg, job.get('fills', {}).get(str(result['data'].get('orderId')), {}))
             for result, leg in zip(results, job['request']['legs'])]
    pairs += [(r['request'], r['fills']) for r in job['repairs']]
    for leg, projection in pairs:
        for fill in projection.get('data', []):
            q = abs(amount(fill['quantity']))
            expected += (1 if leg['action'] == 'sell' else -1) * q * amount(fill['price'])
            shares += q
    change = amount(job['after']['cash']) - amount(job['before']['cash'])
    extra = expected - change
    allowance = shares * amount(config['costs']['fee_per_share']) + Decimal('.01')
    if extra < Decimal('-.01') or extra > allowance:
        raise Halt('Fill/cash costs do not reconcile to verified fee allowance')
    job['cash_change'], job['observed_extra_cost'] = float(change), float(extra)
    return extra


class Runner:
    def __init__(self, api, journal, config, live=False):
        self.api, self.journal, self.config, self.live = api, journal, config, live

    def check_stop(self):
        if now() >= END or Path(self.config.get('kill_switch', 'private/STOP')).exists():
            raise Halt('Trading deadline or kill switch reached')

    def submit(self, plan, state):
        self.check_stop()
        if (self.config.get('entries_paused', False) is not False
                and not str(plan.get('kind', '')).endswith('_exit')):
            raise Halt('New entries paused for drawdown review')
        if (self.config.get('defensive_entries') is not None
                and plan.get('kind') not in ('basket_entry', 'basket_exit')):
            raise Halt('Defensive policy disables directional submissions')
        if str(plan.get('kind', '')).startswith('value') and value_policy(self.config) is None:
            raise Halt('Value trading is disabled')
        if plan.get('kind') == 'basket_entry':
            baskets = [b for b in self.config['baskets'] if b['name'] == plan.get('name')]
            if len(baskets) != 1 or baskets[0]['legs'] != plan.get('legs'):
                raise Halt('Basket entry does not match current configuration')
            reason = (defensive_entry_reason(state, self.config, baskets[0], plan.get('contracts', []))
                      or entry_proof_reason(baskets[0], plan.get('contracts', [])))
            if reason:
                raise Halt(reason)  # Before journal intent or any POST.
            if self.config.get('defensive_entries') is not None:
                fresh = basket_plan(self.api, state, self.config, baskets[0])
                if (fresh is None or fresh['kind'] != 'basket_entry'
                        or any(fresh.get(k) != plan.get(k) for k in ('quantity', 'legs', 'prices'))):
                    raise Halt('Defensive entry failed fresh executable-price validation')
        if plan.get('exit_method') == 'cost_based_profit_take':
            baskets = [b for b in self.config['baskets'] if b['name'] == plan.get('name')]
            if len(baskets) != 1 or self.config.get('turnover', {}).get('mode') != 'live':
                raise Halt('Turnover exit is not enabled for this basket')
            fresh = basket_plan(self.api, state, self.config, baskets[0])
            if (fresh is None or fresh.get('exit_method') != 'cost_based_profit_take'
                    or any(fresh.get(k) != plan.get(k) for k in
                           ('quantity', 'legs', 'prices', 'allocated_cost_basis', 'net_proceeds_target'))):
                raise Halt('Turnover exit failed fresh price and cost-basis validation')
        if qty(plan['quantity']) > risk_limits(self.config)['clip']:
            raise Halt('Execution clip exceeds configured matched shares')
        legs, prices = plan.get('legs'), plan.get('prices')
        if (not isinstance(legs, list) or not legs or not isinstance(prices, list)
                or len(legs) != len(prices)):
            raise Halt('Execution legs and prices must be complete and aligned')
        ids = [str(l.get('exchange_id', '')) for l in legs]
        if any(not i.isdecimal() for i in ids) or len(set(ids)) != len(ids):
            raise Halt('Execution exchanges must be unique numeric IDs')
        if plan.get('action') not in ('buy', 'sell') or plan.get('side', 'no') not in ('yes', 'no'):
            raise Halt('Invalid execution side or action')
        for price in prices:
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                raise Halt('Execution price must be numeric')
            tick(price)  # Reject before intent is journaled, even in dry-run mode.
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

    def reconcile_rounding_halt(self, job):
        """GET-only recovery of fully filled entries stopped by the legacy float bug."""
        self.check_stop()
        if (job['status'] != 'halted' or job.get('repairs') != []
                or job['plan']['kind'] != 'basket_entry' or 'after' not in job
                or self.config['costs']['fee_per_share'] != 0
                or not .01 < job.get('observed_extra_cost', 0) < .010000001):
            raise Halt('Unfinished repair requires reconciliation; no new orders')
        checked = copy.deepcopy(job)
        if reconcile_cash(checked, self.config) != Decimal('.01'):
            raise Halt('Halt is not the verified one-cent float boundary')
        legs = job['request']['legs']
        results = sorted(job['response']['results'], key=lambda r: r['index'])
        if [r['index'] for r in results] != list(range(len(legs))):
            raise Halt('Incomplete recovery response')
        ids = [str(l['exchangeId']) for l in legs]
        if len(set(ids)) != len(ids) or len(ids) < 2:
            raise Halt('Invalid recovery basket')
        for result, leg in zip(results, legs):
            if leg['side'] != 'no' or leg['action'] != 'buy' or leg['tournamentId'] != TID:
                raise Halt('Unsupported recovery leg')
            oid = result['data']['orderId']
            order = self.api.get(f'/orders/{oid}')
            order = order.get('data', order)
            if (order.get('open') is not False or str(order.get('exchangeId')) != str(leg['exchangeId'])
                    or order.get('side') != 'no' or order.get('action') != 'buy'
                    or order.get('quantity') != leg['quantity']
                    or Decimal(str(order.get('priceLimit'))) != Decimal(str(leg['price']))):
                raise Halt('Recovery order not verified terminal')
            fills = self.api.get(f'/orders/{oid}/fills?limit=100')
            if (fills.get('coverage', {}).get('complete') is not True
                    or fills.get('pagination', {}).get('hasMore') is not False
                    or str(fills.get('exchangeId')) != str(leg['exchangeId'])
                    or fills.get('tournamentId') != TID or fills.get('orderId') != oid
                    or fills.get('data') != job['fills'][str(oid)]['data']):
                raise Halt('Recovery fills incomplete or changed')
            quantities = [Decimal(str(f['quantity'])) for f in fills['data']]
            if (any(q >= 0 or not q.is_finite() for q in quantities)
                    or -sum(quantities) != Decimal(str(leg['quantity']))):
                raise Halt('Recovery requires full original fills')
        fresh = self.api.state()
        if (fresh['orders'] or Decimal(str(fresh['cash'])) != Decimal(str(job['after']['cash']))
                or signed_positions(fresh) != signed_positions(job['after'])):
            raise Halt('Account changed since halted execution; manual reconciliation required')
        before_q, after_q = signed_positions(job['before']), signed_positions(fresh)
        expected_q = dict(before_q)
        for leg in legs:
            i = str(leg['exchangeId'])
            expected_q[i] = expected_q.get(i, 0) - leg['quantity']
        if expected_q != after_q:
            raise Halt('Recovery position deltas do not match original fills')
        for basket in self.config['baskets']:
            if any(after_q.get(str(l['exchange_id']), 0) for l in basket['legs']):
                validate_contracts(self.api, basket)
        committed, race_cost, directional = portfolio_risk(fresh, self.config)
        risk = risk_limits(self.config)
        if (fresh['cash'] < risk['reserve'] or committed > risk['committed']
                or any(c > risk['basket'] for c in race_cost.values()) or directional > risk['directional']):
            raise Halt('Recovery exposure exceeds configured ceilings')
        self.journal.event('rounding_reconciliation_verified', {'job': job['id'], 'at': fresh['at'],
                           'dry_run': not self.live, 'orders_submitted': 0})
        if self.live:
            job.update(cash_change=checked['cash_change'], observed_extra_cost=checked['observed_extra_cost'])
            self.journal.save(job, 'done')
        return {'reconciled': job['id'], 'dry_run': not self.live, 'orders_submitted': 0}

    def reconcile_exposure_halt(self, job):
        """GET-only recovery of a fully filled basket job halted by the post-fill check.

        Every fill is re-verified as closed and unchanged, the account must equal the
        persisted post-trade snapshot exactly, and the exposure ceilings are then
        re-checked against the fully managed basket set. A real breach still halts.
        """
        self.check_stop()
        plan = job.get('plan') or {}
        action = plan.get('action')
        if (job.get('status') != 'halted' or job.get('repairs') != []
                or plan.get('kind') not in ('basket_entry', 'basket_exit') or 'after' not in job
                or action not in ('buy', 'sell') or self.config['costs']['fee_per_share'] != 0):
            raise Halt('Unfinished repair requires reconciliation; no new orders')
        checked = copy.deepcopy(job)
        reconcile_cash(checked, self.config)  # raises unless fills reconcile to cash
        legs = job['request']['legs']
        results = sorted(job['response']['results'], key=lambda r: r['index'])
        if [r['index'] for r in results] != list(range(len(legs))):
            raise Halt('Incomplete recovery response')
        ids = [str(l['exchangeId']) for l in legs]
        if len(set(ids)) != len(ids) or len(ids) < 2:
            raise Halt('Invalid recovery basket')
        for result, leg in zip(results, legs):
            if leg['side'] != 'no' or leg['action'] != action or leg['tournamentId'] != TID:
                raise Halt('Unsupported recovery leg')
            oid = result['data']['orderId']
            order = self.api.get(f'/orders/{oid}')
            order = order.get('data', order)
            if (order.get('open') is not False or str(order.get('exchangeId')) != str(leg['exchangeId'])
                    or order.get('side') != 'no' or order.get('action') != action
                    or order.get('quantity') != leg['quantity']
                    or Decimal(str(order.get('priceLimit'))) != Decimal(str(leg['price']))):
                raise Halt('Recovery order not verified terminal')
            fills = self.api.get(f'/orders/{oid}/fills?limit=100')
            if (fills.get('coverage', {}).get('complete') is not True
                    or fills.get('pagination', {}).get('hasMore') is not False
                    or str(fills.get('exchangeId')) != str(leg['exchangeId'])
                    or fills.get('tournamentId') != TID or fills.get('orderId') != oid
                    or fills.get('data') != job['fills'][str(oid)]['data']):
                raise Halt('Recovery fills incomplete or changed')
            quantities = [Decimal(str(f['quantity'])) for f in fills['data']]
            if (any(q >= 0 or not q.is_finite() for q in quantities)
                    or -sum(quantities) != Decimal(str(leg['quantity']))):
                raise Halt('Recovery requires full original fills')
        fresh = self.api.state()
        if (fresh['orders'] or Decimal(str(fresh['cash'])) != Decimal(str(job['after']['cash']))
                or signed_positions(fresh) != signed_positions(job['after'])):
            raise Halt('Account changed since halted execution; manual reconciliation required')
        expected_q = dict(signed_positions(job['before']))
        for leg in legs:
            i = str(leg['exchangeId'])
            # NO positions are negative: buying NO lowers, selling NO raises.
            expected_q[i] = expected_q.get(i, 0) + (leg['quantity'] if action == 'sell' else -leg['quantity'])
        expected_q = {i: q for i, q in expected_q.items() if q != 0}
        after_q = signed_positions(fresh)
        if expected_q != after_q:
            raise Halt('Recovery position deltas do not match original fills')
        for basket in self.config['baskets']:
            if any(after_q.get(str(l['exchange_id']), 0) for l in basket['legs']):
                validate_contracts(self.api, basket)
        committed, race_cost, directional = portfolio_risk(fresh, self.config)
        risk = risk_limits(self.config)
        if (fresh['cash'] < risk['reserve'] or committed > risk['committed']
                or any(c > risk['basket'] for c in race_cost.values()) or directional > risk['directional']):
            raise Halt('Recovery exposure exceeds configured ceilings')
        self.journal.event('exposure_reconciliation_verified', {'job': job['id'], 'at': fresh['at'],
                           'dry_run': not self.live, 'orders_submitted': 0})
        if self.live:
            job.update(cash_change=checked['cash_change'], observed_extra_cost=checked['observed_extra_cost'])
            self.journal.save(job, 'done')
        return {'reconciled': job['id'], 'dry_run': not self.live, 'orders_submitted': 0}

    def reconcile_halt(self, job):
        plan = job.get('plan', {})
        if (job.get('status') == 'halted' and plan.get('kind') == 'value_entry'
                and 'after' not in job):
            return self.reconcile_value_read_halt(job)
        if (job.get('status') == 'halted' and plan.get('kind') == 'basket_entry'
                and .01 < job.get('observed_extra_cost', 0) < .010000001):
            return self.reconcile_rounding_halt(job)
        if (job.get('status') == 'halted' and plan.get('kind') in ('basket_entry', 'basket_exit')
                and 'after' in job and job.get('repairs') == []):
            return self.reconcile_exposure_halt(job)
        return self.reconcile_rounding_halt(job)

    def reconcile_value_read_halt(self, job):
        """GET-only recovery of an accepted value entry halted during post-trade reads.

        Oct 20: a single-leg value order filled in full, then SIG returned 503 on the
        fill reads, so the job halted with no post-trade snapshot. Clear it only if the
        order is terminal, its fills are complete and equal the requested quantity, and
        the live account equals the pre-trade snapshot plus exactly those fills.
        """
        self.check_stop()
        legs = job['request']['legs']
        results = sorted(job.get('response', {}).get('results', []), key=lambda r: r['index'])
        if (job['status'] != 'halted' or job.get('repairs') != [] or 'after' in job
                or job['plan']['kind'] != 'value_entry' or len(legs) != 1 or len(results) != 1
                or results[0]['index'] != 0 or self.config['costs']['fee_per_share'] != 0):
            raise Halt('Unfinished repair requires reconciliation; no new orders')
        leg, data = legs[0], results[0]['data']
        if leg['side'] != 'no' or leg['action'] != 'buy' or leg['tournamentId'] != TID:
            raise Halt('Unsupported recovery leg')
        oid = data.get('orderId')
        if (oid is None or data.get('open') is not False
                or str(data.get('exchangeId')) != str(leg['exchangeId'])
                or data.get('quantityTraded') != leg['quantity']):
            raise Halt('Recovery response not a full terminal fill')
        order = self.api.get(f'/orders/{oid}')  # plain GET: ReadOnlyAPI keeps that signature
        order = order.get('data', order)
        if (order.get('open') is not False or str(order.get('exchangeId')) != str(leg['exchangeId'])
                or order.get('side') != 'no' or order.get('action') != 'buy'
                or order.get('quantity') != leg['quantity']
                or Decimal(str(order.get('priceLimit'))) != Decimal(str(leg['price']))):
            raise Halt('Recovery order not verified terminal')
        fills = self.api.get(f'/orders/{oid}/fills?limit=100')
        if (fills.get('coverage', {}).get('complete') is not True
                or fills.get('pagination', {}).get('hasMore') is not False
                or str(fills.get('exchangeId')) != str(leg['exchangeId'])
                or fills.get('tournamentId') != TID or fills.get('orderId') != oid):
            raise Halt('Recovery fills incomplete')
        quantities = [Decimal(str(f['quantity'])) for f in fills.get('data', [])]
        if (not quantities or any(q >= 0 or not q.is_finite() for q in quantities)
                or -sum(quantities) != Decimal(str(leg['quantity']))):
            raise Halt('Recovery requires full original fills')
        fresh = self.api.state()
        if fresh['orders']:
            raise Halt('Outstanding orders after execution; no further entries')
        expected_q = dict(signed_positions(job['before']))
        i = str(leg['exchangeId'])
        expected_q[i] = expected_q.get(i, 0) - leg['quantity']
        expected_q = {k: v for k, v in expected_q.items() if v}
        if expected_q != {k: v for k, v in signed_positions(fresh).items() if v}:
            raise Halt('Account changed since halted execution; manual reconciliation required')
        checked = copy.deepcopy(job)
        checked.update(fills={str(oid): fills}, after=fresh)
        reconcile_cash(checked, self.config)  # exact fill cost, existing one-cent allowance
        committed, race_cost, directional = portfolio_risk(fresh, self.config)
        risk = risk_limits(self.config)
        if (fresh['cash'] < risk['reserve'] or committed > risk['committed']
                or any(c > risk['basket'] for c in race_cost.values()) or directional > risk['directional']):
            raise Halt('Recovery exposure exceeds configured ceilings')
        self.journal.event('value_read_reconciliation_verified', {'job': job['id'], 'at': fresh['at'],
                           'dry_run': not self.live, 'orders_submitted': 0})
        if self.live:
            job.update(fills=checked['fills'], after=fresh, cash_change=checked['cash_change'],
                       observed_extra_cost=checked['observed_extra_cost'])
            self.journal.save(job, 'done')
        return {'reconciled': job['id'], 'dry_run': not self.live, 'orders_submitted': 0}

    def finish(self, job):
        self.check_stop()
        if job['status'] == 'halted':
            return self.reconcile_halt(job)
        if job['status'] == 'repairing':
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
                self.api.get(f'/orders/{d["orderId"]}/fills?limit=100', attempts=POST_TRADE_READS)
                order = self.api.get(f'/orders/{d["orderId"]}', attempts=POST_TRADE_READS)
                order = order.get('data', order)
                if order['open']:
                    self.api.request('DELETE', f'/orders/{d["orderId"]}')
                final_order = self.api.get(f'/orders/{d["orderId"]}', attempts=POST_TRADE_READS)
                final_order = final_order.get('data', final_order)
                if final_order['open']:
                    raise Halt('Unfilled remainder could not be cancelled')
                final_fills = self.api.get(f'/orders/{d["orderId"]}/fills?limit=100', attempts=POST_TRADE_READS)
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
            reconcile_cash(job, self.config)
            risk = risk_limits(self.config)
            if after['cash'] < risk['reserve']:
                raise Halt('Cash reserve breached after fills')
            committed, race_cost, directional = portfolio_risk(after, self.config)
            if (committed > risk['committed'] or any(c > risk['basket'] for c in race_cost.values())
                    or directional > risk['directional']):
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
            if action == 'buy' and (after['cash'] - q * (price + cost_allowance(self.config)) < risk_limits(self.config)['reserve']):
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

    def observer(self, basket, blocked):
        def record(reason):
            if isinstance(reason, dict) and reason.get('event') == 'capital_blocked' and reason.get('roi'):
                blocked.append(reason)
            self.journal.event('entry_blocked', {'basket': basket['name'], 'reason': reason})
        return record

    def universe(self, state, blocked):
        """Book-only prescreen of unheld auto-discovered D/R races, then full plan."""
        policy = arb_policy(self.config)
        plans = []
        for candidate in self.config.get('_universe', []):
            basket = dict(candidate, _prescreened=True)
            ids = [str(l['exchange_id']) for l in basket['legs']]
            if {str(l['exchange_id']) for b in self.config['baskets'] for l in b['legs']} & set(ids):
                continue
            try:
                books = fresh_books(self.api, basket['legs'])
                if value_policy(self.config) is not None:
                    try:
                        vp = value_plan(self.api, state, self.config, candidate, books,
                                        self.observer(basket, blocked))
                    except Halt as e:
                        vp = None
                        self.journal.event('value_abstain', {'basket': basket['name'], 'reason': str(e)})
                    if vp:
                        plans.append(vp)
                        continue  # one strategy per race per cycle
                if any(signed_positions(state).get(i, 0) for i in ids):
                    continue  # directional holding: no NO basket on top of it
                per_share, fixed, _ = arb_charges(self.config, policy, len(ids))
                tops = [arb_engine.ladder(books[i], 'no', 'buy') for i in ids]
                if any(not t for t in tops):
                    continue
                edge = arb_engine.expected_payout(len(ids), policy['p_double_yes']) - sum(t[0][0] for t in tops) - per_share
                self.journal.event('universe_prescreen', {'basket': basket['name'], 'edge_per_share': edge})
                if edge < policy['min_edge_per_share'] - 1e-9:
                    continue
                self.config['baskets'].append(basket)
                plan = basket_plan(self.api, state, self.config, basket,
                                   entry_observer=self.observer(basket, blocked))
                self.journal.event('screened', {'basket': basket['name'], 'candidate': bool(plan)})
                if plan:
                    plans.append(plan)
                else:
                    self.config['baskets'].remove(basket)
            except Halt as e:
                if basket in self.config['baskets']:
                    self.config['baskets'].remove(basket)
                self.journal.event('abstain', {'basket': basket['name'], 'reason': str(e)})
        return plans

    def drop_unused_auto(self, keep):
        """Auto baskets join the managed set only once selected for execution."""
        self.config['baskets'] = [b for b in self.config['baskets']
                                  if not b.get('_prescreened') or b['name'] == keep]
        # A selected basket is managed from now on. Clearing the flag stops a
        # later cycle in the same run from dropping it (its held legs would
        # then be misread as directional exposure and halt a clean fill).
        for b in self.config['baskets']:
            if b['name'] == keep:
                b.pop('_prescreened', None)

    def cycle(self, cases=None):
        self.check_stop()
        pending = self.journal.pending()
        if pending:
            if not self.live:
                return [self.reconcile_halt(job) for job in pending]
            return [self.finish(job) for job in pending]
        state = self.api.state()
        self.journal.event('review', state)
        if state['orders']:
            raise Halt('Existing resting orders found; no conflicting new placements')
        portfolio_risk(state, self.config)
        policy = arb_policy(self.config)
        blocked = []
        if policy is not None:
            self.config['_execution_stats'] = arb_engine.execution_stats(self.journal.jobs())
            self.config['_redeploy_roi'] = 0
            self.config['_redeploy_need'] = 0
        plans = []
        review_names = set(self.config.get('review_names', [b['name'] for b in self.config['baskets']]))
        for basket in self.config['baskets']:
            if basket['name'] not in review_names:
                continue
            self.journal.event('checking', {'basket': basket['name']})
            try:
                plan = basket_plan(self.api, state, self.config, basket,
                                   entry_observer=self.observer(basket, blocked))
                if plan:
                    plans.append(plan)
                self.journal.event('screened', {'basket': basket['name'], 'candidate': bool(plan)})
            except Halt as e:
                self.journal.event('abstain', {'basket': basket['name'], 'reason': str(e)})
                if any(signed_positions(state).get(str(l['exchange_id']), 0) for l in basket['legs']):
                    raise Halt('Held basket could not be verified: ' + basket['name']) from None
        if policy is not None:
            plans.extend(self.universe(state, blocked))
            if not plans and blocked:
                # Capital-bound: a held arb is worth releasing only if redeploying
                # its proceeds into the best blocked arb beats holding to settlement.
                top = max(blocked, key=lambda b: b['roi'])
                # Cap at 10%: a held arb is never dumped more than ~4.8% under its
                # floor (haircut .5), however rich a single blocked edge looks.
                self.config['_redeploy_roi'] = min(top['roi'], REDEPLOY_ROI_CAP)
                self.config['_redeploy_need'] = top.get('shortfall', 0)
                for basket in self.config['baskets']:
                    if any(signed_positions(state).get(str(l['exchange_id']), 0) for l in basket['legs']):
                        try:
                            plan = basket_plan(self.api, state, self.config, basket)
                        except Halt:
                            continue
                        if plan and plan['kind'] == 'basket_exit':
                            plans.append(plan)
        for case in (cases or [])[:3]:
            plan = directional_plan(self.api, state, self.config, case)
            if plan:
                plans.append(plan)
        # One job per cycle; always obtain a fresh state and prices on the next cycle.
        plans.sort(key=lambda p: (str(p['kind']).endswith('_exit'), p.get('robust_profit', p.get('conservative_limit_edge', 0))), reverse=True)
        if not plans:
            self.drop_unused_auto(None)
            return [{'decision': 'NO_TRADE', 'cash': state['cash']}]
        # Keep a prescreened basket only if the selected plan IS that basket: a
        # value trade on the same race must not leave a one-legged "hedge".
        self.drop_unused_auto(plans[0]['name'] if plans[0]['kind'].startswith('basket') else None)
        # Long scans must not use stale limits/state to submit. Rebuild selected plan.
        state = self.api.state()
        selected = plans[0]
        if selected['kind'].startswith('basket'):
            b = next(b for b in self.config['baskets'] if b['name'] == selected['name'])
            selected = basket_plan(self.api, state, self.config, b)
        elif selected['kind'].startswith('value'):
            selected = value_plan(self.api, state, self.config, selected['race'])
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
