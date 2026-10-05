"""Prompt-bound receiving addresses — the mail-inbox app's address table.

A **bound address** is a purpose-specific receiving address (``travel@<your-domain>``,
``you+travel@gmail.com``) carrying a **stored, user-authored prompt**. Mail that arrives
at it runs that prompt against the mail — so any service that can send email becomes an
automation trigger with zero per-vendor integration. The user composes it with their
ordinary Gmail/Outlook filters (see the app README's worked example).

Two rules this module exists to keep, and never blur:

1. **The stored prompt is the owner's instruction, the mail is NOT.** They never meet in one
   text here: the provider hands PersonalClaw the mail's words, raw, as the message's ``text``
   and the bound row's ``default_prompt`` as its ``instruction``
   (``personalclaw.sdk.inbox.IncomingMessage``). PersonalClaw takes the instruction as hers
   because this app's manifest declares ``default_prompt`` an instruction
   (``x-meta.instruction``) and her settings hold it word for word, and a fire on the mail
   hands its action her prompt first, outside any fence, then every byte from the wire
   (subject, body) fenced once, by PersonalClaw, with where it came from. This app fences
   nothing: a fence of its own would be wrapped again, and its prompt with it.
2. **Per-address senders FAIL CLOSED, and only NARROW.** A bound row's ``allow_senders``
   is checked *after* the app-wide allowlist (``settings.allow_senders``) has already
   passed, so it can only ever remove senders, never add them. An EMPTY per-address list
   allows NOTHING — the same posture the global list takes: a bound address is an
   inbound *security* surface, so a missing list disables it rather than opening it.

Storage: the table lives in this app's own ``ProviderSettings`` store under
``bound_addresses`` (``~/.personalclaw/apps/mail-inbox/data/config.json``), declared in
``app.json`` so the platform's generated app-settings page renders and validates it. No
secrets live here — the passwords are settings of their own, kept in the credential store
(see ``settings.py``).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: The settings key the address table is stored under (declared in app.json).
SETTINGS_KEY = "bound_addresses"


def normalize_senders(value: object) -> list[str]:
    """Normalize an allowlist: strip, lowercase, drop blanks, dedupe (order-preserving).

    ONE definition for both the app-wide list and each bound row's, so the two can never
    disagree about what a pattern means (a per-address list that compared case-sensitively
    would silently reject a sender the global list accepts).
    """
    if not isinstance(value, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        s = str(item).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def sender_matches(from_addr: str, allow_senders: list[str]) -> bool:
    """Fail-closed glob match of a From address against an allowlist.

    An EMPTY allowlist matches NOTHING. That is the whole posture: an
    unknown sender can never trigger anything, so a missing/emptied list disables
    triggering rather than permitting everything.
    """
    if not allow_senders:
        return False
    addr = (from_addr or "").strip().lower()
    if not addr:
        return False
    return any(fnmatch.fnmatch(addr, pattern) for pattern in allow_senders)


@dataclass
class BoundAddress:
    """One prompt-bound receiving address."""

    address: str
    name: str = ""
    default_prompt: str = ""
    enabled: bool = True
    #: Per-address sender allowlist. NARROWS the app-wide one; empty ⇒ fires nothing.
    allow_senders: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """A human name for logs/UI — the given name, else the address itself."""
        return self.name or self.address

    @property
    def bound(self) -> bool:
        """Whether this row can fire at all.

        A row with no stored prompt is not a binding — it is a half-filled form. Firing it
        would spawn an unattended turn whose only instruction is untrusted mail, which is
        precisely the composition the fence exists to prevent.
        """
        return bool(self.enabled and self.address and self.default_prompt)

    def sender_allowed(self, from_addr: str) -> bool:
        """Fail-closed per-address check (see :func:`sender_matches`)."""
        return sender_matches(from_addr, self.allow_senders)


def load_bound_addresses(value: object) -> list[BoundAddress]:
    """Coerce the stored table into :class:`BoundAddress` rows. Tolerant, never raises.

    The platform's config PUT validates only the top-level type (``array``) — per-item
    shape is this app's job. A malformed row is DROPPED with a warning rather than
    defaulted, because every default here would be a security decision the user did not
    make (an address with no prompt, or a prompt with no sender list).
    """
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[BoundAddress] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            logger.warning("mail-inbox: dropping a non-object bound-address entry")
            continue
        address = str(raw.get("address", "") or "").strip().lower()
        if not address:
            logger.warning("mail-inbox: dropping a bound-address entry with no address")
            continue
        if address in seen:
            logger.warning("mail-inbox: dropping a duplicate bound address %r", address)
            continue
        seen.add(address)
        rows.append(
            BoundAddress(
                address=address,
                name=str(raw.get("name", "") or "").strip(),
                default_prompt=str(raw.get("default_prompt", "") or "").strip(),
                enabled=bool(raw.get("enabled", True)),
                allow_senders=normalize_senders(raw.get("allow_senders", [])),
            )
        )
    return rows


def match_bound_address(
    addresses: list[BoundAddress], recipients: list[str]
) -> BoundAddress | None:
    """The first bindable row whose address is one of ``recipients``, else ``None``.

    Exact (case-insensitive) match on the full address, deliberately — not a suffix or
    domain rule. All three receiving-address strategies this app supports resolve to a
    literal address in a recipient header (``travel@your-domain``, ``you+travel@gmail.com``,
    a per-purpose mailbox), and a looser rule would let ``nottravel@…`` bind to
    ``travel@…``. A row that is disabled or promptless is skipped here, so an unfinished
    binding behaves exactly like no binding.
    """
    wanted = {r.strip().lower() for r in recipients if r and r.strip()}
    if not wanted:
        return None
    for row in addresses:
        if row.bound and row.address in wanted:
            return row
    return None

