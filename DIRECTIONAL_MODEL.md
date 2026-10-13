# Directional model — 13 October 2026

Explicit user authorization scales the earlier directional limits by the same
threefold change as total commitment: race 1,500, directional total 15,000,
shared factor 6,000 SUSQies. These are worst-case debit/loss budgets, including
pending commitments, within total committed 75,000 and cash reserve 25,000.
Basket 5,000 and execution clip 1,000 remain unchanged.

`directional_model.py` is research-only: it performs no network calls and has no
order adapter. Its forecast output uses the existing `probability_range` schema
for `research_preview.py`; it does not create the reviewed preview manifest.
It never enables hosted directional execution.

## Model

Input is one reviewed pre-election polling aggregate per race/year/office/horizon,
with original polling provenance and certified outcome provenance. Margin is
the named claim's candidate/party vote share minus its strongest opponent.
Multi-candidate, ranked-choice and independent-party mappings require separate
settlement review; a positive margin must correspond to the actual contract.
Do not use this model for unsupported resolution structures.

Estimate win probability using historical final-margin minus poll-margin
residuals in the same office and a predeclared seven-day horizon window.
Election years have equal weight. Jeffreys half-count smoothing avoids certainty
from finite samples. Bounds span individual training-year probabilities and a
reviewed current-margin sensitivity interval. They are assumption sensitivities,
not confidence intervals or guarantees; correlated races and regime shifts are
not eliminated. News has no automatic likelihood multiplier.

Rolling holdouts train only on earlier election years. A scored fold requires
30 earlier races across at least three years. Passing engineering gates require
60 heldout races across three years, Brier below 0.25 and historical climatology,
calibration error at most 0.10, and every year's Brier below 0.25.
Thresholds were declared before retrieval/fitting. They are screening rules and
must not be interpreted as proof of trading EV or adequate tail-risk coverage.

## Validation status and precise blockers

10 synthetic software tests passed locally. Synthetic results are NOT empirical
electoral calibration. Historical dataset: NOT RETRIEVED/REVIEWED. Real Brier,
calibration error, holdout counts and live probability bounds: UNKNOWN.
Original historical source leads:
https://github.com/fivethirtyeight/data/blob/master/polls/README.md
https://github.com/fivethirtyeight/election-results
https://arxiv.org/abs/2206.14570

Next validate a pinned historical dataset with publication timestamps, original
polls and certified results, deduplicated aggregates and separate office/horizon
strata. Freeze data/model hashes, examine holdout reliability and tail coverage,
and independently review contract mapping before creating a preview manifest.
Current hosted config still marks fees unverified. Fresh executable books,
entry/emergency-exit depth, cancellation/N/A rules and conservative net edge
>=0.05/share remain mandatory. Existing protected hedges cannot be broken.

The preview is not consumed by the hosted executor. A future adapter must bind
model/data/config/contract hashes and recheck all inputs immediately before
using the existing journal, shared executor lock, kill switch and cutoff.
No manual dispatch, new runner or order was issued for this change.
