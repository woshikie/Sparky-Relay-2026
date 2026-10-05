# Relay — Telegram bot that relays step screenshots to the Olympics 2026 site

A bot you send a step-tracker screenshot to. It uploads through the site's own
upload flow, reports the number **the site reads**, asks you to confirm the date,
and records it only when you press Confirm.

Nothing is written to the leaderboard until you confirm, and the site's own
OCR is the only thing that ever produces a step count.

## Commands

| Command | What it does |
|---|---|
| send a photo | upload, report the site's read, ask for the date, confirm, record |
| `/login` | supply or replace your site username and password |
| `/logout` | forget the stored credentials |
| `/log` | recent Submissions from the local ledger |
| `/status` | access mode, who you are signing in as, memory headroom |
| `/help` | usage |

Date selection offers **Today**, **Yesterday**, and a **datepicker**. A later,
fuller screenshot of the same day upgrades silently; a *lower* number is always
challenged first, because that usually means a blurry photo.

## Running it

```bash
make setup                 # venv, deps, geckodriver
cp secrets.env.example secrets.env && vi secrets.env
make check                  # run the test suite
make up                     # build the image and start it
make logs                   # follow
```

`ACCESS_MODE` is mandatory — the bot exits rather than guess, because the
difference between the safest mode and the open one is who can write to a live
leaderboard account. See [ADR 0006](docs/adr/0006-access-modes-and-prompted-credentials.md).

## What is where

| File | Role |
|---|---|
| `bot.py` | Telegram handlers and the confirm flow |
| `access.py` | the Access Modes, behind one `check()` |
| `relay_site.py` | headless Firefox: upload, read, date, commit |
| `vault.py` | AES-GCM sealing for a supplied password |
| `ledger.py` | SQLite: Submissions, credentials, grants, denials |
| `memory.py` | the RAM budget and the pre-flight that guards it |
| `datepicker.py` | Telegram month-grid keyboard |
| `words.py` | all user-facing copy |
| `config.py` | secrets, from the environment or `secrets.env` |
| `errors.py` | the app's exception types |

## Tests

```bash
make check           # pytest with branch coverage, floor 70%
make check-verbose   # with the coverage report
```

`relay_site.py` drives a real browser and is covered by `make verify`, which
runs the OCR path against the live site inside the container. The unit suite
covers everything around it — parsing the site's response, driving the
calendar, refusing to commit — against a fake driver.

## Why it drives a browser

The site runs its OCR in the page (PaddleOCR v5 via ONNX Runtime Web, a 16.5MB
model). So the browser is the OCR runtime, not just a UI driver, and no lighter
engine can substitute — Lightpanda has no `createImageBitmap` and never fetches
the model at all. Measured and recorded in
[ADR 0004](docs/adr/0004-browser-lifecycle-on-1gb-host.md).

Because of that, the browser is launched per Screenshot and closed afterwards:
~640MB for ~25 seconds, twice a day, instead of resident forever.

## Deploying

Built for an always-on host. On Oracle Cloud, the 1GB Always Free AMD micro is
the target — tight but workable with the per-Screenshot lifecycle, and
`deploy/provision.sh` adds the swap that makes it safe. Note Oracle quietly
halved the ARM A1 free allowance from 4 OCPU/24GB to 2 OCPU/12GB in June 2026.

## The 1GB question, honestly

The 640MB peak was measured on a 32GB desktop with a warm page cache and
nothing else running. On a real 1GB VM the kernel has no spare, and the 16.5MB
model has to be *decoded* rather than just mapped. `BROWSER_PEAK_MB = 780`
carries headroom but is unproven on actual 1GB hardware.

The failure mode is safe rather than silent: the pre-flight refuses to launch,
the bot tells you, nothing is submitted, and the ledger is intact. Start at 1GB
— it costs nothing to try and tells you the truth about your real workload.

## Further reading

- [CONTEXT.md](CONTEXT.md) — the domain language; read before renaming things
- [SITE-NOTES.md](SITE-NOTES.md) — verified observations about the site
- [docs/adr/](docs/adr/) — why the big decisions went the way they did