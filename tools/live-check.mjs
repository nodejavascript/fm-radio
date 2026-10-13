/* The live check — the third kind of test, and the only one that looks at the DEPLOYED site.
 *
 *     npm run test:live
 *
 * The unit suite proves the page's own rules hold on the page as served locally; the end-to-end
 * suite proves the cookie gate in a real browser. Neither can see the things that only exist once
 * a site is deployed: the certificate, the edge, the cache headers Cloudflare decides, the
 * owner-exclusion rule at the origin, and whether the icons and the sitemap a crawler asks for are
 * really there. Those are what this checks, against the address a stranger would type.
 *
 * Exit 0 = every check passed. Exit 1 = at least one did not, and it says which.
 */
const HOST = "fm-radio.nodejavascript.com";
const BASE = `https://${HOST}`;

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${ok || !detail ? "" : " — " + detail}`);
  if (!ok) failures++;
};

async function head(path) {
  const res = await fetch(BASE + path, { redirect: "follow" });
  return { status: res.status, headers: res.headers, res };
}

console.log(`live check — ${BASE}\n`);

// 1 · the page itself, and the title rule: the <title> IS the full domain name
{
  const { status, res } = await head("/");
  const html = await res.text();
  check("the page answers 200", status === 200, `HTTP ${status}`);
  const title = (html.match(/<title>([^<]*)<\/title>/) || [, ""])[1];
  check("the title is the full domain name", title === HOST, `title is "${title}"`);
  check("exactly one <h1>", (html.match(/<h1[\s>]/g) || []).length === 1);
  check("no analytics tag in the document", !/googletagmanager/.test(html));
  check("no privacy.html is linked", !/privacy\.html/.test(html));
  check("no www hostname anywhere", !/:\/\/www\./.test(html));
  check("the footer carries the cookie door", html.includes('id="consentBtn"'));
  check("the copyright line carries the full domain", html.includes(`© 2026 ${HOST}`));
}

// 2 · part 7: a deploy must be visible, so the shell is never stored anywhere
{
  const { headers } = await head("/");
  const cc = (headers.get("cache-control") || "").toLowerCase();
  check("the shell is no-store", cc.includes("no-store"), cc || "(no header)");
}

// 3 · the icons, the manifest and the crawler files a deployed site must actually serve
for (const [path, type] of [
  ["/img/favicon-180.png", "image/png"],
  ["/img/favicon.svg", "image/svg"],
  ["/img/favicon.ico", "image/"],
  ["/img/apple-touch-icon.png", "image/png"],
  ["/img/app.webp", "image/webp"],
  ["/manifest.webmanifest", "json"],
  ["/robots.txt", "text/plain"],
  ["/sitemap.xml", "xml"],
]) {
  const { status, headers } = await head(path);
  const ct = headers.get("content-type") || "";
  check(`${path} answers 200 as ${type}`, status === 200 && ct.includes(type), `HTTP ${status} ${ct}`);
}

// 4 · the sitemap and robots agree with the host they are served from
{
  const { res } = await head("/sitemap.xml");
  const xml = await res.text();
  check("the sitemap names this host", xml.includes(`https://${HOST}/`));
  const { res: rr } = await head("/robots.txt");
  const robots = await rr.text();
  check("robots.txt carries the Sitemap line", /Sitemap:\s*https:\/\/fm-radio\.nodejavascript\.com\/sitemap\.xml/.test(robots));
}

// 5 · the owner-exclusion rule at the origin: his own network is served a STUB for /consent.js,
//     so his visits are never measured. Anyone else gets the real loader — which is why the check
//     accepts either, but insists on one of them and on the file never being cached.
{
  const { status, headers, res } = await head("/consent.js");
  const body = await res.text();
  const cc = (headers.get("cache-control") || "").toLowerCase();
  check(
    "/consent.js is the loader or the owner's stub",
    status === 200 && (body.includes("googletagmanager") || body.includes("analytics suppressed")),
    `HTTP ${status}, ${body.length} bytes`
  );
  check("/consent.js is never cached at the edge", cc.includes("no-store"), cc || "(no header)");
}

// 6 · every page the site links to internally resolves — a 404 behind a link is the quietest fault
{
  const { res } = await head("/");
  const html = await res.text();
  const anchors = [...html.matchAll(/href="(\/[^"#?]*)"/g)].map((m) => m[1]);
  const unique = [...new Set(anchors)].filter((p) => !p.startsWith("//"));
  let bad = [];
  for (const p of unique) {
    const r = await fetch(BASE + p, { redirect: "follow" });
    if (r.status !== 200) bad.push(`${p} → ${r.status}`);
  }
  check(`every internal link resolves (${unique.length} checked)`, bad.length === 0, bad.join(", "));
}

console.log(failures ? `\n  ${failures} FAILED` : "\n  LIVE OK — the deployed site is what it says it is");
process.exit(failures ? 1 : 0);
