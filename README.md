# Relay — Telegram step bot for Olympics 2026

A single-user Telegram bot that forwards a screenshot of your step tracker to
the internal Olympics 2026 site and records it — but only after you confirm the
number and the date.

Send a screenshot. The bot uploads it through the site's own upload flow, tells
you the step count **the site read**, asks which day it belongs to, and records
it when you confirm. Nothing is written to the leaderboard until you press
Confirm.

## Why it drives a browser

The site does the OCR, and it is good: given an iPhone Health day view
containing a day total plus ~10 competing numbers, it returns the day total. The
bot never guesses a step count, and there is no manual-entry path on the site to
fall back to — "wrong number" only offers Retake.

The bot deliberately does **not** read numbers out of images itself. See
[ADR 0001](docs/adr/0001-browser-driven-submission.md) for the reasoning, which
comes down to failure modes: a browser-driven bot that breaks is *down*, while
an RPC bot that misreads a screenshot silently overwrites your real total.

## Requirements

- Linux with Firefox installed
- Python 3.11+
- ~600MB free RAM (Firefox + geckodriver)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Olympics 2026 site username and password

## Setup — container (recommended)

```bash
cp secrets.env.example secrets.env
vi secrets.env          # bot token, site username, site password
./build.sh              # podman or docker, Alpine base
./run.sh up -d relay    # loads secrets into the environment for you
./run.sh logs -f
```

Then message the bot on Telegram. The first chat to message it becomes the
authorised chat, and `/status` reports the id — pin it as `ALLOWED_CHAT_ID` and
restart so nobody else can drive it.

`./run.sh` exists because secrets reach the container as environment variables:
podman-compose 1.6.0 ignores `env_file:`, so the compose file interpolates them
from your shell instead. It also switches the log driver to `json-file`, because
the `journald` driver silently discards everything on a machine with no
systemd-journald and the bot then looks mute.

Verify the image can actually read a number before trusting it with a real
Submission — this signs in, uploads, and reads the site's OCR without submitting:

```bash
podman exec relay /app/.venv/bin/python -u check_container.py
```

## Setup — bare metal (Debian/Ubuntu)

```bash
./setup.sh              # venv, deps, geckodriver
vi secrets.env
./bot.sh
```

Needs ~600MB free RAM while a Screenshot is being processed. On a 1GB box, add
1GB of swap and `vm.swappiness=10` — see `deploy/provision.sh`.

## Commands

| Command | What it does |
|---|---|
| send a photo | upload, report the site's read, ask for the date, confirm, record |
| `/log` | recent Submissions from the local ledger |
| `/status` | browser, session, config, pending confirmations |
| `/help` | usage |

Date selection offers **Today**, **Yesterday**, and **Open datepicker**. The
picker only allows today and the past — you cannot submit a future day.

## Behaviour worth knowing

- **Nothing is recorded until you confirm.** The step count shown is the site's
  read, not the bot's guess.
- **Implausible numbers are refused, not submitted.** Outside 100–200,000 steps
  the bot stops and asks.
- **Downgrades are challenged; upgrades are not.** The site keeps one Submission
  per day and discards the previous one, deleting its screenshot. If a new
  screenshot reads *lower* than what is already recorded for that date, the bot
  asks before overwriting. A higher number goes through — that is normally just
  a fuller screenshot.
- **The date is verified, not assumed.** After driving the site's calendar the
  bot re-reads the control and compares it to what you chose. A mismatch aborts.
- **The site changing is a stop, not a guess.** If the upload page no longer
  looks right, the bot says so instead of clicking something unfamiliar.
- **The ledger is backed up to Telegram nightly**, so the overwrite guard keeps
  working even if the host is rebuilt.

## Files

| File | Role |
|---|---|
| `bot.py` | Telegram handlers and the confirm flow |
| `relay_site.py` | headless Firefox against the site: upload, read, date, commit |
| `ledger.py` | SQLite: Submissions per date, session, authorised chats |
| `memory.py` | the RAM budget, and the pre-flight that refuses to OOM |
| `datepicker.py` | Telegram month-grid keyboard |
| `words.py` | all user-facing copy |
| `config.py` | secrets, from the environment or `secrets.env` |
| `Dockerfile` | Alpine image with Firefox + geckodriver |
| `docker-compose.yml` | the local run: memory cap, shm size, healthcheck |
| `build.sh` / `run.sh` | build, and start with secrets loaded |
| `check_container.py` | in-container proof that the OCR works |
| `check_lifecycle.py` | on-demand browser + memory-return checks |
| `CONTEXT.md` | the domain language — read this before renaming things |
| `SITE-NOTES.md` | verified observations about the site the bot depends on |
| `docs/adr/` | why the big decisions went the way they did |

## Deploying

Designed for an always-on host; the bot is a long-polling process that must stay
up to answer at 7am. On Oracle Cloud, the 1GB Always Free AMD micro is the
target — tight but workable with the per-Screenshot browser lifecycle, and
`deploy/provision.sh` adds the 1GB of swap that makes it safe. Note Oracle
quietly halved the ARM A1 free allowance from 4 OCPU/24GB to 2 OCPU/12GB in June
2026, and terminates Always Free instances left above the new limits.

A systemd unit:

```ini
[Unit]
Description=Relay — sparky steps bot
After=network-online.target

[Service]
WorkingDirectory=/opt/sparky-relay-2026
ExecStart=/opt/sparky-relay-2026/bot.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Restarting is safe. The browser profile keeps the site session, and the ledger
survives, so a restart does not lose the overwrite guard.

## The 1GB question, honestly

The measured browser peak is 640MB, measured on a 32GB desktop where the page
cache was warm and nothing else was running. On a real 1GB VM the kernel has no
spare, and the 16.5MB ONNX model has to be *decoded* rather than just mapped.
`BROWSER_PEAK_MB = 780` in `memory.py` carries headroom, but it has not been
proven on an actual 1GB box.

The failure mode is safe rather than silent: `require_memory()` refuses to
launch and the bot tells you, nothing is submitted, and the ledger is intact.
Start at 1GB — it costs nothing to try and tells you the truth about your
actual workload. If a Submission does get OOM-killed, bump to a larger shape.
