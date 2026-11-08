# Lessons learned

## Trading and risk
1. **Positive edge is not permission to size up.** Phase 2 had a modest paper edge (~5%) but I put ~30% of the account on one binary outcome with a ~10% chance of failure. Size from the tail you can survive, not from the expected value you hope for. Quarter-Kelly for this edge and price is roughly 8–9% of the account; make a rule like that a hard limit in code, not a judgement made late at night.
2. **Variance-seeking needs an explicit objective.** The prize structure (top three only) made some variance rational. But "I want a better rank" is not a plan; the decision should have been written down as: target, probability of reaching it, cost of failure — then sized accordingly. I did that analysis informally; it showed concentrating maximised the chance of the target but carried a meaningful chance of ruin, and I accepted that without writing down a maximum acceptable loss.
3. **Stops don't protect against binary events.** Election outcomes jump; a 30-minute loop with a stop-loss on a thin book is not hedging. If you need protection, hold an offsetting position.
4. **Cheap relative-value is only as good as the reference.** Kalshi/Polymarket were treated as truth. They were calibrated in aggregate, but a single 11% event is exactly what a 89% price means — roughly one in nine such bets should lose.
5. **Don't change the strategy in the last week.** The hedged book was working. The late redesign had no backtest, no paper-trading period and funded itself by selling profitable hedges at a discount.

## Engineering
6. **Make the execution path boring.** Persist intent before the side effect, use idempotency keys, fail closed on anything unverifiable. Across 400+ workflow runs, including a halted partial exit, recovery relied on reconciling against the account rather than resubmitting, which is what the journal was built for.
7. **Test the config, not just the code.** A test that pinned a cron string failed the validation step ahead of the sweep after I changed the schedule, so the sweep did not run until the test was fixed. Tests that assert config values should fail loudly *in CI*, not in the production pre-flight.
8. **Budget the platform, not just the strategy.** Scheduled runs started failing within seconds, with no logs, for several hours; the cause was most likely the free-tier Actions-minute allowance being used up by 10-minute schedules (the failures stopped after upgrading the plan). Compute the cost of your cadence up front; make it a configurable, documented number.
9. **Clock-dependent tests rot.** The suite passed during the competition and failed afterwards because production code (correctly) stops at a deadline. The deadline is now overridable by an explicit test hook so the suite is reproducible.
10. **Separate research code from execution code by import boundaries.** The read-only collectors cannot import the executor; that made it safe to iterate quickly on analysis while the live path stayed frozen.

## Process
11. **Name the owner of each decision.** Several risk-limit changes were requested and then reverted within days. Keep a changelog of limit changes with the reason and an automatic review point, so rollbacks are quick and don't look like panic.
12. **Write the post-mortem while it's fresh** — and publish it. This repository keeps the losing phase in the open on purpose.
