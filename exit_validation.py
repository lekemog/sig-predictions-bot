"""Audit observed exit depth, never infer trades or calibrate an exit probability.

Reports are evidence for a separate validation review. Touching an exit price
does not establish a fill, queue priority, or a reusable out-of-sample model.
"""
from decision_engine import executable, number, timestamp
from market_brain import digest, shares


def audit_exit_trace(observations, exchange_id, side, quantity, entry_cost,
                     target_net_proceeds, fees_per_share=0, maximum_gap_seconds=120):
    q = shares(quantity)
    number(entry_cost, 0); target = number(target_net_proceeds, 0)
    fees = number(fees_per_share, 0)
    gap_limit = number(maximum_gap_seconds, 1)
    if side not in ('yes', 'no') or not observations:
        raise ValueError('Exit trace requires a side and actual observations')
    previous, first, opportunities, gaps, sequences = None, None, [], [], []
    for row in observations:
        if row.get('kind') != 'book_observation' or row.get('paper_fills') != 'UNKNOWN':
            raise ValueError('Actual book observations required; simulated fills prohibited')
        if str(row['exchange_id']) != str(exchange_id):
            raise ValueError('Trace exchange mismatch')
        b = row['book']
        if str(b['exchangeId']) != str(exchange_id):
            raise ValueError('Book exchange mismatch')
        at, received = timestamp(row['observed_at']), timestamp(b['_received_at'])
        if not 0 <= (at-received).total_seconds() <= 15:
            raise ValueError('Trace contains stale/future receipt')
        if previous is not None:
            elapsed = (at-previous).total_seconds()
            if elapsed <= 0:
                raise ValueError('Trace must be strictly chronological without duplicates')
            if elapsed > gap_limit:
                gaps.append(dict(start=previous.isoformat(), end=at.isoformat()))
        first = first or at
        previous = at
        sequence = number(b['asOf']['sequence'], 0)
        if sequences and sequence < sequences[-1]:
            raise ValueError('Regressed engine sequence')
        sequences.append(sequence)
        exit_depth = executable(b, side, 'sell', q)
        if exit_depth['complete']:
            net = exit_depth['notional'] - q*fees
            if net >= target:
                opportunities.append(dict(at=at.isoformat(), net_proceeds=net,
                    observed_pnl=net-entry_cost, hours=(at-first).total_seconds()/3600))
    return dict(trace_hash=digest(observations), observations=len(observations),
        start=first.isoformat(), end=previous.isoformat(), missing_intervals=gaps,
        observed_exit_opportunities=opportunities, realized_fills='UNKNOWN',
        realized_profit='UNKNOWN', validated_for_live=False,
        limitation='Displayed depth at observation time; latency, queue, fills and conditional probabilities unvalidated')
