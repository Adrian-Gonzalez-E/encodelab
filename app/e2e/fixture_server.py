"""EncodeLab con una base de datos de muestra para la prueba e2e (sin worker).

Uso: docker compose run -d --rm --no-deps --name encodelab-e2e encodelab python3 -m e2e.fixture_server
Con la primera petición a /: a los 8 s la prueba en curso pasa a "medir" (80 %) y a los 35 s termina y se
borra la de la cola, para probar la cola en vivo y el refresco.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from flask import request

from encodelab import db, media
from encodelab.web import create_app

DB_PATH = "/tmp/e2e.db"
MEDIA_ROOT = Path("/tmp/e2e-media")
INFO = {"codec": "h264", "width": 1920, "height": 1080, "fps": 23.976, "field_order": "progressive",
        "interlaced": False, "pix_fmt": "yuv420p", "duration_s": 2640.0}
SCENE = dict(source_path="series/Demo (2024)/Season 01/Demo - S01E01 - Piloto.mkv", start_s=300.0, duration_s=20.0,
             codec="h264", width=1920, height=1080, fps=23.976, field_order="progressive",
             episode_duration_s=2640.0, source_kbps=4500.0, ffmpeg_version="ffmpeg version e2e")


def result(kbps, vmaf, p5, fps, args):
    return {"kbps": kbps, "vmaf_mean": vmaf, "vmaf_p5": p5, "vmaf_min": p5 - 3, "vmaf_neg_mean": vmaf - 1.5,
            "psnr_y": 38 + (vmaf - 85) / 3, "ssim": 0.97 + (vmaf - 85) / 1000, "ms_ssim": 0.975 + (vmaf - 85) / 1000,
            "enc_fps": fps, "frames": 480, "size_bytes": int(kbps * 20 * 125), "ffmpeg_args": args}


def seed(conn):
    plan = [  # (encoder, extra, [(calidad, preset, resultado | estado)])
        ("libsvtav1", "-pix_fmt yuv420p10le", [(26, "6", (2600, 95.6, 92.8, 28)), (30, "6", (2050, 94.4, 91.0, 31)),
                                               (34, "6", (1600, 92.1, 87.9, 35))]),
        ("av1_qsv", "-pix_fmt p010le", [(24, "slow", (2300, 93.2, 89.5, 150)), (28, "slow", (1800, 91.0, 86.2, 160))]),
        ("libx265", "", [(24, "medium", (2500, 93.9, 90.4, 22)), (28, "medium", "error")]),
        ("hevc_qsv", "", [(25, "medium", (2700, 92.4, 88.0, 170))]),
        ("libsvtav1", "-pix_fmt yuv420p10le", [(22, "4", "running"), (38, "8", "queued")]),
    ]
    # Otra escena, creada primero: la escena por defecto (la más reciente) es la de SCENE.
    other = db.create_batch(conn, **{**SCENE, "source_path": "series/Demo (2024)/Season 01/Demo - S01E02.mkv"})
    db.add_tests(conn, other, "libsvtav1", "", [(30, "6")])
    db.finish_test(conn, db.batch_tests(conn, other)[0]["id"], result(1900, 93.5, 90.1, 30, "-c:v libsvtav1 -crf 30 -preset 6"))
    for encoder, extra, tests in plan:
        b = db.create_batch(conn, **SCENE)
        db.add_tests(conn, b, encoder, extra, [(q, p) for q, p, _ in tests])
        for row, (q, p, what) in zip(db.batch_tests(conn, b), tests):
            if what == "error":
                db.fail_test(conn, row["id"], "Código 1: [libx265] Parámetro del encoder no aceptado: e2e")
            elif what not in ("running", "queued"):
                kbps, vmaf, p5, fps = what
                db.finish_test(conn, row["id"], result(kbps, vmaf, p5, fps, f"-c:v {encoder} -q {q} -preset {p} {extra}".strip()))


FRAMES_DIR = Path("/tmp/e2e-frames")


def make_image(path, vf, thumb):
    """Fotograma de prueba (WebP sin pérdida) y su miniatura, generados con el ffmpeg de la imagen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    base = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720"]
    subprocess.run([*base, "-vf", vf, "-frames:v", "1", "-c:v", "libwebp", "-lossless", "1", str(path)], check=True)
    subprocess.run([*base, "-vf", f"{vf},scale=480:-2", "-frames:v", "1", "-c:v", "libwebp", "-quality", "80", str(thumb)],
                   check=True)


def seed_images(conn):
    """Originales de SCENE y un fotograma por prueba hecha (cada encode con un difuminado distinto) y un análisis hecho."""
    key = dict(source_path=SCENE["source_path"], start_s=SCENE["start_s"], duration_s=SCENE["duration_s"])
    rows = []
    for idx, role, vf in ((100, "detalle", "null"), (250, "medio", "hue=h=40")):
        o, t = FRAMES_DIR / f"o{idx}.webp", FRAMES_DIR / f"o{idx}_s.webp"
        make_image(o, vf, t)
        rows.append(dict(**key, test_id=None, idx=idx, role=role, vmaf=None, bytes=o.stat().st_size, path=str(o), thumb=str(t)))
    tests = conn.execute("SELECT t.id FROM tests t JOIN batches b ON b.id = t.batch_id WHERE b.source_path = ? AND "
                         "b.start_s = ? AND b.duration_s = ? AND t.status = 'done'",
                         (key["source_path"], key["start_s"], key["duration_s"])).fetchall()
    for n, t in enumerate(tests, start=1):
        for idx, role, vf in ((100, "detalle", "null"), (250, "medio", "hue=h=40")):
            f, th = FRAMES_DIR / f"t{t['id']}_{idx}.webp", FRAMES_DIR / f"t{t['id']}_{idx}_s.webp"
            make_image(f, f"{vf},boxblur={n}", th)
            rows.append(dict(**key, test_id=t["id"], idx=idx, role=None, vmaf=97.0 - n * 1.5 - (idx == 250), bytes=f.stat().st_size,
                             path=str(f), thumb=str(th)))
    db.add_frames(conn, rows)
    # Análisis ya hecho de la fuente de la escena (el fichero de la biblioteca falsa está vacío): "Analizar vídeo" lo reutiliza.
    src = MEDIA_ROOT / SCENE["source_path"]
    src.parent.mkdir(parents=True, exist_ok=True)
    src.touch()
    st = src.stat()
    aid = db.create_analysis(conn, SCENE["source_path"], st.st_size, st.st_mtime_ns, 20.0)
    cands = [{"start": 480, "kbps": 9800, "si": 71.2, "ti": 38.5, "yavg": 96.0}, {"start": 1180, "kbps": 6100, "si": 52.0, "ti": 9.1, "yavg": 84.0},
             {"start": 1720, "kbps": 4400, "si": 49.0, "ti": 7.3, "yavg": 32.0}, {"start": 2100, "kbps": 5200, "si": 88.0, "ti": 12.0, "yavg": 90.0}]
    db.finish_analysis(conn, aid, json.dumps(cands))
    for c in cands:
        make_image(FRAMES_DIR / "analysis" / str(aid) / f"{c['start']}.webp", "null", FRAMES_DIR / "tmp_thumb.webp")


def mid_encode():
    conn = db.connect(DB_PATH)
    for row in conn.execute("SELECT id FROM tests WHERE status = 'running'").fetchall():
        db.set_progress(conn, row["id"], "encode", 0.5, 240, 480)


def to_metrics():
    conn = db.connect(DB_PATH)
    for row in conn.execute("SELECT id FROM tests WHERE status = 'running'").fetchall():
        db.set_progress(conn, row["id"], "metrics", 0.8, 384, 480)


def finish_pending():
    conn = db.connect(DB_PATH)
    for row in conn.execute("SELECT id, status FROM tests WHERE status IN ('running', 'queued')").fetchall():
        if row["status"] == "running":
            db.finish_test(conn, row["id"], result(3100, 96.8, 94.0, 12, "-c:v libsvtav1 -crf 22 -preset 4"))
        else:
            db.delete_test(conn, row["id"])


if __name__ == "__main__":
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    seed(db.connect(DB_PATH))
    # Biblioteca falsa: ficheros vacíos con las rutas de la base de datos y ffprobe simulado (como en los tests).
    for rel in ("series/Demo (2024)/Season 01/Demo - S01E01 - Piloto.mkv", "series/Demo (2024)/Season 01/Demo - S01E02.mkv"):
        (MEDIA_ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
        (MEDIA_ROOT / rel).touch()
    media.probe_source = lambda src: dict(INFO)
    media.segment_kbps = lambda src, start, dur: 4500.0
    app = create_app({"DB_PATH": DB_PATH, "MEDIA_ROOT": str(MEDIA_ROOT), "WORK_DIR": "/tmp", "START_WORKER": False, "QSV_ENCODERS": ["av1_qsv", "hevc_qsv"], "QSV_REASON": "ok",
                      "FRAMES_DIR": str(FRAMES_DIR)})
    seed_images(db.connect(DB_PATH))
    # Después de create_app: su reset_running devolvería a la cola la prueba "en curso".
    conn = db.connect(DB_PATH)
    running = db.claim_next(conn)  # la primera en cola es la CRF 22 de la última batería
    conn.execute("UPDATE tests SET started_ts = ? WHERE id = ?", (time.time() - 6, running["id"]))
    db.set_progress(conn, running["id"], "encode", 0.35, 168, 480)
    timers = [threading.Timer(4, mid_encode), threading.Timer(8, to_metrics), threading.Timer(35, finish_pending)]

    @app.before_request
    def start_timers():
        # Solo "/": la comprobación de arranque de run.sh (/metricas) no debe consumir el tiempo.
        if request.path == "/" and not timers[0].is_alive() and not timers[0].finished.is_set():
            # El reloj de la prueba en curso arranca con el navegador (pip puede tardar minutos antes).
            c = db.connect(DB_PATH)
            c.execute("UPDATE tests SET started_ts = ? WHERE status = 'running'", (time.time() - 6,))
            c.commit()
            for t in timers:
                t.start()

    app.run(host="0.0.0.0", port=8099)
