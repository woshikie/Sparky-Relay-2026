"""Test suite for the Relay.

The config module validates ACCESS_MODE at import time and raises SystemExit
when it is absent, which is the intended production behaviour but makes it
awkward to test. So every test module is given a mode up front by
`conftest.py`, and modules under test are re-imported when a test needs a
different one.
"""
import importlib
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
# tests/ next to the modules under test, so a bare `import vault` resolves.
ROOT = HERE
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# config reads both the environment and secrets.env. Tests must not depend on
# whatever the developer has configured locally, so the file is ignored and the
# environment is pinned. conftest re-applies this before every re-import.
os.environ["RELAY_SKIP_SECRETS_FILE"] = "1"
os.environ.setdefault("ACCESS_MODE", "whitelist_claim")
os.environ.setdefault("RELAY_STATE_DIR", tempfile.mkdtemp(prefix="relay-pytest-"))

# The parent packages come first, and that order is load-bearing. `from
# relay.telegram import access` does NOT do a straight sys.modules lookup the
# way `import access` did: it imports the parent, then takes the `access`
# attribute off it, and only imports the submodule on AttributeError. A stale
# parent still carrying the old attribute short-circuits the whole thing and
# hands back the pre-purge module with no error. Popping the parents forces
# the attribute to be re-bound to the fresh submodule.
#: Every relay module the purge drops. Kept as an explicit list rather than
#: a prefix scan so a new module is a conscious addition here -- and because a
#: static list is what caught the failures.py staleness: it binds `memory` at
#: import for its isinstance mapping, and a purge that does not drop it leaves
#: the mapping comparing against the previous test's classes.
#:
#: relay.errors is deliberately absent: it is stateless -- just exception
#: classes, no environment read -- so purging it only splits class identity
#: between the raiser and pytest.raises. One generation forever is correct.
#:
#: The parent packages are in the list, and that is load-bearing, not tidy.
#: `from relay.telegram import access` does NOT do a straight sys.modules
#: lookup the way `import access` did: it imports the parent, then takes the
#: `access` attribute off it, and only imports the submodule on
#: AttributeError. A stale parent still carrying the old attribute
#: short-circuits the whole thing and hands back the pre-purge module with no
#: error. Popping the parents forces the attribute to be re-bound.
MODULES = ("relay", "relay.telegram", "relay.store", "relay.site",
           "relay.clock", "relay.config", "relay.console",
           "relay.progress", "relay.telegram.access", "relay.store.ledger",
           "relay.store.vault", "relay.telegram.words",
           "relay.memory", "relay.telegram.datepicker",
           "relay.site.driver", "relay.site.parsing",
           "relay.telegram.session", "relay.telegram.keyboards",
           "relay.telegram.prompts", "relay.telegram.commands",
           "relay.telegram.photo", "relay.telegram.callbacks",
           "relay.telegram.failures", "relay.telegram.app")


# The bot's fixtures are imported as bare names, so the repo root has to be on
# sys.path for re-imports to resolve after _purge() drops them.
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _purge():
    """Drop the app modules so the next import re-reads the environment.

    `config` validates ACCESS_MODE and raises SystemExit at import time, which
    is the intended production behaviour but makes module-level `import
    access` in a test depend on whatever the environment happened to hold. So
    tests import their dependencies inside the test body, by which point the
    fixture has set the environment they need.
    """
    for name in MODULES:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Every test gets an isolated state dir and the documented env baseline."""
    monkeypatch.setenv("RELAY_SKIP_SECRETS_FILE", "1")
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("ACCESS_MODE", "whitelist_claim")
    monkeypatch.setenv("BACKUP_TIME", "03:17")
    monkeypatch.delenv("SITE_USERNAME", raising=False)
    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    monkeypatch.delenv("DENY_CHAT_IDS", raising=False)
    monkeypatch.delenv("SHARED_SECRETS", raising=False)
    _purge()
    yield
    _purge()


# ---- things tests reach for -------------------------------------------
# A fake bot token. Never a real one, and never the owner's credentials.

FAKE_TOKEN = "123456:TESTTOKEN"
# Obviously-fake site credentials. The suite must never contain a real
# username or password, not even as a fixture.
FAKE_SITE_USERNAME = "testuser"
FAKE_SITE_PASSWORD = "testpass123"


def reload_with(monkeypatch, **env):
    """Re-import the app under test with specific environment values.

    Raises ConfigRefused if config refuses to load, carrying the message it
    would have printed — so tests can assert on what the operator sees rather
    than on an exit code.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _purge()
    return importlib.import_module("relay.config")


@pytest.fixture
def fresh_vault():
    """A vault module imported after any previous test replaced it."""
    return importlib.import_module("relay.store.vault")


@pytest.fixture
def fresh_ledger():
    mod = importlib.import_module("relay.store.ledger")
    mod.init()
    return mod


@pytest.fixture
def config():
    return importlib.import_module("relay.config")


@pytest.fixture
def ledger():
    mod = importlib.import_module("relay.store.ledger")
    mod.init()
    return mod


@pytest.fixture
def vault():
    return importlib.import_module("relay.store.vault")


@pytest.fixture
def access(config, ledger):
    return importlib.import_module("relay.telegram.access")


@pytest.fixture
def memory(config):
    """The memory module as it is *now*.

    Added because a test module that does `import memory` at the top holds a
    stale reference after _purge(), and monkeypatching that stale copy silently
    does nothing. That is not hypothetical: it left a real Firefox running for
    the whole of one test file, which is why the suite took 50 seconds.
    """
    return importlib.import_module("relay.memory")


@pytest.fixture
def bot(config, ledger):
    """The telegram package: the public surface the tests reach for.

    Imported fresh after every purge, like everything else. The submodules do
    the work; this is the documented API over them.
    """
    mod = importlib.import_module("relay.telegram")
    mod.PROMPTING.clear()
    mod.PENDING.clear()
    return mod


@pytest.fixture
def session():
    """The browser lifecycle and the process singletons.

    Tests patch the seams (browser_session, sign_in, get_relay) here, where
    they are defined. The call sites go through the session module object, so
    there is exactly one place to patch -- patching the package attribute
    would silently do nothing.
    """
    return importlib.import_module("relay.telegram.session")
