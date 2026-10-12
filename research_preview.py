"""Reviewed research -> existing directional planner, PREVIEW ONLY.

python research_preview.py --input private/preview.json --config bot_config.json

Input contains cases (<=3), state, checkpoint, markets, nodes, books and manifest.
All data must be collected by the existing GET-only collector; this CLI performs
no networking and never creates a Runner, journal job or order. A candidate is
not live authorization. The hosted executor does not consume this file.

Manifest: reviewed=true, expires_at, config_sha256, and cases keyed by digest,
each containing forecast_sha256, nodes_sha256, calibration_reference,
calibration_reviewed=true and settlement_reviewed=true. Review flags represent
human-reviewed declarations, not automatic proof of calibration/independence.
"""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_bot import END, Halt, SLUG, directional_plan, portfolio_risk, risk_limits


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def fresh(value, at, seconds):
    t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if t.tzinfo is None or not 0 <= (at - t).total_seconds() <= seconds:
        raise Halt('Stale, future or timezone-free input')


class SnapshotAPI:
    """Only the planner's three reads are supported; no transport or write API."""
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self, path):
        for mid, value in self.snapshot['markets'].items():
            if path == f'/tournaments/{SLUG}/markets/{mid}':
                return copy.deepcopy(value)
        for mid, value in self.snapshot['nodes'].items():
            if path == f'/markets/{mid}/nodes':
                return copy.deepcopy(value)
        raise Halt('Read absent from reviewed snapshot')

    def book(self, exchange_id):
        return copy.deepcopy(self.snapshot['books'][str(exchange_id)])


def preview(snapshot, config, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    result = dict(mode='preview', orders_submitted=0, live_enabled=False,
                  candidates=[], rejections=[], input_sha256=digest(snapshot))
    try:
        at = clock()
        if at >= END:
            raise Halt('Competition cutoff reached')
        if Path(config.get('kill_switch', 'private/STOP')).exists():
            raise Halt('Stop switch is active')
        limits = risk_limits(config)
        cases = snapshot['cases']
        if not isinstance(cases, list) or len(cases) > 3:
            raise Halt('At most three researched cases per review')
        checkpoint = snapshot['checkpoint']
        if checkpoint.get('enabled') is not True:
            raise Halt('Executor disabled')
        if any(j['status'] not in ('done', 'dry_run', 'aborted_before_send')
               for j in checkpoint['jobs']):
            raise Halt('Unresolved executor journal job')
        fresh(checkpoint['updated_at'], at, 90 * 60)
        state = snapshot['state']
        fresh(state['at'], at, 60)
        if state['orders']:
            raise Halt('Open orders require executor reconciliation')
        committed, _, exposure = portfolio_risk(state, config)
        manifest = snapshot['manifest']
        expiry = datetime.fromisoformat(manifest['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry <= at or manifest.get('reviewed') is not True:
            raise Halt('Missing or expired reviewed manifest')
        if manifest['config_sha256'] != digest(config):
            raise Halt('Risk configuration changed since review')
        for case in cases:
            case_id = digest(case)
            try:
                review = manifest['cases'][case_id]
                forecast = case['forecast']
                if (review.get('calibration_reviewed') is not True
                        or review.get('settlement_reviewed') is not True
                        or not review.get('calibration_reference')
                        or review['calibration_reference'] != forecast.get('validation_reference')
                        or review['forecast_sha256'] != digest(forecast)
                        or review['nodes_sha256'] != case['contract'].get('nodes_sha256')):
                    raise Halt('Calibration, forecast or settlement review mismatch')
                book = snapshot['books'][str(case['contract']['exchange_id'])]
                fresh(book['_received_at'], clock(), 60)
                fresh(book['asOf']['at'], clock(), 60)
                plan = directional_plan(SnapshotAPI(snapshot), state, config, case)
                if not plan:
                    raise Halt('Existing directional planner rejected evidence, edge or risk')
                # Decimal final gate includes cost allowance and charges existing
                # directional exposure conservatively against every race/factor.
                d = lambda x: Decimal(str(x))
                loss = d(plan['max_spend'])
                if (d(plan['conservative_limit_edge']) < d('.05')
                        or d(committed) + loss > d(limits['committed'])
                        or d(state['cash']) - loss < d(limits['reserve'])
                        or any(d(exposure) + loss > d(limits[k])
                               for k in ('race', 'factor', 'directional'))):
                    raise Halt('Final Decimal edge/risk gate failed')
                final_at = clock()
                fresh(state['at'], final_at, 60)
                fresh(book['_received_at'], final_at, 60)
                fresh(book['asOf']['at'], final_at, 60)
                if (final_at >= END or final_at >= expiry
                        or Path(config.get('kill_switch', 'private/STOP')).exists()):
                    raise Halt('Final stop, cutoff or manifest gate failed')
                result['candidates'].append(dict(case_sha256=case_id,
                    exchange_id=case['contract']['exchange_id'],
                    side=plan['side'], quantity=plan['quantity'],
                    limit_price=plan['prices'][0], max_spend=plan['max_spend'],
                    probability_bounds=plan['decision']['probability_range'],
                    conservative_net_edge=plan['conservative_limit_edge'],
                    evaluated_at=final_at.isoformat(), execution_authorized=False))
            except (Halt, ValueError, KeyError, TypeError) as error:
                result['rejections'].append(dict(case_sha256=case_id, reason=str(error)))
        result['candidates'].sort(key=lambda c: (-c['conservative_net_edge'], c['max_spend']))
    except (Halt, ValueError, KeyError, TypeError) as error:
        result['candidates'] = []
        result['blocked'] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path('bot_config.json'))
    args = parser.parse_args()
    print(json.dumps(preview(json.loads(args.input.read_text()),
                             json.loads(args.config.read_text())), allow_nan=False))


if __name__ == '__main__':
    main()
