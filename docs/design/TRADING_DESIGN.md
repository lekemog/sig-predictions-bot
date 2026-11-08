# Coordinated strategy research

This branch adds research infrastructure. It does not change the live trading
engine, submit orders, or make profitability claims. GitHub Actions remains the
only live execution owner. No physical laptop is required for hosted Actions.

## What runs

`market_brain.py` provides parallel read-only evaluators, a transactional shared
SQLite ledger, evidence ingestion, feed invalidation, executable relationship
screening, whole-basket exit evaluation, turnover sensitivities and passive quote
proposals. `research_runner.py` can collect the open market and relationship
catalogues through GET requests and inspect at most three detailed candidates.
Each HTTP attempt consumes a shared read budget, including retries.

Run tests without credentials:

```sh
python -m unittest discover -s tests -v
python -m unittest test_market_brain -v
```

Run research on an actual authorized snapshot:

```sh
python research_runner.py --snapshot input.json --database private/brain.sqlite
```

Authenticated collection uses the existing securely supplied SIG_API_KEY
environment variable. Never enter it into source, command arguments or a PR.
Relationship proofs must bind the exact current relationship hash and provide
reviewed settlement rules, cancellation treatment and source references. Missing
proofs produce no actionable relationship candidate. Latest prices only rank
research leads; executable depth determines evaluation.

## Shared brain and execution ownership

Use one host, one shared ledger, parallel scanners and one executor. Separate
SQLite files on separate machines do not coordinate cash or prevent duplicate
orders. This branch has no executor for its new strategies. Reservations are
research coordination, not a substitute for the live owner's durable order
journal, idempotency, fill reconciliation or concurrency lock.

Fresh reconciled portfolio inputs must include committed cash in open orders,
pending requests, actual exposure by race/factor and protected hedge exchanges.
Unknown order outcomes retain reserved capital until owner reconciliation.
Estimated future proceeds never fund an entry. Failures and missing depth cause
abstention. Social posts remain unverified leads; they cannot silently change
forecasts. Multiple stories about the same event must not count as independent
evidence. A settlement probability is not an exit-price forecast.

## Faster turnover and market making

Turnover evaluation requires a separately validated exit model, conservative
net EV of at least 0.05/share, adverse scenarios, entry and emergency-exit depth,
costs, holding time and explicit falsification conditions. Market making uses
inventory-skewed passive proposals with adverse-selection and uncertainty
buffers. Both resting sides reserve cash jointly. No queue position, hit rate,
paper fill, profitable calibration or immediate exit is inferred from a touched
quote. The quote rule is a conservative heuristic, not a solved optimal-control
model. Relationship normal-settlement floors remain conditional on those rules;
cancellation refunds require separate analysis.

## Deployment gates

Current limits remain committed 25,000, reserve 75,000, basket 5,000, clip 1,000,
directional race 500, total 5,000 and factor 2,000. A blanket tenfold increase
would exceed the account capital constraints. Revised numeric limits, including
reserve and worst-case loss, need a separate explicit decision before activation.

An always-on host could improve feed continuity compared with scheduled Actions,
but this branch does not provision one. Before migration: choose hosting and
costs; securely provision credentials without extraction from Actions; persist
the live journal; reconcile positions, orders and fills; disable and confirm the
old execution owner; acquire an exclusive execution lease; only then start the
new owner. Never start competing live replicas.

The book cache handles revision gaps and reconnect invalidation but does not
include a live WebSocket subscription. Authenticated relationship collection,
queue/flow measurement, conservative exit-model validation and an execution
adapter remain deployment blockers. Python remains appropriate until measured
processing latency shows a language change provides an executable advantage.

## Source references

- SIG API: https://sig.thesuper.market/api/v1/docs
- Market-making research: https://arxiv.org/html/2607.17991v1
- GitHub schedule caveats: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

Do not treat leaderboard profiles as a copy signal: public market lists do not
establish side, size, entry price or executable profit.
