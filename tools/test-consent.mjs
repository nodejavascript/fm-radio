/* Test the REAL consent.js — not a copy of it — in a stub DOM, for the path a browser check
 * cannot reach without a click: a visitor who has answered YES.
 *
 *     node tools/test-consent.mjs
 *
 * The browser check (tools/verify-consent.py) proves the refusal: no request to Google before a
 * yes. This proves the other half — that a yes does load the tag, with the id from the script's
 * own data attribute — and that a refusal sets the disable flag. Both halves must hold, or the
 * gate is only half a gate.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(here, "..", "site", "consent.js");
const code = fs.readFileSync(SRC, "utf8");

let failures = 0;
const check = (name, ok) => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}`);
  if (!ok) failures++;
};

function run(choice, query) {
  const created = [];
  const store = new Map(choice ? [["fmradio.consent", choice]] : []);

  const el = (tag) => {
    const node = {
      tagName: tag,
      attrs: {},
      children: [],
      style: { setProperty() {} },
      setAttribute(k, v) {
        node.attrs[k] = String(v);
      },
      getAttribute(k) {
        return k in node.attrs ? node.attrs[k] : null;
      },
      hasAttribute(k) {
        return k in node.attrs;
      },
      appendChild(c) {
        node.children.push(c);
        created.push(c);
      },
      remove() {},
      querySelector() {
        return null;
      },
      closest() {
        return null;
      },
      addEventListener() {},
    };
    return node;
  };

  const script = {
    getAttribute: (k) => (k === "data-ga-id" ? "G-TESTID123" : null),
  };

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    CustomEvent: class CustomEvent {
      constructor(type, opts) {
        this.type = type;
        this.detail = opts && opts.detail;
      }
    },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, v),
      removeItem: (k) => store.delete(k),
    },
    document: {
      currentScript: script,
      readyState: "complete",
      createElement: el,
      head: { appendChild: (c) => created.push(c) },
      body: { appendChild: (c) => created.push(c) },
      documentElement: { style: { setProperty() {} } },
      querySelector: () => null,
      addEventListener() {},
      dispatchEvent() {},
    },
    window: {
      addEventListener() {},
      dispatchEvent() {},
      location: { search: query || "" },
      dataLayer: undefined,
    },
  };
  sandbox.window.window = sandbox.window;
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: "consent.js" });

  const tags = created.filter((c) => c && typeof c.src === "string" && c.src.includes("googletagmanager.com"));
  return { sandbox, tags, store };
}

console.log("consent.js — the real file, in a stub DOM\n");

// 1 · a visitor who has not answered: nothing is loaded
{
  const { tags, sandbox } = run(null);
  check("no choice stored  →  no analytics tag is created", tags.length === 0);
  check("no choice stored  →  no gtag function is defined", sandbox.window.gtag === undefined);
}

// 2 · a visitor who said yes: the tag is created, carrying the id from the data attribute
{
  const { tags, sandbox } = run("granted");
  check("granted            →  exactly one analytics tag", tags.length === 1);
  check(
    "granted            →  the tag carries the script's own id",
    tags.length === 1 && tags[0].src.includes("id=G-TESTID123")
  );
  check("granted            →  gtag is defined and the tag is configured", typeof sandbox.window.gtag === "function");
}

// 3 · a visitor who refused: the tag stays away AND the disable flag is set, so any tag that ever
//     appears from elsewhere is inert rather than merely unused
{
  const { tags, sandbox } = run("denied");
  check("denied             →  no analytics tag is created", tags.length === 0);
  check("denied             →  ga-disable-<id> is set true", sandbox.window["ga-disable-G-TESTID123"] === true);
}

// 4 · the owner's own switch, which is not in the panel at all
{
  const { tags, sandbox } = run("granted", "?ga=off");
  check("?ga=off            →  overrides a stored yes, nothing is loaded", tags.length === 0);
  check("?ga=off            →  ga-disable-<id> is set true", sandbox.window["ga-disable-G-TESTID123"] === true);
}
{
  const { tags } = run(null, "?ga=on");
  check("?ga=on             →  loads the tag and asks nothing", tags.length === 1);
}

console.log(failures ? `\n  ${failures} FAILED` : "\n  TEST OK — the gate holds at both ends");
process.exit(failures ? 1 : 0);
