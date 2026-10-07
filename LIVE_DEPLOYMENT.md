# Deployment status and activation boundary

As verified 7 October 2026 around 15:46 Dublin, the last scheduled execution
was run 37597618534 at 10:00 Dublin. Its successful live checkpoint is
2026-10-07T09:02:15.183085Z. No newer scheduled execution appeared. The
15:38 read-only research job is not a live sweep. The execution workflow
remains enabled with the approved cron and sole ownership of placements.

## Implemented in this change

- Submission rejects missing prices, duplicate exchanges and invalid side,
  action or tick prices before any request intent is persisted.
- Directional entries cannot make an existing configured basket unmatched,
  even when that basket currently has no holdings.
- The collector retains actual market and relationship metadata, including
  when the API returns no relationships. Discovery is not depth coverage.
- A restartable GET-only observation process records up to three watched
  exchanges once per minute and refreshes the complete open catalogue every
  ten minutes. Its SQLite disk is durable on a configured persistent host.
  It stops at the tournament cutoff, excludes competing local instances,
  counts retry attempts and never writes orders or dispatches workflow jobs.
- Exit trace auditing measures observed depth opportunities. It rejects stale,
  duplicated, out-of-order and fabricated observations. Observations are not
  fills; the report explicitly refuses to claim live model validation.

## What this does not activate

The observation process has no signal-to-order adapter. Its service unit is
a deployment template, not a provisioned or running host. No live market
maker, directional turnover model or increased risk profile is activated.
The existing engine still implements the configured basket strategy.
Multiple scanner processes do not justify multiple live executors.

Reliable execution needs a provisioned persistent host or reliable external
trigger for the existing GitHub owner. GitHub cron delivery itself is outside
this code's control. A workflow reset or code change cannot establish future
cadence reliability. A physical laptop is unnecessary; this chat's transient
workspace is not a persistent production host.

## Concrete remaining launch requirements

1. Select and authorize a persistent hosting account, secret provisioning and
   an owner handover mechanism. Confirm old/new execution cannot overlap;
   transfer the exact durable request journal and reconcile live account data
   before any new placement. Do not start this from oversight.
2. Collect chronological executable books for the actual candidate, then
   validate an exit model on a later, separate sample. Include costs,
   adverse selection, partial fills, cancellation/N/A and capital duration.
   An exit trace or synthetic unit tests do not establish profitable trading.
3. Complete and test execution integration against that validated model,
   preserving the existing journal, idempotency and reconciliation path.
4. Agree exact numeric limits. A blanket tenfold multiplier is internally
   inconsistent: the current 25,000 committed cap would become 250,000 against
   approximately 100,000 historical account value. Revised reserve, total,
   basket, clip, directional race/total and shared-factor ceilings need a
   coherent explicit profile. Existing limits remain unchanged.
5. Activation through consequential prize-based trading controls requires
   the user's final action under browser handoff rules. Maintenance code may
   be reviewed and tested before that point; do not represent it as activation.

## Observation host setup (not performed)

The included `sig-research.service` expects a dedicated `sigbot` OS user,
reviewed code in `/opt/sig-predictions-bot`, a writable private durable directory
`/var/lib/sigbot`, a JSON watchlist of at most three numeric exchange IDs, and
an operator-provisioned secret environment file `/etc/sigbot/research.env`.
Never put a key in git, logs, command-line arguments or a task prompt.
The research ceiling of twenty reads per rolling minute is a sub-budget, not
a platform-wide lock. Coordinate all API consumers; independent hosts do not
share SQLite read counters. Do not enable this concurrently with the live
owner without allocating the common account quota.

For tests, no key is needed:

```
python -m unittest discover -s tests -v
python -m unittest test_market_brain test_research_service -v
```
