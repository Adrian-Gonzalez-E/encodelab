// Visor de comparación: cortina, lado a lado y alternar; zoom y arrastre sincronizados. Sin dependencias.
(() => {
  "use strict";
  const fig = document.querySelector("[data-cmp]");
  if (!fig) return;
  const stage = fig.querySelector(".cmp-stage");
  const handle = fig.querySelector(".cmp-handle");
  const state = { z: 1, x: 0, y: 0, pos: 50 };
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  function apply() {
    fig.style.setProperty("--z", state.z);
    fig.style.setProperty("--x", state.x + "px");
    fig.style.setProperty("--y", state.y + "px");
    fig.style.setProperty("--pos", state.pos + "%");
    handle.setAttribute("aria-valuenow", String(Math.round(state.pos)));
  }
  function limit() {
    const r = stage.querySelector(".cmp-pane").getBoundingClientRect();
    state.x = clamp(state.x, r.width * (1 - state.z), 0);
    state.y = clamp(state.y, r.height * (1 - state.z), 0);
  }
  function setMode(mode) {
    fig.dataset.mode = mode;
    fig.querySelectorAll("[data-mode-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.modeSet === mode)));
  }
  function zoomAt(z2, cx, cy) {
    const z1 = state.z;
    z2 = clamp(z2, 1, 16);
    state.x = cx - (cx - state.x) * (z2 / z1);
    state.y = cy - (cy - state.y) * (z2 / z1);
    state.z = z2;
    limit();
    apply();
  }
  function oneToOne() {
    const img = stage.querySelector("img");
    const r = stage.querySelector(".cmp-pane").getBoundingClientRect();
    // object-fit: contain deja la imagen a ancho completo si la proporción coincide: 1:1 = ancho natural / ancho visible.
    zoomAt(Math.max(1, img.naturalWidth / r.width), r.width / 2, r.height / 2);
  }

  fig.addEventListener("click", (e) => {
    const m = e.target.closest("[data-mode-set]");
    if (m) { setMode(m.dataset.modeSet); return; }
    const z = e.target.closest("[data-zoom]");
    if (z) { if (z.dataset.zoom === "fit") { state.z = 1; state.x = state.y = 0; apply(); } else oneToOne(); }
  });
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = stage.querySelector(".cmp-pane").getBoundingClientRect();
    zoomAt(state.z * (e.deltaY < 0 ? 1.25 : 0.8), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });

  // Arrastre: sobre el asa mueve la cortina; en el resto, la imagen (si hay zoom). Un clic sin mover alterna en «Alternar».
  let drag = null;
  stage.addEventListener("pointerdown", (e) => {
    drag = { id: e.pointerId, sx: e.clientX, sy: e.clientY, x: state.x, y: state.y, curtain: e.target === handle, moved: false };
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.curtain) {
      const r = stage.getBoundingClientRect();
      state.pos = clamp((e.clientX - r.left) / r.width * 100, 0, 100);
    } else if (state.z > 1) {
      state.x = drag.x + dx; state.y = drag.y + dy; limit();
    }
    apply();
  });
  stage.addEventListener("pointerup", (e) => {
    if (drag && !drag.moved && fig.dataset.mode === "flip") flip();
    drag = null;
  });
  function flip() { fig.dataset.show = fig.dataset.show === "b" ? "a" : "b"; }

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("select, input, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target === handle && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      state.pos = clamp(state.pos + (e.key === "ArrowLeft" ? -2 : 2), 0, 100); apply(); e.preventDefault(); return;
    }
    if (e.key === "1") setMode("slider");
    else if (e.key === "2") setMode("side");
    else if (e.key === "3") setMode("flip");
    else if (e.key === " ") { e.preventDefault(); setMode("flip"); flip(); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      const links = [...document.querySelectorAll(".cmp-frames a")];
      const i = links.findIndex((a) => a.hasAttribute("aria-current"));
      const next = links[i + (e.key === "ArrowLeft" ? -1 : 1)];
      if (next) location.href = next.href;
    }
  });
  apply();
})();
