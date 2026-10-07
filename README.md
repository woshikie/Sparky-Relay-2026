# Sparky Relay 2026 — Telegram bot that relays step screenshots to the Olympics 2026 site

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

Prerequisites: Python 3.12+, and podman or docker with the compose plugin.

```bash
cp secrets.env.example secrets.env && vi secrets.env
```

`ACCESS_MODE` is mandatory — the bot exits rather than guess, because the
difference between the safest mode and the open one is who can write to a live
leaderboard account. See [ADR 0006](docs/adr/0006-access-modes-and-prompted-credentials.md).

Build and start (POSIX shell — Linux and macOS):

```bash
podman build --format docker -t sparky-relay-2026:latest .
set -a; . ./secrets.env; set +a
podman compose up -d relay
podman compose logs -f relay
```

(`docker` works wherever `podman` appears above. `--format docker` matters:
podman silently drops the image HEALTHCHECK in its default OCI format. The
compose file repeats the check independently, which is what actually runs
under podman.)

Secrets reach the container as environment variables, so exporting them first
is required, not optional: `env_file:` is silently ignored by some compose
implementations (notably podman-compose 1.6.0), and without them the
container exits immediately with "missing configuration".

Windows (PowerShell — one `KEY=value` per line, `#` comments skipped):

```powershell
Get-Content secrets.env | Where-Object { $_ -match '=' -and -not $_.StartsWith('#') } | ForEach-Object { $k, $v = $_.Split('=', 2); Set-Item "env:$($k.Trim())" $v.Trim() }
podman compose up -d relay
```

Verify the image can read the site's OCR (submits nothing):

```bash
podman compose exec relay /app/.venv/bin/python -u check_container.py
```

## What is where

| File | Role |
|---|---|
| `relay/telegram/` | handlers, prompts, keyboards, access, copy |
| `access.py` | the Access Modes, behind one `check()` |
| `relay/site/driver.py` | headless Firefox: upload, read, date, commit |
| `relay/store/vault.py` | AES-GCM sealing for a supplied password |
| `relay/store/ledger.py` | SQLite hot rows: Submissions, site days, credentials |
| `relay/store/policy.py` | SQLite policy: grants, denials, secrets, throttle, identity |
| `relay/store/db.py` | SQLite plumbing both share: path, schema, connections |
| `relay/memory.py` | the RAM budget and the pre-flight that guards it |
| `relay/telegram/datepicker.py` | Telegram month-grid keyboard |
| `relay/telegram/words.py` | all user-facing copy |
| `relay/config.py` | secrets, from the environment or `secrets.env` |
| `relay/errors.py` | the app's exception types |

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m ruff check relay/ check_container.py check_lifecycle.py
.venv/bin/python -m ruff format --check relay/ check_container.py check_lifecycle.py
.venv/bin/python -m mypy relay/ check_container.py check_lifecycle.py
.venv/bin/python -m pytest tests/ --cov=. --cov-report=term-missing:skip-covered
```

(The lint-then-test sequence above is the whole gate: ruff, format check,
mypy with default settings, then pytest with branch coverage and a floor of
90%.)

`relay/site/driver.py` drives a real browser and is covered by
`check_container.py` (see "Running it" above), which runs the OCR path
against the live site inside the container. The unit suite covers everything
around it — parsing the site's response, driving the calendar, refusing to
commit — against a fake driver.

`check_lifecycle.py` exercises the same path bare-metal (launch, login, read,
close, memory back to baseline). It needs a local geckodriver and Firefox:
download geckodriver v0.36.0 from
https://github.com/mozilla/geckodriver/releases (matching `bin/` on `PATH`
or `GECKODRIVER_PATH`), matching your architecture, and point `FIREFOX_BIN`
at your Firefox if it is not at `/usr/bin/firefox`. The image builds its own
copy at build time, so container-only developers never need this.

## Why it drives a browser

The site runs its OCR in the page (PaddleOCR v5 via ONNX Runtime Web, a 16.5MB
model). So the browser is the OCR runtime, not just a UI driver, and no lighter
engine can substitute — Lightpanda has no `createImageBitmap` and never fetches
the model at all. Measured and recorded in
[ADR 0004](docs/adr/0004-browser-lifecycle-on-1gb-host.md).

Because of that, the browser is launched per Screenshot and closed afterwards:
~640MB for ~25 seconds, twice a day, instead of resident forever.

## Deploying

Built for an always-on host, running the container above. On Oracle Cloud,
the 1GB Always Free AMD micro is the target — tight but workable with the
per-Screenshot browser lifecycle. One host prep matters more than any other:
without swap, a browser launch can OOM-kill the bot; with it, the kernel
evicts cold pages instead and the run survives.

```bash
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=10
```

Note Oracle quietly halved the ARM A1 free allowance from 4 OCPU/24GB to
2 OCPU/12GB in June 2026.

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