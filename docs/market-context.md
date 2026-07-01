# Market context: why this is a good engineering exercise, not a startup pitch

This project started as an evaluation of a potential AWS Marketplace
product idea: a dedicated "Cognito Consistency Platform" solving identity
sync, reconciliation, and audit for teams running Cognito + Postgres/MySQL.
Before building anything, the idea was researched and pressure-tested —
worth documenting that process honestly rather than presenting the code
as if it were validated as a business.

## What's real

- Running Cognito alongside a separate application database for business
  logic is a genuinely common architecture. Cognito doesn't support
  relational queries, so teams need a second store for anything beyond
  authentication.
- Teams do build this sync layer themselves, almost always as a Lambda
  trigger (Post Confirmation / Post Authentication) writing to
  DynamoDB or a relational database — this is AWS's own documented
  pattern, not a fringe workaround.
- Reconciliation, drift detection, and audit trails for that sync layer
  are *not* commonly built — most implementations stop at "fire the
  Lambda and hope it doesn't fail."

## Why this probably isn't a sellable product

- **The failure mode is low-severity.** When sync drifts, the usual
  outcome is a stale display name or a missing profile row — not a
  security incident, outage, or compliance violation. AWS Marketplace's
  biggest successes (CrowdStrike, Wiz, Datadog) solve problems with a
  direct line to revenue loss or security risk. A stale email field
  doesn't clear that bar.
- **It's cheap to build in-house.** The reconciliation logic in this repo
  (`src/reconciler/drift.py`) is under 150 lines and took a few hours to
  write and test. When the build cost is this low, the "buy vs. build"
  calculus strongly favors build, which kills willingness-to-pay for a
  packaged product.
- **The self-hosted delivery model doesn't monetize well.** A container
  that runs entirely inside the customer's AWS account, with no usage
  metering hook, has no natural land-and-expand revenue motion — compare
  to Confluent Cloud or MongoDB Atlas, which grow revenue as usage grows.
  Sync volume tracks 1:1 with signups, which isn't a growth lever a buyer
  feels.
- **Regulated buyers are the wrong target for a third-party identity
  tool.** HealthTech/FinTech/GovTech teams (the primary target market
  identified in early research) are typically *more* cautious about
  adding a new vendor with write access to both their identity provider
  and their production database, not less. That's more audit surface,
  not a shortcut past one.
- **Absence of a competitor is weak evidence of an opportunity.** No
  dedicated AWS Marketplace listing exists for this specific problem —
  but for a supposedly expensive, recurring pain in a space with $1B+
  vendors, that absence more likely means the pain isn't large enough to
  pay for, not that it's an untapped market.

## Why it was still worth building

The underlying pattern — synchronous triggers that must never block the
identity provider, a pure-function reconciliation core, an explicit
detect/remediate split, dead-letter replay, and an audit trail — is a
legitimate distributed-systems problem worth solving well, independent of
whether it's commercially viable as a packaged product. That's the value
of this repo: a small, correctly-scoped implementation of a real
production pattern, with the design trade-offs written down rather than
glossed over.

If you're evaluating this repo as a hiring signal: the interesting parts
to look at are the failure-handling design in the Lambda handlers, the
pure/impure split in the reconciler, and this document itself, which
represents having done the "should we build this" analysis before writing
code, and being willing to publish a "probably not, but here's why it's
still worth doing" conclusion.
