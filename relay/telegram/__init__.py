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

import relay.store.ledger as ledger  # noqa: F401
import relay.store.vault as vault  # noqa: F401
import relay.telegram.access as access  # noqa: F401  (re-exported for callers)
import relay.telegram.pending as pending  # noqa: F401
import relay.telegram.words as words  # noqa: F401
from relay import config as config
from relay import memory as memory
from relay.clock import sg_now as sg_now
from relay.clock import sg_today as sg_today
from relay.errors import NoCredentials as NoCredentials
from relay.site import driver as relay_site  # noqa: F401
from relay.site.parsing import parse_profile as parse_profile
from relay.store.backup import backup_job as backup_job
from relay.telegram.app import main as main
from relay.telegram.app import on_ready as on_ready
from relay.telegram.callbacks import (  # noqa: F401
    _cb_date,
    authorised,
    cb_date,
    cb_ok,
    ordinal,
)
from relay.telegram.commands import (  # noqa: F401
    _as_update,
    _run_button,
    on_log,
    on_start,
    on_status,
    on_sync,
    on_text,
)
from relay.telegram.keyboards import (  # noqa: F401
    BUTTON_COMMANDS,
    LABEL_CANCEL,
    LABEL_HELP,
    LABEL_LOG,
    LABEL_LOGIN,
    LABEL_LOGOUT,
    LABEL_NEW_CREDS,
    LABEL_STATUS,
    LABEL_SUBMIT,
    LABEL_SYNC,
    button_actions,
    kb_after_login,
    kb_confirm,
    kb_credential_choice,
    kb_date_default,
    kb_done,
    kb_overwrite,
    kb_pick_date,
    kb_prompt,
    kb_reply,
    label_use_preset,
)
from relay.telegram.photo import (  # noqa: F401
    MAX_EDGE,
    on_photo,
    save_photo,
)
from relay.telegram.prompts import (  # noqa: F401
    PROMPT_TTL,
    PROMPTING,
    ask_credentials,
    ask_password,
    ask_username,
    clear_stage,
    credential_prompt_body,
    has_credentials,
    kb_for,
    on_credential_choice,
    on_login,
    on_logout,
    prompt_stage,
    refuse,
    scrub,
    set_stage,
    site_credentials,
    start_credential_stage,
)
from relay.telegram.session import (  # noqa: F401
    DBG,
    browser_session,
    get_relay,
    log,
    sign_in,
    site_login,
)
