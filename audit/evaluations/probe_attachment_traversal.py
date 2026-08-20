"""Falsifies: a sender-controlled attachment filename cannot write outside the
recipient's working directory (audit 0.9.40). Reproduced BEFORE the fix: a
`../outside/PWNED.txt` filename wrote to the sibling OUTSIDE dir.

After the fix, _safe_attachment_name reduces it to a basename in CWD."""
import os, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from agentbus_client import cli

def main():
    # Sanitizer direct
    cases = {
        "../outside/x.txt": "x.txt",
        "../../etc/passwd": "passwd",
        "normal.png": "normal.png",
        "..": "attachment-3",
    }
    for hostile, want in cases.items():
        got = cli._safe_attachment_name(hostile, 3)
        if got != want:
            print(f"FAIL: _safe_attachment_name({hostile!r}) = {got!r}, want {want!r}")
            return 1
    print("PASS: sanitizer neutralises all hostile filenames")

    # End-to-end write does not escape CWD
    with tempfile.TemporaryDirectory() as tmp:
        cwd = pathlib.Path(tmp) / "cwd"; cwd.mkdir()
        outside = pathlib.Path(tmp) / "outside"; outside.mkdir()
        sentinel = outside / "PWNED.txt"
        os.chdir(cwd)
        class Bus:
            def read(self, d): return {"attachments":[{"filename":"../outside/PWNED.txt","size":5}]}
            def attachment(self, d, i): return b"PWNED"
        import argparse
        args = argparse.Namespace(delivery_id="01D", index=0, output=None, force=False, all=False, agent=None, json=False)
        cli._common._bus = lambda _a: Bus()
        cli.cmd_attachment(args)
        if sentinel.exists():
            print("FAIL: attachment escaped the working directory"); return 1
        if not (cwd/"PWNED.txt").exists():
            print("FAIL: attachment not written to cwd"); return 1
        print("PASS: hostile filename wrote to cwd, not outside")
    print("ALL PASS")
    return 0

if __name__ == "__main__":
    sys.exit(main())
