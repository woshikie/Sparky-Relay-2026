"""Every button the user can tap.

Reply keyboards for standing choices (they stay put above the input box, so
nothing has to be memorised), inline keyboards for answers that belong to one
particular step (the date grid, the confirmation). A reply-keyboard button is
just text arriving as a message, so the labels live here next to the dispatch
table: if the two drift apart, the button silently does nothing.
"""
import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from relay import config
from relay.clock import sg_today
from relay.telegram.datepicker import keyboard as month_grid
import relay.telegram.words as words


# ----------------------------------------------------------------------
# keyboards
# ----------------------------------------------------------------------

def kb_date_default(steps, reported):
    t = sg_today()
    y = t - datetime.timedelta(days=1)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Today (%s)" % t.strftime("%d %b"), callback_data="dt:today"),
        InlineKeyboardButton("Yesterday (%s)" % y.strftime("%d %b"), callback_data="dt:yday"),
    ], [
        InlineKeyboardButton("\U0001F4C5 Open datepicker", callback_data="dt:pick:%d:%d" % (t.year, t.month)),
    ]])


def kb_confirm(steps, reported, iso):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm %s" % reported, callback_data="ok:go"),
    ], [
        InlineKeyboardButton("Change date", callback_data="dt:back"),
        InlineKeyboardButton("\U0001F5D1 Cancel", callback_data="ok:cancel"),
    ]])


def kb_overwrite(new_steps, old_steps, iso):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F504 Overwrite anyway", callback_data="ok:go"),
    ], [
        InlineKeyboardButton("Keep my %s" % "{:,}".format(old_steps), callback_data="ok:cancel"),
    ]])


def kb_pick_date(year, month):
    return month_grid(year, month, sg_today())


# ----------------------------------------------------------------------
# the flow
# Button labels. Centralised because a reply-keyboard button is just text: if
# the label in the keyboard and the label in the dispatch table drift apart, the
# button silently does nothing. tests/test_reply_keyboard.py asserts both
# directions of that mapping.
LABEL_SUBMIT = "📸 Submit steps"
LABEL_LOGIN = "🔑 Sign in"
LABEL_LOGOUT = "🚪 Sign out"
LABEL_STATUS = "📋 Status"
LABEL_LOG = "📜 History"
LABEL_HELP = "❓ Help"
LABEL_SYNC = "🔄 Sync from site"
LABEL_CANCEL = "❌ Cancel"
LABEL_NEW_CREDS = "✏️ Different account"


def label_use_preset():
    """'Use <Original Author's username>' — the username is part of the button, so it has to come
    from config rather than being hard-coded here."""
    return "✅ Use %s" % config.SITE_USERNAME


# Everything reachable by tapping, resolved fresh because one label is dynamic.
BUTTON_COMMANDS = {
    LABEL_SUBMIT: "submit",
    LABEL_LOGIN: "login",
    LABEL_LOGOUT: "logout",
    LABEL_STATUS: "status",
    LABEL_LOG: "log",
    LABEL_HELP: "help",
    LABEL_SYNC: "sync",
    LABEL_CANCEL: "cancel",
    LABEL_NEW_CREDS: "new_creds",
}


def button_actions():
    """The dispatch table, including the label that depends on config."""
    table = dict(BUTTON_COMMANDS)
    if config.has_preset_credentials():
        table[label_use_preset()] = "use_preset"
    return table


def kb_done():
    """Back to the standing commands once a prompt is finished."""
    return kb_reply()


def kb_reply():
    """The standing commands, as ordinary buttons above the input box.

    A reply keyboard rather than inline, for two reasons: they are standing
    commands rather than answers to a question, and a reply keyboard stays
    put, so nothing has to be memorised or re-tapped from a scrolled-away
    message.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(LABEL_SUBMIT), KeyboardButton(LABEL_LOGIN)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_LOG)],
            [KeyboardButton(LABEL_LOGOUT), KeyboardButton(LABEL_HELP)],
            [KeyboardButton(LABEL_SYNC)],
        ],
        resize_keyboard=True,
        is_persistent=True,     # survives the client being restarted
        input_field_placeholder="Send a screenshot, or pick one",
    )


def kb_credential_choice():
    """The preset-or-not question, also as a reply keyboard.

    It was inline, which meant the only visible buttons were those two and the
    standing commands were nowhere to be seen. Inline buttons live on one
    message and scroll away; reply buttons stay. Everything that is a standing
    choice lives here; only the submission flow -- the date picker and the
    confirmation -- uses inline, because those belong to one particular step.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(label_use_preset())],
            [KeyboardButton(LABEL_NEW_CREDS), KeyboardButton(LABEL_CANCEL)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Pick one, or type your username",
    )


def kb_prompt():
    """Mid-prompt: the answer has to be typed, so keep the keyboard minimal."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(LABEL_CANCEL)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Type your answer",
    )


def kb_after_login():
    """Escape hatch on the standing keyboard, for reaching a different account."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(LABEL_NEW_CREDS), KeyboardButton(LABEL_LOGOUT)],
            [KeyboardButton(LABEL_STATUS), KeyboardButton(LABEL_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Send a screenshot, or pick one",
    )
