# #102: setup omo: pin extension 1d9ffcd; exit 0 when a differing install is left alone

Ticket: issuedb #102 (open)

EARS SPEC (draft):
- The client shall pin the omo extension to rodmena-limited/agentbus-omo-plugin 1d9ffcd5f8f7dc522affd08ea27dc5fb40de3d67 (index.ts sha256 75ba7735d82fab61dd22f8cec307127f25a2d4b0d8c0f22e1713fec851c8705f, package.json c0b1e3c49f0f152772d0f7e0cfdca2dcb9d96c83d3e9d7c41b2c3e8756695268), or a later commit omo-test names that closes the stale-takeover race.
- If the extension directory holds files that differ from the pinned commit, then setup omo shall leave them unchanged, print the warning and how to install the pinned commit, and exit 0 when every other step succeeded.
- If a fetch fails or a fetched file's sha256 does not match, then setup omo shall write nothing and exit 1 (unchanged).
- Where an existing install matches the previous pin 181469c, setup omo shall treat it as differing and leave it, as for any other commit.

Reported by omo-test-147b40 (01M7KCBPWEJT0VFHKTGGFA6GVB): a real setup run left a locally edited extension alone and exited 1 although every other step succeeded. SPECS/0099 line 9 named no exit code for that case.

SYNTHESIS: localised change to the existing content-addressed pin (onboarding/_omo_extension.py constants) and the exit-code rule in onboarding/_omo_setup.py. No new component or dependency.

BLOCKED ON: operator go-ahead for release 0.9.109.
