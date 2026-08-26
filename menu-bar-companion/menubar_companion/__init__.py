"""macOS menu-bar companion for a PersonalClaw gateway you already run (AS-7).

This is a **client** app (``platform.installMode = "client"``): the platform never
installs it on the server, so nothing here is imported by the gateway. It runs as a
small process on the owner's own Mac and talks to the gateway the way any other
client does — HTTP for state, ONE ``/api/ws`` connection for change signals.

The module split is the design:

``settings``  local preferences + credentials on the user's machine (0600).
``api``       the HTTP surface: ``GET /api/loops``, ``GET /api/approvals``,
              ``POST /api/approvals/{id}/{action}``. stdlib only.
``model``     the rendered view. Every number it shows is DERIVED from the last
              HTTP read — there is no counter maintained beside a list.
``doorbell``  the socket. It is a doorbell, not a data channel: the frame reader
              discards payload bytes before returning, so a payload physically
              cannot reach ``model``, and a ring means only "re-read over HTTP".
``notify``    native notification posting, suppressed while muted.
``tray``      the macOS status-item host, and the ONLY module that needs a GUI
              toolkit. Imported lazily so everything above is testable anywhere.

Nothing in this package imports PersonalClaw core. A client app must run on a Mac
whose only PersonalClaw is the gateway it is pointed at, which may be another host.
"""

__all__ = ["api", "doorbell", "model", "notify", "settings", "tray"]
