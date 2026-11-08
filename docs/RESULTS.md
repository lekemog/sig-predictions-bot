# Results and post-mortem

All figures are SUSQies (play money) from SIG's portfolio API, the competition leaderboard snapshot of 27 Oct, and the bot's own checkpointed journal. Marks are mid-market valuations, not settled outcomes, unless stated.

## Timeline

| Date (2026) | Account value | Notes |
| --- | --- | --- |
| 1 Oct | 100,000 | Competition opens |
| 6 Oct | | Hosted runner goes live; 11 hedged baskets already matched |
| 14 Oct | ~99,800 | Flat: strict entry rules, mostly waiting for edge |
| 26 Oct | 103,590 (+3.6%) | Platform-reported Sharpe 5.6 |
| 27 Oct | **104,293 (+4.3%)** | Peak. ≈ 350th of 1,795 on the leaderboard |
| 28 Oct | | Concentrated value positions opened (Mississippi Senate, Oregon Governor) |
| 29 Oct | | Concentration rolled back after the first drawdown; open positions kept to settlement |
| 31 Oct | 102,927 (+2.9%) | |
| 1 Nov | 101,696 (+1.7%) | SIG's mark on the Mississippi leg drifting below the outside venues' fair price |
| 4 Nov 17:00 UTC | | Trading closes; runner refuses further orders |
| 8 Nov | **86,297 (−13.7%)** | Mississippi Senate leg marked near zero, settlement pending |

## Phase 1 — market-neutral baskets (6–27 Oct)

About two dozen races held as paired NO baskets whose settlement floor exceeded their purchase cost. Typical edge was 0.3–1.8¢ per share, so the strategy is capacity-bound: the account was almost fully deployed with ~$2k free cash and nothing left with enough edge to add. It earned about +4.3% on the whole account in three weeks with small daily variance.

Where that landed relative to the field: the leaderboard (1,795 traders on 27 Oct) was dominated by high-variance accounts — the top three displayed gains of +$880k to +$1.02M, roughly 9–10× the starting balance, which is only reachable with heavily concentrated or levered directional exposure. The hedged book was in the top ~19% without taking any directional risk.

## Phase 2 — the late-week value bet

**Motivation.** Prizes paid only the top three ($30k / $5k / $2.5k), and rank 3 needed ~+880k. Top-100 was a more realistic target but needed about +15–20% on the account, which the hedged book could not deliver in time. I chose to buy variance.

**Mechanism.** The bot treated Kalshi and Polymarket as fair value and bought SIG contracts priced below their bid–ask band. For the last week I raised the per-race cap from 3k to 50k, allowed the bot to sell hedged baskets (≈2–3% under their floors) to fund purchases, and concentrated in the two races with the best combination of edge and payout.

**Position.** ~36k Mississippi Senate shares (Republican win, bought at ~82¢; outside venues implied ~89%) plus a smaller Oregon Governor position. The Mississippi cost basis was ≈ $29.9k.

**Outcome.** The ~11% tail happened: the Mississippi position was marked at ≈ $5.3k at the last valuation, a loss of ≈ $24.6k. Excluding that one position the account would have ended near $111k. The funding exits cost another ≈ $0.8k.

### Why the protections did not help
- A stop-loss (`value.stop_loss`) existed in some versions but a binary event does not drift: fair value went from ~89% to near zero within hours on election night, through 30-minute sweeps and a thin book at 5¢. The only real protection is sizing.
- The concentration changes were rolled back on 29 Oct once the first mark-to-market loss appeared, but the open positions were deliberately left to settlement rather than sold at SIG's below-fair bids.

## What the numbers say about the strategy
- Phase 1 had positive, low-variance returns but is capacity-limited; scaling it would need more races, more venues or leverage — not more clip size.
- Phase 2 had a positive expected value on paper (≈ +5% edge on ~$30k) but a catastrophic-for-the-goal tail: ~30% of the account on a single ~10% event. With quarter-Kelly sizing (≈ 8–9% of the account for this edge and price) the position would have been about a third of the size, and the loss correspondingly smaller.
