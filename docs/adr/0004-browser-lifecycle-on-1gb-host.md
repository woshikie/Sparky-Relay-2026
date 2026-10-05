# Browser lifecycle: launch per Screenshot, never hold open

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

The saving is in the lifecycle, not the engine. Holding the browser open costs
~640MB resident continuously; per-Screenshot it costs ~640MB for about 25
seconds, twice a day. Idle footprint drops from ~680MB to ~40MB. No engine swap
achieves anything close, because the floor is the 16.5MB ONNX model plus a
raster backend, which every working option pays.

Refusing to launch under memory pressure is deliberate. The alternative is an
OOM kill partway through a run, which is worse than a clear "not enough memory,
try again": it looks like a hang, and it can leave a half-finished browser
behind.

## Consequences

- Committing re-runs the OCR, because the browser was closed after reading the
  number. That is a deliberate trade: the host's memory is needed twice for ~25s
  instead of once for as long as the user takes to choose a date. The bot
  verifies the second read matches the first and aborts if it does not, so this
  cannot silently submit a different number.
- The House-standing line after a commit costs a third browser launch. It is
  best-effort and is skipped when memory is short.
- Sign-in happens per launch, but the persistent browser profile means the site
  Session usually survives, so it is usually a redirect check rather than a
  password submission.
- ~25s per Screenshot, three launches per submission. Acceptable at the rate
  this is used.
- This is sized for a 1GB host. See README for the systemd memory limits that
  stop the OOM killer taking the bot itself.

## Related

Sign-in additionally waits for React to attach handlers before clicking. The
form is server-rendered, so it is visible and clickable before it is functional;
clicking in that window is a silent no-op. This was observed directly when
Chromium "failed to log in" on a correct password.
