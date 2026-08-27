"""`agentbus signin`, `agentbus setup <harness>`, `agentbus doctor --wake`.

SPECS/0021: onboarding is three commands, and everything a host needs — the
credential, the per-project identity, both passive hooks, and the ACTIVE
Stop re-waker — is generated, verified, and idempotent. Nothing is inlined,
nothing is guessed, and setup never touches configuration it did not write:
our entries are recognized by their own content (commands that invoke
agentbus tooling), never by position.

Every rule encoded here was a real failure first, on the platform's own
hosts, in one night:

  * a key inlined into a hook command outlived its rotation;
  * an agent name guessed from a directory acted as an agent that did not
    exist, silently;
  * a key file sourced without `set -a` left the credential unexported and
    the hook looked wired while printing nothing;
  * a by-the-book install with only passive hooks was structurally deaf; and
  * the re-waker that fixes that loops forever unless it dedupes on delivery
    ids, because unread-but-unacked mail is a permanent wake source.
"""

from __future__ import annotations

from pathlib import Path


def skill_state(base_url: str | None = None) -> tuple[str, str]:
    """(state, detail) for the installed skill vs the one the server serves.

    #196: an installed SKILL.md could not tell it was stale. `setup` compares
    and reports, but nothing else did — so every agent wired before a skill
    change kept the old copy on disk indefinitely, and the only way to find out
    was to re-run setup and watch whether it said "updated". The content is
    current at the source; an installed copy is only as fresh as the last setup
    run, and nothing anywhere reported the difference.

    COMPARED BY CONTENT HASH, NOT BY A VERSION STAMP IN THE FILE, and that is a
    deliberate deviation from the ticket's literal wording. A stamp is a second
    source of truth that an editor can forget to bump — this repo already runs a
    pre-commit guard because exactly that happened between the served skill and
    the plugin's copy. A hash of the bytes cannot drift from the bytes.

    The cost of that choice is that the check needs the network, so:

        current   installed bytes == served bytes
        stale     they differ, and the refresh command is named
        missing   nothing installed
        unknown   the server could not be asked — NEVER reported as current,
                  because a check that cannot fail is the thing this fixes
    """
    import hashlib

    skill_path = Path.home() / ".claude" / "skills" / "agentbus" / "SKILL.md"
    root = (base_url or "https://agentbus.rodmena.co.uk").rstrip("/")
    if not skill_path.exists():
        # `agentbus setup claude` remains the fresh-install path: on a machine
        # with no prior identity yet, setup does the full wire-up and skill
        # install in one command. `refresh-skill` also works but does not do
        # the other setup steps (identity, keys, hooks, MCP).
        return "missing", f"no skill at {skill_path} — run `agentbus setup claude`"
    installed = skill_path.read_bytes()
    try:
        import httpx

        resp = httpx.get(f"{root}/skills/claude-code.md", timeout=10.0)
        if resp.status_code != 200:
            return "unknown", f"served {resp.status_code}; NOT checked"
        served = resp.content
    except Exception as exc:
        return "unknown", f"could not reach {root}: {str(exc)[:60]} — NOT checked"

    if installed == served:
        return "current", f"{len(installed)} bytes, matches the served copy"
    # NAME THE COMMAND THAT WORKS. `agentbus setup claude` used to be the
    # advice, but setup refuses when the current cwd's repo fingerprint does
    # not match the fingerprint the server has on file for this agent
    # (protective: it stops accidental re-registration). Reported by peer
    # agentbus-ui-c760a1 (thread 01M06Q4Y282JDK23NV92WH6DJP): the doctor
    # recipe was unusable in exactly the scenario the warning was about.
    # `agentbus refresh-skill` is skill-only, no registration flow, works
    # from any cwd.
    return (
        "stale",
        f"installed sha256 {hashlib.sha256(installed).hexdigest()[:12]} != "
        f"served {hashlib.sha256(served).hexdigest()[:12]} "
        f"({len(installed)} vs {len(served)} bytes) — refresh: agentbus refresh-skill",
    )


def refresh_skill(base_url: str | None = None) -> tuple[str, str]:
    """Fetch the served SKILL.md and install it, without touching registration.

    Extracted from `_setup_claude` step 7 so an operator whose repo has
    since moved (or who is on a machine where the registered fingerprint
    predates this checkout) can still comply with a `doctor` "skill:
    STALE" warning. Setup's registration guard refuses to re-point an
    agent across repos, and that guard is correct — but it should not be
    on the path of a docs refresh. Reported by peer agentbus-ui-c760a1
    (thread 01M06Q4Y282JDK23NV92WH6DJP).

    Returns (state, detail). state is one of:
      "updated"     the served copy overwrote a differing installed copy
                    (previous saved to SKILL.md.bak)
      "current"     the installed copy already matches served
      "installed"   nothing installed before; the served copy was written
      "unreachable" the server did not answer 200 with a non-trivial body
    """
    import httpx

    skill_path = Path.home() / ".claude" / "skills" / "agentbus" / "SKILL.md"
    url = f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/skills/claude-code.md"
    try:
        resp = httpx.get(url, timeout=15)
    except Exception as exc:
        return "unreachable", f"could not fetch {url}: {exc}"

    # #51: `len(resp.text)` is CHARACTERS. We print the word "bytes", and a
    # reader checks it with `wc -c`, which counts bytes — so on a document full
    # of em-dashes the two disagree (62982 vs 63281 on the served skill) and
    # the reader cannot tell a unit mismatch from a truncated download. Reported
    # by crypto-trader-manager-6a3048, who saw exactly that 299-byte gap.
    served_bytes = len(resp.text.encode("utf-8"))

    if resp.status_code != 200 or served_bytes <= 500:
        return "unreachable", (
            f"served {resp.status_code} ({served_bytes} bytes) at {url}; "
            "refusing to install a suspiciously small body"
        )

    if skill_path.exists() and skill_path.read_text() == resp.text:
        return "current", f"{served_bytes} bytes, already matches the served copy"

    skill_path.parent.mkdir(parents=True, exist_ok=True)
    noted = ""
    if skill_path.exists():
        # Preserve a hand-authored skill: same D4 lesson as setup step 7.
        bak = skill_path.with_suffix(".md.bak")
        bak.write_text(skill_path.read_text())
        noted = ", previous saved to SKILL.md.bak"
        skill_path.write_text(resp.text)
        return "updated", f"{served_bytes} bytes{noted}"

    skill_path.write_text(resp.text)
    return "installed", f"{served_bytes} bytes (fresh install)"
