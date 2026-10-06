# SIG Predictions Bot

A Python trading system for the SIG Predictions Cup. It combines contract-level pricing checks, probability sensitivity analysis and bounded execution through the competition API.

My objective is to find defensible pricing errors across election markets while controlling exposure, execution costs and capital lockup. The system separates opportunities supported by settlement logic from trades that require a forecast.

## Architecture

| Component | Responsibility |
| --- | --- |
| `decision_engine.py` | Probability ranges, expected value, joint payoff scenarios and forecast evaluation |
| `trading_bot.py` | Portfolio reconciliation, opportunity ranking, order submission and partial-fill handling |
| `sig_snapshot.py` | Read-only, tournament-scoped order-book snapshots |
| `bot_config.json` | Market watchlist, fee assumptions and operational controls |
| `private/execution.sqlite` | Persistent order requests, execution results and review history |

Python 3.10 or later is required. The project uses the standard library and runs on Linux or macOS.

## Strategies

### Contract consistency

The runner checks distinct party-winner contracts for the same race, stage, election date and settlement date. For a basket of `n` mutually exclusive outcomes, equal NO positions pay at least `n - 1` per basket under normal settlement. An unlisted winner increases that payout; exhaustive party coverage is not required.

Entries require executable depth and a positive margin after the configured fee allowance and an additional 0.005 per share per leg. The default order size is 100 matched shares per leg, reduced when liquidity or risk limits bind. The minimum robust margin is 1 SUSQie and 0.01 per basket.

Whole-basket exits require net limit proceeds at least equal to the normal-settlement payout floor. Cancellation and N/A outcomes follow refund rules, so the normal-settlement floor is conditional on the contracts resolving normally.

### Forecast-based entries

Directional trades use explicit probability ranges or a final-margin model:

`P(YES) = Phi(expected final margin / total error standard deviation)`

Sensitivity ranges account for uncertainty in the expected margin and total error. They are assumption bounds, not calibrated confidence intervals. Polling vote share is not treated as win probability.

A case must include verified sources, assumptions, a validation reference, a thesis and a falsification condition. The runner refreshes executable prices and portfolio exposure before submitting a trade. Conservative expected value at the limit price must exceed 0.05 per share after costs.

Directional entries are restricted to flat exchanges to protect existing hedges. Unclassified directional exposure is conservatively charged to each proposed race and political factor. Thesis-based exits require a separate research decision; they are not automatically triggered by price changes.

Public news and external market prices are research inputs rather than proof of an informational advantage. News updates require justified likelihood ratios and evidence independence checks. Short-term turnover is excluded from live execution until a separate exit model is validated.

## Risk limits

| Constraint | Maximum or minimum |
| --- | ---: |
| Total entry commitments, including pending orders | 25,000 SUSQies |
| Commitment per hedged race or basket | 5,000 SUSQies |
| Minimum cash reserve | 75,000 SUSQies |
| Directional maximum loss per race | 500 SUSQies |
| Total directional maximum loss | 5,000 SUSQies |
| Exposure to a shared political factor | 2,000 SUSQies |
| Partial-fill repair stress-loss budget | 10 SUSQies |

Limits are enforced in code. The runner does not size orders to consume the available ceiling or use price-only stops on intact hedges.

## Execution and recovery

Every placement request and idempotency key is persisted before the API call. If a response is lost, the runner reconciles account state and retries only the identical stored request with the same key.

Multi-leg submission is atomic, but fills may be unequal. The runner inspects order fills, cancels resting remainders and reconciles the resulting positions. It unwinds unmatched incremental entries to the smallest filled basket quantity; for unequal exits, it buys back the unmatched portion to restore the hedge.

Repairs require fresh depth and must remain within the cash reserve and repair budget. An incomplete or uncertain repair halts new placements and retains the evidence for reconciliation.

Orders use explicit tournament IDs, tick-aligned limits and expirations of at most 90 seconds, or 60 seconds for repairs. Market orders are prohibited. All expirations are capped at the trading cutoff: **4 November 2026, 17:00 UTC**.

A process lock prevents concurrent local runners sharing a journal. Only one execution process should manage the account; this lock does not coordinate separate machines or external trading tools. API calls are paced below 100 reads and 30 writes per minute per process.

## Setup

Keep `SIG_API_KEY` in the process environment. Never commit credentials, portfolio snapshots or execution journals. For a one-off read-only review, enter the key through a hidden prompt:

```sh
python -c 'import getpass,os,runpy; os.environ["SIG_API_KEY"]=getpass.getpass("SIG key: "); runpy.run_path("trading_bot.py",run_name="__main__")'
```

Before enabling placements, verify the trading fee schedule, set `costs.fee_per_share` and record the source in `bot_config.json`. Set `costs.verified` only after verification. The default configuration leaves this false, so the runner reconciles the portfolio but abstains from placements.

Run commands from the repository directory:

```sh
python trading_bot.py                 # one dry-run review
python trading_bot.py --live          # one live review
python trading_bot.py --live --loop   # hourly live reviews
python trading_bot.py --cases private/researched_cases.json --live
```

The loop requires a running process and durable storage for its journal. GitHub stores the source; it does not automatically host or execute the bot.

Create `private/STOP` to prevent further submissions. The loop checks it every 10 seconds while waiting. Existing orders expire according to their original limits; the stop file does not instantly cancel them. Do not delete the journal while an order's outcome is unresolved.

## Research cases

Directional inputs follow `examples/synthetic_trade.json`, with an additional `contract.nodes_sha256`: the SHA256 of the current API contract root serialized as sorted-key JSON. The fingerprint protects against changed contract definitions.

The standalone engine supports joint basket scenarios and forecast evaluation:

```sh
python decision_engine.py private/case.json --journal private/audit.sqlite
python decision_engine.py examples/synthetic_basket.json --mode basket --as-of 2026-10-06T12:00:10Z
python decision_engine.py private/resolved_forecasts.json --mode calibration
```

Use `--as-of` only for replay or synthetic examples. Live decisions require current evidence. The examples contain invented data and are not election forecasts.

Basket analysis uses joint outcomes rather than multiplying marginal race probabilities. Include third-party wins, cancellation and refunds. Forecast evaluation reports Brier scores for timestamped predictions made before resolution; comparisons should use chronological out-of-sample data and matching baselines.

## Validation

```sh
python -m unittest discover -s tests -v
```

The 28 tests cover probability sensitivity, stale evidence, correlated payoff scenarios, duplicate evidence, depth, reserves, contract mismatches, tick validation, partial fills, failed repairs, lost accepted responses and exact-key replay.

The suite uses a simulated exchange for execution tests. It validates implementation behavior, not profitability or real-world liquidity. News verification, forecast calibration and directional exit decisions remain research tasks. The runner reviews hourly; it does not claim continuous monitoring or a speed advantage.
