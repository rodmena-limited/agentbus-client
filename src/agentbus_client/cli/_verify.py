"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from . import _common
from ._common import _accept_common_flags_after_subcommand, _client_version, _print


def cmd_verify(args: argparse.Namespace) -> int:
    """#63: inspect a message's claim, and OPT-IN run it to record a verdict.

    THE SECURITY MODEL, ENFORCED HERE RATHER THAN DOCUMENTED:
      * the platform never runs the repro — only this client does, and only
        when the operator explicitly passes --run (never on receipt, never
        automatically);
      * the repro runs with the RECIPIENT's environment and WITHOUT this
        session's bus credentials, unless --with-creds is passed. A claim is
        code from another organisation; running it with your own credential is
        handing that code a token.
      * a claim with no verdicts is printed as NOT VERIFIED — never as fact.
    """
    import subprocess as _subprocess

    bus = _common._bus(args)
    delivery = bus.read(args.delivery_id)
    message_id = delivery.get("message_id")
    if not message_id:
        print(f"no message for delivery {args.delivery_id}", file=sys.stderr)
        return 1
    claim_info = bus.get_claim(message_id)
    claim = claim_info.get("claim")
    if claim is None:
        print(f"message {message_id} carries no claim to verify")
        return 1

    verdicts = claim_info.get("verdicts") or []
    print(f"CLAIM: {claim['assert_text']}")
    print(f"  claimed by: {claim['claimed_by']}  ({claim['created_at']})")
    if claim.get("context"):
        print(f"  context:    {claim['context']}")
    print(
        f"  repro:      {claim['repro']}"
        + (f"  [via {claim['interpreter']}]" if claim.get("interpreter") else "")
    )
    print(f"  expect:     {claim.get('expect')}")
    if claim_info.get("note"):
        # The explicit no-verdict state (EARS line 5): an empty verdict list
        # must be said, not implied.
        print(f"  status:     {claim_info['note']}")
    elif verdicts:
        print(f"  verdicts:   {len(verdicts)}")
        for v in verdicts:
            tag = "verified" if v["result"] == "verified" else v["result"]
            print(
                f"    {tag:<9} by {v['runner']}  [{v['attestation']}]"
                + (f"  exit {v['observed_exit']}" if v.get("observed_exit") is not None else "")
                + (f"  ({v['client_version']})" if v.get("client_version") else "")
            )
            if v.get("env_note"):
                print(f"      {v['env_note']}")

    if not getattr(args, "run", False):
        print()
        print("Not run. A claim is code from another agent; running it is your")
        print("decision, every time. Re-run with --run to execute the repro on")
        print("this host (without this session's bus credentials), then the")
        print("result is recorded as YOUR verdict.")
        return 0

    # THE REPRO RUNS HERE, ON THIS HOST, OPT-IN, WITHOUT BUS CREDENTIALS.
    #
    # The environment is scrubbed of the bus key BEFORE the subprocess starts:
    # a claim from another organisation must not inherit the credential that
    # would let it act as this agent. `--with-creds` is the explicit, deliberate
    # override for claims the operator has already read and decided to trust.
    env = dict(os.environ)
    if not getattr(args, "with_creds", False):
        for key in ("AGENTBUS_API_KEY", "AGENTBUS_AGENT"):
            env.pop(key, None)
    try:
        result = _subprocess.run(
            ["/bin/sh", "-c", claim["repro"]],
            capture_output=True,
            text=True,
            timeout=args.timeout,
            env=env,
            check=False,
        )
    except _subprocess.TimeoutExpired:
        print(f"repro timed out after {args.timeout}s", file=sys.stderr)
        bus.record_verdict(
            message_id,
            result="error",
            observed_exit=None,
            observed_output=f"timed out after {args.timeout}s",
            client_version=_client_version(),
            env_note="timeout",
        )
        return 1

    expected = (claim.get("expect") or {}).get("exit", 0)
    passed = result.returncode == int(expected)
    observed = (result.stdout or "")[-1500:]
    bus.record_verdict(
        message_id,
        result="verified" if passed else "refuted",
        observed_exit=result.returncode,
        observed_output=observed or None,
        client_version=_client_version(),
        env_note=f"expect exit {expected}",
    )
    print()
    print(f"repro exit:   {result.returncode}  (expected {expected})")
    print(f"verdict:      {'VERIFIED' if passed else 'REFUTED'}")
    if result.stdout.strip():
        print("--- repro stdout ---")
        print(result.stdout.strip()[-800:])
    if result.stderr.strip():
        print("--- repro stderr ---")
        print(result.stderr.strip()[-800:])
    print()
    print("Recorded as your verdict, attested to your key's binding.")
    return 0 if passed else 2


def _verify_exit_code(result: dict[str, Any]) -> int:
    """0 verified, 1 a real mismatch, 2 could not be checked.

    #229, found by agentbus-ui-c760a1 on 0.9.8: the `--json` branch computed its
    own code, `0 if verified else 1`, and returned BEFORE the verdict was
    consulted. So the identical delivery exited 2 as text and 1 as JSON, and an
    UNSIGNED message was reported to any script as a failed signature.

    That is the exact collapse #220 existed to prevent, surviving in the
    machine-readable path — the one thing that actually automates on the exit
    code. The human path was fixed and the scripted path was not.

    ONE MAPPING, USED BY BOTH BRANCHES, so they cannot drift again. Two copies
    of a rule is what put the bug here in the first place.
    """
    if result.get("verdict") in ("unverifiable", "unsigned"):
        return 2
    return 0 if result.get("verified") else 1


def cmd_verify_signature(args: argparse.Namespace) -> int:
    """`agentbus verify` — check a signature on THIS machine (#173).

    Deliberately not a flag on `show`. The whole value of the feature is that
    verification is something you DO rather than something you read, and a field
    in a payload you were handed is exactly the thing a recipient asked to stop
    having to trust.
    """
    result = _common._bus(args).verify(args.delivery_id)
    code = _verify_exit_code(result)
    if args.json:
        _print(result, True)
        return code
    if result.get("verified"):
        print(f"VERIFIED — signed by {result['signed_by']} (key {result['key_fingerprint']})")
        print("  checked on this machine against the key you fetched")
        # #231: SAY WHAT THE SIGNATURE ACTUALLY COVERED.
        #
        # agentbus-sig-v1 signs sender, recipients, subject, priority and the
        # BODY HASH. It does not cover html_body, attachments or the structured
        # payload. That is published in `signed_fields`, which nobody reads, and
        # a bare "VERIFIED" beside a message carrying an attachment invites
        # exactly the assumption the protocol does not support.
        #
        # This is the whole lesson of #220 pointed the other way: there, the
        # tool claimed a FAILURE it had not earned; here it claims COVERAGE it
        # has not earned. Both are a verifier saying more than it checked.
        print("  covers: sender, recipients, subject, priority, body")
        print("  NOT covered: html, attachments, payload (agentbus-sig-v1)")
        if result.get("platform_said") != "valid":
            # A DISAGREEMENT IS THE INTERESTING CASE and must never be averaged
            # away: it means the platform and you hold different keys, or one of
            # you is wrong about which bytes were signed.
            print(f"  NOTE: the platform said '{result.get('platform_said')}' — investigate")
        return 0
    # #220: "I COULD NOT CHECK" IS NOT "THIS IS FORGED", and printing both as
    # NOT VERIFIED is how this tool spent an evening telling three agents their
    # own honestly-signed mail did not verify. A negative from a security tool
    # gets acted on; it has to be earned.
    if code == 2:
        # F14 (issuedb #8): the reason string for the `unsigned` verdict is
        # literally "unsigned", so joining `headline` and `reason` across an
        # em-dash used to print `UNSIGNED — unsigned`, which reads as a display
        # glitch and, worse, invites the operator to see UNSIGNED as failure.
        # Lead with reassurance so the eye lands on the benign fact first.
        if result.get("verdict") == "unsigned":
            print("UNSIGNED — no signature attached to verify")
        else:
            print(f"CANNOT VERIFY — {result.get('reason')}")
        if result.get("platform_said"):
            print(f"  the platform said: {result.get('platform_said')}")
        print("  this is NOT a failed signature — nothing here says the sender is wrong")
        return code
    print(f"NOT VERIFIED — {result.get('reason')}")
    print(f"  the platform said: {result.get('platform_said')}")
    print("  the bytes do not match the key: treat this as a real mismatch")
    return code


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser(
        "verify",
        help="inspect a claim; with --run, execute it opt-in and record your verdict (#63)",
    )
    p.add_argument("delivery_id")
    p.add_argument(
        "--run",
        action="store_true",
        help="execute the repro on this host (never automatic; "
        "scrubbed of this session's bus credentials)",
    )
    p.add_argument(
        "--with-creds",
        action="store_true",
        help="explicit override: let the repro inherit the bus "
        "credential (read the claim fully before this)",
    )
    p.add_argument(
        "--timeout", type=float, default=60.0, help="repro timeout in seconds (default 60)"
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_verify)

    # `verify-sender`, NOT `verify`: that verb already means "inspect a #63
    # claim, and with --run execute it". Two different questions — "is this
    # assertion true" and "did this agent really send this" — and a name that
    # answered whichever you happened to mean would be worse than a longer one.
    p = sub.add_parser(
        "verify-sender",
        help="check a message's signature yourself, without trusting the bus (#173)",
    )
    p.add_argument("delivery_id")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_verify_signature)
