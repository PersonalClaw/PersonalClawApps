"""email-channel runtime: a two-way conversational email channel over stdlib IMAP/SMTP.

The package is deliberately NOT named ``email`` — a top-level module or package with
that name shadows the stdlib ``email`` package, and every module here depends on
``email.message`` / ``email.parser`` / ``email.utils`` resolving to the stdlib. The
app dir goes on ``sys.path`` at runtime (the app loader's contract), so a sibling
``email.py`` would break MIME parsing for this app AND anything else importing
stdlib email in-process.
"""
