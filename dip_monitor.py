"""Read-only executable-basket dip detection. Never submits orders.

A historical price drop is a screening condition, not a calibrated forecast.
Keep history in the executor's durable checkpoint, independent of event pruning.
"""
import math
from datetime import timedelta
from statistics import median

from decision_engine import timestamp
from trading_bot import (Halt, cost_allowance, contract_proof_hash,
                         defensive_entry_reason, defensive_price_reason,
                         fresh_books, now, price_for, validate_contracts)

DEFAULT_POLICY = {
    'enabled': True, 'mode': 'observe', 'quantity': 250,
    'lookback_hours': 24, 'min_samples': 3, 'min_span_minutes': 20,
    'min_gap_seconds': 300, 'max_quote_age_seconds': 60, 'max_gap_minutes': 90,
    'min_drop_per_matched_share': .02,
}


def policy_valid(p):
    """Bound history size and reject ambiguous/unsafe configurations."""
    if not isinstance(p, dict) or p.get('mode') != 'observe':
        return False
    for k in DEFAULT_POLICY.keys() - {'enabled', 'mode'}:
        v = p.get(k)
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0:
            return False
    return (type(p['quantity']) is int and p['quantity'] <= 250
            and type(p['min_samples']) is int and 3 <= p['min_samples'] <= 288
            and p['lookback_hours'] <= 24 and p['min_span_minutes'] >= 20
            and p['min_gap_seconds'] >= 300 and p['max_quote_age_seconds'] <= 60
            and p['max_gap_minutes'] <= 90
            and p['min_drop_per_matched_share'] >= .02)


def observe_price(history, quote, policy, at):
    """Compare against PRIOR synchronized observations, then append this quote.

    Warm-up, gaps, changed contract definitions, quantity changes and corrupt
    history cannot silently create an eligible signal. No current-quote leakage
    into its own reference price. Persistence is supplied by the hosted runner.
    """
    if not policy_valid(policy):
        raise ValueError('Invalid dip monitor policy')
    for k in ('buy_cost', 'sell_proceeds', 'floor'):
        v = quote.get(k)
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0:
            raise ValueError('Invalid executable quote')
    received = timestamp(quote['at'])
    if not 0 <= (at - received).total_seconds() <= policy['max_quote_age_seconds']:
        raise ValueError('Stale or future quote')
    if quote['quantity'] != policy['quantity'] or not quote.get('contract_hash'):
        raise ValueError('Quote identity or size mismatch')
    key = quote['contract_hash']
    valid = []
    for row in history if isinstance(history, list) else []:
        try:
            age = (received - timestamp(row['at'])).total_seconds()
            cost = row['buy_cost']
            if (row['contract_hash'] == key and row['quantity'] == quote['quantity']
                    and 0 < age <= policy['lookback_hours'] * 3600
                    and not isinstance(cost, bool) and isinstance(cost, (float, int))
                    and math.isfinite(cost) and cost > 0):
                valid.append(row)
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
    # One observation per slot, including restored or duplicate checkpoint data.
    unique = {}
    for row in sorted(valid, key=lambda r: timestamp(r['at'])):
        slot = int(timestamp(row['at']).timestamp() // policy['min_gap_seconds'])
        unique[slot] = row
    valid = list(unique.values())[-288:]
    if valid and (received - timestamp(valid[-1]['at'])).total_seconds() > policy['max_gap_minutes'] * 60:
        valid = []
    prior = [r for r in valid if (received - timestamp(r['at'])).total_seconds() >= policy['min_gap_seconds']]
    span = ((received - timestamp(prior[0]['at'])).total_seconds() / 60) if prior else 0
    warmed = len(prior) >= policy['min_samples'] and span >= policy['min_span_minutes']
    baseline = median(r['buy_cost'] for r in prior) if warmed else None
    drop = ((baseline - quote['buy_cost']) / quote['quantity']) if warmed else None
    dip = warmed and drop >= policy['min_drop_per_matched_share'] - 1e-9
    current_slot = int(received.timestamp() // policy['min_gap_seconds'])
    kept = [r for r in valid if int(timestamp(r['at']).timestamp() // policy['min_gap_seconds']) != current_slot]
    kept.append(quote)
    return kept[-288:], {
        'at': quote['at'], 'basket': quote['basket'], 'quantity': quote['quantity'],
        'contract_hash': quote['contract_hash'],
        'status': 'dip' if dip else ('no_dip' if warmed else 'warming_up'),
        'prior_samples': len(prior), 'history_span_minutes': span,
        'reference_buy_cost': baseline, 'current_buy_cost': quote['buy_cost'],
        'drop_per_matched_share': drop, 'net_exit_proceeds': quote['sell_proceeds'],
        'conditional_settlement_floor': quote['floor'],
        'price_drop_is_not_probability_or_rebound_prediction': True,
    }


def scan(api, state, config, previous, at=None):
    """GET-only sampling of configured baskets. Missing coverage stays explicit."""
    at = at or now()
    p = config.get('dip_monitor', {})
    if p.get('enabled') is not True:
        return previous, {'at': at.isoformat(), 'status': 'disabled', 'orders_submitted': 0}
    if not policy_valid(p):
        return previous, {'at': at.isoformat(), 'status': 'invalid_policy', 'orders_submitted': 0}
    result = {'at': at.isoformat(), 'status': 'complete', 'mode': 'observe',
              'orders_submitted': 0, 'execution_enabled': False, 'baskets': []}
    history = {}
    for basket in config['baskets']:
        name = basket['name']
        old = previous.get(name, []) if isinstance(previous, dict) else []
        try:
            contracts = validate_contracts(api, basket)
            books = fresh_books(api, basket['legs'])
            q, n = p['quantity'], len(basket['legs'])
            allowance = cost_allowance(config)
            buys = [price_for(books[str(l['exchange_id'])], 'no', 'buy', q)[0] for l in basket['legs']]
            sells = [price_for(books[str(l['exchange_id'])], 'no', 'sell', q)[0] for l in basket['legs']]
            observed_at = now()
            buy, sell = q * (sum(buys) + n * allowance), q * (sum(sells) - n * allowance)
            quote = {'at': observed_at.isoformat(), 'basket': name, 'quantity': q,
                     'contract_hash': contract_proof_hash(basket, contracts),
                     'buy_cost': buy, 'sell_proceeds': sell, 'floor': q * (n - 1)}
            history[name], row = observe_price(old, quote, p, observed_at)
            blockers = []
            if not 0 <= (observed_at - timestamp(state['at'])).total_seconds() <= 60:
                blockers.append('Account snapshot stale or future')
            if config.get('entries_paused', False) is not False:
                blockers.append('New entries paused')
            reason = defensive_entry_reason(state, config, basket, contracts)
            if reason:
                blockers.append(reason)
            # Require defensive gates even if someone removes their config.
            if config.get('defensive_entries') is None:
                blockers.append('Defensive policy missing')
            else:
                reason = defensive_price_reason(config, q, buy, quote['floor'], sell)
                if reason:
                    blockers.append(reason)
            row['entry_blockers'] = blockers
            row['price_and_entry_checks_pass'] = row['status'] == 'dip' and not blockers
            # An observation result NEVER authorizes an order, including when all
            # screening conditions pass. Existing execution ownership is intact.
            row['execution_enabled'] = False
            result['baskets'].append(row)
        except (Halt, ValueError, KeyError, TypeError, OverflowError) as error:
            # Discard stale history after a failed read rather than letting it
            # masquerade as continuous coverage on the next invocation.
            history[name] = []
            result['status'] = 'partial'
            result['baskets'].append({'basket': name, 'status': 'unavailable', 'reason': str(error),
                                      'execution_enabled': False})
    return history, result
