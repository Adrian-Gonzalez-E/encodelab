"""Textos de las métricas: fuente única de los popovers ⓘ y de la guía /metricas."""

VMAF_SCALE = [(0, "diferencias visibles"), (80, "diferencias leves"), (93, "casi indistinguible")]

METRICS = [
    {"key": "vmaf_mean", "name": "VMAF", "range": "0–100", "better": "más es mejor",
     "short": "Nota de calidad percibida (media del tramo). Es la referencia principal.",
     "long": "Métrica de Netflix que combina varios detectores (VIF, ADM y movimiento) con un modelo entrenado "
             "con notas de espectadores reales. Se usa el modelo vmaf_v0.6.1, pensado para 1080p visto en una "
             "TV. Unos 6 puntos equivalen a una diferencia apenas perceptible.",
     "scale": VMAF_SCALE},
    {"key": "vmaf_neg_mean", "name": "VMAF NEG", "range": "0–100", "better": "más es mejor",
     "short": "VMAF que no premia la nitidez o el contraste añadidos.",
     "long": "Variante «No Enhancement Gain»: mide solo la compresión e ignora la ganancia por realce. Si queda "
             "muy por debajo de VMAF, el encoder o un filtro está maquillando la imagen.",
     "scale": VMAF_SCALE},
    {"key": "vmaf_p5", "name": "VMAF p5", "range": "0–100", "better": "más es mejor",
     "short": "Nota que deja por debajo al 5 % de los fotogramas: las escenas peores.",
     "long": "Percentil 5 de VMAF por fotograma. Detecta momentos que se rompen (oscuros, con grano o con "
             "mucho movimiento) aunque la media sea buena. Es la cifra más robusta para decidir.",
     "scale": VMAF_SCALE},
    {"key": "vmaf_min", "name": "VMAF mín.", "range": "0–100", "better": "más es mejor",
     "short": "El peor fotograma del tramo.",
     "long": "VMAF del fotograma con peor nota. Ojo: el fotograma 0 da unos 3 puntos menos aunque sea idéntico, "
             "por cómo VMAF mide el movimiento, así que el mínimo suele reflejar ese fotograma. Usa mejor el p5.",
     "scale": None},
    {"key": "psnr_y", "name": "PSNR-Y", "range": "dB", "better": "más es mejor",
     "short": "Error píxel a píxel del brillo, en decibelios.",
     "long": "Relación señal/ruido de pico del canal de luminancia. Es simple y poco perceptual: por encima de "
             "40 dB suele ser bueno. Los ajustes psicovisuales (tune=0 en SVT-AV1, psy-rd en x265) pueden "
             "bajarlo aunque la imagen se vea mejor.",
     "scale": [(0, "malo"), (30, "aceptable"), (40, "bueno")]},
    {"key": "ssim", "name": "SSIM", "range": "0–1", "better": "más es mejor",
     "short": "Similitud de estructura: bordes, texturas y contraste.",
     "long": "Structural Similarity compara brillo, contraste y estructura local entre original y codificado. "
             "Por encima de 0,98 es muy bueno; las diferencias importan desde el tercer decimal.",
     "scale": None},
    {"key": "ms_ssim", "name": "MS-SSIM", "range": "0–1", "better": "más es mejor",
     "short": "SSIM medido a varias escalas de la imagen.",
     "long": "Multi-Scale SSIM repite el cálculo sobre versiones reducidas de la imagen y las combina. Se ajusta "
             "mejor a la percepción que SSIM, sobre todo con detalles finos y grano.",
     "scale": None},
]

FIELDS = [
    {"key": "kbps", "name": "kbps", "range": "kbit/s", "better": "menos es mejor",
     "short": "Bitrate de vídeo de la prueba en el tramo.",
     "long": "Tamaño del clip codificado en kilobits por segundo. Solo vídeo: sin audio ni subtítulos.",
     "scale": None},
    {"key": "ratio", "name": "% del original", "range": "%", "better": "menos es mejor",
     "short": "Tamaño frente al vídeo original en el mismo tramo.",
     "long": "kbps de la prueba dividido entre los kbps del vídeo original en ese tramo. Un 40 % significa "
             "un ahorro del 60 %. Es la cifra para comparar ahorro entre configuraciones.",
     "scale": None},
    {"key": "est_gb", "name": "GB vídeo/capítulo", "range": "GB", "better": "menos es mejor",
     "short": "Estimación del tamaño de la pista de vídeo del capítulo entero.",
     "long": "kbps × duración del capítulo. Es orientativa: un tramo no representa todo el capítulo. No incluye "
             "audio ni subtítulos; compárala con la pista de vídeo del resultado de tu transcodificador, no con el fichero.",
     "scale": None},
    {"key": "enc_fps", "name": "fps", "range": "fotogramas/s", "better": "más es mejor",
     "short": "Velocidad de codificación medida en la prueba.",
     "long": "Fotogramas codificados por segundo en este servidor. Incluye el arranque de ffmpeg, así que en "
             "tramos cortos sale algo pesimista. QSV comparte la GPU con otros procesos que la usen.",
     "scale": None},
    {"key": "est_hours", "name": "Tiempo/capítulo", "range": "h:mm:ss", "better": "menos es mejor",
     "short": "Tiempo estimado para codificar el capítulo entero.",
     "long": "Fotogramas del capítulo ÷ fps de la prueba. Sirve para calcular cuánto tardará la transcodificación de toda la "
             "serie.",
     "scale": None},
]

BY_KEY = {d["key"]: d for d in METRICS + FIELDS}
VMAF_KEYS = ("vmaf_mean", "vmaf_neg_mean", "vmaf_p5", "vmaf_min")


def vmaf_scale(warn: int, good: int) -> list[tuple[int, str]]:
    return [(0, "diferencias visibles"), (warn, "diferencias leves"), (good, "casi indistinguible")]


def with_thresholds(warn: int, good: int) -> dict:
    """BY_KEY con la escala VMAF de los ajustes (copia: el original no cambia)."""
    out = dict(BY_KEY)
    for key in VMAF_KEYS:
        out[key] = {**BY_KEY[key], "scale": vmaf_scale(warn, good)}
    return out

# Tarjetas de "Nueva batería". Medidores orientativos (0–100), no calculados.
ENCODER_INFO = {
    "libsvtav1": {
        "title": "SVT-AV1", "codec": "AV1", "engine": "CPU",
        "text": "La mejor compresión de los cuatro: menos tamaño a igual calidad. Lento en CPU con presets bajos.",
        "meters": {"compresion": 95, "velocidad": 30, "compat": 55},
        "quality": {"flag": "CRF", "range": "1–63", "start": "26–34", "default": "26,30,34",
                    "hint": "menos = más calidad y más tamaño"},
        "preset": {"range": "0–13", "start": "4–8", "default": "6", "hint": "menos = más lento y mejor"},
        "extras_hint": "10 bits: -pix_fmt yuv420p10le · grano: -svtav1-params film-grain=8 · visual: -svtav1-params tune=0",
    },
    "av1_qsv": {
        "title": "AV1 por QSV (Intel)", "codec": "AV1", "engine": "Intel QSV",
        "text": "AV1 por hardware en GPU Intel (Arc o posterior). Muy rápido; ficheros algo mayores que SVT-AV1 a igual "
                "calidad. Comparte la GPU con otros procesos.",
        "meters": {"compresion": 70, "velocidad": 95, "compat": 55},
        "quality": {"flag": "ICQ", "range": "1–51", "start": "22–30", "default": "24,28",
                    "hint": "menos = más calidad y más tamaño"},
        "preset": {"range": "veryslow…veryfast", "start": "slow o medium", "default": "slow",
                   "hint": "más lento = algo mejor"},
        "extras_hint": "10 bits: -pix_fmt p010le",
    },
    "libx265": {
        "title": "x265", "codec": "HEVC", "engine": "CPU",
        "text": "Maduro y fino; suele necesitar más bitrate que AV1. HEVC se reproduce en más dispositivos.",
        "meters": {"compresion": 75, "velocidad": 35, "compat": 85},
        "quality": {"flag": "CRF", "range": "0–51", "start": "20–26", "default": "22,24,26",
                    "hint": "menos = más calidad y más tamaño"},
        "preset": {"range": "ultrafast…placebo", "start": "medium o slow", "default": "medium",
                   "hint": "más lento = mejor, y mucho más lento"},
        "extras_hint": "10 bits: -pix_fmt yuv420p10le · ajustes: -x265-params aq-mode=3",
    },
    "hevc_qsv": {
        "title": "HEVC por QSV (Intel)", "codec": "HEVC", "engine": "Intel QSV",
        "text": "HEVC por hardware en GPU Intel. El más rápido; el que más ocupa a igual calidad.",
        "meters": {"compresion": 55, "velocidad": 100, "compat": 85},
        "quality": {"flag": "ICQ", "range": "1–51", "start": "22–28", "default": "24,27",
                    "hint": "menos = más calidad y más tamaño"},
        "preset": {"range": "veryslow…veryfast", "start": "medium", "default": "medium",
                   "hint": "más lento = algo mejor"},
        "extras_hint": "10 bits: -pix_fmt p010le",
    },
}
