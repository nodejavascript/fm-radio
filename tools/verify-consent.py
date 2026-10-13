#!/usr/bin/env python3
"""Prove the cookie gate in a real browser: a visitor who has not said yes makes NO request to
Google FROM THIS PAGE. Run it with the site already being served (tools/serve.py).

    python3 tools/verify-consent.py                 # check the site on 127.0.0.1:8155
    python3 tools/verify-consent.py --selftest      # prove this check can actually fail

HOW IT PROVES IT, AND WHY IT IS NOT JUST A grep. A counting proxy stands in front of Chrome, so
every request is recorded by this program rather than by the page's own report of itself. Chrome
bypasses a proxy for loopback, so the page load never reaches the proxy — the only traffic it can
see is traffic leaving the machine, which is the thing being counted.

🔴 AND THE FIRST VERSION OF THIS CHECK WAS WRONG, IN THE MOST EXPENSIVE DIRECTION. It failed the
site because the proxy had recorded `www.google.com` and `accounts.google.com` — traffic that is
**Chrome's own background networking** (variation seeds, component updates, phishing updates), not
the page's. A false failure teaches the reader to ignore the instrument, so attribution is now
explicit: a request counts against the PAGE only when it goes to the analytics host, or when a
request to a google.com host carries a Referer naming this page. Anything else is reported as
Chrome's own chatter and does not fail the run — and Chrome is started with background networking
off, so there is less of it to explain away.
"""

from __future__ import annotations

import http.server
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

PAGE_TAG_HOST = "googletagmanager.com"
# Hosts Chrome itself talks to — variation seeds, component and update checks, phishing updates.
# A request to one of these with NO Referer naming the page is Chrome, not the page: it was
# measured doing exactly this on a blank profile before the page had even finished loading, and an
# earlier version of this check failed the site over it. A request that DOES carry the page's
# Referer is the page's, whichever host it goes to.
BROWSER_OWN = ("google.com", "gstatic.com", "googleapis.com", "gvt1.com", "gvt2.com", "ggpht.com")
REQUESTS: list[dict] = []
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")

QUIET = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-client-side-phishing-detection",
    "--disable-domain-reliability",
    "--disable-breakpad",
    "--no-pings",
    "--no-service-autorun",
    "--metrics-recording-only",
    "--disable-features=OptimizationHints,Translate,MediaRouter",
]


class Proxy(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline().decode("latin-1", "replace").strip()
        if not line:
            return
        parts = line.split()
        target = parts[1] if len(parts) >= 2 else ""
        referer = ""
        while True:
            h = self.rfile.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
            k, _, v = h.decode("latin-1", "replace").partition(":")
            if k.strip().lower() == "referer":
                referer = v.strip()
        REQUESTS.append({"target": target, "referer": referer, "connect": line.upper().startswith("CONNECT")})
        try:
            if line.upper().startswith("CONNECT"):
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Type: text/plain\r\n\r\n")
        except OSError:
            pass


class ProxyServer(socketserver.ThreadingTCPServer):
    # a second run must not collide with the first: bind a free port, and let a socket in
    # TIME_WAIT be reused rather than refusing to start (which failed the first run of this file)
    allow_reuse_address = True
    daemon_threads = True


def host_of(req: dict) -> str:
    t = req["target"]
    if req["connect"]:
        return t.split(":")[0].lower()
    return t.split("://", 1)[-1].split("/")[0].split(":")[0].lower()


def probe(url: str) -> tuple[str, list[dict]]:
    """Load the page in headless Chrome behind the proxy; return (dom, the requests it made)."""
    REQUESTS.clear()
    server = ProxyServer(("127.0.0.1", 0), Proxy)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="fmradio-verify-")
    try:
        proc = subprocess.run(
            [CHROME, "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-gpu"]
            + QUIET
            + [
                "--proxy-server=127.0.0.1:%d" % port,
                "--user-data-dir=" + profile,
                "--virtual-time-budget=5000",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.stdout, list(REQUESTS)
    finally:
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


def judge(dom: str, seen: list[dict], page_host: str):
    """Split the traffic into 'this page did it' and 'Chrome did it itself'."""
    from_page, chrome_own = [], []
    for r in seen:
        h = host_of(r)
        if not h or h in ("127.0.0.1", "localhost"):
            continue
        if PAGE_TAG_HOST in h:
            from_page.append(r["target"])
        elif page_host in r["referer"]:
            from_page.append("%s (referred by %s)" % (r["target"], r["referer"]))
        elif any(h == x or h.endswith("." + x) for x in BROWSER_OWN):
            chrome_own.append(r["target"])
        else:
            from_page.append(r["target"])  # any other third party is unaccounted for: count it as ours
    asked = "May this page count the visit?" in dom
    loaded = bool(re.search(r"googletagmanager\.com", dom, re.I))
    return asked and not loaded and not from_page, from_page, chrome_own


def main() -> int:
    if not CHROME:
        print("no chrome found — cannot check the gate in a browser")
        return 2

    selftest = "--selftest" in sys.argv
    srv = None

    if selftest:
        # a page that DELIBERATELY loads the tag: the check must catch it, or it proves nothing
        root = tempfile.mkdtemp(prefix="fmradio-selftest-")
        with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(
                "<!doctype html><html><head><title>selftest</title>"
                '<script async src="https://www.googletagmanager.com/gtag/js?id=G-SELFTEST"></script>'
                "</head><body>May this page count the visit?</body></html>"
            )
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=root, **k)  # noqa: E731
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8156), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url, host = "http://127.0.0.1:8156/", "127.0.0.1:8156"
    else:
        url, host = "http://127.0.0.1:8155/", "127.0.0.1:8155"

    try:
        dom, seen = probe(url)
        ok, from_page, chrome_own = judge(dom, seen, host)

        if selftest:
            caught = (not ok) and any(PAGE_TAG_HOST in r for r in from_page)
            print("  SELFTEST — a page that really does load the tag")
            print("  requests attributed to the page :", from_page or "none")
            print("  the check caught it             :", "yes" if caught else "NO")
            print("\n  SELFTEST OK — the check can fail" if caught else "\n  SELFTEST FAILED — the check cannot fail")
            return 0 if caught else 1

        print("  the gate ran and asked          :", "yes" if "May this page count the visit?" in dom else "NO")
        print(
            "  a Google tag in the served DOM  :",
            "YES — it must not be" if re.search(r"googletagmanager", dom, re.I) else "no",
        )
        print("  requests ATTRIBUTED to the page :", from_page or "none")
        print("  Chrome's own background traffic :", ", ".join(sorted(set(chrome_own))) or "none")
        print("\n  VERIFY OK — nothing goes to Google before a yes" if ok else "\n  VERIFY FAILED")
        return 0 if ok else 1
    finally:
        if srv is not None:
            srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
