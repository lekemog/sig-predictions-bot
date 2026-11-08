# Drawdown review and defensive entry controls

Verified repository checkpoint at 2026-10-14 10:44:33 UTC: account value 99,827.78; period P&L -172.22; unrealized holdings P&L -206.72; cash 65,121.31; cost basis 34,913.19. Full live sweep at 10:45:38 UTC screened all 12 baskets, NO_TRADE, no open orders or unresolved jobs. These are timestamped observations, not current executable liquidation values.

NC Senate contributed -110.71 and RI Governor -81.91 unrealized P&L, offset partly by other baskets. Matched NO legs can lose marked value when independently quoted YES probabilities sum above one. This does not establish a broken conditional settlement payoff, nor does an assumed payoff establish a realized profit. Unreviewed category/settlement mappings remain a separate risk.

The strategy optimizes a conditional settlement floor, not near-term account marks. Its exits require net proceeds at least that floor. Tiny apparent settlement margins and repeated additions can therefore lock up capital without improving current marked performance. Kalshi/Polymarket research is not an active directional execution model.

## Implemented controls

New entries remain paused. Defensive controls also apply if that pause is later lifted:

- Never add to a basket already held.
- Require the full hash-bound reviewed candidate/settlement proof for every new basket, using the existing proof validator.
- At most 250 matched shares and 1,000 SUSQies initial basket cost.
- Require at least 3% conditional net settlement return on conservative cost.
- Require executable sell depth for the same clip and at most 1% conservative immediate round-trip loss, including allowances on both sides.
- Block entries if account value is unavailable/nonfinite or below 99,750 (250 SUSQies below the initial 100,000). This and the 3%/1% thresholds are chosen defensive risk budgets, not statistically calibrated optimal parameters.
- Keep directional submissions disabled under this policy.
- Recheck policy and executable prices before creating a submission intent.

Existing exits, payout floors, durable recovery, reconciliation, concurrency and pending-job handling remain active. Existing positions may exceed the new initial-position limits; these limits do not force liquidation or interfere with recovery. Total capital ceiling is unchanged; entry pause is the active exposure freeze.

Persist latest account review separately from the bounded event history. Persist per-held-basket diagnostics: cost, marked value, conditional floor, tested exit clip and conservative executable net proceeds (unknown when full depth is absent). The clip estimate is not a full-portfolio liquidation value. These diagnostics explain blocked exits without manufacturing a confidence interval or realized profit.

Validation: 81 existing tests and 46 entry-pause, defensive, directional-model, cross-venue and collector tests passed locally. Seven new defensive tests include unknown account valuation, proof absence, held additions, spread/depth rejection, malformed budgets, submission bypass and preservation of exits. Software tests do not validate strategy profitability. Hosted adoption requires the next automatic execution checkpoint; no manual live workflow dispatch or order is performed by this review.
