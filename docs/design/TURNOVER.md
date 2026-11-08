# Executable dip detection and basket turnover

The previous rule sold only above the conditional settlement payout. The new
optional profit-take rule sells a balanced clip when executable proceeds, after
conservative exit costs, cover the clip's pro-rata recorded cost basis plus the
larger of 0.5% or 1 SUSQie. This can realize a small profit below the settlement
payout. Missing/invalid basis or mismatched holdings cannot qualify. Fresh prices
and basis are checked again before creating an execution intent.

Dip observations use 250 matched shares, both sides of every leg, fees and the
existing extra tick per leg. Compare current purchase cost with the median of
prior valid observations from the last 24 hours. Require three prior samples,
at least 20 minutes of history and a drop of at least 0.02 per matched share.
No present observation is included in its own baseline. Missing depth and failed
reads are explicit partial coverage; a gap exceeding 90 minutes or changed
contract definitions restarts warm-up. A historical drop does not establish
rebound probability or profitability. Thresholds are policy choices, not fitted
or historically validated parameters.

The hosted executor persists `dip_quote_history`, `latest_dip_scan` and
`latest_turnover_candidates` in its existing private checkpoint. No second
scheduler or execution runner is added. Sampling runs on the existing periodic
sweeps; GitHub delays and short-lived moves remain possible. Pending execution
recovery takes priority and skips sampling.

## Deployment state

`dip_monitor.enabled=true`, `dip_monitor.mode=observe`: read-only sampling active
on subsequent hosted sweeps. `turnover.mode=observe`: new cost-based exit
candidates are logged, while existing exit rules remain active.
`entries_paused=true`: no new entries.

Changing `turnover.mode` to `live` enables cost-based profit-taking on existing
matched holdings. Entries remain paused unless separately lifted. If later
enabled, basket entries ALSO require a fresh qualified dip reference, current
executable-price revalidation, reviewed settlement proof, no held-basket adds,
minimum 3% conditional settlement return, maximum 1% immediate round-trip loss,
250-share/1,000-cost initial ceilings, account-value floor and existing portfolio
limits. Missing proofs in the current configuration block purchases. Directional
entries remain disabled. This is not an implemented individual-leg mean-reversion
strategy, a loss stop, or a guarantee of an eventual exit. Failure to rebound can
still leave holdings locked up.

The new dip and turnover tests use synthetic books and histories. They validate
gating, cost-based profit exits, fresh-price rejection and absence of order calls
in observation mode. They do not establish a profitable trading edge.

Validation: existing `tests/` suite and root test discovery, plus
`python -m unittest test_dip_monitor test_turnover -v` after upload.
The stale profile regression now tests 100001 against the already-authorized
100000 commitment ceiling; production risk limits were not changed.
