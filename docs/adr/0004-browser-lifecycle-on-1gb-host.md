# Browser lifecycle: launch per Screenshot, hold briefly for Confirm

## Context

The site does the OCR in the page. `ocrEngine.js` rasterises the Screenshot with
`createImageBitmap` + canvas, then runs PaddleOCR v5 mobile through ONNX Runtime
Web (WASM, single-threaded, a 16.5MB recognition model plus a detection model
and a 74KB dictionary). The number never leaves the browser; the server is only
told a number when the user clicks Submit.

That makes the browser the OCR runtime, not merely a UI driver, and it means no
lightweight browser can be substituted. Measured on the real upload flow,
reading a screenshot end to end:

| engine | peak tree RSS | login | total | OCR |
|---|---|---|---|---|
| Lightpanda (Zig) | 308MB | 5.3s | never | **broken** |
| Firefox + geckodriver | 640MB | 14.0s | 26.9s | works |
| Chromium `--single-process` | 794MB | 8.1s | 23.1s | works |
| Chromium default | 1060MB | 5.5s | 18.5s | works |

Lightpanda is the only lighter engine and it cannot do the job: it has no
`createImageBitmap`, so `ocrEngine.js` throws before the model is ever fetched
and the confirm panel never appears. Its 308MB is a symptom of the missing
raster backend, not an optimisation.

## Decision

Keep Firefox. Launch it for the duration of a single Screenshot and close it
afterwards, rather than holding one browser for the process lifetime. Before
launching, require `MemAvailable` to clear ~780MB or refuse and tell the user.

## Why

The saving is in the lifecycle, not the engine. Holding the browser open
indefinitely costs ~640MB resident continuously; launching per Screenshot and
holding it only briefly for the coming Confirm costs ~640MB for about 25
seconds plus the hold, twice a day. Idle footprint drops from ~680MB to ~40MB
outside the hold. No engine swap achieves anything close, because the floor is
the 16.5MB ONNX model plus a raster backend, which every working option pays.

Refusing to launch under memory pressure is deliberate. The alternative is an
OOM kill partway through a run, which is worse than a clear "not enough memory,
try again": it looks like a hang, and it can leave a half-finished browser
behind.

## Consequences

- Commit re-uploads the Screenshot in the usually-held browser, then sets the
  date and commits in one go -- no relaunch between the read and the commit.
  The bot verifies the second read matches the first and aborts if it does
  not, so this cannot silently submit a different number.
- The House-standing line after a commit costs a second browser launch. It is
  best-effort and is skipped when memory is short.
- Sign-in happens per launch, unconditionally: every launch mints a fresh
  relay-profile-* dir, so no session ever survives a close and no chat can
  ride another chat's login.
- ~25s per Screenshot, two launches per submission: the held read+commit,
  then the best-effort standings. Acceptable at the rate this is used.
- This is sized for a 1GB host. See the README Deploying section for the swap
  setup that stops the OOM killer taking the bot itself, and the compose
  file's mem_limit: 950m.

## Related

Sign-in additionally waits for React to attach handlers before clicking. The
form is server-rendered, so it is visible and clickable before it is functional;
clicking in that window is a silent no-op. This was observed directly when
Chromium "failed to log in" on a correct password.

## Amendment (PR #13)

The read phase now holds the browser open up to HOLD_BROWSER_SECS (default
60) for the coming Confirm, instead of closing immediately. 0 restores the
close-immediately behaviour above. The commit reuses the held browser -- no
relaunch, no second login while its session lives -- then closes it on its
own exit; the hold only ever delays one close. Expiry is lazy, observed on
the next acquire rather than by a timer task, and there is ever at most one
browser: a foreign or expired hold is evicted before launching. Rationale: the
read-to-Confirm relaunch costs ~20s, and the user has already waited through
one launch.
