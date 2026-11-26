from encodelab.chart import PAD, W, scatter

ROW = dict(status="done", encoder="libsvtav1", quality=30, preset="6", extra_args="", est_gb=1.4)


def test_scatter_maps_axes():
    s = scatter([{**ROW, "id": 1, "ratio": 0.5, "vmaf_mean": 94.0, "vmaf_p5": 91.0},
                 {**ROW, "id": 2, "ratio": 0.3, "vmaf_mean": 88.0, "vmaf_p5": 80.0, "encoder": "av1_qsv"},
                 {**ROW, "id": 3, "status": "queued", "ratio": None, "vmaf_mean": None, "vmaf_p5": None}])
    p1, p2 = s["points"]
    # Eje X ajustado a los datos: 30–50 % con 3 de margen, a múltiplos de 5.
    assert (s["x_min"], s["x_max"], s["y_min"]) == (25, 55, 85) and len(s["points"]) == 2
    assert p1["cx"] == round(PAD["l"] + 25 / 30 * (W - PAD["l"] - PAD["r"]), 1) and p1["cy"] < p2["cy"]
    assert [v for v, _ in s["x_ticks"]] == [25, 30, 35, 40, 45, 50, 55]
    assert (p1["codec"], p1["engine"], p2["engine"]) == ("av1", "cpu", "qsv") and p1["pct"] == 50.0
    assert p1["label"] == "libsvtav1 · 30 · 6"


def test_scatter_extends_x_and_empty():
    s = scatter([{**ROW, "id": 1, "ratio": 1.27, "vmaf_mean": 99.0, "vmaf_p5": 98.0}])
    assert (s["x_min"], s["x_max"]) == (120, 130) and s["x_ticks"][-1][0] == 130
    assert s["y_min"] == 90  # el umbral 93 entra aunque todos los puntos estén por encima
    assert scatter([]) is None
    assert scatter([{**ROW, "id": 1, "status": "error", "ratio": None, "vmaf_mean": None}]) is None


def test_scatter_layers():
    rows = [{**ROW, "id": 1, "quality": 26, "ratio": 0.58, "vmaf_mean": 95.6, "vmaf_p5": 92.8},
            {**ROW, "id": 2, "quality": 30, "ratio": 0.46, "vmaf_mean": 94.4, "vmaf_p5": 91.0},
            {**ROW, "id": 3, "quality": 34, "ratio": 0.36, "vmaf_mean": 92.1, "vmaf_p5": 87.9},
            {**ROW, "id": 4, "encoder": "av1_qsv", "preset": "slow", "ratio": 0.40, "vmaf_mean": 91.0, "vmaf_p5": 86.2},
            {**ROW, "id": 5, "extra_args": "-pix_fmt yuv420p10le", "ratio": 0.5, "vmaf_mean": 93.0, "vmaf_p5": 90.0}]
    s = scatter(rows, best_vmaf=93, vmaf_warn=80)
    # Ganador: menor % con VMAF >= 93 (el 5 llega justo al umbral pero pesa más que el 2).
    assert s["win"]["id"] == 2 and [p["id"] for p in s["points"] if p["win"]] == [2]
    # Frontera: el 4 queda dominado por el 3 (pesa más y da menos VMAF) y el 5 por el 2.
    assert [p["id"] for p in s["points"] if p["front"]] == [1, 2, 3]
    assert s["frontier"].startswith("M") and s["frontier"].count("H") == 2
    # Curvas: solo libsvtav1·6 sin extras tiene 2 o más calidades; otros extras van aparte.
    assert [c["label"] for c in s["curves"]] == ["libsvtav1·6"] and len(s["curves"][0]["points"].split()) == 3
    assert s["warn"] is None and s["best"]["v"] == 93  # 80 queda bajo el eje (85–100)
    assert scatter(rows, best_vmaf=99)["win"] is None
    assert scatter(rows, vmaf_warn=88)["warn"]["v"] == 88
