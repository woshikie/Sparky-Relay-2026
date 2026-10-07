"""config.py's env and file loading, and the access check that depends on it.

config validates ACCESS_MODE at import time and raises SystemExit when it is
absent. That is the intended production behaviour — a missing mode is the
difference between "only me" and "the internet" — so the tests that need a
different mode re-import the module and expect the failure.
"""
import os
import subprocess
import sys
import tempfile

import pytest

from relay.errors import ConfigRefused

from conftest import (reload_with, FAKE_TOKEN, FAKE_SITE_USERNAME,
                      FAKE_SITE_PASSWORD)


# ------------------------------------------------------- the mandatory mode

def test_unset_mode_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCESS_MODE", raising=False)
    with pytest.raises(ConfigRefused):
        reload_with(monkeypatch, ACCESS_MODE="", RELAY_STATE_DIR=str(tmp_path))


def test_the_refusal_lists_the_modes(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCESS_MODE", raising=False)
    with pytest.raises(ConfigRefused) as exc:
        reload_with(monkeypatch, ACCESS_MODE="", RELAY_STATE_DIR=str(tmp_path))
    for mode in ("whitelist_claim", "blacklist", "shared_secret"):
        assert mode in str(exc.value)


def test_the_refusal_says_it_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCESS_MODE", raising=False)
    with pytest.raises(ConfigRefused) as exc:
        reload_with(monkeypatch, ACCESS_MODE="", RELAY_STATE_DIR=str(tmp_path))
    assert "Refusing to start" in str(exc.value)


def test_an_unknown_mode_refuses_to_start(monkeypatch, tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        reload_with(monkeypatch, ACCESS_MODE="trustme", RELAY_STATE_DIR=str(tmp_path))
    assert "trustme" in str(exc.value)
    assert "whitelist_claim" in str(exc.value)


@pytest.mark.parametrize("mode", ["whitelist_claim", "blacklist", "shared_secret"])
def test_each_documented_mode_is_accepted(monkeypatch, tmp_path, mode):
    cfg = reload_with(monkeypatch, ACCESS_MODE=mode, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.ACCESS_MODE == mode
    assert mode in cfg.ACCESS_MODES


def test_the_mode_is_trimmed_and_lowercased(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, ACCESS_MODE="  BlackList\t",
                      RELAY_STATE_DIR=str(tmp_path))
    assert cfg.ACCESS_MODE == "blacklist"


# ------------------------------------------------------------- precedence

def test_the_environment_beats_the_file(monkeypatch, tmp_path):
    """A container has no secrets.env; the environment is the only source."""
    monkeypatch.setenv("SITE_USERNAME", "from-env")
    monkeypatch.setenv("SITE_PASSWORD", "from-env")
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.SITE_USERNAME == "from-env"


def test_the_file_is_read_when_present(monkeypatch, tmp_path):
    """A bare-metal install configures via secrets.env, not the environment."""
    secrets = tmp_path / "secrets.env"
    secrets.write_text("SITE_USERNAME=from-file\nSITE_PASSWORD=pw\n")
    monkeypatch.delenv("SITE_USERNAME", raising=False)
    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    monkeypatch.delenv("RELAY_SKIP_SECRETS_FILE", raising=False)
    from relay import config
    monkeypatch.setattr(config, "ENV_PATH", str(secrets))
    assert config._load()["SITE_USERNAME"] == "from-file"


def test_skipping_the_file_ignores_it(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("SITE_USERNAME=from-file\n")
    monkeypatch.setenv("RELAY_SKIP_SECRETS_FILE", "1")
    from relay import config
    monkeypatch.setattr(config, "ENV_PATH", str(secrets))
    assert config._load().get("SITE_USERNAME") is None


def test_an_unrelated_variable_is_not_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TOTALLY_UNRELATED", "surprise")
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert "TOTALLY_UNRELATED" not in cfg.ENV


def test_an_empty_variable_does_not_override_the_file(monkeypatch, tmp_path):
    """compose's ${VAR:-} puts an empty string in the environment for "unset".

    That must not blank a value that came from secrets.env, or removing a
    variable from the compose file would silently erase the file's setting.
    """
    secrets = tmp_path / "secrets.env"
    secrets.write_text("SITE_USERNAME=from-file\nSITE_PASSWORD=pw\n")
    monkeypatch.delenv("RELAY_SKIP_SECRETS_FILE", raising=False)
    monkeypatch.setenv("SITE_USERNAME", "")
    from relay import config
    monkeypatch.setattr(config, "ENV_PATH", str(secrets))
    loaded = config._load()
    assert loaded["SITE_USERNAME"] == "from-file"


# -------------------------------------------------------- preset credentials

def test_preset_credentials_are_optional(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.has_preset_credentials() is False


def test_a_username_without_a_password_is_not_a_preset(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, SITE_USERNAME=FAKE_SITE_USERNAME,
                      RELAY_STATE_DIR=str(tmp_path))
    assert cfg.has_preset_credentials() is False


def test_both_halves_make_a_preset(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, SITE_USERNAME=FAKE_SITE_USERNAME,
                      SITE_PASSWORD=FAKE_SITE_PASSWORD,
                      RELAY_STATE_DIR=str(tmp_path))
    assert cfg.has_preset_credentials() is True


# --------------------------------------------------------------- require

def test_require_passes_without_site_credentials(monkeypatch, tmp_path):
    """The whole point of the prompt: the password is not mandatory here."""
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.require() is True


def test_require_demands_the_bot_token(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        cfg.require()
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_the_failure_says_where_it_looked(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        cfg.require()
    assert "secrets.env" in str(exc.value)
    assert "README" in str(exc.value)


def test_the_failure_explains_that_credentials_are_optional(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        cfg.require()
    assert "optional" in str(exc.value)


# ------------------------------------------------------------ state layout

def test_state_paths_hang_off_the_state_dir(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.LEDGER_DB == str(tmp_path / "ledger.sqlite3")
    assert cfg.INBOX == str(tmp_path / "inbox")
    assert cfg.LOGS == str(tmp_path / "logs")


def test_the_state_dir_is_created_on_demand(monkeypatch, tmp_path, db):
    deep = tmp_path / "deep" / "path"
    reload_with(monkeypatch, RELAY_STATE_DIR=str(deep))
    import importlib
    db = importlib.import_module("relay.store.db")
    db.init()
    assert (deep / "ledger.sqlite3").exists()


# ------------------------------------------------------------- deny list

def test_deny_list_parses_comma_and_semicolon(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, DENY_CHAT_IDS="1, 2;3",
                      RELAY_STATE_DIR=str(tmp_path))
    assert cfg.DENY_CHAT_IDS == [1, 2, 3]


def test_deny_list_ignores_junk(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, DENY_CHAT_IDS="1,abc,,3",
                      RELAY_STATE_DIR=str(tmp_path))
    assert cfg.DENY_CHAT_IDS == [1, 3]


def test_deny_list_defaults_to_empty(monkeypatch, tmp_path):
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.DENY_CHAT_IDS == []


# ------------------------------------------------------------- secrets

def test_shared_secrets_default_to_absent(monkeypatch, tmp_path):
    """None, not "": an absent spec must not clobber secrets added at runtime."""
    cfg = reload_with(monkeypatch, RELAY_STATE_DIR=str(tmp_path))
    assert cfg.SHARED_SECRETS is None


# ------------------------------------------------ the failure, in a subprocess

def test_a_missing_mode_fails_before_anything_else():
    """Run it for real, so the import-time guard is proven to fire.

    The unit tests above catch ConfigRefused; this proves the process actually
    exits, which is what an operator sees.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        "PATH": "/usr/bin:/bin",
        "ACCESS_MODE": "",
        "RELAY_SKIP_SECRETS_FILE": "1",
        "RELAY_STATE_DIR": tempfile.mkdtemp(prefix="relay-subproc-"),
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    }
    p = subprocess.run([sys.executable, "-c", "from relay import config"], env=env, cwd=root,
                       capture_output=True, text=True)
    assert p.returncode != 0
    assert "ConfigRefused" in p.stderr
    assert "ACCESS_MODE" in p.stderr


def test_the_import_guard_prints_rather_than_traces():
    """An operator should see the message, not a traceback.

    relay/__main__.py imports config, so running the bot with a bad
    ACCESS_MODE is the realistic failure. If it surfaced a traceback, the
    useful half of the message would be buried under import frames.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        "PATH": "/usr/bin:/bin",
        "ACCESS_MODE": "",
        "RELAY_SKIP_SECRETS_FILE": "1",
        "RELAY_STATE_DIR": tempfile.mkdtemp(prefix="relay-subproc-"),
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    }
    # Running the bot catches the refusal and prints the message. A bare
    # import would surface it as a traceback instead, because config raises
    # during import and there is nothing around it yet.
    p = subprocess.run([sys.executable, "-m", "relay"], env=env, cwd=root,
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert "Traceback" not in p.stderr
    assert "Refusing to start" in p.stderr
    assert "whitelist_claim" in p.stderr


def test_a_valid_mode_lets_the_import_through(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        "PATH": "/usr/bin:/bin",
        "ACCESS_MODE": "blacklist",
        "RELAY_SKIP_SECRETS_FILE": "1",
        "RELAY_STATE_DIR": str(tmp_path),
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    }
    p = subprocess.run(
        [sys.executable, "-c",
         "from relay import config; print(config.ACCESS_MODE)"],
        env=env, cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "blacklist"
