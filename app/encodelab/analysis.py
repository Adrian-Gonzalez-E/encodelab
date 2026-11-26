"""Análisis automático de tramos: perfil de bitrate, keyframes, candidatas y reparto por roles.

Fase 1 (paquetes, sin decodificar) y fase 2 (solo keyframes) se cachean por fichero. La fase 3 decodifica a 480p
unas pocas ventanas candidatas y mide SI, TI y brillo. Nada de esto usa más que ffmpeg/ffprobe y la stdlib."""
import bisect
import json
import logging
import math
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import db, media

log = logging.getLogger(__name__)

STEP_S = 5            # paso entre ventanas candidatas
DARK_MIN = 20         # YAVG por debajo (de 255) = negro o fundido: no es una escena oscura útil
STATIC_TI = 1.0       # TI medio por debajo = imagen casi parada (créditos, logos)
ROLES = ("exigente", "representativa", "oscura", "detalle", "movimiento")


# ---------- funciones puras ----------
def margins(total: float) -> tuple[float, float]:
    """Segundos a ignorar al inicio y al final (intro y créditos)."""
    return max(90.0, 0.04 * total), max(120.0, 0.08 * total)


def window_stats(per_second: list[float], keyframes: list[list[float]], dur: int, total: float) -> list[dict]:
    """Ventanas de `dur` s cada STEP_S: kbps medio y, si hay keyframes, SI y brillo medios."""
    head, tail = margins(total)
    prefix = [0.0]
    for v in per_second:
        prefix.append(prefix[-1] + v)
    times = [k[0] for k in keyframes]
    out = []
    start = math.ceil(head)
    while start + dur <= total - tail and start + dur < len(prefix):
        w = {"start": start, "kbps": (prefix[start + dur] - prefix[start]) / dur, "si": None, "yavg": None}
        lo, hi = bisect.bisect_left(times, start), bisect.bisect_left(times, start + dur)
        if hi > lo:
            w["si"] = statistics.fmean(k[1] for k in keyframes[lo:hi])
            w["yavg"] = statistics.fmean(k[2] for k in keyframes[lo:hi])
        out.append(w)
        start += STEP_S
    return out


def _quantile(sorted_values: list[float], q: float) -> float:
    return sorted_values[math.floor(q * (len(sorted_values) - 1))]


def pick_candidates(per_second: list[float], keyframes: list[list[float]], dur: int, total: float,
                    limit: int = 14) -> list[dict]:
    """Ventanas a analizar a fondo: las de más bitrate, las medianas, las de más SI y las más oscuras.

    Sin solape entre sí; fuera de los márgenes y de lo que queda bajo el p5 de bitrate (créditos, negros)."""
    wins = window_stats(per_second, keyframes, dur, total)
    if not wins:
        return []
    ordered = sorted(w["kbps"] for w in wins)
    p5, p30, median = _quantile(ordered, 0.05), _quantile(ordered, 0.3), _quantile(ordered, 0.5)
    for w in wins:  # percentil de bitrate dentro del episodio entero (para «representativa»)
        w["pct"] = bisect.bisect_right(ordered, w["kbps"]) / len(ordered) * 100
    wins = [w for w in wins if w["kbps"] >= p5]
    chosen: list[dict] = []

    def take(order, count):
        got = 0
        for w in order:
            if got >= count:
                break
            if all(abs(w["start"] - c["start"]) >= dur for c in chosen):
                chosen.append(w)
                got += 1

    take(sorted(wins, key=lambda w: -w["kbps"]), 4)
    take(sorted(wins, key=lambda w: abs(w["kbps"] - median)), 3)
    with_si = [w for w in wins if w["si"] is not None]
    take(sorted(with_si, key=lambda w: -w["si"]), 3)
    take(sorted((w for w in with_si if w["yavg"] >= DARK_MIN and w["kbps"] >= p30), key=lambda w: w["yavg"]), 3)
    return chosen[:limit]


def _robust_z(values: list[float]) -> list[float]:
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values) * 1.4826 or 1.0
    return [(v - med) / mad for v in values]


def assign_roles(cands: list[dict], n: int, dur: int) -> list[dict]:
    """Los n mejores tramos por rol (exigente, representativa, oscura, detalle, movimiento), sin acercarse
    a menos de 2× la duración entre sí. Cada candidata trae kbps, si, ti y yavg (ya medidos)."""
    if not cands or n < 1:
        return []
    for c, zk, zs, zt in zip(cands, _robust_z([c["kbps"] for c in cands]), _robust_z([c["si"] for c in cands]),
                             _robust_z([c["ti"] for c in cands])):
        c["score"] = zk + zs + zt
    mid = statistics.median(c["score"] for c in cands)
    orders = {"exigente": sorted(cands, key=lambda c: -c["score"]),
              # Mediana del episodio (percentil de bitrate); sin él, la de las candidatas.
              "representativa": sorted(cands, key=lambda c: abs(c["pct"] - 50) if "pct" in c else abs(c["score"] - mid)),
              "oscura": sorted(cands, key=lambda c: c["yavg"]),
              "detalle": sorted(cands, key=lambda c: -c["si"]),
              "movimiento": sorted(cands, key=lambda c: -c["ti"])}
    picks: list[dict] = []

    def far(c):
        return all(abs(c["start"] - p["start"]) >= 2 * dur for p in picks)

    for role in ROLES:
        if len(picks) >= n:
            break
        c = next((c for c in orders[role] if far(c)), None)
        if c:
            picks.append({**c, "role": role})
    for c in orders["exigente"]:  # si los roles no llegan a n, se rellena con lo más exigente que quede
        if len(picks) >= n:
            break
        if far(c):
            picks.append({**c, "role": "extra"})
    return picks


def summarize_window(frames: list[dict]) -> dict | None:
    """SI medio, TI (p90 y medio) y brillo medio de los fotogramas de una ventana."""
    si = [f["si"] for f in frames if "si" in f]
    ti = sorted(f["ti"] for f in frames if "ti" in f)
    yavg = [f["yavg"] for f in frames if "yavg" in f]
    if not si or not ti or not yavg:
        return None
    return {"si": statistics.fmean(si), "ti": _quantile(ti, 0.9), "ti_mean": statistics.fmean(ti),
            "yavg": statistics.fmean(yavg)}


# ---------- ffmpeg/ffprobe ----------
def _stream(cmd: list[str], work: Path | None = None):
    """Líneas de stdout de cmd a medida que salen; EncodeError si termina mal (con el final de stderr)."""
    with tempfile.TemporaryFile(mode="w+", dir=work) as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            yield from proc.stdout
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if code != 0:
            err.seek(0)
            raise media.EncodeError(f"Código {code}: {err.read()[-2000:]}")


def scan_packets(src: Path, total: float, report=None) -> list[float]:
    """kbps de cada segundo del vídeo, sumando los tamaños de paquete (sin decodificar)."""
    bits = [0.0] * (math.ceil(total) + 1)
    cmd = ["nice", "-n", "15", "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
           "packet=pts_time,size", "-of", "csv=p=0", str(src)]
    for line in _stream(cmd):
        t, _, size = line.strip().partition(",")
        try:
            sec, nbytes = int(float(t)), int(size)
        except ValueError:
            continue
        if 0 <= sec < len(bits):
            bits[sec] += nbytes * 8
        if report and sec % 20 == 0:
            report(min(sec / total, 1.0))
    return [b / 1000 for b in bits]


def scan_keyframes(src: Path, total: float, report=None) -> list[list[float]]:
    """[t, SI, brillo] de cada keyframe a 480p. El TI de esta pasada se descarta (muestras separadas por segundos)."""
    cmd = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-skip_frame", "nokey",
           "-i", str(src), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf",
           "scale=480:-2:flags=fast_bilinear,siti,signalstats,metadata=print:file=-", "-fps_mode", "passthrough",
           "-f", "null", "-"]
    out, last = [], 0
    for f in media.iter_metadata(_stream(cmd)):
        if {"pts", "si", "yavg"} <= f.keys():
            out.append([f["pts"], f["si"], f["yavg"]])
            if report and f["pts"] - last >= 20:
                last = f["pts"]
                report(min(f["pts"] / total, 1.0))
    return out


def measure_window(src: Path, start: int, dur: int) -> dict | None:
    r = subprocess.run(media.siti_cmd(src, start, dur, signalstats=True), capture_output=True, text=True)
    if r.returncode != 0:
        raise media.EncodeError(f"Código {r.returncode}: {r.stderr[-2000:]}")
    return summarize_window(media.parse_metadata(r.stdout))


def make_thumb(src: Path, at: float, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    media._run(["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", str(at),
                "-i", str(src), "-frames:v", "1", "-an", "-sn", "-vf", "scale=480:-2", "-c:v", "libwebp",
                "-quality", "80", "-y", str(dest)])


# ---------- ejecución ----------
def run_analysis(conn, row, media_root: Path, frames_root: Path) -> None:
    """Fases 1–3 de un análisis ya reclamado; el estado y el error quedan en su fila."""
    aid, rel, dur = row["id"], row["source_path"], int(row["duration_s"])
    last = [0.0]

    def progress(phase, base, span):
        def report(frac):
            now = time.monotonic()
            if now - last[0] >= 1:  # como mucho una escritura por segundo
                last[0] = now
                db.set_analysis_progress(conn, aid, phase, round(base + span * frac, 4))
        return report

    try:
        src = Path(media_root) / rel
        st = src.stat()
        total = media.probe_source(src)["duration_s"]
        prof = db.get_profile(conn, rel, st.st_size, st.st_mtime_ns)
        if prof:
            per_second, keyframes = json.loads(prof["per_second_json"]), json.loads(prof["keyframes_json"])
            base = 0.0
        else:
            per_second = scan_packets(src, total, progress("paquetes", 0.0, 0.35))
            keyframes = scan_keyframes(src, total, progress("keyframes", 0.35, 0.35))
            db.save_profile(conn, rel, st.st_size, st.st_mtime_ns, json.dumps([round(v, 1) for v in per_second]),
                            json.dumps([[round(a, 2), round(b, 2), round(c, 1)] for a, b, c in keyframes]))
            base = 0.7
        cands = pick_candidates(per_second, keyframes, dur, total)
        done = []
        thumbs = Path(frames_root) / "analysis" / str(aid)
        for i, c in enumerate(cands):
            m = measure_window(src, c["start"], dur)
            db.set_analysis_progress(conn, aid, "candidatas", round(base + (1 - base) * (i + 1) / len(cands), 4))
            if m is None or m["ti_mean"] < STATIC_TI or m["yavg"] < DARK_MIN:
                continue
            make_thumb(src, c["start"] + dur / 2, thumbs / f"{c['start']}.webp")
            done.append({"start": c["start"], "kbps": round(c["kbps"], 1), "pct": round(c["pct"], 1), "si": round(m["si"], 2),
                         "ti": round(m["ti"], 2), "yavg": round(m["yavg"], 1)})
        if not done:
            raise media.EncodeError("No se encontró ningún tramo útil (¿vídeo muy corto o casi estático?)")
        db.finish_analysis(conn, aid, json.dumps(done))
        for old in db.prune_analyses(conn, aid):
            shutil.rmtree(Path(frames_root) / "analysis" / str(old), ignore_errors=True)
    except Exception as e:
        log.warning("Análisis %s en error: %s", aid, e)
        db.fail_analysis(conn, aid, str(e))


def analysis_loop(db_path: str, media_root: Path, frames_root: Path, stop: threading.Event, idle_s: float = 2.0) -> None:
    conn = db.connect(db_path)
    while not stop.is_set():
        try:
            row = db.claim_analysis(conn)
            if row is None:
                stop.wait(idle_s)
                continue
            run_analysis(conn, row, media_root, frames_root)
        except Exception:  # fallo de la propia base: el hilo no puede morir
            log.exception("Error del análisis; se reintenta")
            stop.wait(idle_s)
