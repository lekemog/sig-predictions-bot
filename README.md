# SIG Predictions Bot

A Python trading system for the SIG Predictions Cup. My objective is to find defensible election-market pricing errors while controlling exposure, execution costs and capital lockup.

## Hosted operation

GitHub Actions runs the portfolio manager three times per hour, at minutes 17, 37 and 57 UTC. Scheduled runs use live execution; manual runs default to dry-run and offer an explicit live option. GitHub schedules can be delayed. The system uses periodic portfolio sweeps, not continuous market monitoring.

The workflow reads the encrypted repository secret SIG_API_KEY. Credentials stay out of source files and checkpoint records. Validation runs before every portfolio review. The private repository must remain private because its checkpoint includes portfolio and execution information.

Only one workflow invocation can execute at a time. The separate chat-based execution schedule is paused to keep a single automated execution owner.

## Execution and recovery

hosted_runner.py restores the durable checkpoint in .bot/state.json into its local SQLite journal. Every order request and idempotency key is synchronously saved back to GitHub before the SIG API call. A failed checkpoint write blocks the following submission. A lost placement response retains the original request; recovery reconciles the account and replays only the identical request and key.

Unfinished jobs are always retained. Completed receipt history is bounded; earlier versions remain in Git history. Review events record screening decisions and account reconciliation. The workflow uses its short-lived GITHUB_TOKEN to update the private checkpoint.

Multi-leg submission does not guarantee complete fills. The runner checks fills, cancels resting remainders and repairs unmatched incremental positions within its repair budget. Failed repairs halt further entries. Every completed trade reconciles actual fills against the cash change.

Set enabled to false in .bot/state.json to prevent subsequent hosted runs from trading. This takes effect on the next run; outstanding orders retain their short expirations. Disable the workflow in Actions to stop scheduled reviews entirely. Do not erase an unresolved execution checkpoint.

All new submissions stop at 4 November 2026, 17:00 UTC. Limits are tick-aligned and expire within 90 seconds, with 60-second repair limits. Market orders are prohibited.

## Pricing mechanism

Equal NO holdings in n mutually exclusive party-winner contracts for the same race, stage, date and settlement pay at least n-1 under normal settlement. Unlisted winners increase the payout; the model does not assume exhaustive party coverage. Cancellation and N/A outcomes use refund rules instead.

The hosted runner checks all configured baskets, including all 11 current holdings and the newly researched Kansas Senate pair, ranks eligible exits before additions and submits at most one new job per sweep. Whole-basket or matched partial exits require net proceeds at least equal to their normal-settlement floor. Entries require executable depth and positive profit after documented trading costs plus an extra 0.005 per share per leg. The starting clip is up to 1,000 matched shares, reduced when liquidity or risk limits bind.

The fee allowance is zero based on SIG's no-participant-trading-fee documentation and the reconciled historical ledger. Every new fill/cash reconciliation independently checks that assumption.

The standalone engine also supports probability sensitivity and researched directional cases. Those need timestamped sources, auditable probability ranges and at least 0.05 conservative expected profit per share after costs. The hosted workflow currently executes contract-consistency baskets; it does not autonomously collect polling, calibrate forecasts or run the paper turnover experiment.

## Risk limits

| Constraint | Limit |
| --- | --- |
| Total commitments including pending orders | 25,000 SUSQies |
| Commitment per hedged basket | 5,000 |
| Cash reserve | At least 75,000 |
| Directional maximum loss per race | 500 |
| Total directional maximum loss | 5,000 |
| Shared political factor exposure | 2,000 |
| Partial-fill repair loss budget | 10 |

Matched hedges are preserved. Price changes alone do not trigger stops on intact baskets. API requests are paced below 100 reads and 30 writes per minute per runner.

## Components and validation

- decision_engine.py: payoff scenarios, executable depth, sensitivity and forecast evaluation.
- trading_bot.py: reconciliation, bounded planning, fills and repair.
- hosted_runner.py: durable GitHub checkpoints and hourly execution.
- sig_snapshot.py: read-only exchange snapshots.
- bot_config.json: exact contract watchlist and operating controls.
- .github/workflows/sig-management.yml: scheduled and manual runs.

Python 3.10 or later; standard library only. Run python -m unittest discover -s tests -v. All 39 tests pass, including checkpoint-write failure, fresh-run recovery of an unknown request, duplicate prevention, partial fills, failed repairs, reserves, contract mismatch and cash reconciliation. Tests validate implementation behavior, not trading profitability.

For local use, provide SIG_API_KEY privately in the process environment. python trading_bot.py performs a dry-run; --live enables one execution review; --live --loop repeats hourly on a running machine. Do not run a local live loop alongside the hosted workflow.

A separate read-only hourly health review flags stale successful live sweeps after 90 minutes and cannot submit orders. A configured cron is not evidence of successful unattended execution.

Public reference review (6 October 2026): PredictionMarketTrader/openthomas illustrates calibration, settled-outcome feedback and bounded sizing; charlieyang1557/polymarket-arb illustrates cancel/fill state machines and negative execution results. Their reported venue-specific performance is not evidence of a SIG election edge. No third-party code is executed or supplied account credentials. Our existing margin-normal sensitivity engine requires a validation reference rather than treating poll vote share as win probability.

Durable checkpoints distinguish successful dry-runs from successful live sweeps; a failed run preserves the previous successful-live timestamp. The read-only health review uses that explicit heartbeat.

If a held basket cannot be verified during its sweep, all new entries halt; the bot records the failed basket rather than silently treating the portfolio as fully managed.
