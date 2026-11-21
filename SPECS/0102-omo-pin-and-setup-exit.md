# #102: setup omo: pin extension 166de8c; exit 0 when a differing install is left alone

Ticket: issuedb #102

EARS SPEC:
- The client shall pin the omo extension to rodmena-limited/agentbus-omo-plugin 166de8cb19e4cf414378962c7bb4e0521ada9a3f (index.ts sha256 557998d6009b5f3dfe2fee394f26fb55bb9d8a51686c756b4765c5bcec6d5790, package.json c0b1e3c49f0f152772d0f7e0cfdca2dcb9d96c83d3e9d7c41b2c3e8756695268). omo-test owns the extension repo (operator ruling relayed 2026-11-17), and the pin moves only when a client release names a new commit.
- If the extension directory holds files that differ from the pinned commit, then setup omo shall leave them unchanged, print the warning and how to install the pinned commit, and exit 0 when every other step succeeded.
- If a fetch fails or a fetched file's sha256 does not match, then setup omo shall write nothing and exit 1 (unchanged).
- Where an existing install matches the previous pin 181469c, setup omo shall treat it as differing and leave it, as for any other commit.

Reported by omo-test-147b40 (01M7KCBPWEJT0VFHKTGGFA6GVB): a real setup run left a locally edited extension alone and exited 1 although every other step succeeded. SPECS/0099 line 9 named no exit code for that case.

SYNTHESIS: localised change to the existing content-addressed pin (onboarding/_omo_extension.py constants) and the exit-code rule in onboarding/_omo_setup.py. No new component or dependency.

ALTERNATIVES: return True from install_extension for DIFFERS, with WARNING in the line [CHOSEN; one rule, and the warning still says what to do] vs a third tri-state return threaded through _setup_omo [REJECTED: the only caller collapses it back to 0/1].

Operator go-ahead: 2026-11-21 ("release and fix all known issues").

## Verification (2026-11-21)

- Digests checked independently, by gh api contents?ref=166de8c and by raw.githubusercontent.com: index.ts 557998d6...5790 and package.json c0b1e3c4...5268 matched on both paths. 166de8c is still the head of main.
- A real install from GitHub (no fetch stub) into a scratch directory: installed with sha256 checked. A second run said "already at 166de8c; left as is". After a local edit to index.ts it printed the WARNING line, returned success and left the file unchanged.
- tests/test_omo_setup.py: 15 pass. With the old `_omo_extension.py` restored, 7 fail, covering the pin, the digests, the previous-pin install left alone, and setup omo exiting 0 for a differing install. Setup still exits 1 when a fetch fails and when the fetched bytes do not match.
