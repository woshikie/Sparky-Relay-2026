# Step Relay — Domain Model

A Telegram bot that forwards personal step-tracker screenshots to the internal
"Olympics 2026" competition site, and confirms each submission with the user
before it is recorded.

## Language

**Relay**:
This bot, and the act of moving one day's steps from a screenshot into the
competition ledger. The bot never decides the step count — it reads what the
site's OCR reports and asks the user to approve it.
_Avoid_: uploader, poster, bot, submission bot

**Site**:
The internal Olympics 2026 competition application the steps are recorded in.
Not ours; operated by the CIO Office.
_Avoid_: the app, the website, the platform, sparky (the internal codename)

**Screenshot**:
A photo of a personal step tracker's day view, forwarded to the Relay.
_Avoid_: proof, attachment, image

**Detected Steps**:
The step count the Site's OCR reports for a Screenshot, shown to the user for
approval. Distinct from the number finally recorded, which is always the
Detected Steps — the user cannot type a different one.
_Avoid_: OCR result, parsed steps, recognized steps

**Activity Date**:
The day the steps belong to, chosen by the user. Defaults to today (Singapore
time), but is frequently yesterday's, because a day's steps are uploaded at
end of day.
_Avoid_: submission date, upload date, today

**Confirmation**:
The one Telegram message per Screenshot that shows the Detected Steps and the
Activity Date, and offers Confirm / Cancel. Nothing is recorded until Confirm.
_Avoid_: prompt, preview, dialog

**Backfill**:
Recording steps for an Activity Date in the past — for example uploading
Saturday's Screenshot on Sunday. The mechanism is just the user choosing an
Activity Date earlier than today; the Site treats it identically.
_Avoid_: late upload, retroactive entry, correction

**Overwrite**:
Recording a second Submission for an Activity Date that already has one. The
Site keeps exactly one per day and silently discards the earlier entry,
including its proof image. Always asks first, and the check is asymmetric:
downgrades are challenged, upgrades are not.
_Avoid_: update, edit, replace, correction

**Submission**:
One recorded row in the Site's ledger: an Activity Date plus a Detected Steps
value. The unit of success the Relay is judged by.
_Avoid_: row, entry, log line

**Cutoff**:
2026-11-02 15:59 UTC, after which the Site refuses new Submissions.
_Avoid_: end date, deadline, close date

**Proof image**:
The copy of the Screenshot the Site stores alongside a Submission, kept only
until a configured retention date. The Relay uploads it because the Site
reviews Submissions, not because it is required.
_Avoid_: evidence, attachment, audit trail

## Session and identity

**Internal Email**:
The synthetic address the Site authenticates: `<username>@olympics.local`.
There is no real mailbox; the Site's identity provider is the only consumer.
_Avoid_: login, account email

**Session**:
A bearer token from the Site's identity provider, stored on the Relay and
renewed on expiry. Holds the authority to write one user's Submissions — the
Relay can never write anyone else's.
_Avoid_: auth token, login, credentials

**Identity seam**:
The one place that would change if the Relay ever served more than one person:
where a Telegram chat resolves to a Site Session. Today that is a single
hard-wired pair, deliberately, so the multi-user case is a change rather than
something half-built.
_Avoid_: user table, tenant, profile layer

## Vocabulary to watch

**House** has one meaning here and it is not the building: Padang, Istana,
Raffles, Temasek, Esplanade. A participant belongs to exactly one House, and
the House is the unit that wins. Esplanade is the user's House.
_Avoid_: team, side, group, house (as in residence)

**Steps** are the raw input; **points** are what the competition scores, and the
conversion (1,000 steps per point, 20 points per day cap, doubled on a bonus
day) is admin-configurable and read live. Never hardcode it.
_Avoid_: score (ambiguous between steps, points and medals)

**Log** is overloaded in this project. The Relay's activity narration in
Telegram is the *Bot Log*. The Site's audit table of who did what is the *Audit
Trail*. The Site's list of what you submitted is your *Submissions*.
_Avoid_: using bare "log" for any of the three
