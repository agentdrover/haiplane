// Прогон скрипта листов (#1813) на минимальной модели DOM: печатает JSON.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8")
  .replace(/\{#[\s\S]*?#\}/, "").replace(/<\/?script>/g, "");

const listeners = {};
const classes = new Set();
const attrs = { "data-built-submission-generation": "3", "data-built-status": "review" };
const typed = { value: "" };
const warn = { hidden: true };
let focused = null;
const opener = { focus() { focused = "opener"; } };
const field = { focus() { focused = "field"; }, value: "" };
const sheet = {
  id: "sh-cr",
  classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
  getAttribute: (n) => (n in attrs ? attrs[n] : null),
  setAttribute: (n, v) => { attrs[n] = v; },
  querySelector: (sel) => (sel.includes("c2-sheet-stale") ? warn : field),
  querySelectorAll: () => [field],
};
const live = { attrs: { "data-submission-generation": "3", "data-status": "review" },
  getAttribute(n) { return this.attrs[n] ?? null; }, querySelectorAll: () => [] };
const holder = { getAttribute: (n) => ({ "data-built-submission-generation": "3", "data-built-status": "review" })[n] ?? null };
let reloads = 0;
const document = {
  getElementById: (id) => (id === "c2-live" ? live : id === "c2-sheets" ? holder : id === "sh-cr" ? sheet : null),
  querySelector: (sel) => {
    if (sel === ".c2-sheet.is-open") return classes.has("is-open") ? sheet : null;
    if (sel.includes("data-sheet=")) return opener;
    return null;
  },
  querySelectorAll: (sel) => (sel === ".c2-sheet" ? [sheet] : []),
  addEventListener: (n, f) => { (listeners[n] = listeners[n] || []).push(f); },
  activeElement: null,
};
const window = { location: { hash: "#sh-cr", pathname: "/tasks/1", search: "", reload() { reloads += 1; } }, history: { replaceState() {} }, addEventListener() {} };
new Function("window", "document", src)(window, document);
const fire = (n, ev) => (listeners[n] || []).forEach((f) => f(ev));
const out = {};

window.hubSheets.open("sh-cr");
typed.value = field.value = "мой текст";
out.warning_hidden_before = warn.hidden === true;

// автообновление: #c2-live заменён, поколение выросло; лист (и его поле) не тронуты
live.attrs["data-submission-generation"] = "4";
fire("htmx:afterSwap", {});
out.typed_text_after_swap = field.value;
out.warning_shown_after_generation_change = warn.hidden === false;
out.submit_stays_enabled = !("disabled" in sheet);
out.no_reload_while_open = reloads === 0;

fire("keydown", { key: "Escape" });
out.escape_closes_and_returns_focus = !classes.has("is-open") && focused === "opener";
out.reload_after_close_when_panel_moved_on = reloads === 1;
out.storage_free = !/sessionStorage|localStorage/.test(src);
console.log(JSON.stringify(out));
