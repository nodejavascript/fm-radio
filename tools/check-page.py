#!/usr/bin/env python3
"""Check the site's own page against the family's on-page rules, locally, with the site running.

    python3 tools/serve.py 8155 &
    python3 tools/check-page.py [url]

This is the local half of the family's test set: it reads the page as it is SERVED rather than as it
is written, so a head that loses the 180-pixel icon, a canonical that drifts off the host, a second
<h1> or a script pulled from someone else's server all fail here rather than after a deploy. The
live half (`~/.seo_audit.py` and `~/.nodejs_compliance.py --site`) needs a deployed host and runs on
deploy day.

Exit 0 = every check passed. Exit 1 = at least one did not, and it says which.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8155/"
HOST = "fm-radio.nodejavascript.com"

# third-party hosts a LINK may point at; a SUBRESOURCE (script/style/img) from any of these is a fault
OK_LINK_HOSTS = ("nodejavascript.com", "github.com", "openschema.org", "opensource.org", "schema.org")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main() -> int:
    status, html = get(URL)
    check("the page answers 200", status == 200, "HTTP %s" % status)

    title = (re.search(r"<title>(.*?)</title>", html, re.S) or [None, ""])[1].strip()
    check("the title IS the full domain name", title == HOST, "title is %r" % title)

    desc = (re.search(r'<meta\s+name="description"\s+content="(.*?)"', html, re.S) or [None, ""])[1]
    check("one meta description, 120-160 characters", 120 <= len(desc) <= 160, "%d chars" % len(desc))

    canon = (re.search(r'<link rel="canonical" href="(.*?)"', html) or [None, ""])[1]
    check("a self-referencing canonical", canon == "https://%s/" % HOST, canon)

    h1s = re.findall(r"<h1[^>]*>", html)
    check("exactly one <h1>", len(h1s) == 1, "%d found" % len(h1s))

    order = re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"', html)
    check(
        "the 180-pixel PNG is the FIRST icon declared (SVG is not a format Google reads)",
        bool(order) and "favicon-180.png" in order[0],
        "first is %s" % (order[0] if order else "none"),
    )
    for size in ("favicon-180.png", "favicon.svg", "favicon-32.png", "favicon.ico", "apple-touch-icon.png"):
        check("the icon set declares %s" % size, any(size in h for h in order))

    check("no analytics tag in the page (the gate creates it)", "googletagmanager" not in html)
    check("no privacy.html anywhere", "privacy.html" not in html)
    check("no www hostname", not re.search(r"://www\.", html))
    check("a theme colour is declared", 'name="theme-color"' in html)

    theme = (re.search(r'name="theme-color" content="([^"]+)"', html) or [None, ""])[1]
    taken = {"#940f04", "#0c1626", "#08050d", "#0a0f1f", "#1d4ed8", "#34d399", "#f472b6", "#a3e635", "#23523c", "#6d28d9", "#0b0714", "#03090b", "#38bdf8"}
    check("the theme colour is this site's own", theme.lower() not in taken, theme)

    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
            kinds = [n.get("@type") for n in data.get("@graph", [])] or [data.get("@type")]
            check("the JSON-LD parses (%s)" % ", ".join(str(k) for k in kinds), True)
        except Exception as exc:  # noqa: BLE001
            check("the JSON-LD parses", False, str(exc))

    # a subresource is something the page LOADS: a script, an image, or a stylesheet/icon/manifest.
    # 🔴 A RELATION LINK IS NOT A SUBRESOURCE — the canonical and the og:url are <link> elements
    # that fetch NOTHING, and the first version of this check failed the page over its own
    # canonical. A false failure teaches the reader to ignore the gate, so the rel is read.
    subs = [
        u
        for u in re.findall(r'<(?:script|img|iframe)[^>]*\ssrc="(https?://[^"]+)"', html)
        + [
            m
            for m in re.findall(r'<link[^>]*rel="(?:stylesheet|preload|icon|apple-touch-icon|manifest)"[^>]*href="(https?://[^"]+)"', html)
        ]
    ]
    check("no subresource comes from a third party", not subs, ", ".join(subs))

    for need in ("site-footer", "Cookie settings", "© 2026 %s" % HOST, 'id="privacy"', "gh-star"):
        check("the page carries %s" % need, need in html)

    for path, needle in (("/robots.txt", "Sitemap:"), ("/sitemap.xml", HOST)):
        try:
            st, body = get(URL.rstrip("/") + path)
            check("%s answers 200 and carries %r" % (path, needle), st == 200 and needle in body)
        except urllib.error.HTTPError as exc:
            check("%s answers 200" % path, False, "HTTP %s" % exc.code)

    print()
    if failures:
        print("  %d check(s) FAILED: %s" % (len(failures), "; ".join(failures)))
        return 1
    print("  CHECK OK — every on-page rule holds on the served page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
