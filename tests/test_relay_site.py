"""A stand-in for the browser, so the site-driving logic can be tested.

`relay_site.Relay` talks to a real Firefox through geckodriver. That is
verified end to end by check_container.py inside the image. What is testable
without a browser is everything Relay does *with* the page: parsing the
Detected Steps value, paging the calendar, verifying the date stuck, and
refusing to commit when the page is not what we expect.

A fake that records what it was asked and returns page-shaped text covers those
without pretending to be a browser. The three rules it is built to enforce,
because each is a place a silent misread would hurt:

  - commit() only proceeds when the confirm panel is actually up;
  - set_date() re-reads the control and compares, never assumes;
  - upload() distinguishes "the site read nothing" from "the page changed".
"""
import datetime

import pytest

from relay.site import driver as relay_site
from relay.site import parsing


# ------------------------------------------------------------------- fakes

class FakeElement:
    def __init__(self, text="", displayed=True, clickable=True, sink=None):
        self._text = text
        self._displayed = displayed
        self._clickable = clickable
        self.sink = sink

    @property
    def text(self):
        return self._text

    def is_displayed(self):
        return self._displayed

    def click(self):
        if not self._clickable:
            raise RuntimeError("element is not clickable")
        if self.sink is not None:
            self.sink(self._text)

    def clear(self):
        pass

    def send_keys(self, *keys):
        pass

    def get_attribute(self, name):
        return {
            "type": "text", "name": None, "id": None, "value": None,
            "placeholder": None, "accept": None, "href": None, "title": None,
            "aria-label": None,
        }.get(name)


class FakeDriver:
    """The slice of webdriver.Remote that Relay actually uses."""

    def __init__(self, body="", buttons=None, inputs=None, file_inputs=None,
                 elements=None, current_url=""):
        self.body = body
        self.buttons = buttons if buttons is not None else []
        self.inputs = inputs if inputs is not None else []
        self.file_inputs = file_inputs if file_inputs is not None else []
        self.elements = elements or {}
        self.current_url = current_url
        self.visited = []
        self.clicked = []
        self.set_page_load_timeout = lambda s: None
        self.quit = lambda: None

    def get(self, url):
        self.visited.append(url)
        self.current_url = url

    def execute_async_script(self, *a, **kw):
        pass

    def find_element(self, by, selector):
        if selector == "body":
            return FakeElement(self.body)
        if selector == "button" and self.buttons:
            return self.buttons[0]
        raise AssertionError("unexpected selector %r" % selector)

    def find_elements(self, by, selector):
        if selector == "button":
            return list(self.buttons)
        if selector == "a":
            return self.elements.get("a", [])
        if selector == "input":
            return list(self.inputs)
        if selector == "input[type=file]":
            return list(self.file_inputs)
        if selector in ("button, td, [role=gridcell]", "div, button, span, h2, caption"):
            return self.elements.get("cal", [])
        return []

    def execute_script(self, script, *args):
        return self.elements.get("hydrated", True)


def relay_with(driver, base="https://site.example"):
    r = relay_site.Relay(base, headless=True, verbose=False)
    r.driver = driver
    return r


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    """Shrink the wait loops.

    The real values are seconds-to-minutes because a cold 1GB VM hydrates slowly
    and the OCR is in-page. A suite that waits 30s to prove a timeout fires is a
    suite nobody runs, so the loop counts drop and the sleep goes to nothing.
    """
    monkeypatch.setattr(relay_site, "POLL_SECONDS", 0.0)
    for name in ("FORM_PROBES LOGIN_PROBES BUTTON_PROBES "
                 "CALENDAR_PROBES MONTH_PROBES OCR_PROBES COMMIT_PROBES").split():
        monkeypatch.setattr(relay_site, name, 3)


def confirm_body(steps="6,532", date_label="October 5th, 2026"):
    return (
        "Upload steps\nDetected steps\n%s\nWrong number? Tap Retake and upload a "
        "clearer screenshot.\nDate of activity\n%s\nOne submission per day"
        % (steps, date_label)
    )


# ------------------------------------------------------- parsing the result

@pytest.mark.parametrize("body,expected", [
    (confirm_body("2,831"), 2831),
    (confirm_body("6,532"), 6532),
    (confirm_body("100"), 100),
    (confirm_body("200,000"), 200000),
    (confirm_body("12345"), 12345),
])
def test_the_reported_number_is_parsed(body, expected):
    m = parsing.DETECTED_RE.search(body)
    assert int("".join(c for c in m.group(1) if c.isdigit())) == expected


def test_the_regex_needs_the_label():
    """A bare number elsewhere on the page must not be mistaken for the total."""
    assert parsing.DETECTED_RE.search("Detected steps\n6,532") is not None
    assert parsing.DETECTED_RE.search("Goal: 10,000 of 10,000") is None
    assert parsing.DETECTED_RE.search("1,800") is None


def test_the_calendar_header_needs_a_real_month_name():
    """'Olympics 2026' is on the page and is not a calendar header."""
    assert parsing.HEADER_RE.fullmatch("October 2026")
    assert parsing.HEADER_RE.fullmatch("Olympics 2026") is None
    assert parsing.HEADER_RE.fullmatch("Covid 2026") is None
    assert parsing.HEADER_RE.fullmatch("Oct 2026") is None


def test_month_alt_stays_full_names_only():
    """MONTH_ALT feeds HEADER_RE: one abbreviation in it reopens the
    'Oct 2026' match the anchor exists to prevent."""
    assert set(parsing.MONTH_ALT.split("|")) == set(parsing.MONTHS)


def test_the_date_label_pattern():
    assert parsing.DATE_LABEL_RE.search("October 5th, 2026")
    assert parsing.DATE_LABEL_RE.search("October 22nd, 2026")
    assert parsing.DATE_LABEL_RE.search("October 3, 2026")
    assert parsing.DATE_LABEL_RE.search("Feb 2026") is None


# ----------------------------------------------------------------- session

def test_signing_in_fills_the_form():
    driver = FakeDriver(body="USERNAME PASSWORD SIGN IN",
                        inputs=[FakeElement(), FakeElement()])
    # No live session to skip on, then the form appears.
    driver.elements["a"] = []
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.login("user", "pass")


def test_a_missing_form_is_a_site_change_not_a_crash():
    driver = FakeDriver(body="something else entirely", inputs=[])
    driver.elements["a"] = []
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.login("user", "pass")


# --------------------------------------------------------------- navigation

def test_upload_needs_the_mode_toggle():
    driver = FakeDriver(body="Upload steps", elements={"a": []})
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.goto_upload()


# --------------------------------------------------------------- upload

def upload_driver(body="Upload steps\nSteps\nDistance (km)", buttons=None,
                  file_inputs=None):
    """A driver already on /upload, wired for the file-input step."""
    driver = FakeDriver(body=body, buttons=buttons)
    driver.current_url = "https://site.example/upload"
    driver.file_inputs = file_inputs if file_inputs is not None else [FakeElement()]
    driver.elements["a"] = []
    return driver


def test_a_confirm_panel_yields_the_number_and_keeps_the_label():
    """The number and the site's own formatting of it are both needed.

    The label is echoed back to the user in the Confirmation, so it has to be
    the site's formatting ("6,532") rather than a re-derived integer.
    """
    driver = upload_driver()
    r = relay_with(driver)
    r._body = lambda: confirm_body("6,532")
    steps, reported = _upload(r, driver, "x.jpg")
    assert steps == 6532
    assert reported == "6,532"


def _upload(r, driver, path):
    """Drive upload() against a driver already showing a confirm panel.

    _wait_btn is stubbed to hand back a clickable button unconditionally, and
    the body is fixed, so the only thing under test is upload()'s own parsing
    and error handling rather than the DOM choreography.
    """
    r._wait_btn = lambda *a, **k: FakeElement("Steps")
    r._reset_to_form = lambda: None
    return r.upload(path)


def test_a_page_with_no_number_is_not_read_as_zero():
    """The site's refusal is reported as such, not as a failed upload."""
    driver = upload_driver()
    r = relay_with(driver)
    r._body = lambda: "Upload steps\nNothing detected\nRetake"
    with pytest.raises(relay_site.NoStepsFound):
        _upload(r, driver, "x.jpg")


def test_a_confirm_panel_without_a_parseable_value_is_a_site_change():
    driver = upload_driver()
    r = relay_with(driver)
    r._body = lambda: "Detected steps\n???"
    with pytest.raises(relay_site.SiteChanged):
        _upload(r, driver, "x.jpg")


def test_upload_needs_exactly_one_file_input():
    driver = upload_driver(file_inputs=[])
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        _upload(r, driver, "x.jpg")


def test_upload_refuses_two_file_inputs():
    """Two inputs means the page changed underneath us; guessing is unsafe."""
    driver = upload_driver(file_inputs=[FakeElement(), FakeElement()])
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        _upload(r, driver, "x.jpg")


def test_upload_uses_distance_mode_when_asked():
    driver = upload_driver()
    r = relay_with(driver)
    clicked = []
    r._wait_btn = lambda label, *a, **k: FakeElement(label, sink=clicked.append)
    r._reset_to_form = lambda: None
    r._body = lambda: confirm_body("5.2")
    steps, reported = r.upload("x.jpg", mode="distance")
    assert clicked == ["distance"], "the mode toggle is what gets clicked"


def test_reset_clears_a_stale_confirm_panel():
    """Without this, a second Screenshot in the same browser finds no form."""
    clicked = []
    driver = FakeDriver(
        body=confirm_body(),
        buttons=[FakeElement("Retake", sink=clicked.append)],
    )
    r = relay_with(driver)
    r._body = lambda: "Steps" if clicked else confirm_body()
    r._wait_btn = lambda *a, **k: FakeElement("Steps")
    r._reset_to_form()
    assert clicked == ["Retake"]


def test_reset_is_a_no_op_when_no_panel_is_up():
    driver = FakeDriver(body="Upload steps\nSteps", buttons=[])
    r = relay_with(driver)
    r._wait_btn = lambda *a, **k: FakeElement("Steps")
    r._reset_to_form()   # must not raise


def test_reset_needs_the_form_back():
    driver = FakeDriver(body=confirm_body(), buttons=[FakeElement("Retake")])
    r = relay_with(driver)
    r._body = lambda: confirm_body()
    with pytest.raises(relay_site.SiteChanged):
        r._reset_to_form()      # Retake never actually takes effect


def test_a_missing_nav_link_is_a_site_change():
    driver = FakeDriver(body="", elements={"a": []})
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.goto_upload()


# ------------------------------------------------------------------- upload

def test_a_confirm_panel_yields_the_number():
    driver = FakeDriver(body=confirm_body("6,532"))
    driver.elements["a"] = []
    r = relay_with(driver)
    steps, reported = parsing.DETECTED_RE.search(driver.body), None
    m = parsing.DETECTED_RE.search(driver.body)
    assert int("".join(c for c in m.group(1) if c.isdigit())) == 6532


def test_a_page_with_no_number_is_not_silently_zero():
    driver = FakeDriver(body="Upload steps\nNothing detected\nRetake")
    r = relay_with(driver)
    m = parsing.DETECTED_RE.search(driver.body)
    assert m is None


# ------------------------------------------------------------- date picking

def test_the_date_button_is_found_by_its_label():
    driver = FakeDriver(
        body=confirm_body(),
        buttons=[FakeElement("Steps", sink=None), FakeElement("October 5th, 2026")],
    )
    r = relay_with(driver)
    assert r._date_button() is not None


def test_a_confirm_panel_without_a_date_is_a_site_change():
    driver = FakeDriver(body=confirm_body(),
                        buttons=[FakeElement("Submit steps")])
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.set_date(datetime.date(2026, 10, 4))


def test_a_date_that_does_not_stick_aborts():
    """The whole point: verify the calendar, do not assume it worked.

    A misfiled Submission is invisible until someone checks the board.
    """
    target = datetime.date(2026, 10, 4)
    clicked = []

    driver = FakeDriver(
        body=confirm_body(),
        # The control keeps saying October 5th whatever we click.
        buttons=[FakeElement("October 5th, 2026")],
        elements={"cal": [FakeElement("October 2026"),
                          FakeElement(str(target.day), sink=clicked.append)]},
    )
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged) as exc:
        r.set_date(target)
    assert "did not stick" in str(exc.value)
    assert clicked == ["4"], "it did click the day; the control disagreed"


def test_a_date_that_takes_is_returned():
    target = datetime.date(2026, 10, 4)
    driver = FakeDriver(
        body=confirm_body(date_label="October 4th, 2026"),
        buttons=[FakeElement("October 4th, 2026")],
        elements={"cal": [FakeElement("October 2026"),
                          FakeElement("4", sink=None)]},
    )
    r = relay_with(driver)
    assert r.set_date(target) == "October 4th, 2026"


def test_a_day_that_will_not_click_aborts():
    driver = FakeDriver(
        body=confirm_body(),
        buttons=[FakeElement("October 5th, 2026")],
        elements={"cal": [FakeElement("October 2026")],   # no day cells
                  },
    )
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.set_date(datetime.date(2026, 10, 4))


def test_a_stale_element_is_tolerated():
    """React re-renders constantly; a detached reference must not crash us."""
    driver = FakeDriver(body=confirm_body(),
                        buttons=[FakeElement("October 5th, 2026")])
    r = relay_with(driver)

    class Stale(FakeElement):
        @property
        def text(self):
            raise relay_site.StaleElementReferenceException("detached")

    r.driver.buttons = [Stale()]
    assert r._date_button() is None          # skipped, not raised
    assert r._calendar_header() is None


# ------------------------------------------------------------------- commit

def test_commit_refuses_without_a_confirm_panel():
    driver = FakeDriver(body="Upload steps\nChoose a mode", buttons=[])
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.commit(6532)


def test_commit_clicks_submit_and_waits_for_the_panel_to_close():
    """The panel disappearing is how we know the site accepted the write."""
    clicked = []
    driver = FakeDriver(body=confirm_body())

    def on_click(label):
        clicked.append(label)
        driver.body = "Upload steps\nRecorded"      # site accepted
    driver.buttons = [FakeElement("Submit steps", sink=on_click)]
    r = relay_with(driver)
    text = r.commit(6532)
    assert "Recorded" in text
    assert clicked == ["Submit steps"]


def test_commit_gives_up_rather_than_reporting_success():
    """If the panel never closes, that is a failure -- never a success."""
    driver = FakeDriver(body=confirm_body())
    driver.buttons = [FakeElement("Submit steps", sink=lambda _l: None)]
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged) as exc:
        r.commit(6532)
    assert "still open" in str(exc.value)


def test_commit_needs_a_submit_button():
    driver = FakeDriver(body=confirm_body(), buttons=[FakeElement("Retake")])
    r = relay_with(driver)
    with pytest.raises(relay_site.SiteChanged):
        r.commit(6532)


# ------------------------------------------------------------------- helpers

def test_text_survives_a_detached_element():
    driver = FakeDriver()
    r = relay_with(driver)

    class Stale(FakeElement):
        @property
        def text(self):
            raise relay_site.StaleElementReferenceException("detached")

    assert r._text(Stale()) is None
    assert r._text(FakeElement("  hi  ")) == "hi"


def test_the_button_matcher_is_case_insensitive():
    """Case is ignored, so callers can pass a label in any case.

    It was originally case-sensitive and `upload(mode="steps")` silently
    missed the "Steps" button -- which showed up as a SiteChanged naming a
    button that was on screen the whole time.
    """
    driver = FakeDriver(body="", buttons=[FakeElement("SUBMIT STEPS")])
    r = relay_with(driver)
    assert r._btn("submit steps") is not None
    assert r._btn("submit steps", exact=True) is not None
    # exact still means exact: no substring match
    assert r._btn("submit step", exact=True) is None


def test_an_absent_button_is_none_not_an_error():
    driver = FakeDriver(body="", buttons=[])
    r = relay_with(driver)
    assert r._btn("nope") is None


# ------------------------------------------------------- the small helpers

def test_in_container_from_the_environment(monkeypatch):
    """The env var is the reliable signal; /proc/1/cgroup is not."""
    monkeypatch.setenv("container", "podman")
    assert relay_site._in_container() is True


def test_in_container_from_the_marker_file(monkeypatch, tmp_path):
    monkeypatch.delenv("container", raising=False)
    monkeypatch.delenv("IN_CONTAINER", raising=False)
    monkeypatch.setattr(relay_site.os.path, "exists",
                        lambda p: p == "/.dockerenv")
    assert relay_site._in_container() is True


def test_not_in_container_on_bare_metal(monkeypatch):
    monkeypatch.delenv("container", raising=False)
    monkeypatch.delenv("IN_CONTAINER", raising=False)
    monkeypatch.setattr(relay_site.os.path, "exists", lambda p: False)
    monkeypatch.setattr(relay_site.os, "getpid", lambda: 4242)
    assert relay_site._in_container() is False


def test_text_strips_the_element_text():
    r = relay_site.Relay.__new__(relay_site.Relay)
    assert r._text(FakeElement("  hello  ")) == "hello"


def test_text_tolerates_a_detached_node():
    """React re-renders on every state change, so a node can vanish mid-read."""
    class Gone:
        @property
        def text(self):
            raise relay_site.StaleElementReferenceException("detached")
    r = relay_site.Relay.__new__(relay_site.Relay)
    assert r._text(Gone()) is None


def test_text_tolerates_any_other_failure():
    class Broken:
        @property
        def text(self):
            raise RuntimeError("nope")
    r = relay_site.Relay.__new__(relay_site.Relay)
    assert r._text(Broken()) is None


def test_the_calendar_header_is_read_from_the_page(monkeypatch):
    """The caption is how the bot knows which month the picker is showing."""
    r = relay_site.Relay.__new__(relay_site.Relay)
    r.driver = type("D", (), {})()
    r.driver.find_elements = lambda *a, **kw: [
        FakeElement("Some other text"), FakeElement("October 2026")]
    assert r._calendar_header() == "October 2026"


def test_the_calendar_header_is_none_when_absent(monkeypatch):
    r = relay_site.Relay.__new__(relay_site.Relay)
    r.driver = type("D", (), {})()
    r.driver.find_elements = lambda *a, **kw: [FakeElement("nothing")]
    assert r._calendar_header() is None


def test_the_calendar_header_ignores_a_blank_caption():
    r = relay_site.Relay.__new__(relay_site.Relay)
    r.driver = type("D", (), {})()
    r.driver.find_elements = lambda *a, **kw: [FakeElement("  ")]
    assert r._calendar_header() is None


def test_require_raises_when_the_condition_fails():
    """The guard that turns a wrong page into an explainable error."""
    r = relay_site.Relay.__new__(relay_site.Relay)
    with pytest.raises(relay_site.SiteChanged):
        r._require(False, "the page is not what we expected")


def test_require_passes_when_the_condition_holds():
    r = relay_site.Relay.__new__(relay_site.Relay)
    r._require(True, "never used")


def test_authed_is_true_when_the_nav_exposes_upload():
    r = relay_site.Relay.__new__(relay_site.Relay)
    link = type("A", (), {})()
    link.get_attribute = lambda k: "/upload"
    r.driver = type("D", (), {})()
    r.driver.find_elements = lambda *a, **kw: [link]
    assert r._authed() is True


def test_authed_is_false_when_the_nav_does_not():
    r = relay_site.Relay.__new__(relay_site.Relay)
    link = type("A", (), {})()
    link.get_attribute = lambda k: "/home"
    r.driver = type("D", (), {})()
    r.driver.find_elements = lambda *a, **kw: [link]
    assert r._authed() is False


def test_authed_is_false_when_the_page_is_gone():
    """A driver that throws must read as 'not signed in', not as a crash."""
    r = relay_site.Relay.__new__(relay_site.Relay)
    r.driver = type("D", (), {})()
    r.driver.find_elements = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("page gone"))
    assert r._authed() is False


def test_login_signs_in_as_its_chat_despite_a_live_session(monkeypatch):
    """B must never ride A's session.

    A persisted profile used to make login() return early on ANY live
    session without checking whose. Now every launch is a fresh profile
    and login always fills the form, so this test puts a live session in
    front and asserts the given credentials are typed anyway.
    """
    monkeypatch.setattr(relay_site, "LOGIN_PROBES", 1)
    typed = []

    class TypingElement(FakeElement):
        def __init__(self, kind):
            super().__init__()
            self._kind = kind

        def get_attribute(self, name):
            if name == "type":
                return self._kind
            return super().get_attribute(name)

        def send_keys(self, *keys):
            typed.extend(keys)

    class HrefElement(FakeElement):
        def __init__(self, href):
            super().__init__()
            self._href = href

        def get_attribute(self, name):
            if name == "href":
                return self._href
            return super().get_attribute(name)

    driver = FakeDriver(
        inputs=[TypingElement("text"), TypingElement("password")],
        buttons=[FakeElement()],
        elements={"a": [HrefElement("https://site.example/upload")]},
        current_url="https://site.example/home",
    )
    r = relay_with(driver)
    r.login("bee", "hunter2")
    assert typed == ["bee", "hunter2"]


def test_browser_profile_is_fresh_per_launch_and_removed_on_stop(
    tmp_path, monkeypatch
):
    """No cookies survive a Screenshot: temp dir per start, gone at stop."""
    import os
    import sys

    class FakeFirefox:
        def __init__(self, options=None, service=None):
            pass

        def set_page_load_timeout(self, s):
            pass

        def quit(self):
            pass

    monkeypatch.setattr(relay_site, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(relay_site, "GECKO", sys.executable)
    monkeypatch.setattr(relay_site.config, "FIREFOX_BIN", sys.executable)
    monkeypatch.setattr(relay_site.webdriver, "Firefox", FakeFirefox)
    monkeypatch.setattr(
        relay_site.memory, "require_memory", lambda: {"available_mb": 9999}
    )
    r = relay_site.Relay("https://site.example", headless=True, verbose=False)
    r.start()
    first = r._profile
    assert first is not None and os.path.isdir(first)
    assert os.path.dirname(first) == str(tmp_path)
    r.stop()
    assert r._profile is None
    assert not os.path.exists(first)
    r.start()
    try:
        assert r._profile is not None and r._profile != first
    finally:
        r.stop()
    assert os.listdir(tmp_path) == []
