# Confirmation queue: before vs after

Rapid uploads confused the bot: both keyboards retargeted the newest
Screenshot, so Confirm could commit a different Screenshot than the one
shown. The queue gives one chat at most one live confirmation; later
arrivals wait in FIFO and are presented as each active resolves.

## Before (max-wins fallback)

```mermaid
sequenceDiagram
    participant U as User (shot 1 open)
    participant B as Bot
    U->>B: tap Today on shot-1 buttons
    B->>B: latest_for_chat → (1, 101): shot 2!
    B->>U: "Confirming **7,000** steps…"
    Note over U,B: confirming a Screenshot the user never opened
```

## After (FIFO + advance)

```mermaid
sequenceDiagram
    participant U as User
    participant B as Bot
    U->>B: shot 2 while shot 1 is live
    B->>U: "Queued as #1" (site read shown, no buttons)
    U->>B: tap Today on shot 1
    B->>U: "Confirming **1,000** steps…"
    U->>B: Confirm
    B->>U: Recorded, then the date prompt for shot 2
    Note over U,B: every tap lands on the Screenshot in front of the user
```

## Why this shape

- **Read at intake, confirm in turn**: the browser work is unchanged;
  only the staging order changed, so RAM behavior is identical.
- **Queued items carry no buttons**: a tap cannot address them, which is
  what makes the wrong-Screenshot outcome structurally impossible rather
  than merely unlikely.
- **Advance on every resolve** (commit, cancel, failure, expiry): the line
  moves however the head leaves, so nothing wedges and nothing starves
  short of TTL or restart (both accepted, both documented in
  `telegram/pending.py`).
