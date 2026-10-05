# Access Modes, per-chat credentials, and a required setting

## Context

The Relay started with a single hard-wired chat id: one allowlist entry, one
Site account, credentials in `secrets.env`. Three things were wrong with that
for a tool meant to be shared and long-lived:

- The Site password sat in a file that outlives the password.
- `ALLOWED_CHAT_ID` conflated two different jobs — *who may drive the Relay*
  and *whose Site account a Submission belongs to*.
- The prompt that asks for credentials did not exist, so there was no way to
  add or change an account without editing config and restarting.

Meanwhile the site holds a live leaderboard with 800-odd participants per House,
and the Relay's whole job is to write to it.

## Decision

Three things, separately.

**1. `ACCESS_MODE` is mandatory. The bot exits if it is unset.**

| mode | who may drive the Relay |
|---|---|
| `whitelist_claim` | the first chat to `/start` claims it; others need granting |
| `blacklist` | any chat not denied. A public write path. |
| `shared_secret` | each person presents a per-person secret at `/start` |

**2. Credentials are prompted for, per chat, and stored encrypted.** Preset
Credentials from the environment remain available and are *offered*, not
required. They are never the only way in.

**3. Rate-limited, never locked out.** One failed Shared Secret attempt per chat
per 30 seconds, checked inside `access.present_secret` rather than in handlers.

## Why

Failing closed on `ACCESS_MODE` is the load-bearing decision. The difference
between `whitelist_claim` and `blacklist` is who can write to a live
leaderboard account, and that is not something to infer from an absent value.
An unset variable means an unconfigured install, and an unconfigured install
should not silently be the open one.

`blacklist` exists because the owner asked for it, and it is a legitimate
choice for a bot that only a handful of people know about. It is also the
dangerous one, so it is named in the startup banner, in `/status`, and in the
README rather than left to be discovered.

Per-chat credentials are what make `blacklist` survivable. Without them, any
stranger who found the bot would record into the *owner's* account, because the
Relay has exactly one set of Site credentials. With them, a stranger supplies
their own and the Submission belongs to them. The cost is real: the Relay now
holds other people's passwords, encrypted, on a 1GB VM.

Encrypting under a key derived from the bot token is aimed at a specific
exposure this project created — the nightly ledger backup is delivered to the
same Telegram chat, so a plaintext password would sit in the chat history
forever. Deriving from the token means the backup is inert without it, and
rotating the token at @BotFather makes the old ciphertext unrecoverable rather
than merely inconvenient. That is the intended response to a suspected
compromise, and the bot says so rather than silently re-prompting.

Rate limiting instead of lockout, because a lockout lets anyone who knows a chat
id keep the owner out of their own bot. That is a worse outcome than a slow
guessing attack on a passphrase.

The limit is enforced inside `present_secret` rather than in the handler. It
was in the handler first, and a test caught that a caller could therefore
bypass it entirely — the rule was only as good as its most recent call site.

## Consequences

- `ACCESS_MODE` unset is a startup failure with a message listing the modes.
  An operator who upgrades and does not read the release notes gets a clear
  error rather than a silently public bot.
- A prompted password is deleted from the chat as soon as it is read. That does
  not unsend a notification that already rendered, so the README says to use
  private chats for the Shared Secret.
- Rotating the bot token invalidates every stored credential. The bot reports
  this distinctly (`vault_unreadable`) and re-prompts, because "your token
  changed" and "wrong password" need different responses.
- `blacklist` mode with no `DENY_CHAT_IDS` is a bot the internet can drive.
  That is a supported configuration and it is documented as such.
- Credentials are per chat, not per Telegram user. In a group chat, any member
  can trigger a Submission using credentials that member supplied.
