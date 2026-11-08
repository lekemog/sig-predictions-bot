# Deployment and activation

GitHub remains the only SIG execution owner. The approved cron, shared
concurrency group, durable request journal, idempotency and fill reconciliation
remain in place. A physical laptop is unnecessary.

## Persistent trigger

`render.yaml` provisions one Render background worker with a persistent disk.
`dispatch_service.py` calls GitHub only; it never accesses SIG or its key.
The default is `DISPATCH_MODE=dry-run`. It checks workflow/checkpoint health
and active runs, journals the trigger before sending, and waits at least twenty
minutes between attempts. An ambiguous POST is never retried: GET-only matching
against the correlation ID can establish acceptance, otherwise triggering stops
for review. Acceptance is not a successful live sweep.

The Render account is not connected or provisioned by this change. Only the
account owner can complete these secure setup steps:

1. Add the suggested Render integration and connect your Render account.
   Complete Render account/billing setup if required; this worker needs paid
   compute and a persistent disk. Review Render's displayed recurring price.
2. At GitHub Settings > Developer settings > Personal access tokens >
   Fine-grained tokens, create a token limited to `lekemog/sig-predictions-bot`.
   Grant Actions read/write and Contents read, with expiry after the tournament
   cutoff (for example 5 November 2026). Enter it directly into Render's secret
   `GITHUB_DISPATCH_TOKEN`, never chat, source or logs. Do not copy `SIG_API_KEY`.
3. Keep the worker in dry-run until its deployment, accepted correlated GitHub
   run, completed screening and durable checkpoint have been inspected.
   Final live activation is an owner action: set `DISPATCH_MODE=live` on Render.
   Browser rules require a human final action for prize-based trading activation.

Once the integration is connected, deployment and log inspection can be handled
through it where supported. There is no need for multiple order-writing bots.
Never provision a second worker sharing this portfolio; its single persistent
journal and GitHub concurrency protect the execution path. Preserve the disk
across redeployments. Do not erase an unknown dispatch to force a retry.

## Staged scale profile

On 7 October the owner delegated numerical limit selection. The inactive file
`bot_config.aggressive.json` contains a coherent profile: committed 75,000,
reserve 25,000, basket 15,000, matched clip 5,000, directional race 2,500,
directional total 15,000 and shared factor 7,500. Displayed depth, costs and
budget can reduce clips. `bot_config.json` retains the existing live limits.
An activation change should copy the staged configuration only after fresh
account reconciliation, zero unresolved requests and hosted validation. The
owner performs the final activation; maintenance is not proof of activation.

## Strategy evidence

The existing basket engine can enter and exit matched baskets. Larger sizing
increases exposure, not the edge per share. Whole-basket exits still require
the normal payout floor net of costs. The additional political turnover and
market-making research has no validated live signal adapter yet. The GET-only
observation service and exit trace auditor collect real books; they must not
reconstruct fills or imply profitable model validation from synthetic tests.

All services stop at 2026-11-04T17:00:00Z. No secrets are committed. No live
workflow is dispatched by this maintenance change.

Validation:

```sh
python -m unittest discover -s tests -q
python -m unittest test_market_brain test_research_service test_dispatch_service -q
```
