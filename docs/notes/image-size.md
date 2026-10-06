# Why the image is ~1GB, and what actually moved

Measured on `localhost/sparky-relay-2026:latest`, Alpine 3.22, x86_64.
Not theory — `podman history` and `du -sm` inside the built image.

## The breakdown

| what                        | size   | share |
|-----------------------------|--------|-------|
| Mesa software rasteriser    | 309 MB | 30%   |
| Firefox itself              | 234 MB | 23%   |
| system Python 3.12          |  66 MB |  6%   |
| video codecs (ffmpeg/x265/aom/SVT-AV1) | 51 MB | 5% |
| Alpine base                 |   9 MB |  1%   |
| the venv                    | 103 MB | 10%   |
| geckodriver                 |   6 MB |  1%   |

## The part that cannot be removed

`firefox` in Alpine's repo hard-depends on:

```
mesa-egl   mesa-gl   ffmpeg-libavcodec
```

`mesa-gl` pulls `llvm20-libs` (171 MB, `libLLVM.so.20.1`) and the software
rasteriser `gallium-pipe` (98 MB) plus `libgallium-25.1.9.so` (40 MB).
`ffmpeg-libavcodec` pulls `x265`, `aom`, `SvtAv1Enc`.

**None of it is used.** Headless OCR on this site is PaddleOCR v5 running as
ONNX Runtime Web — WebAssembly and 2D canvas. There is no GPU, no GL context,
and no video anywhere in the path. Verified: after the packages were removed
from a running container, `make verify` still read both real screenshots
correctly (2,831 and 6,532) and printed `CONTAINER OK`.

But it cannot be reclaimed on Alpine. Tested directly:

```
$ apk del --force mesa mesa-gl mesa-egl mesa-gbm llvm20-libs
removed mesa
removed mesa-gl
removed mesa-egl
removed mesa-gbm
removed llvm20-libs
$ ls /usr/lib/libLLVM*
/usr/lib/libLLVM-20.so
/usr/lib/libLLVM.so.20.1
$ du -sm /            # unchanged
923    /
```

apk removes the package *records* and leaves the files, because
`firefox` still requires them. So the honest number is: 360 MB of this image,
35%, is a hard dependency of the browser package that this workload provably
does not use.

## What did move

| change                                       | saved |
|---------------------------------------------|-------|
| `py3-pillow` — bot.py uses the pip wheel     |  ~15 MB |
| `pip` itself, once the wheels are in          |   12 MB |
| `py3-pip`, `py3-virtualenv` → build stage    |  ~13 MB |
| `curl` + `libcurl` + `c-ares` → build stage   |   ~5 MB |

Measured live on the running container: 941 MB → 923 MB. The multi-stage build
captures the build-only packages too, which could not be measured in place.

Also checked and already fine: `apk add --no-cache` is set, Firefox ships no
`.xpi` locale bundles in Alpine, and the venv installs with `--no-cache-dir`.

## The only way to halve it

Leave Alpine. On Debian, `firefox-esr` can be installed from Mozilla's own
tarball instead of the distro package, and nothing declares a dependency on
Mesa. That path is not blocked by the reason Alpine was chosen — ADR 0005 was
about the *apt* `firefox` package being a snap shim, which is a different thing
from untarring Mozilla's release.

Estimated result, not measured: Debian slim (~75 MB) + Firefox ESR tarball
(~250 MB) + Python + venv ≈ 500 MB.

### Decision: keep Alpine. 962 MB stands.

Asked, and answered: no. The reasoning, so this does not get re-opened every
few months:

- **Image size is not the constraint that hurts.** It sits on disk. The thing
  that decides whether this bot works on a 1 GB host is the browser's 640 MB
  peak, and Mesa's 360 MB of shared libraries are file-backed — they are mapped
  on demand, not resident. Deleting them from the image would not lower the peak.
- **The cost is not the size, it is the re-verification.** A different Firefox
  build means re-running the OCR check *and* re-measuring the memory peak. That
  peak is the single number the whole host budget hangs off, and it was measured
  on Alpine's build. Trading a known 640 MB for an unknown is a bad deal for
  460 MB of disk.
- **It re-opens ADR 0005 for no gain in the dimension that matters.** The
  reason for Alpine was a working, verified musl Firefox. That is still working.

So the number to hold in mind when this comes up again: **360 MB of this image
is an unused graphics stack that cannot be removed without changing base
image, and changing base image is not worth it.**
