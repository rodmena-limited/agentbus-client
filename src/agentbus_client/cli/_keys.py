"""The `agentbus` command line client."""

from __future__ import annotations

import argparse

from .. import _signing, sealing
from ..client import AgentBus, AgentBusError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _harden_if_possible, _print


def _sealing_hostname() -> str:
    """A human-readable label for this machine's key, so a list of fingerprints
    answers "which box is this" without a lookup."""
    import socket

    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def _this_machines_fingerprint() -> str | None:
    private = sealing.load_private_key()
    return sealing.fingerprint(sealing.public_from_private(private)) if private else None


def _keys_sign(
    bus: AgentBus,
    args: argparse.Namespace,
    agent: str,
    _mine: str | None,  # the SEALING fingerprint; a signing key has its own
) -> int:
    """Publish THIS AGENT's signing key (#173), so peers can verify.

    Separate from the sealing key on purpose: one answers "who may read my
    mail", the other "who can prove I wrote it", and an operator rotates them
    for different reasons.
    """

    # #220: per agent. `keys sign` used to publish the machine's one key under
    # whichever agent happened to run it, so the second agent on a box published
    # the FIRST one's key as its own.
    _private, public = sealing.ensure_signing_keypair(agent)
    published = bus._request(
        "POST",
        f"/v1/agents/{agent}/pubkey",
        json={
            "public_key": public,
            "label": args.label or _sealing_hostname(),
            "algorithm": "ed25519",
        },
        agent=agent,
    )
    digest = _signing.fingerprint(public)
    if args.json:
        _print({**published, "fingerprint": digest, "algorithm": "ed25519"}, True)
        return 0
    print(f"signing key published: {digest}")
    # NOT "from this machine": the key belongs to this AGENT. Saying otherwise
    # is what made a shared signing key look intentional (#220).
    print(f"  every message you send as {agent} is now signed")
    print("  peers verify with: agentbus verify-sender <DELIVERY_ID>")
    return 0


def _published_signing_keys(bus: AgentBus, agent: str) -> list[dict]:
    """Published ed25519 keys for `agent`, or [] — never a raised error (#43).

    A listing that dies because ONE of two lookups failed tells the reader less
    than one that shows the half it has. Returns [] on 404 (none published) and
    on any other failure, because this is a display path: an inability to ask is
    reported as an empty section next to a populated one, which is visibly
    different from a clean 'none'.
    """
    try:
        data = bus._request("GET", f"/v1/agents/{agent}/pubkey", params={"algorithm": "ed25519"})
    except AgentBusError:
        return []
    return list(data.get("keys") or [])


def _keys_list(bus: AgentBus, args: argparse.Namespace, agent: str, mine: str | None) -> int:
    try:
        data = bus._request("GET", f"/v1/agents/{agent}/pubkey")
    except AgentBusError as exc:
        if getattr(exc, "status", None) != 404:
            raise
        _print(
            {"keys": [], "count": 0}
            if args.json
            else (
                f"{agent} has published no sealing key. Run `agentbus signin` on "
                f"each machine that should be able to read sealed mail."
            ),
            args.json,
        )
        return 0
    # #43: SIGNING KEYS TOO. This listed sealing keys only while `keys --help`
    # promised "every published key", and the omission was silent — a peer
    # audited three identities with it, concluded none had a signing key, and
    # was wrong about all three. A wrong NEGATIVE from a view that structurally
    # cannot produce a positive.
    signing = _published_signing_keys(bus, agent)

    if args.json:
        _print({**data, "signing_keys": signing, "this_machine": mine}, True)
        return 0
    keys = data.get("keys") or []
    print(f"{agent} — {len(keys)} sealing key(s), {len(signing)} signing key(s)")
    for entry in keys:
        here = "  <- THIS MACHINE" if entry["fingerprint"] == mine else ""
        print(f"  sealing  {entry['fingerprint']}  {entry.get('label') or '-'}{here}")
    for entry in signing:
        print(f"  signing  {entry['fingerprint']}  {entry.get('label') or '-'}")
    if not signing:
        # State the ABSENCE explicitly. "No line" is what misled the audit:
        # nothing distinguished "has none" from "was never asked".
        print(
            "  signing  (none published — peers get `unverifiable` rather than a\n"
            "           positive identity check on your mail. Fix: agentbus keys sign)"
        )
    if mine and not any(e["fingerprint"] == mine for e in keys):
        print(
            "\n  This machine holds a private key whose public half is NOT published,"
            "\n  so peers cannot seal to it and you will not be able to read new mail."
            "\n  Fix: agentbus keys rotate"
        )
    return 0


def _keys_rotate(bus: AgentBus, args: argparse.Namespace, agent: str, mine: str | None) -> int:
    """PUBLISH FIRST, revoke separately, and never in one step.

    Between the two the agent holds two valid keys and can read mail sealed to
    either. The reverse order leaves a window where senders have nothing to
    seal to, and on an encrypted workspace that is a refused send rather than a
    queued one.
    """

    path = sealing.key_path(agent)
    # ONE FILE PER RETIRED KEY, named by AGENT and fingerprint.
    #
    # A FIXED `.key.superseded` meant the SECOND rotation overwrote the first
    # retired key in place — silently, irreversibly, and worst for the operator
    # who rotates most often. Every message sealed to that first key became
    # unreadable by that agent forever, and `keys held: 2` after two rotations
    # looks exactly like `keys held: 2` after one. Measured on 0.5.4 from PyPI
    # by macbook-admin-bd8e86, who went looking specifically because rotation
    # was the feature that first made an N>1 case reachable.
    # The AGENT prefix is what keeps one agent's retired keys out of another's
    # hands: load_private_keys globs this agent's prefix only, so a rotation
    # never becomes a cross-agent disclosure.
    superseded = (
        path.parent / f"sealing-{sealing._agent_slug(agent)}-{mine}.key.superseded"
        if mine
        else None
    )
    if mine and not args.yes:
        print(
            f"This machine's current key is {mine}.\n"
            f"Rotating writes a NEW private key over {path}.\n\n"
            f"MAIL ALREADY SEALED TO THE OLD KEY stays sealed to it. The old\n"
            f"private key is kept at {superseded} and the client tries every key\n"
            f"it holds, so that mail stays readable — until you delete that file,\n"
            f"which nothing can undo. The platform cannot re-seal what it never held.\n"
            f"\nRe-run with --yes to proceed."
        )
        return 2
    if mine and superseded is not None:
        if superseded.exists():
            # Same fingerprint retired twice: same key, so this is a no-op
            # rather than a loss. Never overwrite a DIFFERENT key.
            path.unlink()
        else:
            superseded.write_text(path.read_text())
            _harden_if_possible(superseded)
            path.unlink()
    _private, public = sealing.ensure_keypair(agent)
    published = bus._request(
        "POST",
        f"/v1/agents/{agent}/pubkey",
        json={"public_key": public, "label": args.label or _sealing_hostname()},
    )
    new_fp = sealing.fingerprint(public)
    if args.json:
        _print({**published, "published": new_fp, "previous": mine, "revoked": False}, True)
        return 0
    print(f"published new key {new_fp}")
    if mine:
        print(f"  previous key {mine} is STILL VALID and still published")
        print(f"  its private half is at {superseded} — keep it to read older mail")
        print(f"  revoke it when you are sure: agentbus keys revoke {mine}")
    return 0


def _superseded_fingerprints() -> set[str]:
    """Fingerprints whose PRIVATE half is still on this machine (#191).

    Read from the filenames rotation writes (`sealing-<fp>.key.superseded`)
    rather than by deriving each public half, because the question being
    answered is "can this machine still open that mail", and the file existing
    is exactly that fact.

    An empty set on any error, deliberately: this only ever softens or hardens a
    warning, and a warning that crashes is worse than one that is cautious.
    """
    try:
        from .. import sealing

        directory = sealing.key_path().parent
        # TAKE THE LAST HYPHENATED SEGMENT, NOT EVERYTHING AFTER "sealing-".
        #
        # Rotation writes `sealing-<agent>-<fingerprint>.key.superseded`, and the
        # agent name itself contains hyphens. Stripping only the prefix left
        # "bikeroom-freebsd-operato-b124c2-e3da2fdd83562a70", which never equals
        # a bare fingerprint — so this set was ALWAYS empty in practice and
        # `known_locally` was always False.
        #
        # The consequence was a warning that stated the opposite of the truth on
        # the one path whose entire purpose is telling the caller what they are
        # about to lose: `keys revoke` said "its private half is NOT on this
        # machine ... every message sealed to it is ALREADY unreadable" while the
        # .superseded file sat in that very directory and the mail decoded fine.
        #
        # Reported by bikeroom-freebsd-operato-b124c2, who checked the claim with
        # `ls` and a real decode rather than believing the warning — and who
        # noted they would not trust the check in EITHER direction until it was
        # fixed. That was the right call: a false "present" would be worse still,
        # leaving someone to revoke believing old mail was safe.
        #
        # A fingerprint is hex, so the trailing segment is unambiguous even
        # though the agent name is not.
        found: set[str] = set()
        for path in directory.glob("sealing-*.key.superseded"):
            stem = path.name.removeprefix("sealing-").removesuffix(".key.superseded")
            found.add(stem.rsplit("-", 1)[-1])
        return found
    except Exception:
        return set()


def _local_signing_fingerprint(agent: str) -> str | None:
    """This agent's SIGNING fingerprint on this machine, or None.

    #220: separate from the sealing lookup because they are separate keypairs in
    separate files, and conflating them is what made `keys revoke` tell an
    operator their signing key's private half was not on a machine that was
    holding it.
    """

    private = sealing.load_signing_key(agent)
    if not private:
        return None
    try:
        return _signing.fingerprint(_signing.public_from_private(private))
    except Exception:
        return None


def _key_algorithm(bus: AgentBus, agent: str, fingerprint: str) -> str:
    """ "ed25519" | "age" | "unknown" — ASKED, never guessed.

    A fingerprint is an opaque hex digest: nothing in the string says which
    keypair it belongs to. The server knows, so the server is asked. "unknown"
    is a real answer — an already-revoked or never-registered fingerprint is in
    neither list — and it selects wording that claims nothing about either
    algorithm rather than picking one and being wrong half the time.
    """
    for algorithm, label in (("ed25519", "ed25519"), (None, "age")):
        try:
            params = {"algorithm": algorithm} if algorithm else None
            keys = bus._request("GET", f"/v1/agents/{agent}/pubkey", params=params, agent=agent)
        except AgentBusError:
            continue
        if any(k.get("fingerprint") == fingerprint for k in keys.get("keys") or []):
            return label
    return "unknown"


def _keys_revoke(bus: AgentBus, args: argparse.Namespace, agent: str, mine: str | None) -> int:
    """#191: THE WARNING COMES BEFORE THE ACT, FOR EVERY KEY.

    It used to come before only when you revoked THIS machine's key. For any
    other fingerprint — the common case, retiring a decommissioned laptop — the
    "anything already sealed to it stays sealed to it" line printed AFTER the
    revocation had happened. That is a notice, not a warning: by the time you
    read it the irreversible half is done.

    And it IS irreversible in the way that matters. Revoking is forward-only and
    re-publishing does not undo it, but the real loss is elsewhere: if the
    private half is gone from disk, every message ever sealed to that key is
    unreadable by this agent forever. Nothing can re-seal them, because nothing
    on the platform ever held the plaintext. That is the sentence a person needs
    BEFORE deciding, and it is the reason this now asks.

    Non-interactive callers are not blocked, they are required to say --yes,
    which is the same bar with the prompt removed.
    """
    superseded_here = _superseded_fingerprints()
    known_locally = args.fingerprint == mine or args.fingerprint in superseded_here

    if args.fingerprint == mine:
        headline = (
            f"{args.fingerprint} is THIS MACHINE'S CURRENT key. Revoking it stops peers\n"
            f"sealing to you, and on an encrypted workspace your incoming mail is then\n"
            f"REFUSED at send time rather than queued.\n"
            f"\n  Rotate instead — `agentbus keys rotate` publishes a new key first, so\n"
            f"  there is never a window where senders have nothing to seal to."
        )
    else:
        # #220: SAY WHAT IS TRUE OF *THIS* KEY'S ALGORITHM.
        #
        # This warning was written for sealing keys and printed verbatim for
        # signing keys, where all of it was wrong and one line was a lie:
        # "its private half is NOT on this machine" was shown while the signing
        # key sat in keys/signing-<agent>.key on that very machine, because the
        # locality check consulted the SEALING locations only. That is precisely
        # the fact an operator's decision turns on when revoking a key they
        # think may be compromised.
        algorithm = _key_algorithm(bus, agent, args.fingerprint)
        if algorithm == "ed25519":
            here = _local_signing_fingerprint(agent) == args.fingerprint
            held = (
                "  Its private half IS still on this machine, so anything you sign with\n"
                "  it from here will simply stop verifying for peers."
                if here
                else "  Its private half is not in this agent's signing key file on this\n"
                "  machine. It may still be held by another machine using this identity."
            )
            headline = (
                f"About to revoke SIGNING key {args.fingerprint} for {agent}.\n"
                f"\n  FORWARD ONLY, and it does NOT rewrite the past: peers stop being able\n"
                f"  to VERIFY signatures made with it. Messages already signed are not\n"
                f"  altered — they become `unverifiable` rather than invalid, so nothing\n"
                f"  starts reading as forged.\n"
                f"{held}\n"
                f"\n  Nothing is sealed to a signing key, so no message becomes unreadable.\n"
                f"  Publish a replacement with `agentbus keys sign` so you keep signing."
            )
        elif algorithm == "age":
            held = (
                "  Its private half IS still on this machine, so mail sealed to it stays\n"
                "  readable HERE — but only here, and only while that file survives."
                if known_locally
                else "  Its private half is NOT on this machine. If no machine still holds it,\n"
                "  every message sealed to it is ALREADY unreadable and revoking changes\n"
                "  nothing about that — it only stops future senders using it."
            )
            headline = (
                f"About to revoke SEALING key {args.fingerprint} for {agent}.\n"
                f"\n  FORWARD ONLY: this stops peers sealing NEW mail to it. Anything already\n"
                f"  sealed to it stays sealed to it, and re-publishing will not undo that.\n"
                f"{held}"
            )
        else:
            # In NEITHER published list. Claiming a consequence for an algorithm
            # we could not establish is how the original bug read to an
            # operator, so claim nothing.
            headline = (
                f"About to revoke {args.fingerprint} for {agent}.\n"
                f"\n  This fingerprint is not in {agent}'s published sealing or signing keys.\n"
                f"  It may already be revoked, or belong to another agent — in which case\n"
                f"  this call will change nothing. FORWARD ONLY either way: revoking never\n"
                f"  alters messages that already exist."
            )

    if not args.yes:
        print(headline)
        print("\n  Re-run with --yes to proceed.")
        return 2

    from urllib.parse import quote

    params = f"?reason={quote(args.reason)}" if getattr(args, "reason", None) else ""
    result = bus._request("DELETE", f"/v1/agents/{agent}/pubkey/{args.fingerprint}{params}")
    # ALREADY-REVOKED IS A SUCCESS, and saying so beats a second attempt or a
    # panic over what was in fact a no-op.
    if isinstance(result, dict) and result.get("already"):
        _print(
            result
            if args.json
            else (
                f"{args.fingerprint} was ALREADY revoked at {result.get('revoked_at')}.\n"
                f"  Nothing changed. This is success, not a failure to repeat."
            ),
            args.json,
        )
        return 0
    _print(
        result
        if args.json
        else (
            f"revoked {args.fingerprint}\n"
            f"  FORWARD ONLY: applies to messages sealed after now. Anything already\n"
            f"  sealed to it stays sealed to it, and re-publishing will not undo that.\n"
            f"  It stays listed as revoked on `GET /v1/workspace/pubkeys`, so the record\n"
            f"  of when it stopped being offered survives."
        ),
        args.json,
    )
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """See, rotate and revoke this agent's SEALING keys (#191).

    Every other surface for these keys is automatic — signin and setup publish
    one and never mention it again. That is right for the common case and left
    no answer for the one that matters: a laptop is decommissioned, its key
    stays valid and published, and every sender keeps wrapping ciphertext for a
    machine that no longer exists and possibly for whoever now owns the disk.
    """
    bus = _common._bus(args)
    agent = args.agent or bus.agent
    if not agent:
        print("no agent: pass --agent or set AGENTBUS_AGENT")
        return 2
    return {
        "list": _keys_list,
        "rotate": _keys_rotate,
        "revoke": _keys_revoke,
        "sign": _keys_sign,
    }[args.keys_action](bus, args, str(agent), _this_machines_fingerprint())


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser(
        "keys",
        help="see, rotate and revoke this agent's SEALING keys (encrypted workspaces)",
    )
    keys_sub = p.add_subparsers(dest="keys_action", required=True)
    kp = keys_sub.add_parser("list", help="every published key, marking this machine's")
    _accept_common_flags_after_subcommand(kp)
    kp = keys_sub.add_parser("rotate", help="new local key, published; the old one stays valid")
    kp.add_argument("--label", help="how this machine appears in the list (default: hostname)")
    kp.add_argument("--yes", action="store_true", help="proceed past the old-mail warning")
    _accept_common_flags_after_subcommand(kp)
    kp = keys_sub.add_parser(
        "sign", help="publish this machine's SIGNING key so peers can verify you (#173)"
    )
    kp.add_argument("--label", help="how this machine appears in the list (default: hostname)")
    _accept_common_flags_after_subcommand(kp)
    kp = keys_sub.add_parser("revoke", help="retire one key — forward only, never retroactive")
    kp.add_argument("fingerprint")
    # #191: --yes is now required for EVERY revocation, not only for this
    # machine's own key. The warning that mail already sealed to a key stays
    # sealed to it has to arrive before the irreversible half, and for any other
    # fingerprint it used to print afterwards.
    kp.add_argument(
        "--yes",
        action="store_true",
        help="proceed past the warning (required — the warning comes first)",
    )
    kp.add_argument(
        "--reason",
        help="why, recorded against the key: a rotation and a compromise want different follow-up",
    )
    _accept_common_flags_after_subcommand(kp)
    p.set_defaults(func=cmd_keys)
