"""
THE LOCAL HOSTILE TARGET (milestone M0) — serving the stand-in legacy bank UI.

Deliberately boring: stdlib `http.server`, single process, no framework.

It matters that this is HTTP and not `file://`. The safety allowlist (M5) is a
statement about origins, and `file://` has none — an allowlist over file paths
would be theatre. A real `http://127.0.0.1:8000` origin is what makes "refuse to
act off-allowlist" a claim with teeth.

Two ways to run it:
    python -m app.server                  # the demo; serves on :8000
    with AppServer() as base_url: ...      # tests and checkpoint scripts
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import threading
from pathlib import Path

APP_DIR = Path(__file__).parent
DEFAULT_PORT = 8000


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Same as the stdlib handler, minus the per-request noise on stderr.

    Our own structured logger (M5) is the record that matters; this server's
    chatter would only pollute the evidence.
    """

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        pass


class AppServer:
    """The target app, startable in-process.

    `port=0` asks the OS for a free port, which is what tests want so they never
    collide with a demo server the user has running. The demo uses the fixed
    port, because the artifact's `target.entry_url` names it.
    """

    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
        self._host = host
        handler = functools.partial(_QuietHandler, directory=str(APP_DIR))
        # allow_reuse_address avoids "address already in use" on a quick restart.
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._httpd = socketserver.ThreadingTCPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def entry_url(self) -> str:
        """The URL an artifact's `target.entry_url` points at."""
        return f"{self.base_url}/members.html"

    def start(self) -> str:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> AppServer:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the CoreBankPro target app.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = AppServer(port=args.port)
    server.start()
    print(f"CoreBankPro target app serving at {server.entry_url}")
    print("  ?maintenance=1   interstitial before results (recoverable)")
    print("  ?slow=2000       delayed results (wait on the checkpoint, don't sleep)")
    print("Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
