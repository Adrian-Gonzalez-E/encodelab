import json
import os
import sqlite3
import threading
import time

import pytest

from encodelab import db, media, settings
from encodelab.glossary import BY_KEY, ENCODER_INFO
from encodelab.web import best_by_column, create_app, hms, parse_time, worker_loop

VALID = {"file": "s/e01.mkv", "start": "600", "duration": "20", "encoder": "libsvtav1", "qualities": "28,32",
         "presets": "6", "extra": ""}
INFO = {"codec": "h264", "width": 1920, "height": 1080, "fps": 24.0, "field_order": "progressive",
        "interlaced": False, "pix_fmt": "yuv420p", "duration_s": 2640.0}


@pytest.fixture
def info(monkeypatch):
    data = dict(INFO)
    monkeypatch.setattr(media, "probe_source", lambda src: data)
    monkeypatch.setattr(media, "segment_kbps", lambda src, start, dur: 20000.0)
    monkeypatch.setattr(media, "ffmpeg_version", lambda: "ffmpeg version test")
    return data


@pytest.fixture
def client(tmp_path, info):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "e01.mkv").touch()
    (tmp_path / "s" / "notas.txt").touch()
    return make_client(tmp_path, True)


def make_client(tmp_path, qsv_ok):
    app = create_app({"MEDIA_ROOT": str(tmp_path), "DB_PATH": str(tmp_path / "t.db"), "WORK_DIR": str(tmp_path),
                      "VMAF_THREADS": 1, "START_WORKER": False,
                      "QSV_ENCODERS": ["av1_qsv", "hevc_qsv"] if qsv_ok else [],
                      "QSV_REASON": "ok" if qsv_ok else "sin dispositivo /dev/dri/renderD128"})
    return app.test_client()


@pytest.fixture
def client_noqsv(client, tmp_path):
    return make_client(tmp_path, False)  # misma base de datos y medios que client


def test_new_rejects_traversal(client):
    assert client.get("/new?file=../../etc/passwd").status_code == 400


def test_new_page_warns_interlaced(client, info):
    assert "Fuente entrelazada" not in client.get("/new?file=s/e01.mkv").get_data(as_text=True)
    info["interlaced"] = True
    assert "Fuente entrelazada" in client.get("/new?file=s/e01.mkv").get_data(as_text=True)


def test_browse_lists_dirs_and_videos(client):
    root = client.get("/browse").get_data(as_text=True)
    assert "s/" in root
    page = client.get("/browse?path=s").get_data(as_text=True)
    assert "e01.mkv" in page and "notas.txt" not in page
    assert client.get("/browse?path=s/e01.mkv").status_code == 400


def test_post_batch_creates_tests(client):
    r = client.post("/batches", data=VALID)
    assert r.status_code == 303 and "/batches/1" in r.headers["Location"]
    page = client.get("/").get_data(as_text=True)
    assert page.count("libsvtav1") >= 2 and "GB vídeo/capítulo" in page  # la estimación excluye audio y subtítulos
    assert "data-pending" in client.get("/batches/1").get_data(as_text=True)


def test_post_batch_rejects_bad_input(client):
    for bad in ({"duration": "500"}, {"encoder": "libaom-av1"}, {"extra": "-i /etc/passwd"}, {"start": "99999"},
                {"start": "nan"}, {"qualities": ""}, {"file": "s/notas.txt"}):
        assert client.post("/batches", data={**VALID, **bad}).status_code == 400, bad


def test_post_batch_rejects_cross_site(client):
    for site in ("same-site", "cross-site"):
        assert client.post("/batches", data=VALID, headers={"Sec-Fetch-Site": site}).status_code == 403
    assert client.post("/batches", data=VALID, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 303


def run_worker(path, tmp_path, conn, batch_id, runner):
    """Arranca worker_loop hasta vaciar la cola de la batería (máx. 5 s) y devuelve sus pruebas."""
    stop = threading.Event()
    t = threading.Thread(target=worker_loop, args=(path, tmp_path, tmp_path, 1, stop),
                         kwargs={"runner": runner, "idle_s": 0.05})
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(r["status"] in ("queued", "running")
                                              for r in db.batch_tests(conn, batch_id)):
        time.sleep(0.05)
    stop.set()
    t.join(2)
    return db.batch_tests(conn, batch_id)


def test_worker_survives_db_error(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    b = db.create_batch(conn, source_path="s/e01.mkv", start_s=0, duration_s=2)
    db.add_tests(conn, b, "libsvtav1", "", [(28, "6")])
    real, calls = db.claim_next, []

    def flaky(c):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(c)

    monkeypatch.setattr(db, "claim_next", flaky)
    rows = run_worker(path, tmp_path, conn, b, lambda *args, **kwargs: {"kbps": 1.0})
    assert [r["status"] for r in rows] == ["done"]


def test_worker_continues_after_error(tmp_path):
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    b = db.create_batch(conn, source_path="s/e01.mkv", start_s=0, duration_s=2)
    db.add_tests(conn, b, "libsvtav1", "", [(28, "6"), (32, "6")])
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise media.EncodeError("boom")
        return {"kbps": 1.0, "vmaf_mean": 90.0}

    rows = run_worker(path, tmp_path, conn, b, runner)
    assert [r["status"] for r in rows] == ["error", "done"] and "boom" in rows[0]["error"]
    assert calls[0][0] == tmp_path / "s/e01.mkv" and calls[0][6] == []


ARGS = "-c:v libsvtav1 -crf 28 -preset 6"


def seed(client, file="s/e01.mkv", qualities="28,32"):
    """Crea una batería por POST y termina su primera prueba; devuelve (batch_id, conexión)."""
    r = client.post("/batches", data={**VALID, "file": file, "qualities": qualities})
    batch_id = int(r.headers["Location"].rsplit("/", 1)[-1])
    conn = db.connect(client.application.config["DB_PATH"])
    first = db.batch_tests(conn, batch_id)[0]
    db.finish_test(conn, first["id"], {"kbps": 4000.0, "vmaf_mean": 95.0, "vmaf_p5": 92.0, "enc_fps": 30.0,
                                       "ffmpeg_args": ARGS})
    return batch_id, conn


def page(client, url="/"):
    return client.get(url).get_data(as_text=True)


def test_results_empty_db(client):
    html = page(client)
    assert "Aún no hay pruebas." in html and '<svg class="chart"' not in html


def test_results_has_both_views(client):
    seed(client)
    html = page(client)
    for part in ("view-panel", "view-baterias", '<svg class="chart"', f'data-copy="{ARGS}"', 'id="m-vmaf_p5"',
                 'popovertarget="m-ssim"', 'data-set-view="baterias"', 'id="confirm"',
                 'data-layers="umbral ganador"', 'data-layer="frontera"', 'class="l-umbral"'):
        assert part in html, part


def test_running_test_has_no_trash(client):
    b, conn = seed(client)
    running = db.claim_next(conn)
    html = page(client)
    assert f'id="t-{running["id"]}"' in html and f'action="/tests/{running["id"]}/delete"' not in html
    done = [t for t in db.batch_tests(conn, b) if t["status"] == "done"][0]
    assert f'action="/tests/{done["id"]}/delete"' in html


def test_pending_flag(client):
    b, conn = seed(client)
    assert "data-pending" in page(client)
    for t in db.batch_tests(conn, b):
        db.finish_test(conn, t["id"], {"kbps": 1.0})
    assert "data-pending" not in page(client) and "data-pending" not in page(client, f"/batches/{b}")


def test_results_bad_escena_falls_back(client):
    seed(client)
    for q in ("999", "abc"):
        assert client.get(f"/?escena={q}").status_code == 200


def test_results_escena_selects_scene(client, tmp_path):
    (tmp_path / "s" / "e02.mkv").touch()
    b1, conn = seed(client, "s/e01.mkv")
    b2, _ = seed(client, "s/e02.mkv")
    t1 = [t["id"] for t in db.batch_tests(conn, b1)]
    t2 = [t["id"] for t in db.batch_tests(conn, b2)]

    def panel(url):
        return page(client, url).split("view-baterias")[0]

    assert all(f'id="t-{i}"' in panel(f"/?escena={b1}") for i in t1)
    assert not any(f'id="t-{i}"' in panel(f"/?escena={b1}") for i in t2)
    assert all(f'id="t-{i}"' in panel("/") for i in t2)  # por defecto, la escena más reciente


def test_delete_routes(client):
    b, conn = seed(client, qualities="28,32,36")
    running = db.claim_next(conn)
    queued = [t for t in db.batch_tests(conn, b) if t["status"] == "queued"][0]
    assert client.post(f"/tests/{running['id']}/delete").status_code == 409
    assert client.post(f"/tests/{queued['id']}/delete", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(f"/tests/{queued['id']}/delete").status_code == 303
    assert queued["id"] not in [t["id"] for t in db.batch_tests(conn, b)]
    assert client.post("/tests/999/delete").status_code == 404
    assert client.post("/batches/999/delete").status_code == 404
    assert client.post(f"/batches/{b}/delete").status_code == 303
    assert [t["id"] for t in db.batch_tests(conn, b)] == [running["id"]]  # la que está en curso se queda


def test_delete_next_is_internal_only(client):
    for nxt, loc in (("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil.com", "/"), ("/\t/evil.com", "/"),
                     ("/\n/evil.com", "/"), ("/?sort=ratio", "/?sort=ratio")):
        b, conn = seed(client)
        done = [t for t in db.batch_tests(conn, b) if t["status"] == "done"][0]
        r = client.post(f"/tests/{done['id']}/delete", data={"next": nxt})
        assert r.status_code == 303 and r.headers["Location"] == loc, nxt


def test_delete_last_test_from_batch_page_goes_home(client):
    b, conn = seed(client, qualities="28")
    t = db.batch_tests(conn, b)[0]
    r = client.post(f"/tests/{t['id']}/delete", data={"next": f"/batches/{b}"})
    assert r.headers["Location"] == "/"


def test_metricas_page(client):
    html = page(client, "/metricas")
    assert all(d["name"] in html for d in BY_KEY.values())


def test_delete_keeps_scene(client, tmp_path):
    (tmp_path / "s" / "e02.mkv").touch()
    b1, conn = seed(client, "s/e01.mkv")
    seed(client, "s/e02.mkv")
    b3, _ = seed(client, "s/e01.mkv")  # misma escena que b1; el selector usa su id (el más reciente)
    for t in db.batch_tests(conn, b3)[:-1]:
        client.post(f"/tests/{t['id']}/delete", data={"next": f"/?escena={b3}"})
    last = db.batch_tests(conn, b3)[-1]
    r = client.post(f"/tests/{last['id']}/delete", data={"next": f"/?escena={b3}&sort=ratio"})
    assert r.headers["Location"] == f"/?escena={b1}&sort=ratio"  # sigue en la misma escena
    r = client.post(f"/batches/{b1}/delete", data={"next": f"/?escena={b1}"})
    assert r.headers["Location"] == "/"  # escena vacía: vuelve a la escena por defecto


def test_saving_sign_when_bigger(client):
    b, conn = seed(client)
    t = [t for t in db.batch_tests(conn, b) if t["status"] == "queued"][0]
    db.finish_test(conn, t["id"], {"kbps": 25400.0, "vmaf_mean": 99.0, "vmaf_p5": 98.0, "enc_fps": 5.0})
    html = page(client)
    assert "+27 %" in html and "−-" not in html


def test_new_page_fact_cards(client):
    assert page(client, "/new?file=s/e01.mkv").count('class="kpi glass"') >= 5


def test_card_keeps_v1_figures(client):
    b, conn = seed(client)
    panel = page(client).split("view-baterias")
    assert "<dd>95.00</dd>" in panel[0] and f'href="/batches/{b}"' in panel[0]  # VMAF con 2 decimales y enlace a la batería
    assert "0:10:00–0:10:20" in panel[1]


def test_worker_records_progress(tmp_path):
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    b = db.create_batch(conn, source_path="s/e01.mkv", start_s=0, duration_s=2, fps=24.0)
    db.add_tests(conn, b, "libsvtav1", "", [(28, "6")])
    seen = []

    def runner(*args, on_progress=None, **kwargs):
        on_progress("metrics", 0.8, 40)
        c = db.connect(path)  # este runner corre en el hilo del worker: conexión propia
        t = db.get_test(c, db.batch_tests(c, b)[0]["id"])
        seen.append((t["progress"], t["phase"], t["frame"], t["frames_total"]))
        return {"kbps": 1.0}

    rows = run_worker(path, tmp_path, conn, b, runner)
    assert seen == [(0.8, "metrics", 40, 48)] and rows[0]["status"] == "done"


def test_worker_progress_then_error(tmp_path):
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    b = db.create_batch(conn, source_path="s/e01.mkv", start_s=0, duration_s=2, fps=24.0)
    db.add_tests(conn, b, "libsvtav1", "", [(28, "6")])

    def runner(*args, on_progress=None, **kwargs):
        on_progress("encode", 0.3, 10)
        raise media.EncodeError("boom a mitad")

    rows = run_worker(path, tmp_path, conn, b, runner)
    assert rows[0]["status"] == "error" and "boom a mitad" in rows[0]["error"]
    assert db.queue_state(conn, time.time())["running"] == []


def test_new_prefill_from_query(client):
    html = page(client, "/new?file=s/e01.mkv&start=600&duration=20")
    assert 'name="start" value="0:10:00"' in html and 'name="duration" value="0:00:20"' in html


def test_new_defaults_from_settings(client):
    conn = db.connect(client.application.config["DB_PATH"])
    db.save_settings(conn, {"default_duration": "30", "default_encoder": "av1_qsv"})
    html = page(client, "/new?file=s/e01.mkv")
    assert 'name="duration" value="0:00:30"' in html and 'value="av1_qsv" selected' in html
    assert 'name="extra" value="-pix_fmt p010le"' in html  # extras por encoder de los ajustes


def test_new_same_scene_notice(client):
    seed(client)
    assert "Misma escena que 2 pruebas" in page(client, "/new?file=s/e01.mkv&start=600&duration=20")
    assert "Misma escena" not in page(client, "/new?file=s/e01.mkv&start=10&duration=20")


def test_new_encoder_cards(client):
    html = page(client, "/new?file=s/e01.mkv")
    assert html.count('data-enc-info=') == 4 and all(e["title"] in html for e in ENCODER_INFO.values())
    assert '<select name="encoder"' in html and html.count('data-enc-info="libsvtav1"') == 1
    assert 'data-enc-info="av1_qsv" hidden' in html  # solo se ve la del encoder elegido
    assert "data-speeds=" in html and 'data-frames="480"' in html  # 20 s a 24 fps


def test_new_bad_start_param(client):
    html = page(client, "/new?file=s/e01.mkv&start=abc&duration=nan")
    assert 'name="start" value="0:13:12"' in html and 'name="duration" value="0:00:20"' in html


SETTINGS_OK = {"default_duration": "25", "default_start_pct": "40", "default_encoder": "libx265",
               "extras_libsvtav1": "-pix_fmt yuv420p10le", "extras_av1_qsv": "-pix_fmt p010le", "extras_libx265": "",
               "extras_hevc_qsv": "", "best_vmaf": "93", "default_view": "panel", "refresh_s": "2", "animations": "1",
               "vmaf_warn": "80", "card_style": "grid", "background": "particulas"}


def test_topbar_scene_pill(client):
    seed(client)
    html = page(client)
    assert 'class="scene-pick"' in html and "e01.mkv · 0:10:00" in html and "1 hecha" in html
    assert "mejor ahorro" in html and 'id="scenes"' in html and 'id="sorts"' in html


def test_queue_pill_states(client):
    html = page(client)
    assert "✓ Cola vacía" in html
    b, conn = seed(client)
    r = db.claim_next(conn)
    db.set_progress(conn, r["id"], "metrics", 0.63, 300, 480)
    html = page(client)
    assert "63 %" in html and f'data-queue-id="{r["id"]}"' in html and "✓ Cola vacía" not in html


def test_plus_menu_prefill_link(client):
    seed(client)
    with client.application.test_request_context():
        from flask import url_for
        href = url_for("new", file="s/e01.mkv", start=600.0, duration=20.0)
    html = page(client)
    assert "Añadir pruebas a esta escena" in html and "Nueva batería con otro archivo" in html
    assert f'href="{href.replace("&", "&amp;")}"' in html


def test_settings_save_and_validate(client):
    conn = db.connect(client.application.config["DB_PATH"])
    r = client.post("/ajustes", data={**SETTINGS_OK, "next": "/metricas"})
    assert r.status_code == 303 and r.headers["Location"] == "/metricas"
    assert settings.load(conn)["default_encoder"] == "libx265" and settings.load(conn)["default_duration"] == 25
    bad = client.post("/ajustes", data={**SETTINGS_OK, "default_duration": "60", "extras_libsvtav1": "-i /x"})
    assert bad.status_code == 400 and settings.load(conn)["default_duration"] == 25  # nada del POST inválido
    assert client.post("/ajustes", data=SETTINGS_OK, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_settings_affect_pages(client):
    seed(client)
    client.post("/ajustes", data={**SETTINGS_OK, "default_view": "baterias", "refresh_s": "5", "best_vmaf": "99",
                                  "animations": ""})
    html = page(client)
    assert 'data-view="baterias"' in html and 'data-refresh="5"' in html and 'class="no-motion"' in html
    assert "ninguna ≥ 99" in html
    assert 'name="default_duration" value="25"' in html  # el panel de ajustes muestra lo guardado


def test_api_queue_json(client):
    r = client.get("/api/queue")
    assert r.status_code == 200 and r.get_json()["pending"] == 0 and r.get_json()["running"] == []
    b, conn = seed(client)
    q = client.get("/api/queue").get_json()
    assert q["pending"] == 1 and q["queued"][0]["label"] == "libsvtav1 32·6"


def test_running_card_progress(client):
    b, conn = seed(client)
    r = db.claim_next(conn)
    db.set_progress(conn, r["id"], "encode", 0.5, 240, 480)
    html = page(client)
    assert f'data-progress-for="{r["id"]}"' in html and "--w: 50" in html and "fotograma 240 / 480" in html


def test_header_marks_swappable(client):
    seed(client)
    html = page(client)
    assert 'id="scene-pick"' in html and html.count("data-swap") >= 2


def test_worker_progress_write_failure_is_ignored(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    b = db.create_batch(conn, source_path="s/e01.mkv", start_s=0, duration_s=2, fps=24.0)
    db.add_tests(conn, b, "libsvtav1", "", [(28, "6")])

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "set_progress", locked)

    def runner(*args, on_progress=None, **kwargs):
        on_progress("encode", 0.5, 10)
        return {"kbps": 1.0}

    assert run_worker(path, tmp_path, conn, b, runner)[0]["status"] == "done"


def test_live_marker_only_on_live_pages(client):
    b, conn = seed(client)
    assert "data-live" in page(client) and "data-live" in page(client, f"/batches/{b}")
    for url in ("/new?file=s/e01.mkv", "/browse", "/metricas"):
        assert "data-live" not in page(client, url), url


def test_best_by_column():
    rows = [dict(status="done", vmaf_mean=94.0, vmaf_p5=90.0, vmaf_min=88.0, vmaf_neg_mean=93.0, psnr_y=41.0, ssim=0.98,
                 ms_ssim=0.985, enc_fps=30.0, ratio=0.5, kbps=2000.0, est_gb=0.7, est_hours=0.6),
            dict(status="done", vmaf_mean=99.0, vmaf_p5=None, vmaf_min=95.0, vmaf_neg_mean=None, psnr_y=None, ssim=None,
                 ms_ssim=None, enc_fps=5.0, ratio=1.27, kbps=5000.0, est_gb=1.8, est_hours=3.0),
            dict(status="queued", vmaf_mean=None, vmaf_p5=None, vmaf_min=None, vmaf_neg_mean=None, psnr_y=None,
                 ssim=None, ms_ssim=None, enc_fps=None, ratio=None, kbps=None, est_gb=None, est_hours=None)]
    b = best_by_column(rows)
    assert (b["vmaf_mean"], b["vmaf_p5"], b["ratio"], b["enc_fps"], b["est_hours"]) == (99.0, 90.0, 0.5, 30.0, 0.6)
    assert best_by_column([]) == {}


def test_popover_uses_thresholds(client):
    client.post("/ajustes", data={**SETTINGS_OK, "vmaf_warn": "85", "card_style": "grid"})
    html = page(client)
    assert "0–85" in html and "≥ 93" in html


def pill(html):
    return html.split('class="scene-pick"', 1)[1].split("</button>", 1)[0]


def test_scene_sheet(client):
    seed(client)
    html = page(client)
    sheet = html.split('id="sheet"', 1)[1].split("</section>", 1)[0]
    for part in ("h264", "1920×1080", "original 20000 kbps", "Mejor VMAF", "Más rápida", "Añadir pruebas", "1 hecha"):
        assert part in sheet, part
    assert "hecha" not in pill(html) and "⌘K" in pill(html)


def test_rows_and_detail_styles(client):
    b, conn = seed(client)
    done = [t for t in db.batch_tests(conn, b) if t["status"] == "done"][0]
    html = page(client)
    assert f'<details class="trow status-done" id="t-{done["id"]}"' in html and 'class="detail-grid"' in html
    client.post("/ajustes", data={**SETTINGS_OK, "card_style": "gauges"})
    html = page(client)
    assert 'class="detail-gauges"' in html and 'class="gauge"' in html and "aceptable desde 80" in html


def test_running_row_progress(client):
    b, conn = seed(client)
    r = db.claim_next(conn)
    db.set_progress(conn, r["id"], "encode", 0.5, 240, 480)
    row = page(client).split(f'id="t-{r["id"]}"', 1)[1].split("</summary>", 1)[0]
    assert f'data-progress-for="{r["id"]}"' in row and "50 %" in row


def test_tests_table_best(client):
    b, conn = seed(client)
    html = page(client)
    assert 'class="cmp"' in html and 'data-sort="vmaf_mean"' in html
    assert '<td class="best" data-v="95.0">★ 95.00</td>' in html


def test_batch_page_has_table_and_toggle(client):
    b, conn = seed(client)
    html = page(client, f"/batches/{b}")
    assert 'class="cmp"' in html and 'data-set-tests="table"' in html


def test_palette_markup(client):
    seed(client)
    html = page(client)
    assert 'class="palette-input"' in html and 'data-search="s/e01.mkv 0:10:00–0:10:20"' in html
    assert "Ninguna escena coincide" in html and 'data-tests="cards"' in html
    assert "Sueltas" not in html and 'id="scene-sibs"' not in html and "1 escena</p>" in html


def test_palette_groups_and_siblings(client):
    seed(client)  # escena suelta a las 0:10:00
    gid = int(post_group(client).headers["Location"].rsplit("/", 1)[-1])
    conn = db.connect(client.application.config["DB_PATH"])
    oscura = next(b["id"] for b in db.list_batches(conn) if b["role"] == "oscura")
    html = page(client, f"/?escena={oscura}")
    pal = html[html.index('id="scene-list"'):]
    assert "3 escenas · 1 grupo" in html and pal.count('<div role="group" class="pgroup"') == 1 and "Sueltas" in pal
    assert pal.index("role-exigente") < pal.index("role-oscura") < pal.index("Sueltas") < pal.index("e01.mkv</b> · 0:10:00")
    assert f'href="/grupos/{gid}">Resumen ›' in pal and 'grupo exigente"' in pal
    sibs = html[html.index('id="scene-sibs"'):html.index("</nav>", html.index('id="scene-sibs"'))]
    assert sibs.count('class="chip role') == 2 and 'aria-current="page">oscura</a>' in sibs
    assert "role-oscura" in html[html.index('id="scene-pick"'):html.index("</button>", html.index('id="scene-pick"'))]


def test_table_and_palette_accessible(client):
    seed(client)
    html = page(client)
    assert '<button type="button" class="sort">VMAF</button>' in html
    assert 'role="combobox"' in html and 'aria-controls="scene-list"' in html and 'id="scene-list"' in html
    assert 'aria-live="polite"' in html and 'id="opt-' in html


def test_running_row_pct_marked(client):
    b, conn = seed(client)
    r = db.claim_next(conn)
    db.set_progress(conn, r["id"], "encode", 0.5, 240, 480)
    assert f'data-progress-pct="{r["id"]}"' in page(client)


def test_brand_logo_and_background(client):
    html = page(client)
    assert 'data-bg="particulas"' in html and '<svg class="logo"' in html and 'class="bgfx"' in html
    client.post("/ajustes", data={**SETTINGS_OK, "background": "ondas"})
    html = page(client)
    assert 'data-bg="ondas"' in html and 'value="ondas" selected' in html


def test_favicon_logo_and_segments(client):
    html = page(client)
    assert 'rel="icon"' in html and 'favicon.svg' in html
    assert client.get("/static/favicon.svg").status_code == 200
    assert '<svg class="logo"' in html and 'class="nav"' in html and "seg-thumb" in html


def test_hms_format():
    assert (hms(300), hms(600.3), hms(3725), hms(59.96), hms(None)) == ("0:05:00", "0:10:00.3", "1:02:05", "0:01:00", "–")


def test_parse_time():
    assert (parse_time("1:02:05"), parse_time("05:00"), parse_time("300.5"), parse_time(" 0:10:00.3 ")) == (3725, 300, 300.5, 600.3)
    for bad in ("abc", "1:99:00", "1:2:3:4", "nan", ""):
        with pytest.raises(ValueError):
            parse_time(bad)


def test_batch_accepts_hms_and_shows_it(client):
    r = client.post("/batches", data={**VALID, "start": "0:10:00", "duration": "0:00:20"})
    assert r.status_code == 303
    html = page(client)
    assert "0:10:00–0:10:20" in html and "600.0–620.0 s" not in html
    assert 'name="start" value="0:10:00"' in page(client, "/new?file=s/e01.mkv&start=600&duration=20")


def test_single_tests_toggle_in_topbar(client):
    b, conn = seed(client)
    html = page(client)
    assert html.count('data-set-tests="table"') == 1
    bar = html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert 'data-set-tests="table"' in bar and 'data-set-view="panel"' in bar
    assert 'data-set-tests="table"' in page(client, f"/batches/{b}").split("</header>", 1)[0]


# ---------- fotogramas de comparación ----------
def seed_frames(client, tmp_path):
    """Una batería con una prueba hecha y 2 fotogramas (original y encode) como ficheros reales."""
    batch_id, conn = seed(client)
    t = db.batch_tests(conn, batch_id)[0]
    folder = tmp_path / "frames" / "k"
    folder.mkdir(parents=True)
    rows = []
    for test_id, name in ((None, "o"), (t["id"], "t")):
        for idx in (10, 40):
            main, thumb = folder / f"{name}{idx}.webp", folder / f"{name}{idx}_s.webp"
            main.write_bytes(b"RIFFxxxxWEBP")
            thumb.write_bytes(b"RIFFxxxxWEBP")
            rows.append(dict(source_path="s/e01.mkv", start_s=600.0, duration_s=20.0, test_id=test_id, idx=idx,
                             role="detalle" if idx == 10 else "medio", vmaf=None if test_id is None else 90.0 + idx / 10,
                             bytes=12, path=str(main), thumb=str(thumb)))
    db.add_frames(conn, rows)
    return batch_id, t["id"], conn, folder


def test_compare_page_and_images(client, tmp_path):
    batch_id, test_id, conn, folder = seed_frames(client, tmp_path)
    html = page(client, f"/comparar?escena={batch_id}&b={test_id}&f=40")
    assert "cmp-stage" in html and "VMAF del fotograma 94.0" in html and 'aria-current="true"' in html
    assert "medio" in html and "detalle" in html and "compare.js" in html
    frame = db.test_frames(conn, test_id)[0]
    r = client.get(f"/frames/{frame['id']}")
    assert r.status_code == 200 and r.mimetype == "image/webp" and client.get(f"/frames/{frame['id']}/thumb").status_code == 200
    assert client.get("/frames/9999").status_code == 404 and client.get("/comparar?escena=9999").status_code == 404
    conn.execute("UPDATE frames SET path = '/etc/passwd' WHERE id = ?", (frame["id"],))
    conn.commit()
    assert client.get(f"/frames/{frame['id']}").status_code == 404  # fuera de /data/frames


def test_compare_page_without_images(client):
    batch_id, _ = seed(client)
    assert "no tiene imágenes guardadas" in page(client, f"/comparar?escena={batch_id}")


def test_thumbs_and_compare_links(client, tmp_path):
    batch_id, test_id, conn, folder = seed_frames(client, tmp_path)
    html = page(client)
    assert 'class="thumbs"' in html and "Comparar con el original" in html and "Comparar visualmente" in html
    assert f"/comparar?escena={batch_id}&amp;a=orig&amp;b={test_id}" in html


def test_delete_test_removes_its_images(client, tmp_path):
    batch_id, test_id, conn, folder = seed_frames(client, tmp_path)
    other = db.batch_tests(conn, batch_id)[1]["id"]
    assert client.post(f"/tests/{test_id}/delete").status_code == 303
    assert not list(folder.glob("t*.webp")) and len(list(folder.glob("o*.webp"))) == 4  # la escena sigue viva
    client.post(f"/tests/{other}/delete")  # última prueba: cae la batería
    assert not folder.exists()  # y los originales huérfanos también
    assert db.frames_usage(conn) == (0, 0)


def test_delete_one_test_keeps_originals(client, tmp_path):
    batch_id, test_id, conn, folder = seed_frames(client, tmp_path)
    other = db.batch_tests(conn, batch_id)[1]["id"]
    client.post(f"/tests/{other}/delete")
    assert len(list(folder.glob("*.webp"))) == 8
    client.post(f"/batches/{batch_id}/delete")
    assert not folder.exists()


def test_clear_images_and_settings(client, tmp_path):
    batch_id, test_id, conn, folder = seed_frames(client, tmp_path)
    assert "4 imágenes" in page(client)
    assert client.post("/ajustes/imagenes/borrar").status_code == 303
    assert not folder.exists() and db.frames_usage(conn) == (0, 0)
    r = client.post("/ajustes", data={**SETTINGS_OK, "frames_per_scene": "9"})
    assert r.status_code == 400
    client.post("/ajustes", data={**SETTINGS_OK, "frames_per_scene": "4", "frames_cap_gb": "5", "save_frames": "1"})
    s = settings.load(conn)
    assert s["frames_per_scene"] == 4 and s["frames_cap_gb"] == 5 and s["save_frames"] is True


def test_cachedir_tag_created(client, tmp_path):
    assert (tmp_path / "frames" / "CACHEDIR.TAG").read_text().startswith("Signature: 8a477f597d28d172789f06886806bc55")


def test_worker_saves_test_frames(tmp_path, monkeypatch):
    from encodelab import frames
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    monkeypatch.setattr(media, "probe_source", lambda src: dict(INFO))
    b = db.create_batch(conn, source_path="s/e01.mkv", start_s=600, duration_s=20, fps=24)
    db.add_tests(conn, b, "libsvtav1", "", [(28, "6")])
    root = tmp_path / "frames"

    def fake_originals(c, r, mr, src, start, dur, k, info):
        db.add_frames(c, [dict(source_path=src, start_s=start, duration_s=dur, test_id=None, idx=5, role="medio",
                               vmaf=None, bytes=1, path=str(root / "o.webp"), thumb=str(root / "o_s.webp"))])
        return db.scene_originals(c, src, start, dur)

    monkeypatch.setattr(frames, "ensure_originals", fake_originals)
    seen = {}

    def runner(*args, frames=None, **kwargs):
        seen["plan"] = frames
        for i, d, t in frames["items"]:
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(b"x")
            t.write_bytes(b"y")
        return {"kbps": 1.0, "frame_vmaf": {5: 93.2}}

    stop = threading.Event()
    th = threading.Thread(target=worker_loop, args=(path, tmp_path, tmp_path, 1, stop),
                          kwargs={"runner": runner, "idle_s": 0.05, "frames_root": root})
    th.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and db.batch_tests(conn, b)[0]["status"] != "done":
        time.sleep(0.05)
    time.sleep(0.2)
    stop.set()
    th.join(2)
    saved = db.test_frames(conn, db.batch_tests(conn, b)[0]["id"])
    assert seen["plan"]["matrix"] == "bt709" and [(f["idx"], f["vmaf"]) for f in saved] == [(5, 93.2)]


def test_worker_skips_frames_when_disabled_or_full(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    db.save_settings(conn, {"save_frames": "0"})
    b = db.create_batch(conn, source_path="s/e01.mkv", start_s=600, duration_s=20, fps=24)
    db.add_tests(conn, b, "libsvtav1", "", [(28, "6")])
    seen = []
    stop = threading.Event()
    th = threading.Thread(target=worker_loop, args=(path, tmp_path, tmp_path, 1, stop),
                          kwargs={"runner": lambda *a, **k: seen.append(k) or {"kbps": 1.0}, "idle_s": 0.05,
                                  "frames_root": tmp_path / "frames"})
    th.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and db.batch_tests(conn, b)[0]["status"] != "done":
        time.sleep(0.05)
    stop.set()
    th.join(2)
    assert "frames" not in seen[0]


# ---------- análisis automático ----------
def finished_analysis(client, n_cands=4):
    r = client.post("/analisis", data={"file": "s/e01.mkv", "duration": "20"})
    aid = r.get_json()["id"]
    conn = db.connect(client.application.config["DB_PATH"])
    cands = [{"start": 100 * (i + 1), "kbps": 20000 + 3000 * i, "si": 50 + 10 * i, "ti": 8 + 3 * i, "yavg": 90 - 20 * i}
             for i in range(n_cands)]
    db.finish_analysis(conn, aid, json.dumps(cands))
    return aid, conn


def test_start_analysis_dedupes_and_validates(client):
    first = client.post("/analisis", data={"file": "s/e01.mkv", "duration": "20"}).get_json()["id"]
    assert client.post("/analisis", data={"file": "s/e01.mkv", "duration": "0:20"}).get_json()["id"] == first
    assert client.post("/analisis", data={"file": "s/e01.mkv", "duration": "1"}).status_code == 400
    assert client.post("/analisis", data={"file": "../x.mkv", "duration": "20"}).status_code == 400
    assert client.post("/analisis", data={"file": "s/e01.mkv", "duration": "20"},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    state = client.get(f"/api/analisis/{first}").get_json()
    assert state["status"] == "queued" and state["proposals"] == []
    assert client.get("/api/analisis/999").status_code == 404


def test_api_analysis_proposals_follow_n(client, tmp_path):
    aid, conn = finished_analysis(client)
    one = client.get(f"/api/analisis/{aid}?n=1").get_json()
    assert one["status"] == "done" and [p["role"] for p in one["proposals"]] == ["exigente"]
    three = client.get(f"/api/analisis/{aid}?n=3").get_json()["proposals"]
    assert len(three) == 3 and three[0]["start_hms"].count(":") == 2 and three[0]["thumb"].endswith(".webp")
    assert len(client.get(f"/api/analisis/{aid}?n=99").get_json()["proposals"]) == 4  # tope: las candidatas que hay
    assert client.get(f"/api/analisis/{aid}").get_json()["proposals"].__len__() == 3  # n de Ajustes (3 por defecto)


def test_analysis_thumb_route(client, tmp_path):
    aid, conn = finished_analysis(client)
    folder = tmp_path / "frames" / "analysis" / str(aid)
    folder.mkdir(parents=True)
    (folder / "100.webp").write_bytes(b"RIFFxxxxWEBP")
    assert client.get(f"/analisis/{aid}/100.webp").mimetype == "image/webp"
    assert client.get(f"/analisis/{aid}/200.webp").status_code == 404
    client.post("/ajustes/imagenes/borrar")
    assert not folder.exists()


# ---------- varios tramos y resumen del grupo ----------
def post_group(client, starts=("300", "900"), roles=("exigente", "oscura"), qualities="28,32"):
    r = client.post("/batches", data={**VALID, "start": list(starts), "role": list(roles), "qualities": qualities})
    return r


def done(conn, batch_id, vmaf_by_quality):
    for t in db.batch_tests(conn, batch_id):
        v = vmaf_by_quality[t["quality"]]
        db.finish_test(conn, t["id"], {"kbps": 4000.0 + t["quality"] * 10, "vmaf_mean": v, "vmaf_p5": v - 3, "vmaf_min": v - 8,
                                       "enc_fps": 20.0, "ffmpeg_args": f"-c:v libsvtav1 -crf {t['quality']} -preset 6"})


def test_post_multi_start_creates_group(client):
    r = post_group(client)
    assert r.status_code == 303 and r.headers["Location"].startswith("/grupos/")
    conn = db.connect(client.application.config["DB_PATH"])
    batches = db.list_batches(conn)
    assert sorted((b["start_s"], b["role"], b["n_tests"]) for b in batches) == [(300.0, "exigente", 2), (900.0, "oscura", 2)]
    assert len({b["group_id"] for b in batches}) == 1 and batches[0]["group_id"] is not None
    assert db.get_group(conn, batches[0]["group_id"])["source_path"] == "s/e01.mkv"


def test_post_single_start_has_no_group(client):
    r = client.post("/batches", data=VALID)
    assert r.headers["Location"].startswith("/batches/")
    conn = db.connect(client.application.config["DB_PATH"])
    assert db.list_batches(conn)[0]["group_id"] is None


@pytest.mark.parametrize("starts", [("300", "300"), ("1", "2", "3", "4", "5", "6"), ("300", "99999")])
def test_post_multi_start_rejects_bad(client, starts):
    assert post_group(client, starts, ()).status_code == 400
    assert db.list_batches(db.connect(client.application.config["DB_PATH"])) == []


def test_post_unknown_role_is_ignored(client):
    post_group(client, roles=("hacker", "<b>x</b>"))
    conn = db.connect(client.application.config["DB_PATH"])
    assert {b["role"] for b in db.list_batches(conn)} == {None}


def test_group_page_only_complete_configs(client):
    r = post_group(client)
    gid = int(r.headers["Location"].rsplit("/", 1)[-1])
    conn = db.connect(client.application.config["DB_PATH"])
    b1, b2 = sorted(db.list_batches(conn), key=lambda b: b["start_s"])
    assert "Ninguna configuración está terminada" in page(client, f"/grupos/{gid}")
    done(conn, b1["id"], {28: 96.0, 32: 94.0})
    assert "Ninguna configuración está terminada" in page(client, f"/grupos/{gid}")  # falta el otro tramo
    done(conn, b2["id"], {28: 95.0, 32: 91.0})
    html = page(client, f"/grupos/{gid}")
    assert "Resumen entre tramos" in html and "★ libsvtav1 28·6" in html  # 32 queda bajo 93 en el peor tramo
    assert "exigente" in html and "oscura" in html and "-c:v libsvtav1 -crf 28" in html
    assert 'class="chart"' in html
    rows = db.group_summary(conn, gid)
    assert [r["quality"] for r in rows] == [32, 28] or [r["quality"] for r in rows] == [28, 32]
    best = next(r for r in rows if r["quality"] == 28)
    assert best["vmaf_worst"] == 95.0 and best["p5_worst"] == 92.0 and best["n"] == 2


def test_group_links_and_missing(client):
    gid = int(post_group(client).headers["Location"].rsplit("/", 1)[-1])
    seed_conn = db.connect(client.application.config["DB_PATH"])
    b = db.list_batches(seed_conn)[0]
    db.finish_test(seed_conn, db.batch_tests(seed_conn, b["id"])[0]["id"], {"kbps": 4000.0, "vmaf_mean": 95.0})
    assert f"/grupos/{gid}" in page(client) and "Σ Resumen entre tramos" in page(client)
    assert client.get("/grupos/999").status_code == 404
    for batch in db.list_batches(seed_conn):
        client.post(f"/batches/{batch['id']}/delete", data={"next": f"/grupos/{gid}"})
    assert client.get(f"/grupos/{gid}").status_code == 404


def test_delete_redirects_home_when_group_empties(client):
    gid = int(post_group(client).headers["Location"].rsplit("/", 1)[-1])
    conn = db.connect(client.application.config["DB_PATH"])
    first, second = db.list_batches(conn)
    assert client.post(f"/batches/{first['id']}/delete", data={"next": f"/grupos/{gid}"}).headers["Location"] == f"/grupos/{gid}"
    assert client.post(f"/batches/{second['id']}/delete", data={"next": f"/grupos/{gid}"}).headers["Location"] == "/"


def test_new_page_has_auto_panel(client):
    html = page(client, "/new?file=s/e01.mkv")
    for marker in ("data-tramo-mode", "data-auto-panel", 'data-tramo="auto"', "data-auto-run", "data-manual-only",
                   'data-analysis-url="/analisis"', 'data-auto-n'):
        assert marker in html
    assert 'value="3" data-auto-n' in html  # auto_tramos de Ajustes


def test_nav_tabs(client, tmp_path):
    html = page(client, "/metricas")
    assert 'class="nav"' in html and 'href="/?vista=panel"' in html and 'href="/?vista=baterias"' in html
    assert 'href="/metricas" aria-current="page"' in html and ">Grupos<" not in html and ">Comparar<" not in html
    seed(client)
    assert 'data-set-view="panel"' in page(client) and 'href="/?vista=' not in page(client)  # en el panel son botones
    gid = int(post_group(client).headers["Location"].rsplit("/", 1)[-1])
    html = page(client, "/metricas")
    assert 'href="/grupos"' in html and ">Comparar<" not in html
    seed_frames(client, tmp_path)
    assert ">Comparar<" in page(client, "/metricas") and "aria-current=\"page\"" in page(client, f"/grupos/{gid}")


def test_compare_and_group_pickers(client, tmp_path):
    batch_id, test_id, conn, folder = seed_frames(client, tmp_path)
    html = page(client, f"/comparar?escena={batch_id}")
    assert 'name="escena" data-autosubmit' in html and f'value="{batch_id}" selected' in html and "0:10:00–0:10:20" in html
    g1 = int(post_group(client, ("300", "900")).headers["Location"].rsplit("/", 1)[-1])
    g2 = int(post_group(client, ("400", "1000")).headers["Location"].rsplit("/", 1)[-1])
    r = client.get("/grupos")
    assert r.status_code == 302 and r.headers["Location"] == f"/grupos/{g2}"
    assert client.get(f"/grupos?g={g1}").headers["Location"] == f"/grupos/{g1}"
    html = page(client, f"/grupos/{g1}")
    assert 'name="g" data-autosubmit' in html and f'value="{g1}" selected' in html and f'value="{g2}"' in html


def test_groups_without_data_is_404(client):
    assert client.get("/grupos").status_code == 404


def test_new_hides_qsv_without_device(client_noqsv):
    html = client_noqsv.get("/new?file=s/e01.mkv").get_data(as_text=True)
    form = html.split('name="encoder"', 1)[1].split("Argumentos extra", 1)[0]  # select y fichas de la batería
    assert "av1_qsv" not in form and "hevc_qsv" not in form and "libsvtav1" in form
    assert 'data-enc-info="hevc_qsv"' not in html


def test_new_falls_back_when_default_is_qsv(client_noqsv):
    assert client_noqsv.post("/ajustes", data={**SETTINGS_OK, "default_encoder": "av1_qsv"}).status_code == 303
    html = client_noqsv.get("/new?file=s/e01.mkv").get_data(as_text=True)
    assert 'value="libsvtav1" selected' in html


def test_post_batch_rejects_qsv_without_device(client_noqsv):
    r = client_noqsv.post("/batches", data={**VALID, "encoder": "hevc_qsv", "presets": "medium"})
    assert r.status_code == 400 and "QSV no disponible" in r.get_data(as_text=True)


def test_old_qsv_results_still_render(client, client_noqsv):
    r = client.post("/batches", data={**VALID, "encoder": "hevc_qsv", "presets": "medium"})
    batch_id = int(r.headers["Location"].rsplit("/", 1)[-1])
    conn = db.connect(client.application.config["DB_PATH"])
    db.finish_test(conn, db.batch_tests(conn, batch_id)[0]["id"], {"kbps": 4000.0, "vmaf_mean": 95.0, "vmaf_p5": 92.0,
                                                                    "enc_fps": 30.0})
    for url in ("/", f"/batches/{batch_id}"):
        resp = client_noqsv.get(url)
        assert resp.status_code == 200 and "hevc_qsv" in resp.get_data(as_text=True), url


def test_settings_panel_shows_qsv_state(client, client_noqsv):
    assert "QSV" in client.get("/").get_data(as_text=True)
    assert "no disponible: sin dispositivo" in client_noqsv.get("/").get_data(as_text=True)
    sel = client_noqsv.get("/").get_data(as_text=True).split('name="default_encoder"', 1)[1].split("</select>", 1)[0]
    assert "av1_qsv" not in sel


def test_partial_qsv_offers_only_working_encoders(client, tmp_path):
    app = create_app({"MEDIA_ROOT": str(tmp_path), "DB_PATH": str(tmp_path / "t.db"), "WORK_DIR": str(tmp_path),
                      "VMAF_THREADS": 1, "START_WORKER": False, "QSV_ENCODERS": ["hevc_qsv"],
                      "QSV_REASON": "av1_qsv: Current resolution is unsupported"})
    c = app.test_client()
    form = c.get("/new?file=s/e01.mkv").get_data(as_text=True).split('name="encoder"', 1)[1].split("Argumentos extra", 1)[0]
    assert "hevc_qsv" in form and "av1_qsv" not in form
    assert c.post("/batches", data={**VALID, "encoder": "av1_qsv", "presets": "slow"}).status_code == 400
    assert c.post("/batches", data={**VALID, "encoder": "hevc_qsv", "presets": "medium"}).status_code == 303
    assert "hevc_qsv (no: av1_qsv: Current resolution is unsupported)" in c.get("/").get_data(as_text=True)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignora los permisos")
def test_unwritable_data_dir_gives_clear_error(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o555)
    try:
        with pytest.raises(RuntimeError, match=r"no se puede escribir en .*data.*mkdir"):
            create_app({"MEDIA_ROOT": str(tmp_path), "DB_PATH": str(data / "t.db"), "WORK_DIR": str(tmp_path),
                        "VMAF_THREADS": 1, "START_WORKER": False, "QSV_ENCODERS": []})
    finally:
        data.chmod(0o755)
