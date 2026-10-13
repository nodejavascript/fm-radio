#!/usr/bin/env python3
"""Serve the site on loopback, and never from a cache.

    python3 tools/serve.py [port]

`Cache-Control: no-store` on every response is NOT a nicety — a browser that keeps a cached copy
makes a correct change look like no change at all, which is the one failure this site's own
deployment rule exists to prevent. Loopback only: this is a development server and there is no
reason for it to be reachable from anywhere else.
"""

from __future__ import annotations

import functools
import http.server
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "site")


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s\n" % (fmt % args))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8155
    handler = functools.partial(Handler, directory=ROOT)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print("site on http://127.0.0.1:%d/  (root: %s)" % (port, ROOT), flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
