"""Web de EncodeLab: páginas y worker de la cola."""
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from contextlib import closing
from functools import cache
from urllib.parse import parse_qsl, urlencode, urlsplit
from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, url_for

from . import analysis, chart, db, frames, glossary, media, settings

log = logging.getLogger("encodelab")



def safe_next(value: str | None) -> str:
    """Solo rutas internas: "//host" y "/\\host" serían redirecciones a otro sitio."""
    # Los navegadores ignoran \t \r \n en URLs: "/\t/host" acabaría en "//host".
    if (value and value.startswith("/") and not value.startswith(("//", "/\\"))
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        return value
    return "/"


def _frames_plan(conn, row, media_root: Path, frames_root: Path) -> dict | None:
    """Fotogramas a guardar de esta prueba (None: desactivado, sin sitio o fallo: la prueba sigue sin imágenes)."""
    prefs = settings.load(conn)
    if not prefs["save_frames"] or db.frames_usage(conn)[1] >= prefs["frames_cap_gb"] * 1e9:
        return None
    try:
        info = media.probe_source(Path(media_root) / row["source_path"])
        originals = frames.ensure_originals(conn, frames_root, media_root, row["source_path"], row["start_s"],
                                            row["duration_s"], prefs["frames_per_scene"], info)
        if not originals:
            return None
        return {"items": frames.plan_for_test(frames_root, row, originals), "matrix": frames.matrix_for(info),
                "range": frames.range_for(info)}
    except Exception as e:
        log.warning("Sin fotogramas en la prueba %s: %s", row["id"], e)
        return None


def _save_test_frames(conn, row, plan: dict, result: dict) -> None:
    rows = [dict(source_path=row["source_path"], start_s=row["start_s"], duration_s=row["duration_s"],
                 test_id=row["id"], idx=i, role=None, vmaf=result["frame_vmaf"][i], bytes=d.stat().st_size,
                 path=str(d), thumb=str(t))
            for i, d, t in plan["items"] if i in result.get("frame_vmaf", {}) and d.exists() and t.exists()]
    if rows:
        db.add_frames(conn, rows)


def worker_loop(db_path: str, media_root: Path, work: Path, threads: int, stop: threading.Event,
                runner=media.run_test, idle_s: float = 2.0, frames_root: Path | None = None) -> None:
    conn = db.connect(db_path)
    while not stop.is_set():
        try:
            row = db.claim_next(conn)
            if row is None:
                stop.wait(idle_s)
                continue
            total = round(row["duration_s"] * row["fps"]) if row["fps"] else None
            last = [0.0]

            def report(phase, frac, frame, test_id=row["id"], total=total):
                # Como mucho una escritura por segundo (y siempre la final).
                now = time.monotonic()
                if now - last[0] >= 1 or frac >= 1:
                    last[0] = now
                    try:
                        db.set_progress(conn, test_id, phase, frac, frame, total)
                    except Exception as e:  # el progreso es de mejor esfuerzo: no puede tumbar la prueba
                        log.warning("Progreso no guardado (prueba %s): %s", test_id, e)

            try:
                plan = _frames_plan(conn, row, media_root, frames_root) if frames_root else None
                result = runner(Path(media_root) / row["source_path"], row["start_s"], row["duration_s"],
                                row["encoder"], row["quality"], row["preset"],
                                media.parse_extra_args(row["extra_args"]), Path(work), threads, on_progress=report,
                                **({"frames": plan} if plan else {}))
                db.finish_test(conn, row["id"], result)
                if plan:
                    _save_test_frames(conn, row, plan, result)
            except Exception as e:  # cualquier fallo queda en la prueba y la cola sigue
                log.warning("Prueba %s en error: %s", row["id"], e)
                db.fail_test(conn, row["id"], str(e))
        except Exception:  # fallo de la propia base (p. ej. "database is locked"): el hilo no puede morir
            log.exception("Error del worker; se reintenta")
            stop.wait(idle_s)


@cache
def _ffmpeg_version() -> str:
    try:
        return media.ffmpeg_version()
    except Exception as e:  # sin ffmpeg (tests) o roto: no tumba la página
        return f"no disponible ({e})"


def _cgroup(name: str) -> str | None:
    try:
        return Path("/sys/fs/cgroup", name).read_text().strip()
    except OSError:
        return None


def system_info(db_path: str, threads: int, qsv: str = "") -> dict:
    """Datos de solo lectura del panel de ajustes."""
    cpu = _cgroup("cpu.max")  # "600000 100000" = 6 CPU; "max 100000" = sin límite
    mem = _cgroup("memory.max")
    quota, _, period = (cpu or "").partition(" ")
    try:
        size_mb = os.path.getsize(db_path) / 1e6
    except OSError:
        size_mb = None
    return {"ffmpeg": _ffmpeg_version().split(" Copyright")[0], "threads": threads,
            "cpus": f"{int(quota) / int(period):g} CPU" if quota.isdigit() and period.isdigit() else "sin límite",
            "mem": f"{int(mem) / 2**30:g} GiB" if mem and mem.isdigit() else "sin límite",
            "db_mb": size_mb, "qsv": qsv}


BEST_MAX = ("vmaf_mean", "vmaf_p5", "vmaf_min", "vmaf_neg_mean", "psnr_y", "ssim", "ms_ssim", "enc_fps")
BEST_MIN = ("ratio", "kbps", "est_gb", "est_hours")


def best_by_column(tests) -> dict[str, float]:
    """Mejor valor de cada columna de la tabla entre las pruebas hechas (None no cuenta)."""
    done = [t for t in tests if t["status"] == "done"]
    best = {}
    for keys, pick in ((BEST_MAX, max), (BEST_MIN, min)):
        for key in keys:
            values = [t[key] for t in done if t[key] is not None]
            if values:
                best[key] = pick(values)
    return best


def hms(seconds) -> str:
    """Segundos como h:mm:ss (con una décima solo si la hay)."""
    if seconds is None:
        return "–"
    tenths = round(float(seconds) * 10)
    h, rest = divmod(tenths, 36000)
    m, s10 = divmod(rest, 600)
    sec = f"{s10 // 10:02d}" if s10 % 10 == 0 else f"{s10 // 10:02d}.{s10 % 10}"
    return f"{h}:{m:02d}:{sec}"


def parse_time(text: str) -> float:
    """Acepta h:mm:ss, mm:ss o segundos; ValueError si no es un tiempo válido."""
    parts = str(text).strip().split(":")
    if not parts[0] or len(parts) > 3:
        raise ValueError(f"Tiempo no válido: {text}")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"Tiempo no válido: {text}") from e
    if not all(math.isfinite(n) and n >= 0 for n in nums) or any(n >= 60 for n in nums[1:]):
        raise ValueError(f"Tiempo no válido: {text}")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return round(total, 3)


def _maybe_time(text: str | None) -> float | None:
    try:
        return parse_time(text) if text is not None else None
    except ValueError:
        return None


def _maybe_number(text: str | None) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _number(text: str, name: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise ValueError(f"{name} debe ser un número")
    return value


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(MEDIA_ROOT=os.environ.get("MEDIA_ROOT", "/media"),
                      DB_PATH=os.environ.get("DB_PATH", "/data/encodelab.db"),
                      WORK_DIR=os.environ.get("WORK_DIR", "/work"),
                      VMAF_THREADS=int(os.environ.get("VMAF_THREADS") or os.cpu_count() or 1), START_WORKER=True)
    app.config.update(config or {})
    if "QSV_ENCODERS" not in app.config:
        app.config["QSV_ENCODERS"], app.config["QSV_REASON"] = media.detect_qsv()
        log.info("QSV: %s", app.config["QSV_REASON"])
    available = media.available_encoders(app.config["QSV_ENCODERS"])
    qsv_ok, qsv_reason = app.config["QSV_ENCODERS"], app.config.get("QSV_REASON", "")
    qsv_state = (f"no disponible: {qsv_reason}" if not qsv_ok else
                 ", ".join(qsv_ok) + ("" if qsv_reason == "ok" else f" (no: {qsv_reason})"))
    root = Path(app.config["MEDIA_ROOT"]).resolve()
    frames_root = Path(app.config.get("FRAMES_DIR") or Path(app.config["DB_PATH"]).parent / "frames").resolve()
    try:
        frames_root.mkdir(parents=True, exist_ok=True)
        # Marca de directorio de caché (CACHEDIR.TAG): las herramientas de backup pueden ignorarlo.
        (frames_root / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n"
                                                  "# Imágenes de comparación de EncodeLab (regenerables).\n")
    except OSError as e:
        log.warning("No se pudo preparar %s: %s", frames_root, e)

    def conn():
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
        return g.conn

    def rel_of(path: Path) -> str:
        return str(path.relative_to(root))

    @app.teardown_appcontext
    def close_conn(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.before_request
    def csrf():
        # CSRF: un proxy con cookie SameSite=Lax compartida con otras apps no basta; se exige same-origin.
        if request.method == "POST" and request.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            abort(403)

    @app.context_processor
    def page_context():
        c = conn()
        prefs = settings.load(c)
        return {"glossary": glossary.with_thresholds(prefs["vmaf_warn"], prefs["best_vmaf"]), "settings": prefs,
                "queue": db.queue_state(c, time.time()),
                "now": time.time(), "encoder_names": list(media.ENCODERS), "encoders_available": available, "frames_usage": db.frames_usage(c), "nav": db.nav_targets(c),
                "system": system_info(app.config["DB_PATH"], app.config["VMAF_THREADS"], qsv_state)}

    app.jinja_env.globals["best_by_column"] = best_by_column
    app.jinja_env.globals["thumbs_of"] = lambda test_id: db.test_frames(conn(), test_id)
    app.jinja_env.globals["scene_has_frames"] = lambda src, start, dur: bool(db.scene_originals(conn(), src, start, dur))

    @app.template_filter("dur")
    def duration_text(seconds):
        return "–" if seconds is None else "~" + hms(seconds)

    app.add_template_filter(hms, "hms")

    @app.errorhandler(ValueError)
    @app.errorhandler(media.EncodeError)
    def bad_request(e):
        return render_template("error.html", message=str(e)), 400

    @app.template_filter("n")
    def number(value, digits=1):
        return "–" if value is None else f"{value:.{digits}f}"

    @app.get("/")
    def results():
        c = conn()
        prefs = settings.load(c)
        scenes = db.list_scenes(c, prefs["best_vmaf"])
        sort = request.args.get("sort", "id")
        sort = sort if sort in db.SORTS else "id"
        scene = scenes[0] if scenes else None
        wanted = request.args.get("escena", type=int)
        b = db.get_batch(c, wanted) if wanted is not None else None
        if b is not None:
            key = (b["source_path"], b["start_s"], b["duration_s"])
            scene = next((s for s in scenes if (s["source_path"], s["start_s"], s["duration_s"]) == key), scene)
        tests = db.scene_tests(c, scene["batch_id"], sort) if scene else []
        good = [t for t in tests if t["status"] == "done" and t["ratio"] is not None
                and t["vmaf_mean"] is not None and t["vmaf_mean"] >= prefs["best_vmaf"]]
        done = [t for t in tests if t["status"] == "done"]
        sheet = None
        if scene:
            sb = db.get_batch(c, scene["batch_id"])
            sheet = {"title": scene["source_path"].rsplit("/", 1)[-1].rsplit(".", 1)[0],
                     "tramo": f'{hms(scene["start_s"])}–{hms(scene["start_s"] + scene["duration_s"])}',
                     "facts": [x for x in (sb["codec"], f'{sb["width"]}×{sb["height"]}' if sb["width"] else None,
                                           f'{sb["fps"]:.3f} fps' if sb["fps"] else None, sb["field_order"],
                                           f'original {sb["source_kbps"]:.0f} kbps' if sb["source_kbps"] else None,
                                           f'capítulo {hms(sb["episode_duration_s"])}' if sb["episode_duration_s"] else None) if x],
                     "group_id": scene["group_id"],
                     "best_saving": min(good, key=lambda t: t["ratio"], default=None),
                     "best_vmaf": max((t for t in done if t["vmaf_mean"] is not None), key=lambda t: t["vmaf_mean"], default=None),
                     "fastest": max((t for t in done if t["enc_fps"] is not None), key=lambda t: t["enc_fps"], default=None)}
        # Paleta: un apartado por grupo (el más reciente primero, tramos por rol) y al final las escenas sueltas.
        groups = {r["id"]: r for r in db.list_groups(c)}
        role_order = {r: i for i, r in enumerate(analysis.ROLES)}
        sections = {}
        for s in sorted(scenes, key=lambda s: (role_order.get(s["role"], len(role_order)), s["start_s"])):
            if s["group_id"] in groups:
                sections.setdefault(s["group_id"], []).append(s)
        sections = [(groups[gid], sections[gid]) for gid in sorted(sections, reverse=True)]
        loose = [s for s in scenes if s["group_id"] not in groups]
        siblings = next((items for grp, items in sections if scene and grp["id"] == scene["group_id"]), [])
        return render_template("results.html", scenes=scenes, scene=scene, tests=tests,
                               sections=sections, loose=loose, siblings=siblings,
                               chart=chart.scatter(tests, prefs["best_vmaf"], prefs["vmaf_warn"]),
                               best=best_by_column(tests), sheet=sheet,
                               batches=[(row, db.batch_tests(c, row["id"])) for row in db.list_batches(c)],
                               sort=sort, best_vmaf=prefs["best_vmaf"])

    @app.get("/api/queue")
    def api_queue():
        return jsonify(db.queue_state(conn(), time.time()))

    @app.post("/ajustes")
    def ajustes():
        values = settings.parse(request.form)  # ValueError → 400 y no se guarda nada
        db.save_settings(conn(), values)
        return redirect(safe_next(request.form.get("next")), code=303)

    @app.get("/metricas")
    def metricas():
        prefs = settings.load(conn())
        scaled = glossary.with_thresholds(prefs["vmaf_warn"], prefs["best_vmaf"])
        return render_template("metricas.html", metrics=[scaled[m["key"]] for m in glossary.METRICS],
                               fields=glossary.FIELDS)

    @app.post("/tests/<int:test_id>/delete")
    def delete_test(test_id):
        c = conn()
        test = db.get_test(c, test_id)
        if test is None:
            abort(404)
        batch = db.get_batch(c, test["batch_id"])
        paths = db.frame_paths(c, [test_id])
        if not db.delete_test(c, test_id):
            return render_template("error.html", message="La prueba está en curso: espera a que termine."), 409
        frames.remove_files(frames_root, paths + db.drop_orphan_originals(c))
        return redirect(_after_delete(c, batch), code=303)

    @app.post("/batches/<int:batch_id>/delete")
    def delete_batch(batch_id):
        c = conn()
        batch = db.get_batch(c, batch_id)
        if batch is None:
            abort(404)
        paths = db.frame_paths(c, db.batch_test_ids(c, batch_id))
        db.delete_batch(c, batch_id)
        frames.remove_files(frames_root, paths + db.drop_orphan_originals(c))
        return redirect(_after_delete(c, batch), code=303)

    def _after_delete(c, batch) -> str:
        """next validado; si la batería borrada era la de la URL, se apunta a lo que sobreviva."""
        target = safe_next(request.form.get("next"))
        if db.get_batch(c, batch["id"]) is not None:
            return target
        if target == f"/batches/{batch['id']}":
            return "/"  # la página de una batería que ya no existe daría 404
        url = urlsplit(target)
        query = parse_qsl(url.query, keep_blank_values=True)
        m = re.fullmatch(r"/grupos/(\d+)", url.path)
        if m and not db.group_batches(c, int(m.group(1))):
            return "/"  # el grupo se quedó sin baterías
        if ("escena", str(batch["id"])) not in query:
            return target
        # El selector de escena usa el id de su batería más reciente: si era la borrada, otra de la misma escena.
        key = (batch["source_path"], batch["start_s"], batch["duration_s"])
        alive = next((s["batch_id"] for s in db.list_scenes(c)
                      if (s["source_path"], s["start_s"], s["duration_s"]) == key), None)
        query = [(k, str(alive) if k == "escena" else v) for k, v in query if k != "escena" or alive is not None]
        return url.path + ("?" + urlencode(query) if query else "")

    @app.get("/browse")
    def browse():
        folder = media.safe_media_path(root, request.args.get("path", ""), must_be_video=False)
        try:
            entries = sorted((p for p in folder.iterdir() if not p.name.startswith(".")), key=lambda p: p.name.lower())
            dirs = [(p.name, rel_of(p)) for p in entries if p.is_dir()]
            videos = [(p.name, rel_of(p)) for p in entries if p.is_file() and p.suffix.lower() in media.VIDEO_EXT]
        except OSError as e:
            raise ValueError(f"No se puede leer la carpeta: {e.strerror}") from e
        parent = rel_of(folder.parent) if folder != root else None
        return render_template("browse.html", here=rel_of(folder), parent=parent, dirs=dirs, videos=videos)

    @app.get("/new")
    def new():
        src = media.safe_media_path(root, request.args.get("file", ""))
        info = media.probe_source(src)
        c = conn()
        prefs = settings.load(c)
        if prefs["default_encoder"] not in available:  # p. ej. QSV guardado y este equipo sin QSV
            prefs = {**prefs, "default_encoder": available[0]}
        # start/duration llegan de "Añadir pruebas a esta escena"; si no valen, mandan los ajustes.
        duration = _maybe_time(request.args.get("duration"))
        if duration is None or not 2 <= duration <= 120:
            duration = float(prefs["default_duration"])
        start = _maybe_time(request.args.get("start"))
        if start is None or start < 0 or start + duration > info["duration_s"]:
            start = float(min(round(info["duration_s"] * prefs["default_start_pct"] / 100),
                              max(0, math.floor(info["duration_s"] - duration))))
        key = (rel_of(src), start, duration)
        same = sum(s["n_tests"] for s in db.list_scenes(c) if (s["source_path"], s["start_s"], s["duration_s"]) == key)
        return render_template("new.html", file=rel_of(src), info=info, start=start, duration=duration,
                               kbps=media.segment_kbps(src, start, duration), encoders={k: v for k, v in glossary.ENCODER_INFO.items() if k in available},
                               prefs=prefs, speeds=db.speed_history(c), frames=round(duration * info["fps"]),
                               same_scene_tests=same)

    @app.post("/batches")
    def create_batch():
        form = request.form
        src = media.safe_media_path(root, form.get("file", ""))
        starts = [parse_time(v) for v in form.getlist("start")]
        roles = (form.getlist("role") + [None] * len(starts))[:len(starts)]
        if not 1 <= len(starts) <= 5 or len(set(starts)) != len(starts):
            raise ValueError("Indica de 1 a 5 inicios distintos")
        dur = parse_time(form.get("duration", ""))
        if not 2 <= dur <= 120:
            raise ValueError("La duración del tramo debe estar entre 2 y 120 s")
        encoder = form.get("encoder", "")
        if encoder not in media.ENCODERS:
            raise ValueError(f"Encoder no admitido: {encoder}")
        if encoder not in available:
            raise ValueError(f"QSV no disponible en este equipo para {encoder}: {qsv_reason}")
        extra = form.get("extra", "").strip()
        media.parse_extra_args(extra)
        matrix = media.expand_matrix(form.get("qualities", ""), form.get("presets", ""))
        info = media.probe_source(src)
        if any(start < 0 or start + dur > info["duration_s"] for start in starts):
            raise ValueError("El tramo se sale del vídeo")
        c = conn()
        group = None
        if len(starts) > 1:
            analysis_id = form.get("analysis", type=int)
            group = db.create_group(c, rel_of(src), analysis_id if analysis_id and db.get_analysis(c, analysis_id) else None)
        batch_ids = []
        for start, role in zip(starts, roles):
            batch_ids.append(db.create_batch(
                c, source_path=rel_of(src), start_s=start, duration_s=dur, codec=info["codec"], width=info["width"],
                height=info["height"], fps=info["fps"], field_order=info["field_order"],
                episode_duration_s=info["duration_s"], source_kbps=media.segment_kbps(src, start, dur),
                ffmpeg_version=media.ffmpeg_version(), group_id=group, role=role if role in analysis.ROLES + ("extra",) else None))
            db.add_tests(c, batch_ids[-1], encoder, extra, matrix)
        return redirect(url_for("group", group_id=group) if group else url_for("batch", batch_id=batch_ids[0]), code=303)

    @app.get("/grupos")
    def groups():
        c = conn()
        wanted = request.args.get("g", type=int) or db.nav_targets(c)["group"]
        if wanted is None:
            abort(404)
        return redirect(url_for("group", group_id=wanted))

    @app.get("/grupos/<int:group_id>")
    def group(group_id):
        c = conn()
        g_ = db.get_group(c, group_id)
        batches = db.group_batches(c, group_id) if g_ else []
        if not batches:
            abort(404)
        prefs = settings.load(c)
        rows = [dict(r) for r in db.group_summary(c, group_id)]
        for i, r in enumerate(rows, start=1):
            # La gráfica pinta el peor tramo (VMAF) frente al % medio del original: así «ganador» = cumple en todos.
            r.update(id=i, status="done", vmaf_mean=r["vmaf_worst"], vmaf_p5=r["p5_worst"])
        good = [r for r in rows if r["vmaf_worst"] >= prefs["best_vmaf"]]
        return render_template("grupo.html", g=g_, all_groups=db.list_groups(c), batches=batches, rows=rows, best_vmaf=prefs["best_vmaf"],
                               win=min(good, key=lambda r: r["ratio"], default=None),
                               chart=chart.scatter(rows, prefs["best_vmaf"], prefs["vmaf_warn"]),
                               pending=sum(b["n_pending"] for b in batches))

    def _image(frame_id: int, key: str):
        f = db.get_frame(conn(), frame_id)
        path = Path(f[key]).resolve() if f else None
        if path is None or not path.is_relative_to(frames_root) or not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/webp", max_age=86400)

    @app.get("/frames/<int:frame_id>")
    def frame_image(frame_id):
        return _image(frame_id, "path")

    @app.get("/frames/<int:frame_id>/thumb")
    def frame_thumb(frame_id):
        return _image(frame_id, "thumb")

    @app.post("/ajustes/imagenes/borrar")
    def clear_images():
        frames.remove_files(frames_root, db.clear_frames(conn()))
        shutil.rmtree(frames_root / "analysis", ignore_errors=True)
        return redirect(safe_next(request.form.get("next")), code=303)

    @app.post("/analisis")
    def start_analysis():
        src = media.safe_media_path(root, request.form.get("file", ""))
        dur = round(parse_time(request.form.get("duration", "")))
        if not 2 <= dur <= 120:
            raise ValueError("La duración del tramo debe estar entre 2 y 120 s")
        st = src.stat()
        return jsonify({"id": db.create_analysis(conn(), rel_of(src), st.st_size, st.st_mtime_ns, float(dur))})

    @app.get("/api/analisis/<int:aid>")
    def api_analysis(aid):
        a = db.get_analysis(conn(), aid)
        if a is None:
            abort(404)
        n = min(5, max(1, request.args.get("n", type=int) or settings.load(conn())["auto_tramos"]))
        out = {"id": aid, "status": a["status"], "phase": a["phase"], "progress": a["progress"] or 0,
               "error": a["error"], "duration": a["duration_s"], "proposals": []}
        if a["status"] == "done":
            dur = int(a["duration_s"])
            for p in analysis.assign_roles(json.loads(a["candidates_json"]), n, dur):
                out["proposals"].append({
                    "role": p["role"], "start": p["start"], "start_hms": hms(p["start"]), "kbps": p["kbps"],
                    "si": p["si"], "ti": p["ti"], "yavg": p["yavg"],
                    "thumb": url_for("analysis_thumb", aid=aid, start=p["start"])})
        return jsonify(out)

    @app.get("/analisis/<int:aid>/<int:start>.webp")
    def analysis_thumb(aid, start):
        path = (frames_root / "analysis" / str(aid) / f"{start}.webp").resolve()
        if not path.is_relative_to(frames_root) or not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/webp", max_age=86400)

    @app.get("/comparar")
    def comparar():
        c = conn()
        b = db.get_batch(c, request.args.get("escena", type=int) or 0)
        if b is None:
            abort(404)
        originals = db.scene_originals(c, b["source_path"], b["start_s"], b["duration_s"])
        by_test: dict = {}
        for f in db.scene_frames(c, b["source_path"], b["start_s"], b["duration_s"]):
            by_test.setdefault(f["test_id"], {})[f["idx"]] = f
        options = [{"key": "orig", "label": "Original", "frames": by_test.get(None, {}), "test": None}]
        for t in db.scene_tests(c, b["id"]):
            if t["id"] in by_test:
                options.append({"key": str(t["id"]), "label": f'{t["encoder"]} {t["quality"]}·{t["preset"]}',
                                "frames": by_test[t["id"]], "test": t})
        keys = [o["key"] for o in options]
        a = request.args.get("a", "orig")
        a = a if a in keys else "orig"
        bsel = request.args.get("b", keys[1] if len(keys) > 1 else "orig")
        bsel = bsel if bsel in keys else keys[-1]
        idxs = [o["idx"] for o in originals]
        f = request.args.get("f", type=int)
        f = f if f in idxs else (idxs[0] if idxs else None)
        return render_template("comparar.html", b=b, scenes=db.compare_scenes(c), options=options, a=a, bsel=bsel, idxs=idxs, f=f,
                               roles={o["idx"]: o["role"] for o in originals})

    @app.get("/batches/<int:batch_id>")
    def batch(batch_id):
        b = db.get_batch(conn(), batch_id)
        if b is None:
            abort(404)
        tests = db.batch_tests(conn(), batch_id)
        pending = any(t["status"] in ("queued", "running") for t in tests)
        return render_template("batch.html", b=b, tests=tests, pending=pending)

    with closing(db.connect(app.config["DB_PATH"])) as c:
        db.reset_running(c)
        db.reset_running_analyses(c)
    if app.config["START_WORKER"]:
        # shortcut: un hilo por proceso; exige gunicorn con -w 1 (si no, habría varios workers en la cola).
        threading.Thread(target=worker_loop, daemon=True,
                         args=(app.config["DB_PATH"], root, Path(app.config["WORK_DIR"]), app.config["VMAF_THREADS"],
                               threading.Event()), kwargs={"frames_root": frames_root}).start()
        threading.Thread(target=analysis.analysis_loop, daemon=True,
                         args=(app.config["DB_PATH"], root, frames_root, threading.Event())).start()
    return app
