"""The Relay: a Telegram bot that moves Screenshots into Submissions.

Layout, by layer rather than by file type:

  relay/                  runtime support: config, clock, memory, console,
                          progress, errors. No Telegram, no browser.
  relay/store/            what survives a restart: the ledger and the vault.
  relay/site/             the competition Site, driven through headless
                          Firefox. driver.py is the browser; parsing.py is the
                          pure functions that read the dashboard.
  relay/telegram/         everything the user touches: handlers, prompts,
                          keyboards, the date picker, access control, copy.

Imports point one way: telegram -> site/store/support, never the reverse.
A module in site/ or store/ that needs to talk to Telegram is a layering
violation, and the import graph will say so loudly.
"""
