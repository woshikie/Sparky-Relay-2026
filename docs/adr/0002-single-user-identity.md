# Single-user identity, held as one hard-wired pair

## Context

Today the Relay serves exactly one person. The user asked for a personal bot
now, with a seam available later if they end up uploading on behalf of a whole
house of colleagues.

## Decision

Resolve Telegram chat -> Site Session through a single hard-wired pair of
values: one authorised chat id, one Site username/password. No user table, no
tenant layer, no per-sender credential lookup.

## Why

The multi-user version has a real problem the single-user version does not:
per-person Site credentials for a house of colleagues, each holding standing
write authority to a shared leaderboard, plus the question of whose screenshot
is whose. Building that speculatively would be building it badly and untested.

Hard-wiring makes the constraint explicit in the code. If the house-scribe case
arrives, the change is localised to one resolver function rather than
retrofitting identity into every handler.

## Consequences

- The multi-user change is anticipated but deliberately unbuilt.
- Any chat id other than the configured one is rejected. This is the bot's only
  authorisation check, so it must be correct.
- Adding users later means changing this decision, not just adding config.
