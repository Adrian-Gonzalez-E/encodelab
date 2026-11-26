"""Ajustes del usuario (tabla settings): valores por defecto, validación y carga tipada."""
from . import db, media

DEFAULTS = {
    "default_duration": 20, "default_start_pct": 30, "default_encoder": "libsvtav1",
    "extras_libsvtav1": "-pix_fmt yuv420p10le", "extras_av1_qsv": "-pix_fmt p010le",
    "extras_libx265": "", "extras_hevc_qsv": "",
    "best_vmaf": 93, "vmaf_warn": 80, "card_style": "grid", "background": "particulas", "default_view": "panel", "refresh_s": 2, "animations": True,
    "save_frames": True, "frames_per_scene": 3, "frames_cap_gb": 3, "auto_tramos": 3,
}
VIEWS = ("panel", "baterias")
REFRESH = ("0", "2", "5", "10")
CARD_STYLES = ("grid", "gauges")
BACKGROUNDS = ("particulas", "aurora", "rejilla", "ondas")


def _int(form, key: str, lo: int, hi: int, label: str) -> str:
    try:
        value = int(str(form.get(key, "")).strip())
    except ValueError as e:
        raise ValueError(f"{label}: debe ser un número entero") from e
    if not lo <= value <= hi:
        raise ValueError(f"{label}: debe estar entre {lo} y {hi}")
    return str(value)


def parse(form) -> dict[str, str]:
    """Valida el formulario de ajustes; ValueError ante el primer valor inválido (no se guarda nada)."""
    out = {"default_duration": _int(form, "default_duration", 2, 120, "Duración del tramo"),
           "default_start_pct": _int(form, "default_start_pct", 0, 95, "Inicio del tramo"),
           "best_vmaf": _int(form, "best_vmaf", 50, 100, "Bueno desde (VMAF)"),
           "vmaf_warn": _int(form, "vmaf_warn", 0, 99, "Aceptable desde (VMAF)")}
    if int(out["vmaf_warn"]) >= int(out["best_vmaf"]):
        raise ValueError("«Aceptable desde» debe ser menor que «Bueno desde»")
    if form.get("card_style") not in CARD_STYLES:
        raise ValueError("Detalle de las pruebas no válido")
    out["card_style"] = form["card_style"]
    if form.get("background") not in BACKGROUNDS:
        raise ValueError("Fondo no válido")
    out["background"] = form["background"]
    encoder = form.get("default_encoder", "")
    if encoder not in media.ENCODERS:
        raise ValueError(f"Encoder no admitido: {encoder}")
    out["default_encoder"] = encoder
    for enc in media.ENCODERS:
        extra = str(form.get(f"extras_{enc}", "")).strip()
        media.parse_extra_args(extra)
        out[f"extras_{enc}"] = extra
    if form.get("default_view") not in VIEWS:
        raise ValueError("Vista inicial no válida")
    out["default_view"] = form["default_view"]
    if form.get("refresh_s") not in REFRESH:
        raise ValueError("Refresco no válido: 0, 2, 5 o 10 s")
    out["refresh_s"] = form["refresh_s"]
    out["animations"] = "1" if form.get("animations") in ("1", "on", "true") else "0"  # casilla ausente = desmarcada
    out["save_frames"] = "1" if form.get("save_frames") in ("1", "on", "true") else "0"
    # Opcionales (los formularios anteriores no los traen): sin valor, el de por defecto.
    for key, lo, hi, label in (("frames_per_scene", 1, 6, "Fotogramas por escena"), ("frames_cap_gb", 1, 50, "Tope de imágenes (GB)"),
                               ("auto_tramos", 1, 5, "Tramos automáticos")):
        out[key] = _int(form, key, lo, hi, label) if form.get(key) not in (None, "") else str(DEFAULTS[key])
    return out


def load(conn) -> dict:
    out = dict(DEFAULTS)
    for key, value in db.get_settings(conn).items():
        default = DEFAULTS.get(key)
        if default is None:
            continue
        out[key] = value == "1" if isinstance(default, bool) else int(value) if isinstance(default, int) else value
    return out
