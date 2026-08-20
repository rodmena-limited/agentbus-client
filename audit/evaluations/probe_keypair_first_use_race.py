#!/usr/bin/env python3
"""CLAIM (sealing.ensure_keypair): THIS AGENT's keypair, generated on first use. FALSIFIED
2026-08-20: exists()-then-write is not atomic; 8 concurrent first users each generated a key
and 7 of them hold a private key that no longer exists on disk. A public key they published or
sealed to is unreadable forever. Fix: O_CREAT|O_EXCL then re-read on EEXIST."""
import os, sys, tempfile, subprocess, time
from _common import SRC, verdict
cfg = tempfile.mkdtemp()
code = f'''
import sys, time; sys.path.insert(0, {SRC!r})
from agentbus_client import sealing
start = float(sys.argv[1])
while time.time() < start: pass
print(sealing.ensure_keypair("probe")[1])
'''
start = time.time() + 1.5
procs = [subprocess.Popen([sys.executable, "-c", code, str(start)], env={**os.environ, "AGENTBUS_CONFIG_DIR": cfg}, stdout=subprocess.PIPE, text=True) for _ in range(8)]
pubs = [p.communicate()[0].strip() for p in procs]
os.environ["AGENTBUS_CONFIG_DIR"] = cfg
from agentbus_client import sealing
on_disk = sealing.public_from_private(open(sealing.key_path("probe")).read().strip())
lost = [p for p in pubs if p != on_disk]
print(f"{len(set(pubs))} distinct keys returned to 8 concurrent first users; {len(lost)} hold a key not on disk")
raise SystemExit(verdict(not lost, "non-atomic first-use key creation overwrites a key another process already holds"))
