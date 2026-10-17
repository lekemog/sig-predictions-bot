"""Probability-aware basket arbitrage maths. Pure functions: no network, no orders.

Mutually exclusive party-winner NO basket with n legs:
    payout = n - 1               normal settlement (exactly one listed party wins)
    payout = n                   an unlisted party wins (pure upside)
    payout = n - 2               one winner resolves TWO legs YES (the only loss path)
    refund                       cancellation / N/A (no loss at cost)

So the expected payout per matched share is
    E[payout] = (n - 1) + P(other) - P(double YES)
and the expected profit of a clip of q shares bought at worst-level limits L_i is
    q * (E[payout] - sum(L_i) - fee * n) - E[execution loss]
where E[execution loss] = P(partial fill) * repair cost, estimated from our own
journal with a Beta(1, 1) prior rather than a flat per-leg tick.

P(other) is ignored for entries (it only helps) so the decision stays a floor.
"""
import argparse
import json
import math
import re
from statistics import median

D, R = 'Democratic Party', 'Republican Party'
TITLE = re.compile(r'^Will the (.+?) win the (.+)\?$')


# ---------------------------------------------------------------- books

def ladder(book, side, action):
    """Best-first (price, quantity) levels for the side we trade.

    Books are YES-normalized: buying NO lifts YES bids at 1 - bid, selling NO
    hits YES asks at 1 - ask.
    """
    if side not in ('yes', 'no') or action not in ('buy', 'sell'):
        raise ValueError('Invalid side/action')
    source = 'asks' if (side == 'yes') == (action == 'buy') else 'bids'
    levels = []
    for row in book.get(source, []):
        price, size = float(row['price']), float(row['quantity'])
        if not (0 <= price <= 1) or not math.isfinite(size) or size <= 0:
            continue
        levels.append((round(price if side == 'yes' else 1 - price, 6), size))
    levels.sort(key=lambda x: x[0], reverse=(action == 'sell'))
    return levels


def walk(levels, q):
    """Return (worst price, notional) to trade q shares, or None if too thin."""
    remaining, notional, worst = q, 0.0, None
    for price, size in levels:
        take = min(remaining, size)
        if take > 0:
            notional += take * price
            worst = price
            remaining -= take
        if remaining <= 1e-9:
            return worst, notional
    return None


def breakpoints(ladders, max_q):
    """Quantities where some leg's worst price can change, plus max_q.

    Between breakpoints every leg's marginal price is constant, so profit is
    linear in q and its maximum sits at a breakpoint.
    """
    points = {int(max_q)}
    for levels in ladders:
        depth = 0
        for _, size in levels:
            depth += size
            if depth >= max_q:
                break
            points.add(int(math.floor(depth)))
    return sorted(p for p in points if p >= 1)


def optimal_entry(ladders, payout_per_share, charge_per_share, fixed_charge,
                  max_q, min_edge_per_share=0.0, min_profit=1.0):
    """Size a matched basket purchase by walking depth, not by halving.

    Robust profit uses each leg's WORST filled level (= the limit we send), so
    it is a guaranteed bound if the book only moves within our limits.
    expected_profit uses VWAP (what a sweeping limit actually pays).
    """
    best = None
    for q in breakpoints(ladders, max_q):
        walks = [walk(levels, q) for levels in ladders]
        if any(w is None for w in walks):
            continue
        limits = [w[0] for w in walks]
        robust_spend = q * (sum(limits) + charge_per_share) + fixed_charge
        expected_spend = sum(w[1] for w in walks) + q * charge_per_share + fixed_charge
        profit = q * payout_per_share - robust_spend
        if profit < max(min_profit, q * min_edge_per_share) - 1e-9:
            continue
        if best is None or profit > best['robust_profit'] + 1e-9:
            best = {'quantity': q, 'limits': limits, 'robust_spend': robust_spend,
                    'expected_spend': expected_spend, 'robust_profit': profit,
                    'expected_profit': q * payout_per_share - expected_spend,
                    'roi': profit / robust_spend if robust_spend > 0 else None}
    return best


def optimal_exit(ladders, hold_value_per_share, charge_per_share, held):
    """Largest-surplus matched sale whose worst-level proceeds beat holding."""
    best = None
    for q in breakpoints(ladders, held):
        walks = [walk(levels, q) for levels in ladders]
        if any(w is None for w in walks):
            continue
        limits = [w[0] for w in walks]
        proceeds = q * (sum(limits) - charge_per_share)
        surplus = proceeds - q * hold_value_per_share
        if surplus < -1e-9:
            continue
        if best is None or surplus > best['surplus'] + 1e-9:
            best = {'quantity': q, 'limits': limits, 'net_proceeds': proceeds,
                    'surplus': surplus}
    return best


# ---------------------------------------------------------------- probabilities

def execution_stats(jobs):
    """Count completed multi-leg basket jobs and those with unequal leg fills."""
    total = bad = 0
    for job in jobs if isinstance(jobs, list) else []:
        plan = job.get('plan') or {}
        if not str(plan.get('kind', '')).startswith('basket'):
            continue
        if job.get('status') in ('halted', 'repairing'):
            total += 1
            bad += 1
            continue
        if job.get('status') != 'done':
            continue
        legs = (job.get('request') or {}).get('legs') or []
        filled = {}
        for f in (job.get('fills') or {}).values():
            filled[str(f.get('exchangeId'))] = sum(abs(float(x['quantity'])) for x in f.get('data', []))
        requested = {str(l['exchangeId']): float(l['quantity']) for l in legs}
        total += 1
        if job.get('repairs') or any(abs(filled.get(i, 0) - q) > 1e-9 for i, q in requested.items()):
            bad += 1
    return {'jobs': total, 'partial': bad}


def leg_risk(stats, repair_budget, stress=2.0):
    """Expected execution loss per basket job.

    Beta(1,1) prior + our fill history: P(partial) = (partial + 1) / (jobs + 2).
    A partial costs at most the repair budget before the runner halts; stress
    doubles that so unrecovered tails are not free. Intuition: with 0 partials
    in 25 jobs the rate is ~1/27, not 0 - but nowhere near the old flat tick.
    """
    n = int(stats.get('jobs', 0)) if isinstance(stats, dict) else 0
    k = int(stats.get('partial', 0)) if isinstance(stats, dict) else 0
    p = (k + 1) / (n + 2)
    return {'p_partial': p, 'expected_loss_per_job': p * float(repair_budget) * stress,
            'jobs': n, 'partial': k}


def tier(basket):
    """'standard' = exactly one Democratic and one Republican leg, nothing else."""
    parties = sorted(l['party'] for l in basket['legs'])
    return 'standard' if parties == [D, R] else 'proof'


def expected_payout(n, p_double, p_other=0.0):
    return (n - 1) + p_other - p_double


def hold_threshold(hold_value_per_share, redeploy_roi, haircut):
    """Sell only if proceeds, redeployed at the best blocked arb ROI, beat holding.

    S * (1 + h * r) >= V  <=>  S >= V / (1 + h * r). With no capital constraint
    r = 0 and this is the settlement floor: never sell an arb for less than it pays.
    """
    r = max(0.0, float(redeploy_roi or 0)) * max(0.0, min(1.0, float(haircut)))
    return hold_value_per_share / (1 + r)


def microprice(book):
    """Size-weighted YES fair price: leans toward the side with less depth."""
    bids, asks = book.get('bids') or [], book.get('asks') or []
    if not bids or not asks:
        return None
    b = max(bids, key=lambda x: x['price'])
    a = min(asks, key=lambda x: x['price'])
    bq, aq = float(b['quantity']), float(a['quantity'])
    if bq + aq <= 0:
        return None
    return (float(b['price']) * aq + float(a['price']) * bq) / (bq + aq)


def fair_value(books):
    """De-vigged YES probabilities per leg from microprices (diagnostic only)."""
    micro = {i: microprice(b) for i, b in books.items()}
    if any(v is None for v in micro.values()):
        return None
    total = sum(micro.values())
    return {'microprice': micro, 'overround': total - 1,
            'fair_yes': {i: v / total for i, v in micro.items()} if total > 0 else None}


# ---------------------------------------------------------------- universe

def discover(catalog, configured):
    """Standard D-vs-R race baskets from the exchange catalog, minus configured ones."""
    taken = {str(l['exchange_id']) for b in configured for l in b['legs']}
    names = {b['name'] for b in configured}
    races = {}
    for exchange, row in (catalog or {}).items():
        m = TITLE.match(str(row.get('title', '')))
        if not m:
            continue
        races.setdefault(m.group(2), []).append(
            {'exchange_id': str(exchange), 'market_id': str(row['market_id']), 'party': m.group(1)})
    out = []
    for race, legs in sorted(races.items()):
        if sorted(l['party'] for l in legs) != [D, R]:
            continue
        if taken & {l['exchange_id'] for l in legs} or race in names:
            continue
        out.append({'name': race, 'clip': 1000, 'auto': True,
                    'legs': sorted(legs, key=lambda l: l['party'])})
    return out


def rotation(items, size, slot):
    """Deterministic slice of the universe so each sweep reads a bounded chunk."""
    if not items or size <= 0:
        return []
    if size >= len(items):
        return list(items)
    chunks = math.ceil(len(items) / size)
    k = int(slot) % chunks
    return list(items[k * size:(k + 1) * size])


# ---------------------------------------------------------------- report

def history_report(state):
    """Summarize recorded 250-share basket quotes: how often was there a raw arb?"""
    out = {}
    for name, rows in (state.get('dip_quote_history') or {}).items():
        if not rows:
            continue
        q, floor = rows[0]['quantity'], rows[0]['floor']
        n = round(floor / q) + 1
        # Recorded costs embed the legacy 0.005/leg buffer; remove it for raw prices.
        raw = [r['buy_cost'] / q - n * .005 for r in rows]
        bids = [r['sell_proceeds'] / q + n * .005 for r in rows]
        out[name] = {'legs': n, 'observations': len(rows), 'floor': n - 1,
                     'sum_no_ask_min': round(min(raw), 4), 'sum_no_ask_median': round(median(raw), 4),
                     'share_of_time_raw_arb': round(sum(x < n - 1 - 1e-9 for x in raw) / len(raw), 3),
                     'median_settlement_roi': round((n - 1) / median(raw) - 1, 4),
                     'sum_no_bid_max': round(max(bids), 4)}
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--state', default='.bot/state.json')
    args = p.parse_args()
    state = json.loads(open(args.state).read())
    print(json.dumps({'history': history_report(state),
                      'execution': leg_risk(execution_stats(state.get('jobs')), 10)}, indent=2))


if __name__ == '__main__':
    main()
