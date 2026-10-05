# example.invalid — recon notes

Static read of the served JS bundle plus headless-Firefox observation with the
user's own account. No auth probing, no credential guessing, no Submissions
written. Credentials live in gitignored `secrets.env`.

## Stack

- Vite + React + TanStack Router. Route tree is **client-side only** — direct
  navigation to `/upload` or `/dashboard` bounces to `/home`, so flows must be
  driven by clicking nav links.
- Supabase project `icdfrhuaybsnkguvcemn.supabase.co`, publishable key
  `sb_publishable_aYr4Ot1iLImP9TJwEloGpg_SMCQmCcV` (shipped in the client).
- Proof images in storage bucket `proofs`. Timeline math uses a hardcoded
  `+288e5` ms (UTC+8) offset, not a tz library.

## Identity

`signIn(username, password)` builds `<username>.trim().toLowerCase()@olympics.local`
and uses Supabase password auth. The `olympics.local` domain is synthetic.

Observed for user `<Original Author's username>`:

- user_id `42c9c729-2ffc-42f4-86a7-c32ac5116825`
- role `user` (`user_roles`)
- profile: `total_steps: 11225`, `total_points: 10`
- House **Esplanade** (rank 2 of 5 at time of recon)

## Tables / RPCs

| Name | Kind | Notes |
|---|---|---|
| `activity_logs` | table | one Submission per user per Activity Date |
| `profiles` | table | `total_steps`, `total_points` denormalised |
| `houses` / `departments` | table | houses ordered by `sort_order` |
| `events` | table | `is_extra_event`, `is_charity_run`, `sig_match` |
| `app_settings` | table | key/value, e.g. `banner` |
| `global_settings` | table | single row `id=true`, the scoring config |
| `user_roles` / `system_logs` | table | role, audit trail |
| `replace_my_daily_steps` | RPC | **the write path** |
| `admin_amend_daily_steps` | RPC | admin override |
| `get_leaderboard_cached` | RPC | leaderboard with `max_age_seconds` |

`activity_logs` columns: `id, user_id, activity_date, steps_added, raw_steps,
points_awarded, source, event_label, distance_km, image_url, verified,
created_at, admin_amended_at, admin_amended_steps, admin_remark`

`source` values: `ocr` | `manual` | `extra_event` | `charity_run`

### `replace_my_daily_steps`

```ts
rpc('replace_my_daily_steps', {
  _date: string,          // YYYY-MM-DD
  _steps: number,
  _distance_km: number | null,
  _image_url: string | null,
})
// -> { old_image_url, old_steps, old_distance_km, ... }
```

Upsert-by-day. The client then deletes `old_image_url` from storage, so an
overwrite also destroys the previous proof image.

Preflight before submitting: `SELECT id, raw_steps, steps_added, distance_km
FROM activity_logs WHERE user_id=eq.<uid> AND activity_date=eq.<date> AND
source IN (ocr, manual) ORDER BY created_at DESC LIMIT 1` — this is how a
client learns what is already recorded for a date.

## Scoring (live `global_settings`)

```
step_to_point_ratio   = 1000     // 1000 steps -> 1 point
daily_step_point_cap  = 20        // points, i.e. 20,000 steps
bonus_day             = 2026-10-30
bonus_multiplier      = 2
steps_per_km          = 1250
gold/silver/bronze    = 5000/3000/1000
cutoff_date           = 2026-11-02T15:59:00+00:00
full_launch_date      = 2026-09-30T16:01:00+00:00
```

**The bundle's fallback defaults are wrong** (they say ratio 100, cap 20000) —
a client that hardcodes either set scores incorrectly. Always read this row.

## OCR — verified behaviour

Runs in-page. No OCR endpoint was observable on the wire; no tesseract, no
wasm, no `.traineddata`, no `new Worker`, no `OffscreenCanvas` in any of the
80 served assets. `GET /ocr/dict.txt` was seen once on `/home` and is
unexplained — it may be unrelated to the upload path.

Empirical tests, all through the real UI in headless Firefox:

| Input | Detected Steps | Verdict |
|---|---|---|
| `Saturday-3-October.jpg` (iPhone Health, day total 2,831) | **2,831** | correct |
| `Sunday-4-October.jpg` (iPhone Health, day total 6,532) | **6,532** | correct |
| `zzz-alpha.png` — synthetic image showing 12,004, filename has no digits | **12,004** | real OCR, not filename parsing |
| `blank-qq.png` — no digits | *no confirm panel* | correctly refused |

It picks the day total out of iPhone Health screenshots that also contain ~10
competing numbers (per-activity steps, chart axis labels, clock times). iPhone
Health is *not* among the 9 supported apps (Samsung Health, Google Fit,
Healthy 365, Apple Health, Apple Fitness, Google Health, Strava, Nike Run
Club, Asics Runkeeper) and still works, so it is not template-matched.

Plausible range in the client parser: `100 .. 200000`, plus `1..99` only when a
template slot is matched. Tokens containing `%`, `lte`, `4g`, `5g`, `wifi` are
rejected; `away|remaining|deficit|to go` are filtered.

## The upload flow (observed)

1. `/upload` — mode toggle **Steps** | **Distance (km)**, then a tracker
   picker that sets which parser slot to use.
2. File input `accept="image/*"`.
3. Confirm panel appears: **"Detected steps N"** with the thumbnail, and
   **"Wrong number? Tap Retake and upload a clearer screenshot."**
4. **Date of activity** — defaults to *today*, opens a Radix calendar.
5. `Submit steps` / `Retake`.
6. Client downscales to 1200px, encodes webp@0.75 (jpeg@0.65 fallback),
   uploads to `proofs` as `{user_id}/{uuid}.{ext}`, then calls the RPC.

**There is no manual entry anywhere.** The Retake button is the only escape
hatch from a wrong number — no override field, no `/manual` route, no
`source=manual` UI. A wrong read cannot be corrected in the UI; only an admin
`admin_amend_daily_steps` can change it.

Banner on the page: *"Please upload authentic screenshots only. Submissions
will be saved and reviewed."*

## Proof retention

Uploaded only if `save_step_screenshots_until` is set and now < that date.
Client tolerates upload failure (warns, submits anyway).

## Date behaviour

Defaults to today (SGT) **regardless of the date in the screenshot** — an
Oct 4 screenshot uploaded on Oct 5 prefills October 5th. Backfill requires the
user to change it. Calendar allows past dates back well beyond the Cutoff.

## Not observed (would need an admin account)

- Whether there is anti-automation or rate limiting
- RLS policies on `activity_logs` (can a plain user insert directly, or must
  every write go through the RPC?)
- What the review process actually checks, and whether an amended Submission is
  distinguishable from a normal one to other participants
