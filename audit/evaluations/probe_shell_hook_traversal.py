"""Falsify: a hostile AGENTBUS_AGENT cannot source the OPERATOR key via the
harness hook shell snippets (REG-8d shell sibling, audit 0.9.40).

Reproduced BEFORE the fix: AGENTBUS_AGENT=../operator made _SESSION_START_CMD
source $HOME/.config/agentbus/operator.env on every session start. The
[!a-zA-Z0-9._-] guard rejects it.

Drives the actual emitted shell snippets with a hostile agent name and asserts
the operator key is NOT sourced. Requires sh; safe (no network, no blast).
"""
import os, subprocess, tempfile, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from agentbus_client import onboarding

def main():
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        cfg = home / ".config" / "agentbus"
        (cfg / "keys").mkdir(parents=True)
        (cfg / "operator.env").write_text("export AGENTBUS_API_KEY=ab_sk_OPERATOR_SHOULD_NOT_LEAK\n")
        (cfg / "keys" / "legit.env").write_text("export AGENTBUS_API_KEY=ab_sk_LEGIT_OK\n")
        env = {"HOME": str(home), "AGENTBUS_AGENT": "../operator", "PATH": os.environ.get("PATH","")}
        for name, snippet in [("SESSION_START", onboarding._SESSION_START_CMD),
                              ("STOP_REWAKE", onboarding.STOP_REWAKE_SH)]:
            s = snippet.replace("exec agentbus-hook monitor", "exit 0")
            p = subprocess.run(["sh","-c",s], capture_output=True, text=True, env=env, timeout=10)
            if "OPERATOR_SHOULD_NOT_LEAK" in p.stdout or "OPERATOR_SHOULD_NOT_LEAK" in p.stderr:
                print(f"FAIL: {name} sourced the operator key via traversal")
                return 1
            print(f"PASS: {name} blocked the ../operator traversal")
    # Known-positive: legit agent still sources its own key
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        cfg = home / ".config" / "agentbus"
        (cfg / "keys").mkdir(parents=True)
        (cfg / "keys" / "legit.env").write_text("export AGENTBUS_API_KEY=ab_sk_LEGIT_OK\n")
        env = {"HOME": str(home), "AGENTBUS_AGENT": "legit", "PATH": os.environ.get("PATH","")}
        s = onboarding._SESSION_START_CMD + "; echo KEY=${AGENTBUS_API_KEY:-NONE}"
        p = subprocess.run(["sh","-c",s], capture_output=True, text=True, env=env, timeout=10)
        if "KEY=ab_sk_LEGIT_OK" not in p.stdout:
            print(f"FAIL: legit agent key not sourced (known-positive)"); return 1
        print("PASS: legit agent still sources its own key")
    print("ALL PASS")
    return 0

if __name__ == "__main__":
    sys.exit(main())
