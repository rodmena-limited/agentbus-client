"""Terminal QR rendering, in one place because two callers need it.

`segno` is an OPTIONAL extra (`pip install 'rodmena-agentbus[qr]'`) and is
imported inside `render()`, never at module scope. It measures 23ms to import —
more than this client's entire startup — and every `send`, `inbox` and hook
invocation would otherwise have paid it for a feature almost nobody calls.

This module exists so the CLI and the session-start hook cannot drift: a caption
printed under an absent QR, or one caller learning about the extra while the
other still says "pip install segno", is the class of bug that comes from two
copies of four lines.
"""

from __future__ import annotations

import sys

INSTALL_HINT = "QR rendering needs the optional `qr` extra: pip install 'rodmena-agentbus[qr]'"


def render(payload: str, *, quiet: bool = False) -> bool:
    """Print a QR for `payload`. Returns whether anything was actually drawn.

    The return value is load-bearing: the caller must not caption a QR that did
    not render. `quiet` suppresses the install hint for unrequested displays —
    a session that never asked for a QR should not be nagged about an extra it
    has no reason to want.
    """
    try:
        import segno
    except ImportError:
        if not quiet:
            print(INSTALL_HINT, file=sys.stderr)
        return False
    segno.make(payload, error="m").terminal(compact=True)
    return True


def should_offer_unrequested(ingress_policy: str | None) -> bool:
    """May we show a QR nobody asked for? (SPECS/0032 R6, R8)

    Only under `open`. A QR is an INVITATION to mail this agent; under
    `contacts-only` (the default) or `closed`, a stranger who accepts it is
    rejected — `not_in_contacts` or `ingress_closed` — so we would be
    advertising an address that will not accept the mail it asks for.

    Unknown is NOT open. The field is absent from servers older than the release
    that added it, so the default must be silence rather than a guess: the cost
    of wrongly staying quiet is a missing convenience, and the cost of wrongly
    inviting is mail that bounces off a closed workspace.
    """
    return ingress_policy == "open"
