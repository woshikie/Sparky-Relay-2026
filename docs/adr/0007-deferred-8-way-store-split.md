# Deferred: the 8-way store split

## Context

The sqlite file holds 8 table groups: `submissions`, `site_days`,
`credentials`, `access`, `denied`, `shared_secrets`, `secret_attempts`,
`telegram_users`. After the volatility split (`db.py` plumbing,
`ledger.py` hot rows, `policy.py` slow policy), the question was whether
to go further: one module per table group.

## Decision

Defer. The groups stay clustered by rate of change, not by table name.

## Why

A residual audit (every `ledger`/`policy`/`db` touchpoint, every test
that reaches the store) showed the groupings are already clean: three of
four handler modules touch a single group, and the two multi-group
readers (`on_status`, `backup_job`) are multi-group by definition — a
split would multiply their imports rather than reduce them. `access.py`'s
breadth is facade coherence, and `store.access_control` next to
`telegram/access.py` would be a confusion factory. The split optimizes
for taxonomy, not for how the code changes.

## Consequences

- Revisit when one of these fires: a schema change forcing coordinated
  migration, a third raw-SQL bypass outside `access.py`, or a handler
  needing a multi-table write transaction spanning groups.
- Until then, new tables default into `ledger.py` (hot) or `policy.py`
  (slow) by volatility, and `preset_choice` is the precedent: hot-adjacent
  per-chat state went to `ledger.py`.
