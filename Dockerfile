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

# firefox + geckodriver come from Alpine's own repos, so they are built for
# musl. gtk+3 and nss are pulled in by the firefox package; --no-cache keeps
# the layer small. python3 is the system interpreter; a venv holds the deps.
# Runtime packages only. Note: NOT the `firefox` apt package -- on Ubuntu that
# is a snap shim, and snap cannot run inside a container, so `firefox --version`
# just prints "install the snap". We install Alpine's own firefox instead, which
# is built for musl. The cost is that it tracks Mozilla's release cadence rather
# than pinning an ESR tarball; see docs/adr/0005-alpine-base.md.
RUN apk add --no-cache \
      firefox \
      python3 \
      py3-pip \
      py3-virtualenv \
      py3-pillow \
      curl \
      ca-certificates \
 && firefox --version

# geckodriver, from Mozilla rather than Alpine, so the version is pinned
# independently of the rest of the archive and the checkout carries no binary.
# Needs curl and ca-certificates, so it comes after the apk layer.
ARG GECKODRIVER_VERSION=0.36.0
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64)          gecko_arch=linux64 ;; \
      aarch64|arm64)   gecko_arch=linux-aarch64 ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    mkdir -p /app/bin; \
    curl -fsSL --retry 3 \
      "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${gecko_arch}.tar.gz" \
      -o /tmp/gecko.tar.gz; \
    tar -xzf /tmp/gecko.tar.gz -C /tmp; \
    install -m 0755 /tmp/geckodriver /app/bin/geckodriver; \
    rm -rf /tmp/gecko.tar.gz /tmp/geckodriver; \
    /app/bin/geckodriver --version | head -1

WORKDIR /app

# Dependencies in their own layer: cached until the pins actually move.
COPY requirements.txt ./
RUN python3 -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt \
 && mkdir -p /app/data

COPY access.py bot.py config.py datepicker.py errors.py ledger.py memory.py \
     relay_site.py vault.py words.py ./
# Verification helpers, so the built image can prove it can read the site's OCR
# without needing the source tree on the host.
COPY check_container.py check_lifecycle.py ./
COPY bot.sh ./
COPY CONTEXT.md README.md SITE-NOTES.md ./
COPY docs ./docs

RUN chmod +x /app/bot.sh

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
# the image is built with --format docker (see build.sh). The compose file
# repeats this check independently, which is what actually runs under podman.
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
  CMD /app/.venv/bin/python -c "import sys, memory; print(memory.describe()); sys.exit(0 if memory.budget()['can_launch'] else 1)"

STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/bot.sh"]
