# SIG trading bot and decision engine

A SIG Predictions Cup virtual-SUSQie bot with a separate auditable decision engine.
`trading_bot.py` can place and cancel actual competition orders in explicit live mode.
`decision_engine.py` remains read-only. The default bot run sends no orders.

## Run

Python 3.10+; no third-party dependencies.

```sh
python -m unittest discover -s tests -v
python decision_engine.py examples/synthetic_trade.json --as-of 2026-10-06T12:00:10Z
python decision_engine.py examples/synthetic_basket.json --mode basket --as-of 2026-10-06T12:00:10Z
```

Examples are invented test cases, not election forecasts or trading recommendations.
The directional example deliberately returns INSUFFICIENT_EVIDENCE: a mathematical
calculation without forecast validation has not demonstrated an edge.

## A decision on every review

1. Read the exact contract, settlement date and cancellation/refund rules.
2. Obtain fresh executable books with quantities. `sig_snapshot.py EXCHANGE_ID` reads
   the competition API using `SIG_API_KEY` from the environment. Never commit keys.
3. Research primary polling, official election data and timestamped reporting. Use X,
   Reddit and Facebook to discover leads, then verify the original source. This repository
   does **not** have authenticated social feeds, news subscriptions or a validated US
   election dataset. It does not claim to monitor those services.
4. Prepare a private input JSON using the example schema. Supply a defensible final-margin
   and total-error range, or an independently justified probability range. Document
   assumptions and a real validation reference. `verified` and validation fields are
   researcher attestations, not claims the program can verify by itself.
5. Run `python decision_engine.py private/case.json --journal private/audit.sqlite`.
   For live analysis omit `--as-of`; otherwise stale data could be mistaken for current data.
6. Inspect probability range, conservative edge, depth, loss caps, hold-versus-sell value,
   related-market diagnostics, and reasons to abstain. The decision is a candidate for
   review, never an execution instruction. Preserve its input hash and evidence.
7. Re-evaluate existing positions when material evidence changes. Hold-versus-sell compares
   future payout with sale proceeds today: entry price is sunk. A failure to establish a new
   entry edge is not an instruction to break an existing hedge.

## Probability model

The transparent baseline is `P(YES) = Phi(expected final margin / total error SD)`.
The engine evaluates the endpoints of both assumption ranges. Total error must cover
systematic polling error, turnout, late movement and other uncertainties; survey sampling
error alone is inadequate. These sensitivity bounds are **not** calibrated confidence
intervals. A single-race normal model is a baseline, not a complete election simulator.

Optional verified news updates multiply prior odds by an explicitly justified likelihood
ratio range. The code does not manufacture ratios from headlines or sentiment. Group IDs
reject repeated or recycled evidence and updates already in the baseline. This reduces
obvious double counting but cannot discover hidden source dependence.

External prices are comparison points only. Their freshness and settlement equivalence
are displayed; they never automatically change the forecast. Exact implications and
mutually exclusive/exhaustive relationships produce consistency diagnostics, not proof of
executable arbitrage. Bid/ask spread, costs and simultaneous liquidity still matter.

Basket analysis uses explicit **joint** outcome scenarios and alternative joint probability
vectors. It never assumes independent races. Include third-party wins, cancellation and
refund states. If probabilities cannot be justified, it still reports conditional payoff
floors and explicitly leaves expected profit unknown. Baskets require further risk and
execution review; the basket command never emits CANDIDATE.

## Costs, risk and honest evaluation

Order books are YES-normalized; NO prices use complements. The engine consumes displayed
depth, reports shortfalls, includes user-specified fees/slippage, and treats immediate
resale at the bid as a cost. Displayed depth can disappear. It checks total loss exposure,
race exposure, common political-factor exposure and available cash. Supply conservative
current exposures, including pending orders; it does not fetch your portfolio automatically.

A probability of winning at settlement does not imply a price will rise in two minutes.
Quick-resale objectives are blocked until a separate validated exit model exists. Completed
paper/live cycles must include forced exits, unfinished inventory and losses. Trade count
and volume are not success metrics.

`--mode calibration` computes Brier score for resolved, timestamped forecasts. Use a
chronological out-of-sample evaluation against the same-event market and simple baselines.
Related races are correlated samples. A handful of correct predictions proves little.

Private inputs and SQLite logs stay out of git. There are no API credentials, personal
account snapshots, actual portfolio balances or real news claims in this repository.

## Current limitations

This is a tested calculation and decision framework, not a validated profitable strategy.
News intake and evidence verification remain research tasks. No automatic calibration, election-wide simulation, social scraper, or autonomous
news verification is implemented. The runner supports hourly reviews and execution,
not continuous order-book monitoring. The hourly assistant must run this code with fresh evidence and explicitly
report inability to do so; merely storing it in GitHub does not make it run.


## Trading runner

Python 3.10+ on Linux/macOS (uses `fcntl` to reject concurrent local instances).
No third-party packages are required. Run commands from this repository's directory.

Set `SIG_API_KEY` privately in the process environment. Do not place a key in GitHub,
source files, command-line arguments, screenshots or chat logs. You can enter it with
Python's hidden prompt for one run:

```sh
python -c 'import getpass,os,runpy; os.environ["SIG_API_KEY"]=getpass.getpass("SIG key: "); runpy.run_path("trading_bot.py",run_name="__main__")'
```

Before live use, document the actual fee schedule under `costs.source`, set its
per-share charge, and mark `costs.verified` true only after verifying it. The supplied
configuration deliberately leaves fees unverified. Until then, the runner reads and
journals the portfolio but abstains from all placements. A guessed zero fee is not a
verified schedule. An additional 0.005 per share per leg is always reserved for cost stress.

```sh
python trading_bot.py                       # one read-only review
python trading_bot.py --live                # one real execution review
python trading_bot.py --live --loop         # hourly reviews while this process runs
python trading_bot.py --cases private/researched_cases.json --live
```

`--live` enables real competition API writes. It does not authorize new strategies or
change the user-approved limits. This is virtual competition currency, not a connection
to a brokerage. This delivery does not start a background process, host a server, configure
GitHub secrets, or enable a GitHub Actions trading workflow.

The configured watchlist covers11 verified-existing race groups. New markets can be added
with market/exchange IDs and exact party names; every live review verifies current
race/stage/election date/settlement date, party definitions, tournament context and status.
Normal NO basket floor is `(number of distinct mutually exclusive parties - 1) * shares`;
the list need not contain every possible winner. Cancellation and N/A can follow different
refund rules and are not described as guaranteed normal payouts. Rule verification is
based on the API's typed Party Winner definition, not headline similarity.

The bot favors whole-basket exits at net limit proceeds at least their normal-settlement
floor. New basket entries use a small100-pair clip (reduced for depth/budget) and require
at least1 SUSQie robust margin and at least0.01 per pair after the cost allowance. It does
not repeatedly cross spreads for turnover, run the paper experiment, or replay historical
micro-pilot orders. No market orders or price-only hedge stops are used.

Directional case files follow the decision-engine example schema plus a
`contract.nodes_sha256` SHA256 of the current API contract root (sorted-key JSON).
They must contain current verified evidence, justified probability sensitivity, validation,
thesis and falsification. Fresh books and live portfolio risk override supplied inputs.
The conservative edge at the actual limit must be at least0.05 after costs. Directional
orders only enter flat exchanges so they cannot break configured hedges. Existing
unclassified directional exposure is conservatively charged to every new race and factor;
this can prevent additional trades before the formal maximums are reached. Directional
thesis exits are research decisions, not unattended automatic selling in this version.

Hard ceilings are25000 total entry commitments,5000 per hedge group,75000 minimum cash,
500 directional maximum loss per race,5000 directional total and2000 per political factor.
Limits are constants in the runner and cannot be increased through configuration. Resting
orders and unknown outcomes prevent additional entries. Use one account writer: coordinate
with any existing hourly assistant before starting a second live process. The file lock
coordinates this runner's local instances, not other machines or the assistant automation.
Reads/writes are paced below100/30 per minute per local process; unrelated account activity
must share those limits. Credentials are sent only to the fixed SIG API origin and HTTP
redirects are rejected.

Every request/key is committed to `private/execution.sqlite` before submission. A lost
response leaves an unresolved job: the next live review reads positions/fills first, then
replays only the exact persisted request and key. Each returned order's fills are inspected,
its unfilled remainder cancelled, and the resulting portfolio reconciled. Multi-leg
submission does not guarantee full fills. Unmatched incremental buys are unwound to the
smallest fully filled basket quantity; unmatched exits are bought back to restore the
original hedge. Repair uses tick-aligned limits, a maximum10-SUSQie stress-loss budget,
the cash reserve and fresh depth. An incomplete/unknown repair halts further orders and
requires reconciliation from the journal; it is not blindly retried under a new key.
Cash changes and final holdings are recorded, including failed/unprofitable operations.

Create `private/STOP` to stop future submissions; the loop checks it at most10 seconds
apart while waiting. This does not instantly cancel previously submitted orders: orders
expire within90 seconds, repair orders within60. All expirations are capped at the
4 November2026 17:00 UTC trading cutoff. The bot refuses submissions at/after the cutoff.
Do not delete the journal while orders may be unresolved. Keep it on durable disk when
running outside this workspace. A stopped/crashed process is not continuous monitoring.

Tests use a fake exchange. They cover partial fills, lost accepted responses, exact-key
replay, no duplicate economic effect, failed repairs, hedge preservation, reserves,
contract mismatches, fees, tick validation and the deadline. They do not establish real
liquidity, profitable forecasts or production uptime.
