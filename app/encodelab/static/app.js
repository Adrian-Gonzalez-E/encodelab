// EncodeLab UI: vista Panel/Baterías, confirmación de borrado, cola en vivo (/api/queue) con refresco de <main>
// solo cuando cambia su versión, tooltip del gráfico, copiar argumentos, tarjetas de encoder y ajustes.
// Sin dependencias; todo por delegación en document para que siga funcionando cuando el refresco sustituye <main>.
(() => {
  "use strict";
  const root = document.documentElement;
  const VIEW_KEY = "encodelab.view";
  const REFRESH_S = Number(root.dataset.refresh || 2);  // 0 = sin sondeo (ajuste)

  // ---------- vista ----------
  const TESTS_KEY = "encodelab.tests";
  function syncPressed() {
    document.querySelectorAll("[data-set-view]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.setView === root.dataset.view)));
    document.querySelectorAll("[data-set-tests]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.setTests === root.dataset.tests)));
    const layers = (root.dataset.layers || "").split(" ");
    document.querySelectorAll("[data-layer]").forEach((b) =>
      b.setAttribute("aria-pressed", String(!b.disabled && layers.includes(b.dataset.layer))));
  }
  function setTests(mode) {
    root.dataset.tests = mode;
    try { localStorage.setItem(TESTS_KEY, mode); } catch (e) { /* almacenamiento bloqueado */ }
    syncPressed();
  }
  // Capas del gráfico (umbral, ganador, frontera, curvas): lista separada por espacios en <html data-layers>.
  const LAYERS_KEY = "encodelab.layers";
  function toggleLayer(layer) {
    const on = new Set((root.dataset.layers || "").split(" ").filter(Boolean));
    if (!on.delete(layer)) on.add(layer);
    root.dataset.layers = [...on].join(" ");
    try { localStorage.setItem(LAYERS_KEY, root.dataset.layers); } catch (e) { /* almacenamiento bloqueado */ }
    syncPressed();
  }
  function setView(view) {
    root.dataset.view = view;
    try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* almacenamiento bloqueado: solo esta visita */ }
    syncPressed();
  }

  // Desde otras páginas, las pestañas Panel/Baterías llegan con ?vista=…: se aplica y se limpia la URL.
  {
    const wanted = new URLSearchParams(location.search).get("vista");
    if (wanted === "panel" || wanted === "baterias") {
      setView(wanted);
      history.replaceState(null, "", location.pathname);
    }
  }

  // ---------- confirmación de borrado ----------
  const dialog = document.getElementById("confirm");
  let pendingForm = null;
  document.addEventListener("submit", (e) => {
    const form = e.target.closest("form[data-confirm]");
    if (!form || !dialog || typeof dialog.showModal !== "function") return;
    e.preventDefault();
    pendingForm = form;
    dialog.querySelector(".confirm-text").textContent = form.dataset.confirm;
    dialog.returnValue = "";
    dialog.showModal();
  });
  if (dialog) {
    dialog.addEventListener("close", () => {
      if (dialog.returnValue === "ok" && pendingForm && pendingForm.isConnected) pendingForm.submit();
      pendingForm = null;
    });
  }

  // ---------- tooltip del gráfico y resaltado punto <-> tarjeta ----------
  // Punto, fila de la lista y fila de la tabla de una misma prueba comparten data-id.
  function highlight(id, on) {
    document.querySelectorAll('[data-id="' + CSS.escape(id) + '"]').forEach((el) => el.classList.toggle("hl", on));
  }
  function line(text, bold) {
    const el = document.createElement(bold ? "b" : "span");
    el.textContent = text;
    return el;
  }
  function showTip(pt) {
    const fig = pt.closest(".chart-wrap");
    const tip = fig && fig.querySelector(".chart-tip");
    if (!tip) return;
    const d = pt.dataset;
    tip.replaceChildren(line(d.label, true), line("VMAF " + d.vmaf + " · p5 " + d.p5),
      document.createElement("br"), line(d.pct + " % del original · " + d.gb + " GB vídeo/capítulo"));
    tip.hidden = false;
    const fr = fig.getBoundingClientRect();
    const pr = pt.getBoundingClientRect();
    const half = tip.offsetWidth / 2;
    const left = Math.min(Math.max(pr.left + pr.width / 2 - fr.left, half + 4), fr.width - half - 4);
    tip.style.left = left + "px";
    tip.style.top = (pr.top - fr.top + pr.height / 2) + "px";
    // Con el gráfico fijo, arriba puede quedar bajo la cabecera: entonces va debajo del punto.
    tip.classList.remove("below");
    const header = document.querySelector(".topbar");
    if (tip.getBoundingClientRect().top < (header ? header.getBoundingClientRect().bottom : 0) + 4) {
      tip.classList.add("below");
    }
    highlight(d.id, true);
  }
  function hideTip(pt) {
    const tip = pt.closest(".chart-wrap")?.querySelector(".chart-tip");
    if (tip) tip.hidden = true;
    highlight(pt.dataset.id, false);
  }
  document.addEventListener("mouseover", (e) => {
    const pt = e.target.closest(".pt");
    if (pt && !pt.contains(e.relatedTarget)) showTip(pt);
    const card = e.target.closest(".trow[data-id], tr[data-id]");
    if (card && !card.contains(e.relatedTarget)) highlight(card.dataset.id, true);
  });
  document.addEventListener("mouseout", (e) => {
    const pt = e.target.closest(".pt");
    if (pt && !pt.contains(e.relatedTarget)) hideTip(pt);
    const card = e.target.closest(".trow[data-id], tr[data-id]");
    if (card && !card.contains(e.relatedTarget)) highlight(card.dataset.id, false);
  });
  document.addEventListener("focusin", (e) => { const pt = e.target.closest(".pt"); if (pt) showTip(pt); });
  document.addEventListener("focusout", (e) => { const pt = e.target.closest(".pt"); if (pt) hideTip(pt); });

  // ---------- copiar ----------
  async function copy(btn) {
    const old = btn.textContent;
    let ok = false;
    try { await navigator.clipboard.writeText(btn.dataset.copy); ok = true; } catch (e) { /* sin permiso o sin HTTPS */ }
    if (!ok && btn.previousElementSibling) {
      const range = document.createRange();
      range.selectNodeContents(btn.previousElementSibling);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    }
    btn.textContent = ok ? "Copiado" : "Selecciona y copia";
    setTimeout(() => { btn.textContent = old; }, 1500);
  }

  document.addEventListener("click", (e) => {
    const v = e.target.closest("[data-set-view]");
    if (v) { setView(v.dataset.setView); return; }
    const m = e.target.closest("[data-set-tests]");
    if (m) { setTests(m.dataset.setTests); return; }
    const l = e.target.closest("[data-layer]");
    if (l) { toggleLayer(l.dataset.layer); return; }
    const th = e.target.closest("table.cmp th[data-sort]");
    if (th && !e.target.closest(".info")) { sortTable(th.closest("table"), th.dataset.sort, null); return; }
    const c = e.target.closest("[data-copy]");
    if (c) { copy(c); return; }
    const pt = e.target.closest(".pt");
    if (pt) showTip(pt);  // en táctil no hay hover
  });
  document.addEventListener("change", (e) => {
    if (e.target.matches("select[data-autosubmit]")) e.target.form.submit();
  });

  // ---------- duraciones como el filtro "dur" del servidor ----------
  // h:mm:ss, como el filtro "hms" del servidor.
  function hms(s) {
    const t = Math.round(s * 10);
    const h = Math.floor(t / 36000), m = Math.floor(t / 600) % 60, s10 = t % 600;
    const sec = String(Math.floor(s10 / 10)).padStart(2, "0") + (s10 % 10 ? "." + (s10 % 10) : "");
    return h + ":" + String(m).padStart(2, "0") + ":" + sec;
  }
  function dur(s) { return s == null ? "–" : "~" + hms(s); }
  const pct = (p) => Math.round((p || 0) * 100);

  // ---------- cola en vivo: píldora, desplegable y barras de las tarjetas en curso ----------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function mini(p) {
    const m = el("span", "mini");
    const i = el("i");
    i.style.setProperty("--w", ((p || 0) * 100).toFixed(1) + "%");
    m.append(i);
    return m;
  }
  function progressInfo(r) {
    let text = pct(r.progress) + " %";
    if (r.phase === "metrics") text += " · midiendo VMAF";
    else if (r.frame) {
      text += " · fotograma " + r.frame + (r.frames_total ? " / " + r.frames_total : "");
      if (r.elapsed_s) text += " · " + Math.round(r.frame / r.elapsed_s) + " fps";
    }
    if (r.elapsed_s && r.progress > 0) text += " · quedan " + dur(r.eta_s);
    return text;
  }
  function renderQueue(q) {
    const pill = document.querySelector(".qpill");
    if (pill) {
      const r = q.running[0];
      pill.classList.toggle("busy", q.pending > 0);
      if (q.pending) {
        pill.replaceChildren(el("span", "dot"), el("span", "qtext", q.running.length + " de " + q.pending),
          mini(r && r.progress), el("span", "qpct", pct(r && r.progress) + " %"), el("span", "caret", "▾"));
      } else {
        pill.replaceChildren("✓ Cola vacía");
      }
    }
    const rows = document.querySelector("#queue .qrows");
    if (rows) {
      rows.replaceChildren(...q.running.map((r) => {
        const row = el("div", "qrow");
        row.dataset.queueId = r.id;
        const label = el("span", null, r.label + " ");
        label.append(el("small", null, "· " + (r.phase === "metrics" ? "midiendo VMAF" : "codificando")));
        row.append(label, mini(r.progress), el("b", null, pct(r.progress) + " %"));
        return row;
      }), ...q.queued.map((r) => {
        const row = el("div", "qrow");
        row.dataset.queueId = r.id;
        const label = el("span", "muted", r.label + " ");
        label.append(el("small", null, "· en espera"));
        row.append(label, mini(0), el("b", "muted", "—"));
        return row;
      }));
    }
    const foot = document.querySelector("#queue .qfoot");
    if (foot) {
      foot.textContent = !q.pending ? "No hay pruebas en marcha."
        : q.eta_total_s != null ? "Estimado: " + dur(q.eta_total_s) + " para terminar la cola"
        : "Calculando el tiempo restante…";
    }
    for (const r of q.running) {
      // Hay dos barras por prueba en curso (la fila y su detalle desplegado), más el % del resumen.
      document.querySelectorAll('[data-progress-for="' + r.id + '"] i').forEach((bar) =>
        bar.style.setProperty("--w", ((r.progress || 0) * 100).toFixed(1) + "%"));
      document.querySelectorAll('[data-progress-pct="' + r.id + '"]').forEach((el) => { el.textContent = pct(r.progress) + " %"; });
      const info = document.querySelector('[data-progress-info="' + r.id + '"]');
      if (info) info.textContent = progressInfo(r);
    }
  }

  let timer = null;
  function schedule() {
    clearTimeout(timer);
    const main = document.querySelector("main");
    if (REFRESH_S > 0 && main && main.hasAttribute("data-pending")) timer = setTimeout(poll, REFRESH_S * 1000);
  }
  async function poll() {
    try {
      const r = await fetch("/api/queue", { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(String(r.status));
      const q = await r.json();
      renderQueue(q);
      const main = document.querySelector("main");
      if (main && q.version !== main.dataset.version) {
        if (main.hasAttribute("data-live")) {
          await refresh();
        } else {
          // Formularios y guías no se tocan: solo cambia la píldora (y deja de sondear si ya no hay cola).
          main.dataset.version = q.version;
          main.toggleAttribute("data-pending", q.pending > 0);
        }
      }
    } catch (e) { /* servidor reiniciando o red caída: se reintenta */ }
    schedule();
  }
  // El foco se recupera por id o por selector relativo a su ancestro con id (p. ej. "#t-12 button.copy").
  function focusPath(main) {
    const el = document.activeElement;
    if (!el || !main.contains(el)) return null;
    if (el.id) return { sel: "#" + CSS.escape(el.id), i: 0 };
    const anc = el.parentElement && el.parentElement.closest("[id]");
    if (!anc || !main.contains(anc)) return null;
    const sel = "#" + CSS.escape(anc.id) + " " + el.tagName.toLowerCase() +
      (el.classList[0] ? "." + CSS.escape(el.classList[0]) : "");
    return { sel, i: [...document.querySelectorAll(sel)].indexOf(el) };
  }
  function busy(main) {
    const el = document.activeElement;
    return (dialog && dialog.open) || (el && main.contains(el) && el.matches("select, input, textarea"));
  }
  // Elementos de fuera de <main> marcados data-swap (selector de escena) salvo si tienen un popover abierto o el foco.
  function swapOutside(doc) {
    for (const old of document.querySelectorAll("[data-swap][id]")) {
      const next = doc.getElementById(old.id);
      if (!next || old.contains(document.activeElement)) continue;
      if (old.matches(":popover-open") || old.querySelector(":popover-open")) continue;
      old.replaceWith(document.importNode(next, true));
    }
  }
  async function refresh() {
    const main = document.querySelector("main");
    if (!main || busy(main)) return;  // la versión sigue sin coincidir: el próximo sondeo lo reintenta
    try {
      const r = await fetch(location.href, { headers: { "X-Requested-With": "fetch" } });
      if (!r.ok) throw new Error(String(r.status));
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      const fresh = doc.querySelector("main");
      if (fresh && !busy(main)) {
        const open = [...main.querySelectorAll("details[id]")].map((d) => [d.id, d.open]);
        const sorts = [...main.querySelectorAll("table.cmp")].map((t) => [t.dataset.sortKey, t.dataset.sortDir]);
        const focus = focusPath(main);
        const hovered = main.querySelector(".pt:hover");
        const y = window.scrollY;
        root.classList.add("no-anim");
        main.innerHTML = fresh.innerHTML;
        main.toggleAttribute("data-pending", fresh.hasAttribute("data-pending"));
        main.dataset.version = fresh.dataset.version;
        swapOutside(doc);
        for (const [id, isOpen] of open) {
          const d = document.getElementById(id);
          if (d) d.open = isOpen;
        }
        main.querySelectorAll("table.cmp").forEach((t, i) => {
          if (sorts[i] && sorts[i][0]) sortTable(t, sorts[i][0], sorts[i][1]);
        });
        window.scrollTo(0, y);
        if (focus) {
          const el = document.querySelectorAll(focus.sel)[focus.i];
          if (el) el.focus({ preventScroll: true });
        }
        const pt = hovered && document.getElementById(hovered.id);
        if (pt) showTip(pt);
        syncPressed();
      }
    } catch (e) { /* servidor reiniciando o red caída: el próximo sondeo lo reintenta */ }
  }

  // ---------- tabla comparativa: orden por columna sin recargar (vacías al final) ----------
  function sortTable(table, key, dir) {
    const th = table.querySelector('th[data-sort="' + key + '"]');
    if (!th) return;
    if (!dir) dir = table.dataset.sortKey === key ? (table.dataset.sortDir === "asc" ? "desc" : "asc") : th.dataset.dir;
    const idx = th.cellIndex;
    const body = table.tBodies[0];
    const val = (row) => row.cells[idx].dataset.v;
    const rows = [...body.rows].sort((a, b) => {
      const va = val(a), vb = val(b);
      if (va === "" || vb === "") return (va === "") - (vb === "");
      const na = Number(va), nb = Number(vb);
      const cmp = Number.isNaN(na) || Number.isNaN(nb) ? va.localeCompare(vb) : na - nb;
      return dir === "asc" ? cmp : -cmp;
    });
    body.append(...rows);
    table.querySelectorAll("th[data-sort]").forEach((h) => h.removeAttribute("aria-sort"));
    th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
    table.dataset.sortKey = key;
    table.dataset.sortDir = dir;
  }

  // ---------- paleta de escenas ----------
  const norm = (text) => text.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
  function markMatch(el, name, q) {
    // Índices sobre el texto normalizado, llevados al original carácter a carácter.
    let n = "";
    const map = [];
    [...name].forEach((ch, i) => { for (const c of norm(ch)) { n += c; map.push(i); } });
    const chars = [...name];
    const pos = q ? n.indexOf(q) : -1;
    if (pos < 0) { el.textContent = name; return; }
    const start = map[pos], end = map[pos + q.length - 1] + 1;
    const mark = document.createElement("mark");
    mark.textContent = chars.slice(start, end).join("");
    el.replaceChildren(chars.slice(0, start).join(""), mark, chars.slice(end).join(""));
  }
  function paletteOptions(pal) { return [...pal.querySelectorAll("[role=option]")]; }
  function setActive(pal, option) {
    paletteOptions(pal).forEach((o) => o.setAttribute("aria-selected", String(o === option)));
    const input = pal.querySelector(".palette-input");
    if (option) {
      option.scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", option.id);
    } else input.removeAttribute("aria-activedescendant");
  }
  function filterPalette(pal) {
    const q = norm(pal.querySelector(".palette-input").value.trim());
    const options = paletteOptions(pal);
    let shown = 0;
    for (const o of options) {
      const hit = !q || norm(o.dataset.search).includes(q);
      o.hidden = !hit;
      shown += hit;
      const b = o.querySelector(".pname b");
      if (b) {
        if (!b.dataset.name) b.dataset.name = b.textContent;
        markMatch(b, b.dataset.name, q);
      }
    }
    // Cabecera de grupo (o «Sueltas») solo si queda alguna escena suya a la vista.
    for (const g of pal.querySelectorAll(".pgroup")) g.hidden = !g.querySelector("[role=option]:not([hidden])");
    const count = pal.querySelector(".palette-count"), groups = Number(count.dataset.groups || 0);
    count.textContent = q ? shown + " de " + options.length
      : options.length + " escena" + (options.length !== 1 ? "s" : "") + (groups ? " · " + groups + " grupo" + (groups !== 1 ? "s" : "") : "");
    pal.querySelector(".palette-empty").hidden = shown > 0;
    setActive(pal, options.find((o) => !o.hidden) || null);
  }
  document.addEventListener("toggle", (e) => {
    const pal = e.target;
    const input = pal.id === "scenes" && pal.querySelector(".palette-input");
    if (!input) return;
    // El evento llega asíncrono (puede que ya se haya escrito): se vacía al cerrar, no al abrir.
    if (e.newState === "closed") input.value = "";
    filterPalette(pal);
    if (e.newState === "open" && document.activeElement !== input) input.focus();
  }, true);
  document.addEventListener("input", (e) => {
    if (e.target.matches(".palette-input")) filterPalette(e.target.closest(".palette"));
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.matches && e.target.matches(".palette-input")) {
      const pal = e.target.closest(".palette");
      const visible = paletteOptions(pal).filter((o) => !o.hidden);
      const current = visible.findIndex((o) => o.getAttribute("aria-selected") === "true");
      if (e.key === "Escape") {  // en un input search el primer Esc solo borraría el texto
        e.preventDefault();
        pal.hidePopover();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = visible[(current + (e.key === "ArrowDown" ? 1 : -1) + visible.length) % visible.length];
        setActive(pal, next || null);
      } else if (e.key === "Enter" && visible[current]) {
        e.preventDefault();
        location.href = visible[current].href;
      }
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      const el = document.activeElement;
      if (el && el.matches("input, textarea, select")) return;
      const pal = document.getElementById("scenes");
      if (pal && pal.showPopover) { e.preventDefault(); pal.showPopover(); }
    }
  });

  // ---------- ajustes: la vista inicial manda tras guardarlos ----------
  document.addEventListener("submit", (e) => {
    if (e.target.matches(".settings-form")) {
      try { localStorage.removeItem(VIEW_KEY); } catch (err) { /* almacenamiento bloqueado */ }
    }
  });

  // ---------- nueva batería: tarjetas de encoder, valores por defecto y estimación ----------
  const batchForm = document.querySelector(".batch-form");
  function updateEstimate() {
    const est = batchForm.querySelector(".estimate");
    if (!est) return;
    const speeds = JSON.parse(est.dataset.speeds || "{}");
    const frames = Number(est.dataset.frames) || 0;
    const enc = batchForm.elements.encoder;
    const list = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
    const qs = list(batchForm.elements.qualities.value);
    const ps = list(batchForm.elements.presets.value);
    const tramos = autoCount();
    const n = qs.length * ps.length * tramos;
    if (!n || !enc) { est.textContent = ""; return; }
    let secs = 0;
    for (const p of ps) {
      const spf = speeds[enc.value + "|" + p];
      if (spf == null) { secs = null; break; }
      secs += spf * frames * qs.length * tramos;
    }
    est.textContent = (tramos > 1 ? tramos + " tramos × " + (n / tramos) + " = " : "") + n + " prueba" + (n === 1 ? "" : "s") + " · " +
      (secs == null ? "tiempo estimado: sin datos todavía" : "tiempo estimado " + dur(secs));
  }
  // ---------- tramos automáticos: análisis en segundo plano y propuestas editables ----------
  const auto = { id: null, timer: null, mode: "manual" };
  function autoCount() {
    if (!batchForm || auto.mode !== "auto") return 1;
    return batchForm.querySelectorAll("[data-pick]:checked").length || 1;
  }
  function syncAutoInputs() {
    // Los inicios elegidos viajan como campos ocultos «start» (y «role»); el manual se desactiva.
    batchForm.querySelectorAll("[data-auto-field]").forEach((x) => x.remove());
    const manual = batchForm.elements.start;
    if (manual) manual.disabled = auto.mode === "auto";
    const submit = batchForm.querySelector("[data-submit]");
    if (auto.mode !== "auto") { if (submit) { submit.textContent = "Encolar pruebas"; submit.disabled = false; } updateEstimate(); return; }
    const picks = [...batchForm.querySelectorAll("[data-pick]:checked")];
    const add = (name, value) => {
      const i = el("input"); i.type = "hidden"; i.name = name; i.value = value; i.dataset.autoField = "1"; batchForm.append(i);
    };
    for (const p of picks) {
      const row = p.closest("[data-prop]");
      add("start", row.querySelector("[data-start]").value);
      add("role", row.dataset.role);
    }
    if (auto.id) add("analysis", auto.id);
    if (submit) {
      submit.disabled = !picks.length;
      submit.textContent = picks.length ? "Encolar " + picks.length + " tramo" + (picks.length === 1 ? "" : "s") : "Analiza el vídeo primero";
    }
    updateEstimate();
  }
  function renderProposals(list) {
    const box = batchForm.querySelector("[data-proposals]");
    box.replaceChildren();
    for (const p of list) {
      const row = el("div", "prop");
      row.dataset.prop = "1"; row.dataset.role = p.role;
      const pick = el("input"); pick.type = "checkbox"; pick.checked = true; pick.dataset.pick = "1";
      pick.setAttribute("aria-label", "Usar el tramo " + p.role);
      const img = el("img"); img.src = p.thumb; img.alt = "Fotograma central del tramo " + p.role; img.loading = "lazy";
      const info = el("div", "prop-info");
      info.append(el("span", "chip", p.role));
      const start = el("input"); start.type = "text"; start.value = p.start_hms; start.dataset.start = "1";
      start.setAttribute("aria-label", "Inicio del tramo " + p.role); start.inputMode = "decimal";
      const facts = el("small", null, Math.round(p.kbps) + " kbps · SI " + p.si.toFixed(0) + " · TI " + p.ti.toFixed(0) + " · brillo " + p.yavg.toFixed(0));
      info.append(start, facts);
      row.append(pick, img, info);
      box.append(row);
    }
    syncAutoInputs();
  }
  function setAutoMessage(text, error) {
    const m = batchForm.querySelector("[data-auto-msg]");
    m.textContent = text;
    m.classList.toggle("errtext", !!error);
  }
  async function pollAnalysis() {
    clearTimeout(auto.timer);
    if (!auto.id) return;
    const n = Number(batchForm.querySelector("[data-auto-n]").value) || 3;
    try {
      const r = await fetch("/api/analisis/" + auto.id + "?n=" + n, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(String(r.status));
      const a = await r.json();
      const bar = batchForm.querySelector("[data-auto-bar]");
      bar.hidden = a.status === "done" || a.status === "error";
      bar.firstElementChild.style.setProperty("--w", ((a.progress || 0) * 100).toFixed(1) + "%");
      if (a.status === "done") {
        setAutoMessage(a.proposals.length + " tramo" + (a.proposals.length === 1 ? "" : "s") + " propuesto" + (a.proposals.length === 1 ? "" : "s") + ". Marca los que quieras y ajusta el inicio si hace falta.");
        renderProposals(a.proposals);
        return;
      }
      if (a.status === "error") { setAutoMessage("No se pudo analizar: " + a.error, true); return; }
      const names = { paquetes: "leyendo paquetes", keyframes: "analizando keyframes", candidatas: "midiendo candidatas" };
      setAutoMessage("Analizando (" + (names[a.phase] || "en cola") + ", " + Math.round((a.progress || 0) * 100) + " %)…");
    } catch (e) { /* servidor reiniciando: se reintenta */ }
    auto.timer = setTimeout(pollAnalysis, 2000);
  }
  async function runAnalysis() {
    const body = new FormData();
    body.set("file", batchForm.elements.file.value);
    body.set("duration", batchForm.elements.duration.value);
    setAutoMessage("Encolando el análisis…");
    batchForm.querySelector("[data-proposals]").replaceChildren();
    syncAutoInputs();
    try {
      const r = await fetch(batchForm.querySelector("[data-auto-panel]").dataset.analysisUrl, { method: "POST", body });
      if (!r.ok) throw new Error((await r.text()).replace(/<[^>]+>/g, " ").trim().slice(0, 160) || r.status);
      auto.id = (await r.json()).id;
      pollAnalysis();
    } catch (e) { setAutoMessage("No se pudo empezar: " + e.message, true); }
  }
  function setTramoMode(mode) {
    auto.mode = mode;
    batchForm.querySelector(".tramo-seg").dataset.mode = mode;
    batchForm.querySelectorAll("[data-tramo]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tramo === mode)));
    batchForm.querySelector("[data-auto-panel]").hidden = mode !== "auto";
    batchForm.querySelectorAll("[data-manual-only]").forEach((x) => { x.hidden = mode === "auto"; });
    syncAutoInputs();
  }
  if (batchForm && batchForm.querySelector("[data-tramo-mode]")) {
    batchForm.querySelector("[data-tramo-mode]").hidden = false;  // sin JS solo existe el modo manual
    batchForm.addEventListener("click", (e) => {
      const m = e.target.closest("[data-tramo]");
      if (m) setTramoMode(m.dataset.tramo);
      else if (e.target.closest("[data-auto-run]")) runAnalysis();
    });
    batchForm.addEventListener("change", (e) => {
      if (e.target.matches("[data-pick], [data-start]")) syncAutoInputs();
      else if (e.target.matches("[data-auto-n]") && auto.id) pollAnalysis();
      else if (e.target.matches('input[name="duration"]') && auto.id) {
        // Las ventanas del análisis son de la duración antigua: hay que analizar de nuevo.
        auto.id = null; clearTimeout(auto.timer);
        batchForm.querySelector("[data-proposals]").replaceChildren();
        batchForm.querySelector("[data-auto-bar]").hidden = true;
        setAutoMessage("Has cambiado la duración: vuelve a analizar el vídeo.");
        syncAutoInputs();
      }
    });
  }

  if (batchForm) {
    batchForm.addEventListener("input", (e) => {
      if (e.target.matches('input[name="qualities"], input[name="presets"], input[name="extra"]')) e.target.dataset.dirty = "1";
      updateEstimate();
    });
    batchForm.addEventListener("change", (e) => {
      if (!e.target.matches('select[name="encoder"]')) return;
      const d = e.target.selectedOptions[0].dataset;
      batchForm.querySelectorAll("[data-enc-info]").forEach((c) => { c.hidden = c.dataset.encInfo !== e.target.value; });
      for (const [hint, text] of [["quality", d.qualityHint], ["preset", d.presetHint], ["extras", d.extrasHint]]) {
        const h = batchForm.querySelector('[data-hint-for="' + hint + '"]');
        if (h) h.textContent = text;
      }
      // Los campos que el usuario no ha tocado toman los valores de partida del encoder.
      for (const [name, value] of [["qualities", d.qualityDefault], ["presets", d.presetDefault], ["extra", d.extrasDefault]]) {
        const input = batchForm.elements[name];
        if (input && !input.dataset.dirty) input.value = value;
      }
      updateEstimate();
    });
    updateEstimate();
  }

  // Los menús de la barra se colocan justo debajo de ella (su alto cambia en móvil).
  function barHeight() {
    const bar = document.querySelector(".topbar");
    if (bar) root.style.setProperty("--bar-h", Math.round(bar.getBoundingClientRect().height) + "px");
  }
  window.addEventListener("resize", barHeight);
  barHeight();

  syncPressed();
  schedule();
})();
