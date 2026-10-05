# Drive the real site with a headless browser

## Context

Submissions must reach a shared, externally-operated competition leaderboard.
Two ways to do it: call the `replace_my_daily_steps` RPC directly, or drive the
site's own upload UI in a headless browser and let the site do what it does.

## Decision

Drive the real site with headless Firefox + geckodriver. The bot logs in as the
user, sets the file input, waits for the site's "Detected steps" value, presents
it for confirmation, sets the Activity Date, and clicks Submit.

## Why

The site's OCR is the part of this system we did not write and cannot match
cheaply. Verified against real iPhone Health screenshots containing ~10
competing numbers: it correctly extracts the day total (2,831 / 6,532) and
correctly refuses an image with no digits. Reimplementing that would mean owning
number-recognition correctness into a leaderboard other people are competing on.

The decisive factor is the failure mode. A browser-driven bot that breaks
because the page was redesigned is *down* — visible, recoverable, no data
damage. An RPC bot that misreads a screenshot writes a wrong number that
silently overwrites the correct Submission for that date, because
`replace_my_daily_steps` is an upsert-by-day. That failure is neither obvious
nor cleanly reversible by us.

There is no manual-entry escape hatch on the site. "Wrong number?" only offers
Retake. So a misread is not merely wrong, it is wrong with no user-side fix
short of an admin amendment.

## Consequences

- ~400MB RAM and a Firefox+geckodriver install on the host. Constrains host
  choice (Oracle Always Free 1GB VM is acceptable; smaller targets are not).
- ~10-20s per Submission end to end. Acceptable at one or two a day.
- The bot is coupled to the site's DOM. Any redesign breaks it, and it must fail
  loudly ("the site looks different, I need a human") rather than clicking
  something it does not recognise.
- Committing is gated on reading the Detected Steps value and checking it
  against a plausible band, so an implausible read never reaches Submit.
- The bot holds the user's real Site password to log in. Accepted: it is the
  same credential the user types into the browser, and the bot can only ever
  write that one user's Submissions.
