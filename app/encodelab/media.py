"""ffmpeg/ffprobe: comandos de codificación y de métricas."""
import itertools
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ENCODERS = {
    "libsvtav1": {"quality": "-crf", "qsv": False, "codec": "av1"},
    "av1_qsv": {"quality": "-global_quality", "qsv": True, "codec": "av1"},
    "libx265": {"quality": "-crf", "qsv": False, "codec": "hevc"},
    "hevc_qsv": {"quality": "-global_quality", "qsv": True, "codec": "hevc"},
}
# Sin .ts/.m2ts: el seek en MPEG-TS es inexacto y desalinea el tramo (remuxar a MKV antes).
VIDEO_EXT = {".mkv", ".mp4"}
INTERLACED = {"tt", "bb", "tb", "bt"}
PRESET_RE = re.compile(r"^[a-z0-9]+$")
# libsvtav1 y libx265 solo avisan (y siguen) ante un -*-params erróneo: se trata como fallo.
BAD_PARAM_RE = re.compile(r"Error parsing option|Unknown option|Invalid value for")
# Prefijo de protocolo de ffmpeg (file:, tcp:, http:...): sería una salida o entrada extra.
PROTOCOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# Van antes de -i: los encoders QSV reciben fotogramas en memoria de sistema y los suben a la GPU.
QSV_DEVICE = os.environ.get("QSV_DEVICE", "/dev/dri/renderD128")


def qsv_args(device: str) -> list[str]:
    # QSV derivado de VAAPI: con varias GPU, qsv=hw:<nodo> ignora el nodo y usa el primer adaptador.
    return ["-init_hw_device", f"vaapi=va:{device}", "-init_hw_device", "qsv=hw@va", "-filter_hw_device", "hw"]


QSV_ARGS = qsv_args(QSV_DEVICE)


def _qsv_try(device: str, encoder: str) -> str | None:
    """None si `encoder` codifica 1 fotograma en `device`; si no, el motivo."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *qsv_args(device), "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.04",
           "-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return "ffmpeg no respondió en 8 s"
    except OSError as e:
        return str(e)
    if r.returncode == 0:
        return None
    lines = [line for line in r.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f"ffmpeg salió con código {r.returncode}"


def detect_qsv(device: str = QSV_DEVICE) -> tuple[list[str], str]:
    """Encoders QSV que funcionan en `device` y un resumen legible ("ok" o los motivos de los que no)."""
    if not Path(device).exists():
        return [], f"sin dispositivo {device}"
    # Sin permiso, el ffmpeg estático aborta (core dump) al intentar X11: se evita lanzarlo.
    if not os.access(device, os.R_OK | os.W_OK):
        return [], (f"sin permiso en {device} (uid {os.getuid()}, grupos {os.getgroups()}): "
                    "añade RENDER_GID en .env y usa compose.qsv.yaml")
    ok, failed = [], []
    for name in (n for n, e in ENCODERS.items() if e["qsv"]):
        reason = _qsv_try(device, name)
        if reason is None:
            ok.append(name)
        else:
            failed.append(f"{name}: {reason}")
    return ok, "ok" if not failed else "; ".join(failed)


def available_encoders(qsv: list[str]) -> list[str]:
    return [name for name, enc in ENCODERS.items() if not enc["qsv"] or name in qsv]


# Una pasada de libvmaf: VMAF, VMAF NEG, PSNR, SSIM y MS-SSIM. Entradas: [distorsionada][referencia].
VMAF_FILTER = (
    "libvmaf=model='version=vmaf_v0.6.1\\:name=vmaf|version=vmaf_v0.6.1neg\\:name=vmaf_neg'"
    ":feature='name=psnr|name=float_ssim|name=float_ms_ssim'"
    ":log_fmt=json:log_path={log}:n_threads={threads}"
)


def safe_media_path(root: Path, rel: str, must_be_video: bool = True) -> Path:
    """Ruta dentro de root: un vídeo admitido o, con must_be_video=False, una carpeta."""
    root = Path(root).resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.exists():
        raise ValueError(f"Ruta no válida: {rel}")
    if must_be_video and not (path.is_file() and path.suffix.lower() in VIDEO_EXT):
        raise ValueError(f"No es un vídeo admitido: {rel}")
    if not must_be_video and not path.is_dir():
        raise ValueError(f"No es una carpeta: {rel}")
    return path


def parse_extra_args(text: str) -> list[str]:
    try:
        args = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Argumentos extra mal formados: {e}") from e
    for arg in args:
        # Sin entradas, sobrescritura, rutas ni protocolos: la única salida la fija encode_cmd.
        if arg in ("-i", "-y") or "/" in arg or PROTOCOL_RE.match(arg):
            raise ValueError(f"Argumento extra no permitido: {arg}")
    return args


def expand_matrix(qualities: str, presets: str, limit: int = 24) -> list[tuple[int, str]]:
    qs = [q.strip() for q in qualities.split(",") if q.strip()]
    ps = [p.strip() for p in presets.split(",") if p.strip()]
    if not qs or not ps:
        raise ValueError("Indica al menos una calidad y un preset")
    try:
        nums = [int(q) for q in qs]
    except ValueError as e:
        raise ValueError("Las calidades deben ser números enteros") from e
    if any(not 0 <= q <= 255 for q in nums):
        raise ValueError("Calidad fuera del rango 0-255")
    if any(not PRESET_RE.match(p) for p in ps):
        raise ValueError("Preset no válido (solo minúsculas y dígitos)")
    if len(nums) * len(ps) > limit:
        raise ValueError(f"Máximo {limit} pruebas por batería")
    return list(itertools.product(nums, ps))


def codec_args(encoder: str, quality: int, preset: str, extra: list[str]) -> list[str]:
    return ["-c:v", encoder, ENCODERS[encoder]["quality"], str(quality), "-preset", preset, *extra]


def encode_cmd(src: Path, start: float, dur: float, codec: list[str], out: Path, qsv: bool) -> list[str]:
    # -ss antes de -i: seek rápido y exacto al decodificar; metrics_cmd lee el mismo tramo.
    return ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-y", *(QSV_ARGS if qsv else []),
            "-ss", str(start), "-t", str(dur), "-i", str(src),
            "-map", "0:v:0", "-an", "-sn", "-dn", *codec, "-progress", "pipe:1", "-nostats", str(out)]


def metrics_cmd(src: Path, start: float, dur: float, encoded: Path, log: Path, threads: int) -> list[str]:
    # Emparejado por índice (setpts=N): en MKV a 23,976 los ts redondeados a 1 ms desalinean PTS-STARTPTS.
    graph = ("[0:v]settb=AVTB,setpts=N,format=yuv420p10le[dis];"
             "[1:v]settb=AVTB,setpts=N,format=yuv420p10le[ref];"
             "[dis][ref]" + VMAF_FILTER.format(log=log, threads=threads))
    return ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-i", str(encoded),
            "-ss", str(start), "-t", str(dur), "-i", str(src), "-lavfi", graph,
            "-progress", "pipe:1", "-nostats", "-f", "null", "-"]


METADATA_KEYS = {"lavfi.siti.si": "si", "lavfi.siti.ti": "ti", "lavfi.signalstats.YAVG": "yavg"}


def siti_cmd(src: Path, start: float | None, dur: float | None, extra_in: list[str] | None = None,
             signalstats: bool = False) -> list[str]:
    """Decodifica (a 480p) y escribe SI/TI —y brillo— por fotograma con metadata=print en stdout."""
    seek = ["-ss", str(start), "-t", str(dur)] if start is not None else []
    chain = "scale=480:-2:flags=fast_bilinear,siti" + (",signalstats" if signalstats else "") + ",metadata=print:file=-"
    return ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *(extra_in or []), *seek,
            "-i", str(src), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", chain, "-f", "null", "-"]


def iter_metadata(lines):
    """Un dict por fotograma ({pts, si, ti, yavg}) a medida que se completa; los NaN de siti no aparecen."""
    cur = None
    for line in lines:
        if line.startswith("frame:"):
            if cur is not None:
                yield cur
            cur = {}
            m = re.search(r"pts_time:(\S+)", line)
            if m:
                try:
                    cur["pts"] = float(m.group(1))
                except ValueError:
                    pass
            continue
        key, _, value = line.strip().partition("=")
        if cur is not None and key in METADATA_KEYS:
            try:
                number = float(value)
            except ValueError:
                continue
            if math.isfinite(number):
                cur[METADATA_KEYS[key]] = number
    if cur is not None:
        yield cur


def parse_metadata(text: str) -> list[dict]:
    return list(iter_metadata(text.splitlines()))


def summarize_vmaf(log: dict) -> dict:
    metrics = [f["metrics"] for f in log["frames"]]
    vmaf = sorted(m["vmaf"] for m in metrics)

    def mean(key):
        return float(statistics.mean(m[key] for m in metrics))

    return {"frames": len(metrics), "vmaf_mean": mean("vmaf"), "vmaf_min": vmaf[0],
            "vmaf_p5": vmaf[math.floor(0.05 * (len(vmaf) - 1))], "vmaf_neg_mean": mean("vmaf_neg"),
            "psnr_y": mean("psnr_y"), "ssim": mean("float_ssim"), "ms_ssim": mean("float_ms_ssim")}


def parse_probe(data: dict) -> dict:
    s = data["streams"][0]
    num, den = (int(x) for x in s["r_frame_rate"].split("/"))
    field_order = s.get("field_order", "unknown")
    return {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"),
            "fps": num / den if den else 0.0, "field_order": field_order,
            "interlaced": field_order in INTERLACED, "pix_fmt": s.get("pix_fmt"),
            "color_space": s.get("color_space"), "color_range": s.get("color_range"),
            "duration_s": float(data["format"]["duration"])}


class EncodeError(Exception):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise EncodeError(f"Código {r.returncode}: {r.stderr[-2000:]}")
    return r


def _run_live(cmd: list[str], work: Path, report) -> tuple[str, int]:
    """Como _run, leyendo el -progress de stdout: report(frame, out_us) por bloque. Devuelve (stderr, último frame)."""
    # stderr a un fichero anónimo: leer dos PIPE a la vez podría bloquear ffmpeg.
    with tempfile.TemporaryFile(mode="w+", dir=work) as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        frame, out_us = 0, 0
        try:
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "frame" and value.isdigit():
                    frame = int(value)
                elif key == "out_time_us" and value.isdigit():
                    out_us = int(value)
                elif key == "progress" and report:
                    report(frame, out_us)
            code = proc.wait()
        finally:
            # Si report (o la lectura) falla, ffmpeg no puede quedar huérfano con el pipe lleno.
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        err.seek(0)
        stderr = err.read()
    if code != 0:
        raise EncodeError(f"Código {code}: {stderr[-2000:]}")
    return stderr, frame


def ffmpeg_version() -> str:
    return _run(["ffmpeg", "-version"]).stdout.splitlines()[0]


def probe_source(src: Path) -> dict:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_format", "-show_streams",
                "-of", "json", str(src)]).stdout
    return parse_probe(json.loads(out))


def segment_kbps(src: Path, start: float, dur: float) -> float:
    # shortcut: read_intervals empieza en el keyframe anterior a start; sobrestima un poco en GOPs largos.
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{start}%+{dur}",
                "-show_entries", "packet=size", "-of", "csv=p=0", str(src)]).stdout
    return sum(int(line) for line in out.split() if line.isdigit()) * 8 / dur / 1000


def _packets(path: Path) -> int:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)]).stdout
    return int(out.strip())


def run_test(src: Path, start: float, dur: float, encoder: str, quality: int, preset: str,
             extra: list[str], work: Path, threads: int, on_progress=None, frames=None) -> dict:
    """on_progress(phase, frac, frame): frac global, codificar 0–0,7 y medir 0,7–1,0.

    frames = {"items": [(idx, imagen, miniatura)], "matrix": ..., "range": ...}: fotogramas del clip codificado
    que se guardan antes de borrarlo (un fallo al guardarlos no invalida la prueba)."""
    codec = codec_args(encoder, quality, preset, extra)

    def stage(phase, base, span, frac_of):
        if on_progress is None:
            return None
        return lambda frame, out_us: on_progress(
            phase, round(base + span * min(max(frac_of(frame, out_us), 0.0), 1.0), 4), frame)

    tag = uuid.uuid4().hex
    out, log = Path(work) / f"{tag}.mkv", Path(work) / f"{tag}.json"
    try:
        t0 = time.monotonic()
        enc_err, enc_frames = _run_live(encode_cmd(src, start, dur, codec, out, ENCODERS[encoder]["qsv"]), work,
                                        stage("encode", 0.0, 0.7, lambda f, us: us / (dur * 1e6)))
        encode_s = time.monotonic() - t0
        bad = [line for line in enc_err.splitlines() if BAD_PARAM_RE.search(line)]
        if bad:
            raise EncodeError("Parámetro del encoder no aceptado: " + " | ".join(bad))
        t1 = time.monotonic()
        # En la medida out_time no sirve (setpts=N deja los pts en µs de índice): cuenta por fotogramas.
        _run_live(metrics_cmd(src, start, dur, out, log, threads), work,
                  stage("metrics", 0.7, 0.3, lambda f, us: f / enc_frames if enc_frames else us / (dur * 1e6)))
        measure_s = time.monotonic() - t1
        summary = summarize_vmaf(json.loads(log.read_text()))
        packets = _packets(out)
        if packets != summary["frames"]:
            raise EncodeError(f"Tramo desalineado: {packets} fotogramas codificados y {summary['frames']} medidos")
        size = out.stat().st_size
        saved = {}
        if frames and frames["items"]:
            from . import frames as fr  # import tardío: frames.py usa EncodeError y _run de este módulo
            vmaf_log = json.loads(log.read_text())["frames"]
            items = [it for it in frames["items"] if it[0] < len(vmaf_log)]
            try:
                fr.extract(out, [i for i, _, _ in items], [d for _, d, _ in items], [t for _, _, t in items],
                           frames["matrix"], frames["range"], work=work)
                saved = {i: vmaf_log[i]["metrics"]["vmaf"] for i, _, _ in items}
            except Exception as e:  # las imágenes son accesorias: la prueba sigue valiendo
                import logging
                logging.getLogger(__name__).warning("Fotogramas no guardados: %s", e)
        if on_progress:
            on_progress("metrics", 1.0, summary["frames"])
        return {**summary, "frame_vmaf": saved, "ffmpeg_args": shlex.join(codec), "size_bytes": size, "kbps": size * 8 / dur / 1000,
                "encode_s": encode_s, "enc_fps": summary["frames"] / encode_s, "measure_s": measure_s}
    finally:
        out.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
