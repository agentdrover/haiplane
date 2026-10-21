// Прогон скрипта раскрытия блоков (#1812) на минимальной модели DOM.
// Печатает JSON {сценарий: результат}; проверяет их tests/test_card_c2_script_behavior.py.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8")
  .replace(/\{#[\s\S]*?#\}/, "").replace(/<\/?script>/g, "");

function makeEnv({ wide, hash = "", storageThrows = false, serverOpen = [] }) {
  const listeners = {};
  const store = {};
  const mk = (id, parent) => ({
    id, tagName: "DETAILS", open: serverOpen.includes(id), parentElement: parent || null,
    hasAttribute(n) { return n === "open" && serverOpen.includes(id); },
  });
  const state = { nodes: null };
  const fresh = () => {
    const outer = mk("card-details");
    const inner = mk("c2-checks");
    const target = { id: "task-controls", tagName: "SECTION", parentElement: outer };
    state.nodes = [outer, inner];
    state.target = target;
  };
  fresh();
  const document = {
    querySelectorAll: () => state.nodes,
    getElementById: (id) => (id === "task-controls" ? state.target : state.nodes.find((n) => n.id === id) || null),
    addEventListener: (n, f) => { (listeners[n] = listeners[n] || []).push(f); },
  };
  const window = {
    location: { hash },
    matchMedia: () => ({ matches: wide }),
    addEventListener: (n, f) => { (listeners["w:" + n] = listeners["w:" + n] || []).push(f); },
    sessionStorage: {
      getItem: (k) => { if (storageThrows) throw new Error("denied"); return k in store ? store[k] : null; },
      setItem: (k, v) => { if (storageThrows) throw new Error("denied"); store[k] = v; },
    },
  };
  const run = () => new Function("window", "document", src)(window, document);
  const fire = (name, ev) => (listeners[name] || []).forEach((f) => f(ev || {}));
  return { run, fire, state, fresh, store, window };
}
const out = {};

// 1. узкий экран: начальное значение закрыто и записано
let e = makeEnv({ wide: false });
e.run();
out.narrow_initial = e.state.nodes[0].open;

// 2. ручное раскрытие переживает замену .main
e.state.nodes[0].open = true; e.fire("toggle", { target: e.state.nodes[0] });
e.fire("htmx:beforeSwap"); e.fresh(); e.run(); e.fire("htmx:afterSwap");
out.manual_open_survives_swap = e.state.nodes[0].open;

// 3. широкий экран: ручное закрытие не перебивается ни заменой, ни шириной
e = makeEnv({ wide: true });
e.run();
out.wide_initial = e.state.nodes[0].open;
e.state.nodes[0].open = false; // закрыли; toggle ещё не доставлен (гонка)
e.fire("htmx:beforeSwap"); e.fresh(); e.run(); e.fire("htmx:afterSwap");
out.wide_manual_close_survives_swap = e.state.nodes[0].open;

// 4. фрагмент с целью внутри закрытого блока: при загрузке раскрывает, при замене нет
e = makeEnv({ wide: false, hash: "#task-controls" });
e.run();
out.hash_opens_on_load = e.state.nodes[0].open;
e.state.nodes[0].open = false; e.fire("toggle", { target: e.state.nodes[0] });
e.fire("htmx:beforeSwap"); e.fresh(); e.run(); e.fire("htmx:afterSwap");
out.hash_does_not_override_close_on_swap = e.state.nodes[0].open;

// 5. ответ на действие (open с сервера) виден при загрузке, потом уважает закрытие
e = makeEnv({ wide: false, serverOpen: ["card-details"] });
e.run();
out.feedback_open_on_load = e.state.nodes[0].open;
e.state.nodes[0].open = false; e.fire("toggle", { target: e.state.nodes[0] });
e.fire("htmx:beforeSwap"); e.fresh(); e.state.nodes[0].open = true; /* сервер снова с open */
e.run(); e.fire("htmx:afterSwap");
out.feedback_close_survives_swap = e.state.nodes[0].open;

// 6. хранилище недоступно: не падает
e = makeEnv({ wide: true, storageThrows: true });
try { e.run(); e.fire("htmx:beforeSwap"); e.fire("htmx:afterSwap"); out.no_storage_ok = true; }
catch (err) { out.no_storage_ok = String(err); }

console.log(JSON.stringify(out));
