# Relay — Telegram step bot for Olympics 2026
#
# Alpine base. That is a deliberate choice with a caveat worth knowing:
# Alpine is musl, and Firefox's official builds are glibc, so we use Alpine's
# own firefox package. It runs, and the capabilities the site's OCR depends on
# are all present (WebAssembly, createImageBitmap, canvas 2d — verified in this
# image), but it tracks Mozilla's release cadence rather than pinning to an ESR
# tarball the way the Debian build does. See docs/adr/0005-alpine-base.md.
#
# Why Alpine here at all: the runtime image is much smaller, which matters when
# it is going on a 1GB host that is also running a browser. RAM is dominated by
# the browser either way (~640MB measured), so this is a disk and cold-start
# win, not a memory win.

FROM docker.io/library/alpine:3.22 AS gecko
# Build-only. geckodriver is a static binary, so it is fetched in a stage that
# carries neither curl nor ca-certificates into the runtime image.
ARG GECKODRIVER_VERSION=0.36.0
RUN apk add --no-cache curl ca-certificates
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64)          gecko_arch=linux64 ;; \
      aarch64|arm64)   gecko_arch=linux-aarch64 ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 \
      "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${gecko_arch}.tar.gz" \
      -o /tmp/gecko.tar.gz; \
    tar -xzf /tmp/gecko.tar.gz -C /tmp; \
    install -m 0755 /tmp/geckodriver /geckodriver; \
    /geckodriver --version | head -1

FROM docker.io/library/alpine:3.22 AS venv
# Build-only, so pip and virtualenv never reach the runtime image. The venv is
# copied wholesale instead: it symlinks /usr/bin/python3 and reads stdlib from
# /usr/lib/python3.12, both of which the runtime stage has from `firefox`.
ENV PYTHONDONTWRITEBYTECODE=1
RUN apk add --no-cache python3 py3-pip py3-virtualenv
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --upgrade pip \
 && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 # pip has no job once the wheels are in, and it is 12MB.
 && rm -rf /venv/lib/python3.12/site-packages/pip* /venv/bin/pip* \
 && /venv/bin/python -c "import telegram, selenium, PIL, cryptography"

FROM docker.io/library/alpine:3.22

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RELAY_HOME=/app \
    RELAY_STATE_DIR=/app/data \
    HEADLESS=1 \
    FIREFOX_BIN=/usr/bin/firefox \
    MOZ_HEADLESS=1 \
    # A container's seccomp/apparmor default blocks syscalls the Firefox
    # content process needs. relay_site.py also relaxes the content sandbox
    # when it detects a container; this covers the outer process.
    MOZ_DISABLE_CONTENT_SANDBOX=1 \
    # Rootless podman does not name itself in /proc/1/cgroup, so the container
    # detection in relay_site.py needs telling.
    container=podman

# firefox comes from Alpine's own repo, so it is built for musl. Note: NOT the
# `firefox` apt package on Debian -- that is a snap shim, and snap cannot run
# inside a container, so `firefox --version` just prints "install the snap". The
# cost of Alpine's own build is that it tracks Mozilla's release cadence rather
# than pinning an ESR tarball. See docs/adr/0005-alpine-base.md.
#
# What is deliberately NOT installed, having been measured as unused:
#   py3-pillow  photo.py imports PIL, but it gets the pip wheel from the venv
#               stage. Alpine's copy is a second, larger install of the same
#               library, plus libimagequant/openjpeg that nothing else needs.
#   py3-pip,
#   py3-virtualenv,
#   curl        all build-time only; both stages above are discarded.
# ca-certificates stays: python-telegram-bot needs it to reach the Telegram API.
#
# Not removable: `firefox` hard-depends on mesa-gl, mesa-egl and
# ffmpeg-libavcodec, which drag in llvm20-libs (171MB) and the Mesa software
# rasteriser (138MB). None of it is used -- headless OCR is WebAssembly and
# 2D canvas -- but they are package dependencies, so `apk del --force` removes
# the records and leaves the files. See docs/notes/image-size.md.
RUN apk add --no-cache \
      firefox \
      python3 \
      ca-certificates \
 && firefox --version

COPY --from=gecko /geckodriver /app/bin/geckodriver
COPY --from=venv /venv /app/.venv

WORKDIR /app

RUN mkdir -p /app/data

COPY relay ./relay
# Verification helpers, so the built image can prove it can read the site's OCR
# without needing the source tree on the host.
COPY check_container.py check_lifecycle.py ./
COPY CONTEXT.md README.md SITE-NOTES.md ./
COPY docs ./docs

# A browser parsing images from Telegram is exactly the thing that should not
# be root. Alpine's adduser is BusyBox: -D no password, -H no home dir.
RUN addgroup -S relay && adduser -S -G relay -H -h /app -s /sbin/nologin relay \
 && chown -R relay:relay /app
USER relay

VOLUME ["/app/data"]

# Reports unhealthy when the container cannot afford the browser, rather than
# half-starting and looking like a hang.
#
# Podman silently drops HEALTHCHECK when building in the default OCI format, so
# build with --format docker (see README). The compose file repeats this check
# independently, which is what actually runs under podman.
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
  CMD /app/.venv/bin/python -c "import sys; from relay import memory; print(memory.describe()); sys.exit(0 if memory.budget()['can_launch'] else 1)"

STOPSIGNAL SIGTERM
# The venv holds everything the bot needs, and exec-form ENTRYPOINT forwards
# SIGTERM straight to Python so the long-poll shuts down promptly instead of
# waiting out the container's grace period.
ENTRYPOINT ["/app/.venv/bin/python", "-m", "relay"]
