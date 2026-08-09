"""mail-inbox runtime — the IMAP MessageSourceProvider and its helpers.

A multi-module package (provider · imap_client · mime · settings) that imports
core ONLY through ``personalclaw.sdk.*``. The gateway's app loader keeps the app
dir on sys.path only while it execs the entry module, so ``provider.py`` pins the
dir back on sys.path (mirroring telegram-channel) to keep the sibling imports
resolving for the life of the process.
"""
