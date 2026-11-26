"""Prueba e2e de la UI (v3) con Playwright (Chromium), contra e2e/fixture_server.py. Deja capturas en /out.

Tiempos del fixture desde el primer GET /: a los 8 s la prueba en curso pasa a "medir" (80 %), a los 35 s termina.
"""
import os
import re

from playwright.sync_api import expect, sync_playwright

# localhost (red compartida con el fixture) es contexto seguro, como el HTTPS de producción: navigator.clipboard existe.
BASE = os.environ.get("BASE", "http://localhost:8099")
OUT = os.environ.get("OUT", "/out")
REFRESHED = "!document.contains(window.__probe)"


def shot(page, name, full=True):
    page.screenshot(path=f"{OUT}/{name}.png", full_page=full)


def probe(page):
    page.evaluate("window.__probe = document.querySelector('main .tlist') || document.querySelector('main section')")


def no_hscroll(page, width, what):
    assert page.evaluate("document.documentElement.scrollWidth") <= width, f"scroll horizontal en {what} a {width} px"


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(BASE + "/")
        page.evaluate("window.__e2e = 1")

        # (r) Otra pestaña con Nueva batería a medio rellenar: los cambios de versión de la cola no deben borrarla.
        form_tab = ctx.new_page()
        form_tab.on("pageerror", lambda e: errors.append(str(e)))
        form_tab.goto(BASE + page.locator("#plus a").first.get_attribute("href"))
        form_tab.select_option('select[name="encoder"]', "hevc_qsv")
        form_tab.fill('input[name="qualities"]', "21,23")

        # (j) La píldora de cola arranca en 35 %; más tarde debe subir sin recargar.
        qpct = page.locator(".qpill .qpct")
        expect(qpct).to_have_text("35 %")
        # (v) Con la fila en curso abierta, su barra y su % avanzan en vivo (a los 4 s, 50 % sin cambiar de fase).
        live = page.locator(".view-panel .trow.status-running")
        live.locator("summary").click()
        expect(qpct).to_have_text("50 %", timeout=6000)
        expect(live.locator("summary [data-progress-pct]")).to_have_text("50 %")
        assert "50.0%" in live.locator(".tbody-detail [data-progress-for] i").get_attribute("style"), "barra grande congelada"
        live.locator("summary").click()
        page.wait_for_timeout(1200)
        shot(page, "topbar-1440", full=False)

        # (a) Panel por defecto; Baterías persiste tras recargar.
        expect(page.locator(".view-panel")).to_be_visible()
        expect(page.locator(".view-baterias")).to_be_hidden()
        page.click('[data-set-view="baterias"]')
        page.reload()
        expect(page.locator(".view-baterias")).to_be_visible()
        expect(page.locator(".view-panel")).to_be_hidden()
        page.click('[data-set-view="panel"]')
        page.evaluate("window.__e2e = 1")

        # (g) El refresco (al pasar a "medir", ~8 s) conserva <details> de error abierto y el foco.
        err = page.locator(".view-panel .trow.status-error")
        err.locator("summary").click()
        first_done = page.locator(".view-panel .trow.status-done").first
        first_done.locator("summary").click()
        first_done.locator(".copy").focus()
        probe(page)
        page.wait_for_function(REFRESHED, timeout=15000)
        assert err.evaluate("d => d.open"), "el refresco cerró el <details> del error"
        assert page.evaluate("document.activeElement.matches('.view-panel .copy')"), "el refresco perdió el foco"
        # (j) …y la píldora ya muestra el 80 % sin haber recargado.
        expect(qpct).to_have_text("80 %", timeout=6000)
        expect(page.locator(".view-panel .trow.status-running .steps .now")).to_contain_text("Medir")
        assert page.evaluate("window.__e2e") == 1, "la página se recargó"

        # (r) …tras ese cambio de versión, la pestaña del formulario conserva lo escrito y sus tarjetas siguen vivas.
        expect(form_tab.locator(".qpill .qpct")).to_have_text("80 %", timeout=6000)
        expect(form_tab.locator('input[name="qualities"]')).to_have_value("21,23")
        assert form_tab.locator('select[name="encoder"]').input_value() == "hevc_qsv", "se perdió el encoder elegido"
        form_tab.select_option('select[name="encoder"]', "libx265")
        expect(form_tab.locator('[data-enc-info="libx265"]')).to_be_visible()
        expect(form_tab.locator('[data-hint-for="quality"]')).to_contain_text("CRF")
        form_tab.close()

        # (k) La píldora despliega la cola completa.
        page.click(".qpill")
        expect(page.locator("#queue")).to_be_visible()
        expect(page.locator("#queue .qrow")).to_have_count(2)
        page.wait_for_timeout(400)  # fin de la animación de entrada
        shot(page, "queue-open-1440", full=False)
        page.keyboard.press("Escape")

        # (h) Con el gráfico fijo bajo la cabecera, el tooltip del punto más alto no queda tapado.
        page.evaluate("window.scrollTo(0, 1200)")
        page.wait_for_timeout(300)
        highest = min(page.locator(".pt").all(), key=lambda el: el.bounding_box()["y"])
        highest.hover()
        tip_top = page.locator(".chart-tip").bounding_box()["y"]
        bar = page.locator(".topbar").bounding_box()
        assert tip_top >= bar["y"] + bar["height"], "tooltip bajo la cabecera"
        page.evaluate("window.scrollTo(0, 0)")
        page.mouse.move(0, 0)

        # Capas del gráfico: umbral y ganador de serie; frontera y curvas se encienden y se recuerdan.
        chart = page.locator(".view-panel .chart-wrap")
        expect(chart.locator(".l-umbral")).to_be_visible()
        expect(chart.locator(".win-label")).to_be_visible()
        expect(chart.locator(".frontier")).to_be_hidden()
        expect(chart.locator('[data-layer="umbral"]')).to_have_attribute("aria-pressed", "true")
        chart.locator('[data-layer="frontera"]').click()
        chart.locator('[data-layer="curvas"]').click()
        expect(chart.locator(".frontier")).to_be_visible()
        expect(chart.locator(".curve").first).to_be_visible()
        chart.screenshot(path=f"{OUT}/chart-capas-1440.png")
        assert page.evaluate("localStorage.getItem('encodelab.layers')") == "umbral ganador frontera curvas"
        chart.locator('[data-layer="frontera"]').click()
        chart.locator('[data-layer="curvas"]').click()
        expect(chart.locator(".frontier")).to_be_hidden()

        # (b) Tooltip y resaltado; (f) Copiar.
        pt = page.locator(".pt").first
        pt.hover()
        expect(page.locator(".chart-tip")).to_be_visible()
        expect(page.locator(f'#t-{pt.get_attribute("data-id")}')).to_have_class(re.compile(r"\bhl\b"))
        page.wait_for_timeout(500)
        shot(page, "panel-cards-1440", full=False)
        copy = page.locator(".view-panel .trow[open] .copy").first
        copy.click()
        expect(copy).to_have_text("Copiado")

        # (c) Borrado con diálogo: Cancelar no borra, Borrar sí.
        card = page.locator(".view-panel .trow.status-done").last
        cid = card.get_attribute("data-id")
        card.locator("summary").click()
        card.locator("button.trash").click()
        expect(page.locator("#confirm")).to_be_visible()
        page.click('#confirm button[value="cancel"]')
        expect(page.locator(f"#t-{cid}")).to_have_count(1)
        if not page.locator(f"#t-{cid}").evaluate("d => d.open"):
            page.locator(f"#t-{cid} summary").click()
        page.locator(f"#t-{cid} button.trash").click()
        page.click('#confirm button[value="ok"]')
        page.wait_for_load_state()
        expect(page.locator(f"#t-{cid}")).to_have_count(0)
        page.evaluate("window.__e2e = 1")

        # (p) + (d) + (o): con la cola abierta, la prueba termina (~35 s): la cola sigue abierta, la tarjeta pasa a
        # hecha, el selector de escena actualiza sus cifras y desaparece data-pending, todo sin recargar.
        # (t) Modo tabla: se ve la tabla, se ordena por % del original y pasar por una fila resalta su punto.
        page.locator('[data-set-tests="table"]').click()
        expect(page.locator(".view-panel .tablewrap-panel")).to_be_visible()
        expect(page.locator(".view-panel .tlist")).to_be_hidden()
        rows = page.locator(".view-panel .tablewrap-panel tbody tr")
        before = rows.first.get_attribute("data-id")
        page.click('.view-panel .tablewrap-panel th[data-sort="ratio"]')
        sorted_first = rows.first.get_attribute("data-id")
        assert sorted_first != before, "la tabla no se ordenó"
        rows.first.hover()
        expect(page.locator(".pt.hl")).to_have_count(1)
        page.wait_for_timeout(300)
        shot(page, "panel-table-1440")
        page.mouse.move(0, 0)

        running = page.locator(".view-panel .trow.status-running")
        expect(running).to_have_count(1)
        page.click(".qpill")
        expect(page.locator("#queue")).to_be_visible()
        expect(running).to_have_count(0, timeout=40000)
        assert page.evaluate("document.querySelector('#queue').matches(':popover-open')"), "el refresco cerró la cola"
        expect(page.locator("#sheet .figures")).not_to_contain_text("en cola", timeout=8000)
        # (t) …tras el refresco la tabla conserva su orden; y la elección sobrevive a recargar.
        assert rows.first.get_attribute("data-id") == sorted_first, "el refresco deshizo el orden"
        expect(page.locator("main[data-pending]")).to_have_count(0, timeout=8000)
        expect(page.locator(".qpill")).to_contain_text("Cola vacía", timeout=8000)
        assert page.evaluate("window.__e2e") == 1, "la página se recargó"
        page.keyboard.press("Escape")
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-tests", "table")
        page.locator('[data-set-tests="cards"]').click()
        expect(page.locator(".view-panel .tlist")).to_be_visible()
        page.click('[data-set-view="baterias"]')
        page.locator('[data-set-tests="table"]').click()
        expect(page.locator(".view-baterias .cmp").first).to_be_visible()
        # Cabecera y filas comparten columnas (un display: grid sobre table.cmp las desalineaba).
        assert page.evaluate("""(() => { const t = document.querySelector('.view-baterias table.cmp');
            const x = r => [...r.cells].map(c => Math.round(c.getBoundingClientRect().left));
            return getComputedStyle(t).display === 'table' && x(t.tHead.rows[0]).join() === x(t.tBodies[0].rows[0]).join(); })()"""), \
            "columnas de la tabla desalineadas"
        shot(page, "batch-table-1440", full=False)
        page.locator('[data-set-tests="cards"]').click()
        page.click('[data-set-view="panel"]')

        # (s) Paleta de escenas: clic, buscar, Enter; Ctrl+K, sin resultados, Esc.
        page.click(".scene-pick")
        expect(page.locator("#scenes")).to_be_visible()
        assert page.evaluate("document.activeElement.matches('.palette-input')"), "el buscador no tiene el foco"
        page.keyboard.type("e02")
        expect(page.locator("#scenes [role=option]:visible")).to_have_count(1)
        page.wait_for_timeout(300)
        shot(page, "palette-1440", full=False)
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r"escena="))
        expect(page.locator("#sheet h1")).to_contain_text("S01E02")
        page.goto(BASE + "/")
        page.keyboard.press("Control+k")
        expect(page.locator("#scenes")).to_be_visible()
        page.keyboard.type("zzz")
        expect(page.locator("#scenes .palette-empty")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#scenes")).to_be_hidden()

        # (u) Ajustes: medidores por umbrales con «aceptable desde» 85.
        page.click('button[popovertarget="settings"]')
        page.select_option('#settings select[name="card_style"]', "gauges")
        page.fill('#settings input[name="vmaf_warn"]', "85")
        page.click('#settings button[type="submit"]')
        page.wait_for_load_state()
        row = page.locator(".view-panel .trow.status-done").first
        row.locator("summary").click()
        expect(row.locator(".detail-gauges")).to_be_visible()
        page.wait_for_timeout(300)
        shot(page, "row-open-gauges-1440", full=False)
        row.locator('.detail-gauges button[popovertarget="m-vmaf_p5"]').click()
        expect(page.locator("#m-vmaf_p5")).to_contain_text("0–85")
        page.keyboard.press("Escape")

        # Popover de una métrica.
        page.locator('.view-panel button[popovertarget="m-vmaf_p5"]').first.click()
        expect(page.locator("#m-vmaf_p5")).to_be_visible()
        page.keyboard.press("Escape")

        # (n) Ajustes: refresco 5 s y umbral 99 → guardar → persisten; luego se restauran.
        page.click('button[popovertarget="settings"]')
        expect(page.locator("#settings")).to_be_visible()
        page.wait_for_timeout(400)
        shot(page, "settings-1440", full=False)
        page.select_option('#settings select[name="refresh_s"]', "5")
        page.fill('#settings input[name="best_vmaf"]', "99")
        page.click('#settings button[type="submit"]')
        page.wait_for_load_state()
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-refresh", "5")
        expect(page.locator("#sheet .figures")).to_contain_text("ninguna ≥ 99")
        page.click('button[popovertarget="settings"]')
        page.select_option('#settings select[name="refresh_s"]', "2")
        page.fill('#settings input[name="vmaf_warn"]', "80")
        page.fill('#settings input[name="best_vmaf"]', "93")
        page.select_option('#settings select[name="card_style"]', "grid")
        page.click('#settings button[type="submit"]')
        page.wait_for_load_state()

        # (l) ＋ → Añadir pruebas a esta escena → /new con el aviso de misma escena.
        page.click("button.plus")
        page.click("#plus >> text=Añadir pruebas a esta escena")
        page.wait_for_load_state()
        expect(page.locator(".notice")).to_contain_text("Misma escena")

        # (m) Tarjetas de encoder: al elegir av1_qsv cambian pistas y extras; la estimación muestra texto.
        page.select_option('select[name="encoder"]', "av1_qsv")
        expect(page.locator('[data-enc-info="av1_qsv"]')).to_be_visible()
        expect(page.locator('[data-enc-info="libsvtav1"]')).to_be_hidden()
        expect(page.locator('[data-hint-for="quality"]')).to_contain_text("ICQ")
        expect(page.locator('input[name="extra"]')).to_have_value("-pix_fmt p010le")
        assert page.locator(".estimate").inner_text().strip(), "estimación vacía"
        page.wait_for_timeout(800)
        shot(page, "new-1440")

        page.goto(BASE + "/metricas")
        page.wait_for_timeout(800)
        shot(page, "metricas-1440")

        # (i) A 1000 px las tarjetas no se desbordan; (q) sin scroll horizontal en / y /new.
        mid = browser.new_context(viewport={"width": 1000, "height": 900})
        md = mid.new_page()
        md.goto(BASE + "/")
        md.wait_for_timeout(1200)
        md.locator(".view-panel .trow.status-done summary").first.click()
        overflow = md.evaluate("[...document.querySelectorAll('.view-panel .trow')]"
                               ".filter(c => c.scrollWidth > c.clientWidth + 1).length")
        assert overflow == 0, f"{overflow} tarjetas desbordadas a 1000 px"
        no_hscroll(md, 1000, "Panel")
        md.locator('[data-set-tests="table"]').click()
        no_hscroll(md, 1000, "Panel en tabla")
        shot(md, "panel-1000")
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mp = phone.new_page()
        mp.on("pageerror", lambda e: errors.append(str(e)))
        mp.goto(BASE + "/")
        mp.wait_for_timeout(1200)
        mp.locator(".view-panel .trow.status-done summary").first.click()
        no_hscroll(mp, 390, "Panel")
        mp.locator('[data-set-tests="table"]').click()
        no_hscroll(mp, 390, "Panel en tabla")
        mp.locator('[data-set-tests="cards"]').click()
        shot(mp, "panel-390")
        mp.locator(".chart-wrap").screenshot(path=f"{OUT}/chart-390.png")
        mp.click('[data-set-view="baterias"]')
        mp.wait_for_timeout(800)
        no_hscroll(mp, 390, "Baterías")
        shot(mp, "baterias-390")
        mp.click('[data-set-view="panel"]')
        mp.click("button.plus")
        mp.click("#plus >> text=Añadir pruebas a esta escena")
        mp.wait_for_load_state()
        mp.wait_for_timeout(800)
        no_hscroll(mp, 390, "Nueva batería")
        shot(mp, "new-390")
        # (w) Visor de comparación: cortina, lado a lado, alternar, zoom y teclado.
        page.goto(BASE + "/")
        page.locator("a", has_text="Comparar visualmente").first.click()
        page.wait_for_load_state()
        stage = page.locator(".cmp-stage")
        expect(stage).to_be_visible()
        cmp_ = page.locator("[data-cmp]")
        assert cmp_.get_attribute("data-mode") == "slider"
        box = stage.bounding_box()
        h = page.locator(".cmp-handle").bounding_box()
        page.mouse.move(h["x"] + h["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.25, box["y"] + box["height"] / 2, steps=4)
        page.mouse.up()
        pos = float(page.evaluate("getComputedStyle(document.querySelector('[data-cmp]')).getPropertyValue('--pos')").strip("% "))
        assert 15 < pos < 35, f"cortina en {pos}"
        shot(page, "compare-slider", full=False)
        page.click('[data-mode-set="side"]')
        a, b = page.locator(".cmp-a").bounding_box(), page.locator(".cmp-b").bounding_box()
        assert b["x"] > a["x"] + a["width"] - 2, "no están lado a lado"
        shot(page, "compare-side", full=False)
        page.click('[data-mode-set="flip"]')
        before = cmp_.get_attribute("data-show")
        page.keyboard.press("Space")
        assert cmp_.get_attribute("data-show") != before
        page.keyboard.press("1")
        assert cmp_.get_attribute("data-mode") == "slider"
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, -300)
        assert float(page.evaluate("getComputedStyle(document.querySelector('[data-cmp]')).getPropertyValue('--z')")) > 1
        page.click('[data-zoom="fit"]')
        first_url = page.url
        assert page.locator('select[name="escena"] option').count() >= 1, "el visor no deja elegir tramo"
        page.keyboard.press("ArrowRight")
        page.wait_for_url(re.compile(r"f=250"))
        assert page.url != first_url
        # Entre pruebas: la lista de la escena enlaza a su comparación con miniaturas.
        page.goto(BASE + "/")
        # (el refresco en vivo cierra los <details> mientras hay cola: se comprueba el DOM)
        assert page.locator(".view-panel .trow.status-done .thumbs img").count() >= 2, "faltan miniaturas en las pruebas"
        # (x) Tramos automáticos: analizar, tres propuestas, desmarcar una y encolar el grupo.
        page.goto(BASE + "/")
        page.locator(".view-panel .scene-sheet a", has_text="Añadir pruebas").click()
        page.wait_for_load_state()
        expect(page.locator("[data-auto-panel]")).to_be_hidden()
        bm, ba = page.locator('[data-tramo="manual"]').bounding_box(), page.locator('[data-tramo="auto"]').bounding_box()
        assert abs(bm["y"] - ba["y"]) < 4 and ba["x"] > bm["x"], "el switch Manual/Automático no es horizontal"
        page.click('[data-tramo="auto"]')
        page.click("[data-auto-run]")
        expect(page.locator(".prop")).to_have_count(3, timeout=8000)
        expect(page.locator("[data-submit]")).to_have_text("Encolar 3 tramos")
        assert "3 tramos ×" in page.locator(".estimate").inner_text()
        assert page.locator(".prop img").first.evaluate("i => i.naturalWidth") > 0, "miniatura sin cargar"
        shot(page, "new-auto")
        page.locator(".prop [data-pick]").nth(2).uncheck()
        expect(page.locator("[data-submit]")).to_have_text("Encolar 2 tramos")
        page.click("[data-submit]")
        page.wait_for_url(re.compile(r"/grupos/\d+"))
        expect(page.locator(".group-tramos tbody tr")).to_have_count(2)
        shot(page, "grupo")
        # (y) Tramos del grupo: hermanos en la barra y apartado propio en la paleta.
        page.locator(".group-tramos a", has_text="Escena").first.click()
        page.wait_for_load_state()
        sibs = page.locator("#scene-sibs a.chip")
        expect(sibs).to_have_count(2)
        expect(page.locator("#scene-sibs [aria-current]")).to_have_count(1)
        group_url = page.url
        page.locator("#scene-sibs a.chip:not([aria-current])").click()
        page.wait_for_url(lambda u: u != group_url)
        expect(page.locator("#scene-sibs [aria-current]")).to_have_count(1)
        page.click(".scene-pick")
        grp = page.locator("#scenes .pgroup:not(.loose)").first
        expect(grp.locator(".pghead")).to_contain_text("Grupo")
        expect(grp.locator("[role=option]")).to_have_count(2)
        expect(page.locator("#scenes .pgroup.loose .pghead")).to_have_text("Sueltas")
        shot(page, "palette-grupos-1440", full=False)
        page.fill(".palette-input", "grupo")
        expect(page.locator("#scenes .pgroup.loose")).to_be_hidden()
        expect(grp).to_be_visible()
        page.keyboard.press("Escape")
        for width in (1440, 1000, 390):
            pw = browser.new_context(viewport={"width": width, "height": 844})
            wp = pw.new_page()
            wp.goto(page.url)
            no_hscroll(wp, width, "barra con tramos hermanos")
            shot(wp, f"hermanos-{width}", full=False)
            pw.close()
        for width in (390,):
            ph = browser.new_context(viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True)
            pp = ph.new_page()
            pp.on("pageerror", lambda e: errors.append(str(e)))
            pp.goto(page.url)
            no_hscroll(pp, width, "grupo")
            pp.goto(first_url)
            no_hscroll(pp, width, "comparar")
            shot(pp, "compare-390")
        browser.close()
    assert not errors, f"errores en la consola: {errors}"
    print("E2E OK")


if __name__ == "__main__":
    main()
