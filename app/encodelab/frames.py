"""Fotogramas de comparación: elección, extracción a WebP sin pérdida y miniaturas."""
import hashlib
import logging
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import db, media

log = logging.getLogger(__name__)

MATRICES = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470bg", "bt2020nc": "bt2020"}
THUMB_W = 480


def matrix_for(info: dict) -> str:
    """Matriz YUV→RGB de la fuente; sin etiqueta, bt709 desde 720 líneas. La misma para original y encode."""
    return MATRICES.get(info.get("color_space") or "") or ("bt709" if (info.get("height") or 0) >= 720 else "smpte170m")


def range_for(info: dict) -> str:
    return "pc" if info.get("color_range") == "pc" else "tv"


def scene_key(source_path: str, start: float, dur: float) -> str:
    return hashlib.sha1(f"{source_path}|{start}|{dur}".encode()).hexdigest()[:12]


def _percentile_index(values: list[tuple[int, float]], q: float) -> float:
    ordered = sorted(v for _, v in values)
    return ordered[math.floor(q * (len(ordered) - 1))]


def pick_indexes(series: list[dict], k: int, fps: float) -> list[tuple[int, str]]:
    """k fotogramas (índice, rol) de un tramo a partir de su serie SI/TI, ordenados por índice.

    detalle = máximo SI; movimiento = TI cerca del percentil 90 (el máximo suele ser un corte de plano);
    medio = el central; el resto se reparte. Sin extremos y separados al menos 1 s."""
    n = len(series)
    if n == 0:
        return []
    margin = max(2, n // 20)
    inner = range(margin, max(margin + 1, n - margin))
    sep = max(1, round(fps))
    si = [(i, series[i]["si"]) for i in inner if "si" in series[i]]
    ti = [(i, series[i]["ti"]) for i in inner if "ti" in series[i]]
    prefs: list[tuple[str, list[int]]] = []
    if si:
        prefs.append(("detalle", [i for i, _ in sorted(si, key=lambda x: -x[1])]))
    if ti:
        target = _percentile_index(ti, 0.9)
        prefs.append(("movimiento", [i for i, _ in sorted(ti, key=lambda x: abs(x[1] - target))]))
    prefs.append(("medio", sorted(inner, key=lambda i: abs(i - n // 2))))
    extra = max(0, k - len(prefs))
    for j in range(1, extra + 1):
        goal = n * j / (extra + 1)
        prefs.append(("extra", sorted(inner, key=lambda i, g=goal: abs(i - g))))
    chosen: list[tuple[int, str]] = []
    for role, order in prefs:
        if len(chosen) >= k:
            break
        pick = next((i for i in order if all(abs(i - c) >= sep for c, _ in chosen)), None)
        if pick is not None:
            chosen.append((pick, role))
    return sorted(chosen)


def series_siti(src: Path, start: float, dur: float) -> list[dict]:
    r = subprocess.run(media.siti_cmd(src, start, dur), capture_output=True, text=True)
    if r.returncode != 0:
        raise media.EncodeError(f"Código {r.returncode}: {r.stderr[-2000:]}")
    return media.parse_metadata(r.stdout)


def extract(src: Path, indexes: list[int], dests: list[Path], thumbs: list[Path], matrix: str, rng: str,
            start: float | None = None, dur: float | None = None, work: Path | None = None) -> None:
    """Guarda los fotogramas `indexes` (ascendentes) de src como WebP sin pérdida y su miniatura.

    Con start/dur lee el tramo igual que encode_cmd (-ss/-t antes de -i), así el índice n es el del clip."""
    if not indexes:
        return
    select = "+".join(f"eq(n\\,{i})" for i in indexes)
    # Misma matriz y rango en original y encode: el clip codificado no hereda la etiqueta de color de la fuente.
    chain = (f"select={select},scale=in_range={rng}:in_color_matrix={matrix}:flags=accurate_rnd+full_chroma_int,"
             "format=bgra")
    seek = ["-ss", str(start), "-t", str(dur)] if start is not None else []
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        cmd = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *seek, "-i", str(src),
               "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", chain, "-fps_mode", "passthrough",
               "-frames:v", str(len(indexes)), "-c:v", "libwebp", "-lossless", "1", "-compression_level", "6",
               f"{tmp}/f_%d.webp"]
        media._run(cmd)
        for n, (dest, thumb) in enumerate(zip(dests, thumbs), start=1):
            made = Path(tmp) / f"f_{n}.webp"
            if not made.exists():
                raise media.EncodeError(f"Falta el fotograma {indexes[n - 1]} (el tramo es más corto)")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(made, dest)
            media._run(["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(dest),
                        "-vf", f"scale={THUMB_W}:-2", "-c:v", "libwebp", "-quality", "80", "-frames:v", "1",
                        "-y", str(thumb)])


def ensure_originals(conn, root: Path, media_root: Path, source_path: str, start: float, dur: float, k: int,
                     info: dict) -> list:
    """Fotogramas del original de la escena: se calculan una vez y todas las pruebas reutilizan los mismos."""
    rows = db.scene_originals(conn, source_path, start, dur)
    if rows:
        return rows
    src = Path(media_root) / source_path
    picks = pick_indexes(series_siti(src, start, dur), k, info["fps"])
    if not picks:
        return []
    folder = Path(root) / scene_key(source_path, start, dur)
    outs = [folder / f"orig_{i}.webp" for i, _ in picks]
    thumbs = [folder / f"orig_{i}_s.webp" for i, _ in picks]
    extract(src, [i for i, _ in picks], outs, thumbs, matrix_for(info), range_for(info), start=start, dur=dur)
    db.add_frames(conn, [dict(source_path=source_path, start_s=start, duration_s=dur, test_id=None, idx=i, role=role,
                              vmaf=None, bytes=o.stat().st_size, path=str(o), thumb=str(t))
                         for (i, role), o, t in zip(picks, outs, thumbs)])
    return db.scene_originals(conn, source_path, start, dur)


def plan_for_test(root: Path, row, originals) -> list[tuple[int, Path, Path]]:
    folder = Path(root) / scene_key(row["source_path"], row["start_s"], row["duration_s"])
    return [(o["idx"], folder / f"t{row['id']}_{o['idx']}.webp", folder / f"t{row['id']}_{o['idx']}_s.webp")
            for o in originals]


def remove_files(root: Path, paths) -> None:
    """Borra solo ficheros bajo root (la ruta viene de la base de datos, pero no se fía)."""
    base = Path(root).resolve()
    for p in paths:
        try:
            path = Path(p).resolve()
            if path.is_relative_to(base) and path.is_file():
                path.unlink()
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
        except OSError as e:
            log.warning("No se pudo borrar %s: %s", p, e)
