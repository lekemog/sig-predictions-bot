"""Cost-based whole-basket profit taking; no order transport."""
import math
from decision_engine import timestamp


def dip_entry_reason(config, basket, contracts_hash, quantity, spend, at):
    """Turnover mode permits entries only against a fresh, warmed dip reference.

    Called after current executable prices and normal defensive gates are rebuilt.
    The historical drop is an extra entry gate, never a fair-probability estimate.
    """
    p = config.get('turnover')
    if p is None:
        return None
    if not isinstance(p, dict) or p.get('mode') != 'live':
        return 'Turnover execution is observation-only'
    scan = config.get('_dip_scan', {})
    matches = [r for r in scan.get('baskets', []) if r.get('basket') == basket['name']]
    if len(matches) != 1:
        return 'Fresh executable dip reference missing'
    r = matches[0]
    try:
        if (not 0 <= (at - timestamp(r['at'])).total_seconds() <= 60
                or r['contract_hash'] != contracts_hash
                or r['status'] != 'dip' or r['entry_blockers']
                or r['price_and_entry_checks_pass'] is not True):
            return 'Dip reference stale, unqualified or blocked'
        baseline = r['reference_buy_cost'] / r['quantity']
        drop = baseline - spend / quantity
        if not math.isfinite(drop) or drop < config['dip_monitor']['min_drop_per_matched_share'] - 1e-9:
            return 'Executable price no longer qualifies as a dip'
    except (ValueError, TypeError, KeyError, ZeroDivisionError, OverflowError):
        return 'Invalid dip reference'
    return None


def profit_exit_target(state, basket, quantity, policy):
    """Return a target only with complete, matched, positive basis records.

    Cost basis is allocated pro rata to the matched shares being sold. Exit
    fees/slippage must already have been subtracted from executable proceeds.
    No assumption that either the old price or the settlement floor is reachable.
    """
    if not isinstance(policy, dict) or policy.get('mode') not in ('observe', 'live'):
        return None
    fraction, minimum = policy.get('profit_fraction'), policy.get('min_profit')
    if any(isinstance(x, bool) or not isinstance(x, (int, float))
           or not math.isfinite(x) or x <= 0 for x in (fraction, minimum)):
        return None
    if fraction < .005 or minimum < 1 or type(quantity) is not int or quantity <= 0:
        return None
    rows = []
    for leg in basket['legs']:
        matches = [p for p in state['positions'] if str(p['exchangeId']) == str(leg['exchange_id'])]
        if len(matches) != 1:
            return None
        row = matches[0]
        held, basis = row.get('quantity'), row.get('costBasis')
        if any(isinstance(x, bool) or not isinstance(x, (int, float))
               or not math.isfinite(x) for x in (held, basis)):
            return None
        if held >= 0 or -held < quantity or basis <= 0:
            return None
        rows.append(row)
    if len({row['quantity'] for row in rows}) != 1:
        return None
    cost = sum(row['costBasis'] * quantity / -row['quantity'] for row in rows)
    profit = max(minimum, cost * fraction)
    return {'allocated_cost_basis': cost, 'profit_target': profit,
            'net_proceeds_target': cost + profit,
            'method': 'matched basket cost basis plus net profit; not settlement payout'}
