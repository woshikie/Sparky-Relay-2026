"""Everything the user touches: handlers, prompts, keyboards, access, copy.

The public surface -- what __main__, the tests, and operators reach for -- is
re-exported here, so callers do not need to know which submodule owns what.
The submodules themselves import from each other, never through this package,
so the dependency direction stays visible in the import statements.

Patchability note: tests patch the seams (browser_session, sign_in, get_relay)
on relay.telegram.session, where they are defined. The call sites go through
the session module object rather than a bound name, so there is exactly one
place to patch. Everything re-exported below is read, never rebound.
"""
import relay.telegram.access as access  # noqa: F401  (re-exported for callers)
from relay import config as config  # noqa: F401
from relay import memory as memory  # noqa: F401
from relay.clock import sg_now as sg_now, sg_today as sg_today  # noqa: F401
from relay.site import driver as relay_site  # noqa: F401
import relay.store.ledger as ledger  # noqa: F401
import relay.store.vault as vault  # noqa: F401
import relay.telegram.words as words  # noqa: F401

from relay.telegram.session import (  # noqa: F401
    DBG, browser_session, get_relay, log, sign_in, site_login,
)
from relay.telegram.keyboards import (  # noqa: F401
    BUTTON_COMMANDS, LABEL_CANCEL, LABEL_HELP, LABEL_LOG, LABEL_LOGIN,
    LABEL_LOGOUT, LABEL_NEW_CREDS, LABEL_STATUS, LABEL_SUBMIT, LABEL_SYNC,
    button_actions, kb_after_login, kb_confirm, kb_credential_choice,
    kb_date_default, kb_done, kb_overwrite, kb_pick_date, kb_prompt, kb_reply,
    label_use_preset,
)
from relay.telegram.prompts import (  # noqa: F401
    PROMPT_TTL, PROMPTING, _NoCredentials, _clear_stage, _prompt_stage, _scrub,
    _set_stage,
    ask_credentials, ask_password, ask_username, has_credentials, kb_for,
    on_credential_choice, on_login, on_logout, site_credentials,
)
from relay.telegram.callbacks import (  # noqa: F401
    PENDING, _cb_date, authorised, cb_date, cb_ok, ordinal,
)
from relay.telegram.commands import (  # noqa: F401
    _as_update, _run_button, backup_job, on_log, on_start, on_status, on_sync,
    on_text,
)
from relay.telegram.photo import (  # noqa: F401
    MAX_EDGE, on_photo, save_photo,
)
from relay.site.parsing import parse_profile as parse_profile  # noqa: F401
from relay.telegram.app import main as main, on_ready as on_ready  # noqa: F401
