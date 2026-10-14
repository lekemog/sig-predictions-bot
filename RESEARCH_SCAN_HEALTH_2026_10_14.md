# Research scan and entry-pause verification — 14 October 2026

Observed 02:35–02:40 BST. Research only; no oversight orders or workflow dispatches.

## Entry pause adopted

Checkpoint last_successful_live_sweep: 2026-10-14T01:13:44.721766+00:00,
live success, 12 baskets. All 12 current-cycle screened events and entry_blocked
events report "New entries paused for drawdown review". Result NO_TRADE;
no unresolved journal jobs and no resting orders in the review.
Run: https://github.com/lekemog/sig-predictions-bot/actions/runs/38494443804

Fresh review at 2026-10-14T01:12:40.047255+00:00: cash 65121.32,
cost basis 34913.19, holdings valuation 34861.31, account value 99982.63,
reported period P&L -17.37 and unrealized P&L -51.87. All configured basket
quantities matched. No configured limit exceeded. These are timestamped marks,
not executable liquidation proceeds or guaranteed settlement values.
Universe discovery complete: 237 contracts at 01:13:47 UTC; live scope remains 12 baskets.

## First hosted public-venue collection is partial

Run: https://github.com/lekemog/sig-predictions-bot/actions/runs/38494793616
Job: https://github.com/lekemog/sig-predictions-bot/actions/runs/38494793616/job/115627991179
Scheduled run began 01:16:20 UTC, collector finished 01:18:13 UTC.
Workflow succeeded and preserved an artifact, but collector status is partial:
Polymarket discovery HTTP 422, candidate_book_count 0, book_count 0,
comparison_status UNKNOWN, calibrated_probability null.
Original log's book_coverage_complete=true is vacuous for zero discovered books
and does not establish full available-universe coverage.

Root cause of HTTP 422 is not established. Artifact download timed out; no
complete catalog counts or market-price observations are asserted here.
No empirical probability adjustment or directional opportunity is supported.
One scheduled run does not establish dependable ten-minute cadence.

## Narrow maintenance mitigation

Preserve pages already received if a later discovery request fails; record the
failing page/offset and keep completeness false. Require catalog completeness
before claiming complete book coverage. This mitigates data loss and misleading
coverage reporting; it does not claim to resolve the HTTP 422 cause.
23 collector/cross-venue regression tests passed locally, including late-page
failure, zero-candidate partial discovery and partial-catalog book retention.
No schedule, execution configuration, limits or SIG interfaces changed.
Hosted validation and fresh recovery remain outstanding after deployment.

## Political review

Two targeted queries found SurveyUSA's original index listing North Carolina
poll #28047 dated 13 October for WRAL/WSOC:
https://results.surveyusa.com/PollHistory.aspx
The linked report returned an empty body:
https://results.surveyusa.com/client/PollReport.aspx?g=0a876c84-9a95-4995-9218-82b1015f5d77
Toplines, fieldwork and sample were not independently retrieved; no numerical
model update is made from secondary snippets. Previously verified Texas and
Carolina Forward evidence is unchanged. Win-probability bounds remain UNKNOWN;
sensitivities include turnout, undecideds, sampling/design and common polling
error. SIG executable exit books, synchronized cross-venue quotes and complete
bookmaker odds are unavailable to this review, so no net-edge ranking is made.

Google Apps Script installed timer/deployment remains unverified; this cycle
confirms native GitHub scheduled execution, not a fresh Google trigger audit.

## Oversight refresh — 14 October, 13:40 BST

GitHub connector returned 404; authorized signed-in browser access succeeded. No oversight dispatches or order operations.

Live scheduled run https://github.com/lekemog/sig-predictions-bot/actions/runs/38560441539 used ce689b12873e552d8f9937c40b1483c8f8fac735. Checkpoint 0ccf685: last_successful_live_sweep 2026-10-14T12:00:21.678989+00:00, success, 12 baskets; all 12 current-cycle screened. NO_TRADE, 25 done / 5 dry_run journals, no unresolved jobs. Review 11:59:16 UTC: cash 65121.31, orders [], basis 34913.19, account value 99816.41, period P&L -183.59, unrealized -218.09. Same valuation already reported, not a new mark. All held baskets matched; each basis below 5000. No tested whole-basket exit reached the normal payout floor.

Config: entries_paused=true; committed 100000, reserve 25000, race 1500, directional 15000, factor 6000. Defensive gates 250 shares, 1000 initial cost, 3% settlement ROI, 1% round-trip loss, account floor 99750. Costs unverified in config. No controls changed. Universe discovery 12:00:24 UTC: 237 records, complete discovery, zero relationship leads/submissions. Live scope remains 12 baskets; catalog completeness is not all-market executable-price/settlement verification.

Venue run https://github.com/lekemog/sig-predictions-bot/actions/runs/38559349195 collected 11:50:08–11:52:48 UTC on 8077cd0. Status partial: 300 / 1066 candidate books, Kalshi discovery runtime budget reached, Polymarket HTTP 422 at offset 2100. Coverage false; comparison UNKNOWN, calibrated probability null. Scheduled venue runs at 02:16, 07:12 and 12:49 BST do not establish ten-minute cadence. Exact reviewed settlement mappings, synchronized SIG books, verified venue quote timestamps and calibrated integration remain blockers. Installed Google timer/trigger and paper-turnover checkpoint not accessed; current state unknown.

Two targeted news queries and primary retrieval found Suffolk Maine release indexed 14 October: fieldwork 8–11 October, 500 likely voters, Jackson 49%, Collins 47%, undecided 3.8%, refused 0.2%. Original: https://www.suffolk.edu/-/media/suffolk/documents/academics/research-at-suffolk/suprc/polls/other-states/2026/10_14_2026_maine_statewide_marginals.pdf?hash=7FCED62D765CBEF2DB2EFD7ADF0213034C981FE0&la=en
No Maine contract in retrieved SIG universe; not executable or held. No win probability inferred. NC SurveyUSA original report again empty; secondary toplines unverified. Marist Texas already reported; direct release retrieval failed this run. No new actionable net-EV opportunity. Probability bounds and complete bookmaker odds UNKNOWN. Sensitivities: turnout, undecideds, design effects, correlated poll errors. No no-vig normalization or invented blending weights. Research only.


## Oversight refresh — 14 October, 14:31 BST

Browser access verified after connector 404. Run https://github.com/lekemog/sig-predictions-bot/actions/runs/38568779852 on f33b1f0: last_successful_live_sweep 2026-10-14T13:05:41.527477+00:00, live success, 12 baskets. NO_TRADE; 25 done / 5 dry_run journals; orders empty; matched holdings. Entries paused. Review 13:04:36 UTC: cash 65121.31, basis 34913.19, account value 99824.02, P&L -175.98, unrealized -210.48. No basket basis above 5000 or tested exit meeting payout floor. Universe discovery 13:05:44 UTC: 237 contracts, complete catalog, zero relationship leads/submissions. No oversight orders, dispatches or control changes.

Newly verified primary source: UT/Texas Politics Project release dated 13 October, secondary coverage 14 October; fieldwork 25 September–5 October, 861 likely voters, reported MOE +/-3.34pp. Senate Talarico 50%, Paxton 47%, Ted Brown 1%, someone else 2%; Governor Hinojosa 49%, Abbott 48%. Original: https://texaspolitics.utexas.edu/blog/latest-ut-texas-politics-poll-finds-historic-levels-of-competition-in-texas-midterm-elections

Research only. Texas Senate held; Texas Governor discovered but unconfigured/not held. Shared turnout and party-error sensitivities; no fresh fieldwork wave or independent race signals. Win-probability bounds UNKNOWN. Synchronized executable SIG/Kalshi/Polymarket depth, complete bookmaker prices, calibrated model and verified fees unavailable; no no-vig normalization or net-EV ranking. No validated news-to-live adapter: do not enable directionals. Installed Google timer and paper-turnover checkpoint not accessed; unknown. No newer venue run visible than previously recorded 38559349195; partial coverage and ten-minute cadence remain unverified.
