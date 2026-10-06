"""Everything the user touches: handlers, prompts, keyboards, access, copy.

The public surface -- what the tests and __main__ reach for -- is re-exported
here, so callers do not need to know which submodule owns what. The submodules
themselves import from each other, never through this package, so the
dependency direction stays visible in the import statements.
"""
