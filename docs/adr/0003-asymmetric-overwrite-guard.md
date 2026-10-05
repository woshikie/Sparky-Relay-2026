# Asymmetric overwrite guard

## Context

The site keeps one Submission per Activity Date and silently discards the
previous one on re-upload, deleting the old proof image. The Relay's SQLite
ledger can therefore detect that a date already has a Submission, and what value
it holds.

## Decision

Challenge the write when the newly Detected Steps is **lower** than what is
already recorded for that Activity Date. Allow a higher value through without
asking, and always show the Activity Date being written.

## Why

The two directions have different meanings. A higher number is nearly always a
later, fuller screenshot of the same day — the normal case, and asking about it
every evening is pure friction. A lower number is nearly always a misread: a
blurry photo, a partial day view, the wrong screenshot. Losing 5,000 steps to a
bad read is a silent, self-inflicted loss, because the upsert erases the good
value with no history to recover from.

The user asked for both this asymmetry and confirmation on every write; this
satisfies the second by making every Commit a Confirmation anyway, with the
overwrite warning as an extra, differently-worded prompt in the risky direction.

## Consequences

- A legitimate downgrade (retracting inflated steps) requires extra taps. Rare
  and intentional.
- The guard depends on the ledger being current, so it degrades to "no warning"
  after ledger loss. Backed up off-host nightly to keep that window small.
- The user's Activity Date is always echoed in the Confirmation, so a misfile is
  visible before Commit rather than after.
