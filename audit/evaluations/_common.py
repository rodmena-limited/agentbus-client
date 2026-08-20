"""Shared plumbing for the local stability probes (review #23, 2026-08-20).

Every probe here is SAFE: it talks only to fake servers on 127.0.0.1 that it
starts itself, and only signals processes it spawned. No probe touches the
production bus. Parameterise the client under test with AGENTBUS_CLIENT_SRC
(default: this checkout's src/)."""
import os, sys, socket, threading, time
SRC = os.environ.get("AGENTBUS_CLIENT_SRC") or os.path.join(os.path.dirname(__file__), "..", "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

def stalled_server():
    """Accepts TCP connections and NEVER answers (a blackholed/NAT-dropped bus). Keeps sockets alive."""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(128)
    held = []
    def acc():
        while True:
            c, _ = srv.accept(); held.append(c)
    threading.Thread(target=acc, daemon=True).start()
    return srv.getsockname()[1], held

def json_server(body: bytes):
    """Answers every request instantly with 200 + `body`. Returns (port, served_timestamps)."""
    import json
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(128)
    served = []
    def serve():
        while True:
            c, _ = srv.accept()
            def one(c=c):
                try:
                    c.recv(65536)
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
                    served.append(time.time())
                finally:
                    c.close()
            threading.Thread(target=one, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], served

def verdict(ok: bool, fail_text: str) -> int:
    print("PASS" if ok else f"FAIL: {fail_text}")
    return 0 if ok else 1
