"""A single Screenshot's journey, start to finish.

Wraps the site in headless Firefox and exposes the upload flow as three steps:
upload + read the Detected Steps, set the Activity Date, commit.
"""
import os
import re
import time

import config
import memory
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

HERE = os.path.dirname(os.path.abspath(__file__))
# State that must survive a restart lives under config.LEDGER_DB's directory, so
# in a container it is the mounted volume rather than the (read-only) image.
STATE_DIR = config.STATE_DIR
PROFILE = os.environ.get("BROWSER_PROFILE",
                         os.path.join(STATE_DIR, ".browserprofile"))
GECKO = os.environ.get("GECKODRIVER_PATH",
                       os.path.join(HERE, "bin", "geckodriver"))
GECKO_LOG = os.path.join(config.LOGS, "geckodriver.log")

# Mirrors the client-side plausibility band we found in ocrParser. Used to
# refuse to Commit a value the site would not plausibly have produced.
MIN_STEPS, MAX_STEPS = 100, 200_000

DETECTED_RE = re.compile(r"Detected steps\s*([\d,. ]+)", re.I)

# Anchored on real month names: a loose [A-Z][a-z]+ \d{4} also matches the page
# title "Olympics 2026", which is not the calendar header.
MONTHS = ("January February March April May June July August September "
          "October November December").split()
MONTH_ALT = "|".join(MONTHS)
HEADER_RE = re.compile(r"\b(%s)\s+(\d{4})\b" % MONTH_ALT)
DATE_LABEL_RE = re.compile(
    r"\b(%s)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b" % MONTH_ALT, re.I)


def _in_container():
    """True when we are running inside a container rather than on bare metal.

    Podman rootless puts the container in its own cgroup namespace, so
    /proc/1/cgroup can name the *host* cgroup and never mention podman. The
    reliable signals are the marker file, a podman-style cgroup path, or the
    container being PID 1 with an init that is not systemd.
    """
    if os.environ.get("container") or os.environ.get("IN_CONTAINER"):
        return True
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            blob = f.read()
        if any(k in blob for k in ("docker", "kubepods", "containerd", "lxc")):
            return True
    except OSError:
        pass
    try:
        with open("/proc/1/comm") as f:
            init = f.read().strip()
        if init in ("init", "sh", "busybox", "sleep", "python3"):
            # PID 1 that is not systemd. On a VM it would be systemd or sshd.
            with open("/proc/1/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
            if "/sbin/init" not in cmd and "systemd" not in cmd:
                return True
    except OSError:
        pass
    return False


class SiteChanged(Exception):
    """The page no longer looks the way we expect. Never guess past this."""


class NoStepsFound(Exception):
    """The site refused the Screenshot: it read no usable number."""


class Relay:
    def __init__(self, base, headless=True, verbose=True):
        self.base = base.rstrip("/")
        self.headless = headless
        self.verbose = verbose
        self.driver = None
        self.log = []

    # ---------- plumbing ----------

    def _say(self, msg):
        self.log.append(msg)
        if self.verbose:
            print("[relay] %s" % msg, flush=True)

    def _opts(self):
        o = Options()
        if self.headless:
            o.add_argument("-headless")
        o.binary_location = config.FIREFOX_BIN
        # A container's seccomp/apparmor default blocks the syscalls geckodriver
        # and the content process need. Only relax this when we really are in a
        # container, so a bare-metal install keeps the sandbox.
        if _in_container():
            o.set_preference("security.sandbox.content.level", 0)
            self._say("container detected: content sandbox relaxed")
        # A persistent profile keeps the Site Session across restarts, so the
        # Relay does not re-authenticate on every process start. This is also
        # what makes on-demand launching tolerable: we pay login once, not once
        # per Screenshot.
        o.add_argument("-profile")
        o.add_argument(PROFILE)
        for k, v in memory.tune_firefox_env().items():
            o.set_preference(k, v)
        return o

    def start(self):
        """Launch the browser. Refuses if the host cannot fit it.

        Called per Screenshot rather than held open: the OCR needs ~640MB, and
        on a 1GB host that has to be transient.
        """
        if self.driver:
            return
        if not os.path.exists(GECKO):
            raise RuntimeError(
                "geckodriver not found at %s\n  run: ./setup.sh" % GECKO)
        if not os.path.exists(config.FIREFOX_BIN):
            raise RuntimeError(
                "firefox not found at %s\n  set FIREFOX_BIN in secrets.env"
                % config.FIREFOX_BIN)
        try:
            rep = memory.require_memory()
            self._say("memory ok: %.0fMB free" % rep["available_mb"])
        except memory.InsufficientMemory as e:
            self._say("NOT launching the browser: %s" % e)
            raise
        os.makedirs(PROFILE, exist_ok=True)
        os.makedirs(os.path.dirname(GECKO_LOG), exist_ok=True)
        os.makedirs(config.INBOX, exist_ok=True)
        svc = Service(executable_path=GECKO, log_output=GECKO_LOG)
        self.driver = webdriver.Firefox(options=self._opts(), service=svc)
        self.driver.set_page_load_timeout(60)
        self._say("browser started")

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self._say("browser closed")

    def _text(self, el):
        """Element text, tolerating the SPA swapping nodes mid-read.

        React re-renders on every state change, so any element reference we
        grabbed a moment ago can already be detached.
        """
        try:
            return (el.text or "").strip()
        except StaleElementReferenceException:
            return None
        except Exception:
            return None

    def _body(self):
        return self.driver.find_element(By.TAG_NAME, "body").text

    def _calendar_header(self):
        """The month/year caption inside the open date picker, or None."""
        for el in self.driver.find_elements(By.CSS_SELECTOR,
                                             "div, button, span, h2, caption"):
            txt = self._text(el)
            if txt and HEADER_RE.fullmatch(txt):
                return txt
        return None

    # ---------- session ----------

    def _authed(self):
        """True when the nav exposes /upload, i.e. a live Site Session."""
        try:
            hrefs = [a.get_attribute("href") or ""
                     for a in self.driver.find_elements(By.CSS_SELECTOR, "a")]
        except Exception:
            return False
        return any("/upload" in h for h in hrefs)

    def _wait_hydrated(self, el, tries=60):
        """Block until React has attached its handlers to the form.

        React marks the nodes it owns with a `__react*` expando. Until that
        appears, the markup is inert and a click does nothing at all. Waiting
        on this is what makes sign-in reliable on a cold VM, where the
        CPU-bound hydration is much slower than on a desktop.
        """
        for _ in range(tries):
            try:
                owned = self.driver.execute_script(
                    "const el = arguments[0];"
                    "if (!el) return false;"
                    "for (const k of Object.keys(el)) {"
                    "  if (k.indexOf('__react') === 0) return true;"
                    "} return false;", el)
            except StaleElementReferenceException:
                owned = False
            if owned:
                return True
            time.sleep(0.5)
        # Not fatal: the submit may still be a native form post. Say so and let
        # the click be tried, rather than refusing to log in at all.
        self._say("warning: no React marker on the form after 30s; "
                  "proceeding anyway")
        return False

    def login(self, username, password):
        if not self.driver:
            self.start()
        self._say("opening sign-in")
        self.driver.get(self.base + "/auth")
        # A persisted profile may already hold a live Session, in which case
        # /auth bounces to /home and there is no form to fill. Check first.
        for _ in range(12):
            if self._authed():
                self._say("existing session still valid; no sign-in needed")
                return
            time.sleep(0.5)
        # Wait for the form to actually render; the SPA hydrates after load and
        # a fixed sleep loses the race on a cold profile / cold VM.
        u = p = None
        for _ in range(40):
            time.sleep(0.5)
            ins = self.driver.find_elements(By.CSS_SELECTOR, "input")
            texty = [e for e in ins if e.get_attribute("type") == "text"]
            pwdy = [e for e in ins if e.get_attribute("type") == "password"]
            if texty and pwdy:
                u, p = texty[0], pwdy[0]
                break
        if u is None or p is None:
            raise SiteChanged("no username/password fields on /auth")

        # The form is server-rendered, so it is visible and clickable before
        # React attaches the submit handler. Clicking in that window is a
        # silent no-op -- we saw exactly this when benchmarking Chromium.
        # Wait for React to actually own the node before typing.
        self._wait_hydrated(u)
        self._say("page hydrated")

        u.clear(); u.send_keys(username)
        p.clear(); p.send_keys(password)
        self.driver.find_element(By.CSS_SELECTOR, "button").click()
        for _ in range(60):
            time.sleep(0.5)
            if "/auth" not in self.driver.current_url:
                break
        # Authenticated DOM = nav exposes /upload. URL alone is not enough:
        # it changes before the session is committed.
        for _ in range(20):
            try:
                hrefs = [a.get_attribute("href") or ""
                         for a in self.driver.find_elements(By.CSS_SELECTOR, "a")]
            except Exception:
                hrefs = []
            if any("/upload" in h for h in hrefs):
                break
            time.sleep(0.5)
        else:
            raise SiteChanged("sign-in did not reach an authenticated page")
        self._say("signed in")
        time.sleep(2)

    # ---------- navigation ----------

    def _on_upload_page(self):
        return "/upload" in (self.driver.current_url or "")

    def goto_upload(self):
        """Routes are client-side only; /upload by URL bounces to /home.

        Idempotent: we are often already on /upload with a confirm panel up from
        a previous Screenshot, in which case there is nothing to navigate to.
        """
        if self._on_upload_page():
            return
        for a in self.driver.find_elements(By.CSS_SELECTOR, "a"):
            if "/upload" in (a.get_attribute("href") or "") and (a.text or "").strip():
                a.click()
                return
        raise SiteChanged("no Upload steps link in nav")

    def _reset_to_form(self):
        """Clear any confirm panel so the file input is usable again."""
        for _ in range(20):
            t = self._body()
            if "Detected steps" not in t:
                break
            retake = self._btn("Retake")
            if retake is None:
                break
            retake.click()
            self._say("cleared a previous confirm panel")
            time.sleep(1.5)
        # the fresh form is present when the mode toggle is back
        self._wait_btn("Steps", exact=True,
                       what="upload form did not come back after clearing")

    def _btn(self, label, exact=False):
        want = (label or "").strip().lower()
        for b in self.driver.find_elements(By.CSS_SELECTOR, "button"):
            t = self._text(b)
            if t is None:
                continue
            if (t.lower() == want) if exact else (want in t.lower()):
                return b
        return None

    def _wait_btn(self, label, exact=False, tries=30, what=None):
        """Wait for a button instead of sleeping a guessed interval."""
        for _ in range(tries):
            b = self._btn(label, exact=exact)
            if b is not None:
                return b
            time.sleep(0.5)
        raise SiteChanged(what or ("no '%s' button appeared" % label))

    def _require(self, cond, msg):
        if not cond:
            raise SiteChanged(msg)

    # ---------- step 1: upload + read ----------

    def upload(self, image_path, mode="steps"):
        self.goto_upload()
        self._reset_to_form()
        self._wait_btn(mode, exact=True).click()
        time.sleep(1.5)
        fis = self.driver.find_elements(By.CSS_SELECTOR, "input[type=file]")
        self._require(len(fis) == 1, "expected exactly one file input")
        self._say("uploading %s" % os.path.basename(image_path))
        fis[0].send_keys(image_path)
        for _ in range(40):
            time.sleep(1)
            t = self._body()
            if "Detected steps" in t:
                break
        else:
            # distinguish "no number found" from "the panel looks different"
            t = self._body()
            if "Detected steps" not in t and "Submit steps" not in t:
                raise NoStepsFound("site read no steps from this image")
        t = self._body()
        if "Detected steps" not in t:
            raise NoStepsFound("site read no steps from this image")
        m = DETECTED_RE.search(t)
        if not m:
            raise SiteChanged("could not parse the Detected steps value")
        raw = m.group(1).strip()
        steps = int(re.sub(r"[^\d]", "", raw))
        self._say("site reported %s steps" % raw)
        return steps, raw

    # ---------- step 2: date ----------

    def _date_button(self):
        for b in self.driver.find_elements(By.CSS_SELECTOR, "button"):
            t = self._text(b)
            if t and DATE_LABEL_RE.search(t):
                return b
        return None

    def set_date(self, date_obj):
        """Pick date_obj in the site's Radix calendar, then verify it stuck."""
        target = self._date_button()
        self._require(target is not None, "no date button in confirm panel")
        start_label = self._text(target) or ""
        target.click()
        self._say("date picker opened")
        # the picker being open is observable: a month header appears
        for _ in range(20):
            if self._calendar_header():
                break
            time.sleep(0.5)
        else:
            raise SiteChanged("date picker did not open")

        for _ in range(36):
            hdr = self._calendar_header()
            if hdr is None:
                raise SiteChanged("calendar header disappeared")
            m = HEADER_RE.search(hdr)
            cur = (int(m.group(2)), MONTHS.index(m.group(1)) + 1)
            if cur == (date_obj.year, date_obj.month):
                break
            nxt = self._btn("Next month")
            prv = self._btn("Previous month")
            if (date_obj.year, date_obj.month) > cur:
                self._require(nxt is not None, "no next-month control")
                nxt.click()
            else:
                self._require(prv is not None, "no previous-month control")
                prv.click()
            time.sleep(1.2)
        else:
            raise SiteChanged("could not page calendar to %s" % date_obj)

        clicked = False
        for el in self.driver.find_elements(
                By.CSS_SELECTOR, "button, td, [role=gridcell]"):
            if self._text(el) == str(date_obj.day):
                try:
                    if el.is_displayed():
                        el.click()
                        clicked = True
                        break
                except Exception:
                    continue
        self._require(clicked, "could not click day %d" % date_obj.day)
        time.sleep(1.5)
        # The whole point: verify, do not assume. A wrong date silently
        # misfiles a Submission, so re-read the control and compare.
        after = self._date_button()
        label = (self._text(after) or "") if after else ""
        self._say("activity date now shows %r (was %r)" % (label, start_label))
        m = DATE_LABEL_RE.search(label)
        if not m:
            raise SiteChanged("could not read the date back (showing %r)" % label)
        got = (MONTHS.index(m.group(1).title()) + 1, int(m.group(2)),
               int(m.group(3)))
        want = (date_obj.month, date_obj.day, date_obj.year)
        if got != want:
            raise SiteChanged("date did not stick: wanted %s, control shows %r"
                              % (want, label))
        return label

    # ---------- step 3: commit ----------

    def commit(self, expect_steps):
        before = self._body()
        self._require("Detected steps" in before, "confirm panel gone before submit")
        btn = self._btn("Submit steps")
        self._require(btn is not None, "no Submit steps button")
        self._say("clicking Submit steps")
        btn.click()
        for _ in range(30):
            time.sleep(1)
            t = self._body()
            if "Detected steps" not in t and "Submit steps" not in t:
                self._say("confirm panel closed -> site accepted the submit")
                return t
        raise SiteChanged("confirm panel still open after Submit")

    # ---------- result ----------

    def read_result(self):
        t = self._body()
        return t

    def leaderboard_line(self):
        """Best-effort rank/points read after a successful commit."""
        try:
            self.driver.get(self.base + "/home")
            for _ in range(20):
                time.sleep(0.5)
                t = self._body()
                if "House standings" in t:
                    return t
            return ""
        except Exception:
            return ""

    def existing_for_date(self, date_str):
        """What the site currently holds for YYYY-MM-DD, via the app's own path.

        This mirrors the preflight SELECT the site issues before submitting.
        """
        self.driver.get(self.base + "/home")
        for _ in range(20):
            time.sleep(0.5)
            if "House standings" in self._body():
                break
        for a in self.driver.find_elements(By.CSS_SELECTOR, "a"):
            if (a.text or "").strip() == "Dashboard":
                a.click()
                time.sleep(3)
                break
        return self._body()
