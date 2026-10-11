const SITES = [
  { id: "tender-portal", name: "Vergabeportal", short: "TND" },
  { id: "customer-portal", name: "Spezifikation", short: "SPC" },
  { id: "erp", name: "ERP", short: "ERP" },
  { id: "mail", name: "Werkpost", short: "MAIL" },
];
const KINDS = ["case", "positions", "qa", "revisions", "materials", "erp_searches", "messages"];
const BEATS = ["open", "system", "desk", "twins", "anatomy", "private", "close"];

const eur = (n) =>
  new Intl.NumberFormat("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
const clock = (seconds) => {
  const safe = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
};
const usd = (n) => `$${n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}`;
const esc = (s) =>
  String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

function locked(s) {
  return String(s)
    .split(/(<br\s*\/?>)/i)
    .map((part) => (/^<br/i.test(part) ? "<br />" : esc(part).replaceAll(" ", "\u00A0")))
    .join("");
}

const COPY = {
  de: {
    kicker: "Autonomer Angebotsleitstand",
    title: "Von der Ausschreibung<br />zum Angebot.",
    tag: "Das Modell findet. Der Kernel entscheidet. Das Portal unterschreibt.",
    deck: "Vier Systeme, die nicht miteinander sprechen. 28 Klauseln, die zusammenpassen müssen. Die Gewinner werden auf einem privaten Datensatz gekürt — den niemand vorher sieht.",
    worker: "ein Arbeiter · eine Site",
    begin: "Weiter",
    live: "Live-Agent",
    hint: "Leertaste blättert weiter.",
  },
  en: {
    kicker: "Autonomous bid desk",
    title: "From RFQ<br />to signed bid.",
    tag: "The model finds. The kernel decides. The portal signs.",
    deck: "Four systems that do not talk. Twenty-eight clauses that must agree. The winners are picked on a private dataset you will never see.",
    worker: "one worker · one site",
    begin: "Begin",
    live: "Attach live agent",
    hint: "Space turns the page.",
  },
};

const state = {
  data: null,
  chapter: "open",
  lang: "de",
  playing: false,
  speed: 4,
  t: 0,
  help: false,
  pitch: null,
  live: false,
  liveEvents: [],
  error: null,
  last: null,
  raf: 0,
  source: null,
};

function t(key) {
  return COPY[state.lang][key];
}

function events() {
  return state.live ? state.liveEvents : state.data.events;
}

function visible() {
  const all = events();
  return state.live ? all : all.filter((e) => (e.t ?? 0) <= state.t);
}

function goBeat(id) {
  state.chapter = id;
  state.playing = id === "desk" && !state.live;
  if (id === "close") state.t = state.data.timeS;
  if (id === "desk" && !state.live && state.t >= state.data.timeS) state.t = 0;
  render();
}

function nextBeat() {
  const i = BEATS.indexOf(state.chapter);
  goBeat(BEATS[Math.min(BEATS.length - 1, i + 1)]);
}

function begin() {
  state.live = false;
  state.liveEvents = [];
  state.t = 0;
  goBeat("system");
}

function restart() {
  state.live = false;
  state.liveEvents = [];
  state.error = null;
  state.t = 0;
  goBeat("desk");
}

async function startLive() {
  state.error = null;
  state.live = true;
  state.playing = false;
  state.liveEvents = [];
  state.t = 0;
  goBeat("desk");
  try {
    const res = await fetch("/api/live", { method: "POST" });
    const body = await res.json();
    if (!body.ok) {
      state.error = body.error || "Live desk refused.";
      state.live = false;
      render();
      return;
    }
    state.source?.close();
    state.source = new EventSource("/api/events");
    state.source.onmessage = (msg) => {
      const event = JSON.parse(msg.data);
      state.liveEvents.push(event);
      if (typeof event.t === "number") state.t = event.t;
      if (event.type === "score") goBeat("close");
      else render();
    };
    state.source.onerror = () => state.source?.close();
  } catch (err) {
    state.live = false;
    state.error = err instanceof Error ? err.message : "Start python pitch/server.py for live attach.";
    render();
  }
}

function converge() {
  const colors = ["#7d9bb8", "#7f9a78", "#c6a36a", "#c4846a"];
  const xs = [100, 300, 500, 700];
  const lines = xs
    .map((x, i) => `<path d="M${x} 68 L 400 10" stroke="${colors[i]}" stroke-width="1.25"/>`)
    .join("");
  const dots = xs
    .map((x, i) => `<circle cx="${x}" cy="68" r="2.4" fill="${colors[i]}"/>`)
    .join("");
  return `
    <svg class="converge" viewBox="0 0 800 80" fill="none" aria-hidden="true">
      ${lines}
      ${dots}
      <circle cx="400" cy="10" r="3" fill="#c6a36a"/>
    </svg>`;
}

function langSwitch() {
  return `
    <span class="lang">
      <button class="${state.lang === "de" ? "active" : ""}" data-act="lang" data-n="de">DE</button>
      <button class="${state.lang === "en" ? "active" : ""}" data-act="lang" data-n="en">EN</button>
    </span>`;
}

function opening() {
  return `
    <section class="open">
      <div class="open-meta">
        <span>Ctrl+Alt+Del</span>
        ${langSwitch()}
        <span>EHL Finale 2026</span>
      </div>
      <div class="open-hero" translate="no">
        <p class="open-kicker">${locked(t("kicker"))}</p>
        <h1>${locked(t("title"))}</h1>
        <p class="open-tag">${locked(t("tag"))}</p>
        <p class="open-deck">${locked(t("deck"))}</p>
        <div class="open-map">
          ${converge()}
          <div class="open-sites">
            ${SITES.map((s) => `<div>${esc(s.name)}<span>${locked(t("worker"))}</span></div>`).join("")}
          </div>
        </div>
      </div>
      <div class="open-foot">
        <div>
          <button class="primary" data-act="next">${locked(t("begin"))}</button>
          <button data-act="live">${locked(t("live"))}</button>
        </div>
        <p class="hint">${locked(t("hint"))}</p>
      </div>
    </section>`;
}

function system() {
  return `
    <section class="scene">
      <div>
        <p class="scene-kicker">The split</p>
        <h2>Retrieval is learned. Price is not.</h2>
        <p class="scene-lead">DeepSeek may choose the next link. It may not invent a cent. The private run will change the software. It will not change the brief.</p>
      </div>
      <div class="split">
        <div class="split-pane">
          <h3>The model</h3>
          <ul>
            <li>Four HTTP workers, one site each</li>
            <li>view · submit · record · gaps</li>
            <li>No browser chrome, no JavaScript theater</li>
            <li>Evidence, never conclusions</li>
            <li>Decoys stay in the store</li>
          </ul>
        </div>
        <div class="split-pane">
          <h3>The kernel</h3>
          <ul>
            <li>The brief, in Python, in milliseconds</li>
            <li>Authorized sender · date · exact reference</li>
            <li>Withdrawals, Satz, usable stroke, FX</li>
            <li>Discount scope per Konditionsgruppe</li>
            <li>HALF_UP once. Reports are not inputs.</li>
          </ul>
        </div>
      </div>
      <div class="open-foot">
        <p class="verdict">If the model priced, we would overfit the public pages.</p>
        <button class="primary" data-act="next">Watch them harvest</button>
      </div>
    </section>`;
}

function desk() {
  const data = state.data;
  const vis = visible();
  const priced = state.t >= 185 || vis.some((e) => e.type === "bid" || (e.type === "phase" && e.title === "Kernel"));
  const signing = vis.filter((e) => e.site === "submit" || e.type === "submit");
  const traces =
    state.t >= 185 || state.live
      ? data.trace.slice(
          0,
          state.live ? data.trace.length : Math.min(data.trace.length, Math.max(1, Math.floor((state.t - 184) * 8))),
        )
      : [];
  const activeRules = new Set(traces.map((x) => x.rule));
  const gaps = priced ? 0 : Math.max(0, 8 - vis.filter((e) => e.type === "record").length);

  const workers = SITES.map((site) => {
    const mine = vis.filter((e) => e.site === site.id);
    const last = mine[mine.length - 1];
    const views = mine.filter((e) => e.type === "view" || e.type === "submit").length;
    const recs = mine.filter((e) => e.type === "record").reduce((n, e) => n + (e.records || 1), 0);
    const done = mine.some((e) => e.type === "say");
    const tape = mine
      .slice(-8)
      .reverse()
      .map(
        (e, i) =>
          `<div class="tape-item ${i === 0 ? "on" : ""}">${esc((e.t != null ? clock(e.t) + "  " : "") + e.title)}</div>`,
      )
      .join("");
    return `
      <article class="worker ${site.id}">
        <div class="worker-head">
          <b>${esc(site.name)}</b>
          <span>${done ? "done" : last ? "live" : "idle"} · ${site.short}</span>
        </div>
        <div class="now">
          ${esc(last ? last.title : "Waiting for the first observation.")}
          ${last?.detail ? `<small>${esc(last.detail)}</small>` : ""}
        </div>
        <div class="tape">${tape}</div>
        <div class="counts"><span>${views} pages</span><span>${recs} records</span></div>
      </article>`;
  }).join("");

  const kinds = KINDS.map((k) => {
    const n = vis.filter((e) => e.kind === k).reduce((a, e) => a + (e.records || 1), 0);
    return `<div class="kind ${n ? "on" : ""}"><span>${k}</span><i>${n || "—"}</i></div>`;
  }).join("");

  const chips = data.rules
    .map((r) => `<span class="chip ${activeRules.has(r.id) ? "on" : ""}">${esc(r.label)}</span>`)
    .join("");

  const proofSource = signing.length
    ? signing.slice(-12)
    : priced
      ? traces
      : vis.filter((e) => e.type === "record" || e.type === "say").slice(-10);
  const proofRows = proofSource
    .map((row) => `<li class="on">${esc(row.text || row.title)}</li>`)
    .join("");

  return `
    <section class="desk">
      <div class="workers">
        ${workers}
        <aside class="rail">
          <h4>Evidence store</h4>
          <div class="kinds">${kinds}</div>
          <h4>Clauses</h4>
          <div class="rules">${chips}</div>
        </aside>
      </div>
      <div class="bottom">
        <div class="proof">
          <h3>${signing.length ? "Configurator + TAN" : priced ? "Kernel proof" : state.live ? "Live observations" : "Waiting for a closed store"}</h3>
          <ol>${proofRows}</ol>
        </div>
        <div class="stats">
          <div class="stat"><b>${priced ? "5 / 5" : "—"}</b><span>Positions priced</span></div>
          <div class="stat"><b>${gaps}</b><span>Open gaps</span></div>
          <div class="stat"><b>${clock(state.t)}</b><span>${state.live ? "Agent clock" : "Run clock"}</span></div>
          <div class="stat"><b>${priced ? eur(data.totalNet) : "—"}</b><span>Net EUR</span></div>
        </div>
      </div>
    </section>`;
}

function twinCard(side, cls) {
  return `
    <article class="twin ${cls}">
      <h3>${esc(side.position)}</h3>
      <div class="price">${eur(side.unit)} €</div>
      <dl>
        <dt>Quantity</dt><dd>${esc(side.qty)}</dd>
        <dt>Scale</dt><dd>${esc(side.scale)}</dd>
        <dt>Discount scope</dt><dd>${esc(side.scope)}</dd>
        <dt>Delivery</dt><dd>${esc(side.delivery)}</dd>
        <dt>Setup</dt><dd>${esc(side.pool)}</dd>
        <dt>Bonus</dt><dd>${esc(side.bonus)}</dd>
      </dl>
    </article>`;
}

function twins() {
  const t = state.data.twins;
  return `
    <section class="scene">
      <div>
        <p class="scene-kicker">The proof</p>
        <h2>One steel. Two prices.</h2>
        <p class="scene-lead">${esc(t.line)}</p>
      </div>
      <div class="twins">
        ${twinCard(t.left, "left")}
        <div class="steel">
          <b>${esc(t.material)}</b>
          <span>${esc(t.spec)}</span>
          <em>${esc(t.fx)}</em>
        </div>
        ${twinCard(t.right, "right")}
      </div>
      <div class="open-foot">
        <p class="verdict">Same catalogue number. Different Konditionsgruppe, scale, commitment, pool and bonus.</p>
        <button class="primary" data-act="next">Open the dense line</button>
      </div>
    </section>`;
}

function anatomy() {
  const a = state.data.anatomy;
  const rows = a.steps
    .map(
      (s, i) => `
      <div class="ledger-row ${i === a.steps.length - 1 ? "last" : ""}">
        <b>${esc(s.k)}</b>
        <strong>${esc(s.v)}</strong>
        <span>${esc(s.note)}</span>
      </div>`,
    )
    .join("");
  return `
    <section class="scene">
      <div>
        <p class="scene-kicker">Position ${esc(a.position)} · ${esc(a.material)}</p>
        <h2>The kernel is a notary.</h2>
        <p class="scene-lead">Every line is a record the model found. The last line is the only number code is allowed to write.</p>
      </div>
      <div class="ledger">${rows}</div>
      <div class="open-foot">
        <p class="verdict">Breakdown fields are reports. They never feed back into the formula.</p>
        <button class="primary" data-act="next">The private dataset</button>
      </div>
    </section>`;
}

function privateScene() {
  const sockets = state.data.sockets;
  const pub = sockets
    .map((s) => `<div class="socket">${esc(s.public)}<small>public · ${esc(s.id)}</small></div>`)
    .join("");
  const priv = sockets
    .map((s) => `<div class="socket">${esc(s.private)}<small>other software · other split</small></div>`)
    .join("");
  return `
    <section class="scene">
      <div>
        <p class="scene-kicker">Why we can win</p>
        <h2>The pages will move. The brief will not.</h2>
        <p class="scene-lead">Final ranking starts with the private dataset: other sites, other names, the same jobs. A crawl of these four URLs is already dead.</p>
      </div>
      <div class="sockets">
        <div class="socket-col">${pub}</div>
        <div class="kernel-node">
          <b>Schema</b>
          <span>typed records, any site</span>
          <b style="margin-top:18px">Kernel</b>
          <span>the brief, in code</span>
        </div>
        <div class="socket-col private">${priv}</div>
      </div>
      <div class="open-foot">
        <p class="verdict">We did not memorize Linden. We implemented the rules.</p>
        <button class="primary" data-act="next">The signed bid</button>
      </div>
    </section>`;
}

function close() {
  const data = state.data;
  const vis = visible();
  const signed = vis.some(
    (e) => e.type === "score" || (e.type === "submit" && /sign/i.test(`${e.title}${e.detail || ""}`)),
  );
  const rows = data.positions
    .map(
      (p) => `
      <tr>
        <td>${esc(p.position)}</td>
        <td>${esc(p.material)}<div style="color:#3c3933;font-size:12px">${p.bore}/${p.stroke} · ${esc(p.mounting)} · ${esc(p.cert)}</div></td>
        <td>${p.quantity}</td>
        <td class="num">${eur(p.unitPrice)}</td>
        <td class="num">${p.leadWeeks} w</td>
        <td class="num">${eur(p.rebate)}</td>
      </tr>`,
    )
    .join("");
  return `
    <section class="close">
      <article class="paper">
        <div class="paper-head"><span>${esc(data.company)}</span><span>Angebot ${esc(data.tender)}</span></div>
        <h2>Signed bid</h2>
        <p class="paper-sub">
          Decision ${esc(data.decision)} · Discount ${data.discount.toFixed(2)} % · Currency EUR
          · public test run, not a memorized form
        </p>
        <table class="lines">
          <thead>
            <tr><th>Pos.</th><th>Material</th><th>Qty</th><th class="num">Unit</th><th class="num">Lead</th><th class="num">Rebate</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
        <div class="paper-foot">
          <div class="signed">${signed ? "TAN accepted · bid final" : "Awaiting signature"}</div>
          <div>
            <div style="font-size:12px;letter-spacing:0.1em;text-transform:uppercase;color:#3c3933">Total net</div>
            <div class="total">${eur(data.totalNet)} EUR</div>
          </div>
        </div>
      </article>
      <aside class="score">
        <div><p class="seal">${data.quality}<small>eligible</small></p></div>
        <div class="score-grid">
          <div><b>${clock(data.timeS)}</b><span>Wall time</span></div>
          <div><b>${usd(data.costUsd)}</b><span>Model cost</span></div>
          <div><b>${data.llmCalls}</b><span>DeepSeek calls</span></div>
          <div><b>${clock(data.rerun.timeS)}</b><span>Rerun, still 100</span></div>
        </div>
        <p class="score-note">
          The model never wrote a price. The kernel did. That is the system we take into the private run.
        </p>
      </aside>
    </section>`;
}

function help() {
  return `
    <aside class="help">
      <h3>3-Minuten Sprechzettel</h3>
      <ol>
        <li>Private Dataset entscheidet. Wer Seiten auswendig lernt, ist schon tot.</li>
        <li>Modell findet. Kernel entscheidet. Darum generalisiert ihr.</li>
        <li>Desk nur als Beweis, dass es wirklich läuft — nicht hängen bleiben.</li>
        <li>HZ-2170: ein Stahl, zwei Preise. Das ist das Produkt.</li>
        <li>01.05 als Notar. Dann 100. Schlusszeile nicht erklären, stehen lassen.</li>
      </ol>
      <p><kbd>Space</kbd> nächster Beat · <kbd>1–7</kbd> springen · <kbd>T</kbd> Pitch-Uhr</p>
    </aside>`;
}

function header() {
  if (state.chapter === "open") return "";
  const data = state.data;
  const beat = BEATS.indexOf(state.chapter);
  const dots = BEATS.map((_, i) => `<i class="${i <= beat ? "on" : ""}"></i>`).join("");
  const speeds = [2, 4, 8]
    .map((n) => `<button class="${state.speed === n ? "active" : ""}" data-act="speed" data-n="${n}">${n}×</button>`)
    .join("");
  const progress =
    state.chapter === "desk" ? Math.min(100, (state.t / data.timeS) * 100) : ((beat + 1) / BEATS.length) * 100;
  return `
    <header class="top">
      <div class="progress"><i style="width:${progress}%"></i></div>
      <button class="brand" data-act="beat" data-n="open" type="button" title="Back to start">
        <div class="brand-mark">Quorum</div>
        <div class="brand-sub">Ctrl+Alt+Del</div>
      </button>
      <nav class="nav">
        <button class="${state.chapter === "system" ? "active" : ""}" data-act="beat" data-n="system">Split</button>
        <button class="${state.chapter === "desk" ? "active" : ""}" data-act="beat" data-n="desk">Desk</button>
        <button class="${state.chapter === "twins" ? "active" : ""}" data-act="beat" data-n="twins">Proof</button>
        <button class="${state.chapter === "anatomy" ? "active" : ""}" data-act="beat" data-n="anatomy">Kernel</button>
        <button class="${state.chapter === "private" ? "active" : ""}" data-act="beat" data-n="private">Private</button>
        <button class="${state.chapter === "close" ? "active" : ""}" data-act="beat" data-n="close">Bid</button>
      </nav>
      <div class="top-meta">
        ${langSwitch()}
        <span class="beats">${dots}</span>
        <span>${esc(data.tender)}</span>
        ${
          state.chapter === "desk" && !state.live
            ? `<span>${clock(state.t)}</span><button data-act="toggle">${state.playing ? "Pause" : "Play"}</button>${speeds}`
            : ""
        }
        <button data-act="live">Live</button>
        ${state.pitch != null ? `<span class="${state.pitch <= 30 ? "pitch-clock low" : "pitch-clock"}">Pitch <b>${clock(state.pitch)}</b></span>` : ""}
      </div>
    </header>`;
}

const views = { open: opening, system, desk, twins, anatomy, private: privateScene, close };

function render() {
  document.documentElement.lang = state.lang;
  const root = document.getElementById("root");
  const view = views[state.chapter] || opening;
  root.innerHTML = `
    <div class="app">
      ${header()}
      <main class="stage">
        ${view()}
        ${state.help ? help() : ""}
        ${state.error ? `<div class="toast">${esc(state.error)}</div>` : ""}
      </main>
    </div>`;
}

function onClick(event) {
  const btn = event.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === "next" || act === "begin") nextBeat();
  if (act === "live") void startLive();
  if (act === "toggle") {
    state.playing = !state.playing;
    render();
  }
  if (act === "speed") {
    state.speed = Number(btn.dataset.n);
    render();
  }
  if (act === "beat") goBeat(btn.dataset.n);
  if (act === "lang") {
    state.lang = btn.dataset.n;
    document.documentElement.lang = state.lang;
    render();
  }
}

function onKey(event) {
  const key = event.key;
  if (key === "?" || key === "h" || key === "H") {
    state.help = !state.help;
    render();
  }
  if (key >= "1" && key <= "7") goBeat(BEATS[Number(key) - 1]);
  if (key === " ") {
    event.preventDefault();
    if (state.chapter === "desk" && state.playing) {
      state.playing = false;
      render();
    } else {
      nextBeat();
    }
  }
  if (key === "t" || key === "T") {
    state.pitch = state.pitch == null ? 180 : null;
    render();
  }
  if (key === "r" || key === "R") restart();
  if (key === "f" || key === "F") void document.documentElement.requestFullscreen?.();
  if (key === "Escape") {
    state.help = false;
    render();
  }
}

function tick(now) {
  if (state.playing && !state.live && state.chapter === "desk") {
    if (state.last == null) state.last = now;
    state.t = Math.min(state.data.timeS, state.t + ((now - state.last) / 1000) * state.speed);
    state.last = now;
    if (state.t >= state.data.timeS) {
      state.playing = false;
      goBeat("twins");
    } else {
      const n = visible().length;
      if (n !== state._vis || now - (state._lastPaint || 0) > 160) {
        state._vis = n;
        state._lastPaint = now;
        render();
      }
    }
  } else {
    state.last = null;
  }
  state.raf = requestAnimationFrame(tick);
}

setInterval(() => {
  if (state.pitch == null) return;
  state.pitch = Math.max(0, state.pitch - 1);
  render();
}, 1000);

document.addEventListener("click", onClick);
window.addEventListener("keydown", onKey);

const data = await fetch("/golden.json").then((r) => r.json());
state.data = data;
render();
state.raf = requestAnimationFrame(tick);
