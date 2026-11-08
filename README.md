# SIG Predictions Cup Bot

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

An automated, risk-controlled trading system I built for the **Susquehanna (SIG) Predictions Cup** — a five-week prediction-market competition on the 2026 US midterm elections. Every entrant started with 100,000 play-money "SUSQies"; the ranking was the final balance.

The system ran unattended on GitHub Actions, placed real (play-money) orders through SIG's API, and was engineered around one question: *how do you trade automatically without ever doing something you can't explain or undo?*

> **Status: retired.** The competition closed on 4 Nov 2026 at 17:00 UTC. Live schedules are removed and the runner refuses to trade after the cutoff. The code is published as an engineering portfolio piece, not as a trading recommendation. See [Results](#results) and [Lessons learned](docs/LESSONS.md) — including the part that went wrong.

## Results

| Phase | Dates | Strategy | Outcome |
| --- | --- | --- | --- |
| 1. Market-neutral book | 6–27 Oct | Hedged "no-lose" baskets across two dozen races | Account **$104.3k (+4.3%)** on 27 Oct, ≈ **350th of 1,795** traders (top ~19%) |
| 2. Concentrated value bet | 28 Oct–4 Nov | Large directional positions where SIG was cheap vs. Kalshi/Polymarket | One binary outcome (Mississippi Senate) went against the position |
| Final mark | 8 Nov | | **$86.3k (−13.7%)**; remaining markets awaiting settlement |

The honest summary: the hedged book did what it was designed to do — small, steady, low-variance gains — and then I deliberately traded that for variance in the last week, sized it too large, and lost on a ~10% tail event. Excluding that single position the account would have finished near $111k. [docs/RESULTS.md](docs/RESULTS.md) has the numbers and the post-mortem; [docs/LESSONS.md](docs/LESSONS.md) has what I'd do differently.

## How the strategy works

**Hedged baskets.** If you hold one NO share in each of *n* mutually exclusive party-winner contracts for the same race, you are paid at least *n − 1* at settlement (more if an unlisted candidate wins). Whenever the cost of the basket is below that floor — after depth, tick size and an execution-risk charge — there is a positive expected profit regardless of who wins.

**Expected-value sizing (`arb_engine.py`).** Instead of flat safety buffers, the planner computes the expected payout (including the small probability that one winner resolves two legs YES), charges execution risk estimated from the bot's own fill history (Beta-binomial prior over partial fills), and evaluates every order-book level boundary to find the depth-optimal clip.

**Capital recycling.** A held basket is sold early only if proceeds beat its settlement floor discounted by the best capital-blocked opportunity, so arbs are never dumped below what they pay when capital is idle.

**Relative value (`value_engine.py`).** SIG prices were compared against a conservative Kalshi/Polymarket bid–ask band; legs trading outside the band by a margin are candidates. This is the component used (too aggressively) in phase 2.

## Engineering highlights

- **Durable, idempotent execution.** Every order request and idempotency key is persisted to a checkpoint *before* the API call; a failed checkpoint write blocks the next submission. A lost response is reconciled against the account and replayed only as the identical request.
- **Fail closed.** Unknown outcomes keep their capital reserved; unverifiable holdings halt new entries; a single-owner concurrency group guarantees one executor at a time.
- **Multi-leg safety.** Fills are checked, resting remainders cancelled and unmatched legs repaired within a loss budget; every trade reconciles fills against the cash change.
- **Hard limits in code.** Limit orders only, tick-aligned, 90-second expiry, committed-capital / per-race / per-basket / reserve caps enforced both at planning and at submission, plus a global cutoff.
- **Read-only research stack.** Separate collectors (`venue_scan.py`, `market_brain.py`, `research_*.py`) that cannot import the executor, used to study cross-venue pricing and exit depth.
- **~290 offline tests** (no network, no credentials) covering checkpoint-write failure, unknown-outcome replay, duplicate prevention, partial fills, failed repairs, reserves and cash reconciliation.

## Repository map

| Path | What it is |
| --- | --- |
| `trading_bot.py` | API client, durable journal, planning, execution, reconciliation, risk limits |
| `hosted_runner.py` | GitHub Actions entry point with repository-backed checkpoints |
| `arb_engine.py` | Pure maths for basket EV, execution risk, depth-optimal sizing |
| `value_engine.py` | Kalshi/Polymarket reference prices and relative-value rules |
| `decision_engine.py`, `directional_model.py` | Payoff scenarios and a research-only poll-residual model (never validated for trading) |
| `market_brain.py`, `venue_scan.py`, `cross_venue_research.py`, `research_*.py` | Read-only research and data collection |
| `reconcile_only.py`, `exit_validation.py`, `dispatch_service.py` | Recovery, audit and scheduling utilities |
| `bot_config.json` | Watch-list and operating controls as run on the last day |
| `tests/`, `test_*.py` | Offline test suite |
| `deploy/` | Alternative scheduler experiments (Apps Script, Render, systemd) |
| `docs/design/` | Design notes for each subsystem |
| `docs/archive/` | Raw working logs and reviews kept for transparency |

## Running the tests

Python 3.10+, standard library only.

```sh
make test          # or: SIG_TRADING_END=2099-01-01T00:00:00+00:00 python -m unittest discover -s tests && ... -s .
```

`SIG_TRADING_END` is a test/replay hook: the production cutoff (4 Nov 2026) is the default, and the offline suite needs a later date to exercise trading paths after the competition. No test touches the network.

To dry-run against SIG you would need your own `SIG_API_KEY` in the environment (never commit it); the bot defaults to dry-run mode and now refuses to trade past the cutoff.

## Disclaimer

Play-money competition; nothing here is financial advice. Past results — including the losing ones — say nothing about future performance. Parts of this repository were developed with an AI pair-programmer (Claude Code); design decisions, risk limits and the trades described above were mine.

Released under the [MIT License](LICENSE).
