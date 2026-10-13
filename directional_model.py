"""Research-only election forecasts and chronological validation.

One reviewed poll aggregate per race/year/horizon. No network or order adapter.
Historical residuals include sampling, systematic error and subsequent movement.
Election years are equally weighted; races within a year are correlated.
Bounds are sensitivity ranges across training years, NOT confidence intervals.
"""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from decision_engine import evidence_check


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def number(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Numeric value required')
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError('Numeric value outside bounds')
    return value


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timezone required')
    return dt


def checked_rows(rows, office, horizon_days):
    """Require auditable, pre-election observations and certified outcomes."""
    if office not in ('senate', 'governor', 'house'):
        raise ValueError('Unsupported office')
    number(horizon_days, 1, 90)
    selected, seen = [], set()
    for row in rows:
        key = (row['race_id'], row['year'], row['office'], row['horizon_days'])
        if key in seen:
            raise ValueError('Duplicate race/year/horizon observation')
        seen.add(key)
        if row['reviewed'] is not True or not row['poll_source'] or not row['result_source']:
            raise ValueError('Reviewed polling and result provenance required')
        if not all(row[k].startswith('https://') for k in ('poll_source', 'result_source')):
            raise ValueError('HTTPS provenance required')
        for k in ('poll_margin_pp', 'final_margin_pp'):
            number(row[k], -100, 100)
        number(row['year'], 1900, 2100)
        if int(row['year']) != row['year']:
            raise ValueError('Whole election year required')
        election = timestamp(row['election_at'])
        observed = timestamp(row['poll_available_at'])
        if election.year != row['year']:
            raise ValueError('Election year mismatch')
        if not horizon_days <= (election-observed).total_seconds()/86400 <= horizon_days+7:
            raise ValueError('Poll availability falls outside predeclared horizon window')
        if row['office'] == office and row['horizon_days'] == horizon_days:
            selected.append(dict(row))
    return selected


def probabilities(rows, margin):
    """Equal-year empirical P(margin + historical residual > 0)."""
    years = sorted({r['year'] for r in rows})
    if not years:
        raise ValueError('No historical residuals')
    values = []
    for year in years:
        residuals = [r['final_margin_pp']-r['poll_margin_pp'] for r in rows if r['year']==year]
        # Ties are conservatively counted as losses; resolution must be reviewed.
        values.append((sum(margin+e > 0 for e in residuals)+.5)/(len(residuals)+1))
    return sum(values)/len(values), [min(values), max(values)]


def validate(rows, office, horizon_days, now=None):
    """Rolling election-year holdouts; never train on the test year or later.

    Thresholds are predeclared engineering gates, not evidence of profitability.
    Requires >=3 prior years and >=30 prior races for every scored fold, then
    >=3 scored holdout years and >=60 races. Metrics alone cannot approve live.
    """
    now = now or datetime.now(timezone.utc)
    data = checked_rows(rows, office, horizon_days)
    if any(timestamp(r['election_at']) >= now for r in data):
        raise ValueError('Future or unsettled historical election')
    scored, folds = [], []
    for year in sorted({r['year'] for r in data}):
        train = [r for r in data if r['year'] < year]
        test = [r for r in data if r['year'] == year]
        if len(train) < 30 or len({r['year'] for r in train}) < 3:
            continue
        baseline = sum(r['final_margin_pp'] > 0 for r in train)/len(train)
        fold = []
        for r in test:
            p, bounds = probabilities(train, r['poll_margin_pp'])
            y = int(r['final_margin_pp'] > 0)
            fold.append(dict(year=year, race_id=r['race_id'], p=p, y=y,
                             bounds=bounds, baseline=baseline))
        scored.extend(fold)
        folds.append(dict(year=year, train_races=len(train), test_races=len(test),
                          brier=sum((r['p']-r['y'])**2 for r in fold)/len(fold)))
    issues = []
    if len(scored) < 60 or len(folds) < 3:
        issues.append('Insufficient chronological holdout races/years')
    brier = sum((r['p']-r['y'])**2 for r in scored)/len(scored) if scored else None
    baseline = sum((r['baseline']-r['y'])**2 for r in scored)/len(scored) if scored else None
    bins = []
    for i in range(5):
        bucket = [r for r in scored if min(int(r['p']*5),4)==i]
        if bucket:
            bins.append(dict(n=len(bucket), mean_p=sum(r['p'] for r in bucket)/len(bucket),
                             win_rate=sum(r['y'] for r in bucket)/len(bucket)))
    ece = sum(b['n']*abs(b['mean_p']-b['win_rate']) for b in bins)/len(scored) if scored else None
    if brier is not None and (brier >= .25 or brier >= baseline):
        issues.append('Does not beat predeclared Brier/climatology gates')
    if ece is not None and ece > .10:
        issues.append('Calibration error exceeds predeclared 0.10 gate')
    if any(f['brier'] >= .25 for f in folds):
        issues.append('At least one election-year fold fails Brier gate')
    return dict(model_version='empirical-residual-v1', dataset_sha256=digest(data),
                office=office, horizon_days=horizon_days, folds=folds, bins=bins,
                brier=brier, baseline_brier=baseline, calibration_error=ece,
                holdout_races=len(scored), statistical_gates_passed=not issues,
                issues=issues, live_enabled=False, execution_authorized=False,
                limitation='Year sensitivity is not a confidence interval; regime shift and rare outcomes remain unknown')


def forecast(rows, observation, report, now=None):
    """Emit the existing preview schema, refusing fabricated calibration refs."""
    now = now or datetime.now(timezone.utc)
    office, horizon = observation['office'], observation['horizon_days']
    fresh_report = validate(rows, office, horizon, now)
    if report != fresh_report or not report['statistical_gates_passed']:
        raise ValueError('Validation report missing, changed or failed')
    age = (now-timestamp(observation['observed_at'])).total_seconds()
    if not 0 <= age <= 86400:
        raise ValueError('Observation stale or future')
    if observation.get('aggregate_reviewed') is not True or not observation.get('evidence'):
        raise ValueError('Reviewed original-source aggregate required')
    issues = evidence_check(observation['evidence'], now, 168)
    if issues or any(e.get('source_kind') != 'original_poll' for e in observation['evidence']):
        raise ValueError('Original polling evidence failed: '+str(issues))
    margin = number(observation['poll_margin_pp'], -100, 100)
    uncertainty = number(observation['margin_sensitivity_pp'], 0, 100)
    data = checked_rows(rows, office, horizon)
    if any(timestamp(r['election_at']) >= timestamp(observation['observed_at']) for r in data):
        raise ValueError('Training outcomes unavailable at forecast origin')
    _, low = probabilities(data, margin-uncertainty)
    _, high = probabilities(data, margin+uncertainty)
    return dict(kind='probability_range', yes_probability=[low[0],high[1]],
                evidence=observation['evidence'], max_age_hours=168,
                validation_reference='sha256:'+digest(report),
                assumptions=['Party/candidate identity and aggregation independently reviewed',
                             'Historical same-office, same-horizon residuals are transferable',
                             'Jeffreys half-count smoothing avoids certainty from finite historical samples',
                             'Sensitivity across election years and reviewed margin range; no news likelihood adjustments'],
                news_updates=[], execution_authorized=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    report = validate(payload['rows'], payload['office'], payload['horizon_days'])
    out = {'validation': report, 'orders_submitted': 0}
    if 'observation' in payload:
        out['forecast'] = forecast(payload['rows'], payload['observation'], report)
    print(json.dumps(out, allow_nan=False))


if __name__ == '__main__':
    main()
