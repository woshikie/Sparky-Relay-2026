# Alpine base for the container image

## Context

The Relay's runtime footprint on a 1GB host was the binding constraint. The
site's OCR runs in the page, so the browser is unavoidable and measured peak for
the Firefox tree is ~640MB. That leaves the bot itself (~44MB loaded) and the OS
in charge of whatever is left, and makes image size and cold start worth
thinking about rather than ignoring.

## Decision

Build the container on `alpine:3.22` and take Firefox and geckodriver from
Alpine's own repositories.

## Why

Alpine's packages are built for musl, not glibc. That is the whole reason for
using its repos rather than Mozilla's tarballs, and it is the cost side of the
trade worth being explicit about:

- **The Debian build could pin an exact Firefox ESR tarball.** Alpine tracks
  Mozilla's release cadence, so a rebuild moves the browser version. This image
  currently gets Firefox 142 where the Debian build got ESR 140.
- **The `firefox` apt package is unusable in a container anyway.** On Ubuntu it
  is a snap shim, and `firefox --version` just prints "install the snap" —
  which is what failed the first Debian build attempt. So the Debian image had
  to install from ftp.mozilla.org anyway, and was paying glibc for it.

What was verified in this image, since it is the make-or-break capability rather
than something to assume: `WebAssembly` present and `validate()` working,
`createImageBitmap` present, canvas 2d with `willReadFrequently` present, and
geckodriver 0.36.0 driving it. The site's OCR needs exactly those. Lightpanda
failed this same test on the same grounds.

Musl's smaller libc and Alpine's `apk` package manager also make the image and
cold start smaller than a Debian base. That is a disk and startup win, not a RAM
win — RAM is dominated by the browser either way, and rewriting the bot in Rust
to save 32MB of the 1024MB budget would be a 3% saving on a problem that lives
in a different process. See docs/adr/0004-browser-lifecycle-on-1gb-host.md.

## Consequences

- Firefox version is not pinned the way the Debian build pinned ESR. Pin it
  inside the image if a surprise release ever breaks the OCR; the version is
  visible in the build log.
- `bot.sh` is POSIX `sh`, not bash, because Alpine has no bash. `setup.sh` is
  bash and is deliberately *not* copied into the image: it is the bare-metal
  path, and the image already has everything it would install.
- The container must set `container=podman` (or `container=docker`) for
  `relay_site.py` to detect it and relax the Firefox content sandbox. Rootless
  podman puts the container in its own cgroup namespace, so `/proc/1/cgroup`
  names the *host* cgroup and never mentions a container — the first version of
  this detection silently returned False inside podman and the sandbox
  relaxation was skipped.
- `build.sh` passes `--format docker` because podman drops `HEALTHCHECK` in the
  default OCI format. The compose file repeats the healthcheck, which is what
  actually runs under podman either way.
