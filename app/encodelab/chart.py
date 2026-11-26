"""Nube de puntos VMAF frente a % del original, como coordenadas de un SVG de servidor.

Las capas opcionales (umbral, ganador, frontera y curvas) se calculan siempre; el cliente las muestra u oculta.
"""
import math

from .media import ENCODERS

W, H = 600, 320
PAD = {"l": 48, "r": 16, "t": 12, "b": 40}
NO_ENC = {"codec": "av1", "qsv": False}


def scatter(rows, best_vmaf: float = 93, vmaf_warn: float = 80) -> dict | None:
    done = [r for r in rows if r["status"] == "done" and r["ratio"] is not None and r["vmaf_mean"] is not None]
    if not done:
        return None
    pcts = [r["ratio"] * 100 for r in done]
    # Eje X ajustado a los datos (3 puntos de margen, múltiplos de 5), no 0–100 fijo.
    x_min = max(0, math.floor((min(pcts) - 3) / 5) * 5)
    x_max = math.ceil((max(pcts) + 3) / 5) * 5
    # El umbral «bueno» siempre entra en el eje Y, para que su banda no quede fuera.
    y_min = max(0, math.floor((min(min(r["vmaf_mean"] for r in done), best_vmaf) - 2) / 5) * 5)
    plot_w, plot_h = W - PAD["l"] - PAD["r"], H - PAD["t"] - PAD["b"]

    def x(v):
        return round(PAD["l"] + (v - x_min) / (x_max - x_min) * plot_w, 1)

    def y(v):
        return round(PAD["t"] + (100 - v) / (100 - y_min) * plot_h, 1)

    # Ganador: el mismo «mejor ahorro ≥ best_vmaf» de la ficha.
    win = min((r for r in done if r["vmaf_mean"] >= best_vmaf), key=lambda r: r["ratio"], default=None)
    # Frontera de Pareto: ninguna otra prueba pesa menos y da más VMAF.
    front, top = [], -1.0
    for r in sorted(done, key=lambda r: (r["ratio"], -r["vmaf_mean"])):
        if r["vmaf_mean"] > top:
            front.append(r)
            top = r["vmaf_mean"]
    front_ids = {r["id"] for r in front}
    frontier = "".join(f'{"M" if i == 0 else "H"}{x(r["ratio"] * 100)}{" " if i == 0 else "V"}{y(r["vmaf_mean"])}'
                       for i, r in enumerate(front))

    points = []
    for r, pct in zip(done, pcts):
        enc = ENCODERS.get(r["encoder"], NO_ENC)
        points.append({"id": r["id"], "cx": x(pct), "cy": y(r["vmaf_mean"]), "codec": enc["codec"],
                       "engine": "qsv" if enc["qsv"] else "cpu",
                       "label": f'{r["encoder"]} · {r["quality"]} · {r["preset"]}',
                       "vmaf": r["vmaf_mean"], "p5": r["vmaf_p5"], "pct": pct, "gb": r["est_gb"],
                       "front": r["id"] in front_ids, "win": win is not None and r["id"] == win["id"]})

    # Curvas: mismo encoder, preset y extras con 2 o más calidades, de menor a mayor tamaño.
    groups = {}
    for r in done:
        groups.setdefault((r["encoder"], r["preset"], r["extra_args"]), []).append(r)
    curves = []
    for (encoder, preset, _), rs in groups.items():
        if len(rs) < 2:
            continue
        rs.sort(key=lambda r: r["ratio"])
        enc = ENCODERS.get(encoder, NO_ENC)
        curves.append({"codec": enc["codec"], "qsv": enc["qsv"], "label": f"{encoder}·{preset}",
                       "points": " ".join(f'{x(r["ratio"] * 100)},{y(r["vmaf_mean"])}' for r in rs),
                       "lx": x(rs[-1]["ratio"] * 100) + 10, "ly": y(rs[-1]["vmaf_mean"]) + 4})

    span = x_max - x_min
    x_step = 5 if span <= 40 else 10 if span <= 80 else 20
    y_step = 10 if 100 - y_min > 40 else 5
    return {"points": points, "x_min": x_min, "x_max": x_max, "y_min": y_min, "w": W, "h": H, "pad": PAD,
            "x_ticks": [(v, x(v)) for v in range(math.ceil(x_min / x_step) * x_step, x_max + 1, x_step)],
            "y_ticks": [(v, y(v)) for v in range(y_min, 101, y_step)],
            "best": {"v": best_vmaf, "y": y(best_vmaf), "top": y(100)},
            "warn": {"v": vmaf_warn, "y": y(vmaf_warn)} if vmaf_warn > y_min else None,
            "win": win and next(p for p in points if p["win"]),
            "frontier": frontier, "curves": curves}
