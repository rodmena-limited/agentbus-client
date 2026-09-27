"""OSV sweep of every package pinned in uv.lock, with a known-vulnerable control.

A sweep that reports zero findings is only evidence if it can report a finding.
The control is a pin OSV is known to flag; if the control comes back clean, the
sweep is broken (network, API shape, ecosystem name) and the run fails rather
than reporting a false all-clear.

Every lock entry is checked, including the per-Python duplicates uv keeps for
older interpreters — a vulnerable pin that only resolves on Python 3.9 is still
a pin this project ships in its lock.

    .venv/bin/python audit/evaluations/probe_osv_lockfile.py

Exit 0 when the control is flagged and no lock entry has a known vulnerability.
"""

from __future__ import annotations

import json
import pathlib
import urllib.request

import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
OSV_BATCH = "https://api.osv.dev/v1/querybatch"
CONTROL = ("jinja2", "2.10")


def _query(pins: list[tuple[str, str]]) -> list[list[str]]:
    body = {
        "queries": [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in pins]
    }
    req = urllib.request.Request(
        OSV_BATCH, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        results = json.load(resp)["results"]
    if len(results) != len(pins):
        raise RuntimeError(f"OSV returned {len(results)} results for {len(pins)} queries")
    return [[v["id"] for v in r.get("vulns") or []] for r in results]


def main() -> int:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    pins = sorted(
        {
            (p["name"], p["version"])
            for p in lock["package"]
            if "version" in p and p.get("source", {}).get("editable") is None
        }
    )
    found = _query([CONTROL, *pins])
    control_hits, lock_hits = found[0], found[1:]

    print(f"control {CONTROL[0]} {CONTROL[1]}: {len(control_hits)} known vulnerabilities")
    if not control_hits:
        print(
            "SWEEP VOID: the known-vulnerable control came back clean, so a clean result below means nothing"
        )
        return 2

    dirty = [(n, v, ids) for (n, v), ids in zip(pins, lock_hits) if ids]
    print(f"lock entries checked: {len(pins)}")
    for n, v, ids in dirty:
        print(f"  VULNERABLE  {n} {v}: {', '.join(ids)}")
    print(f"vulnerable lock entries: {len(dirty)}")
    return 1 if dirty else 0


if __name__ == "__main__":
    raise SystemExit(main())
