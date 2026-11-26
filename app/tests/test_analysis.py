import json
import subprocess

import pytest

from encodelab import analysis, db, media


def test_margins():
    assert analysis.margins(100) == (90.0, 120.0)
    assert analysis.margins(2645) == pytest.approx((105.8, 211.6))


def profile(total=600, hot=(300, 330)):
    per_second = [1000.0 + (3000.0 if hot[0] <= s < hot[1] else 0) for s in range(total + 1)]
    keyframes = [[float(s), 50.0 + (40 if hot[0] <= s < hot[1] else 0), 20.0 if 150 <= s < 190 else 100.0]
                 for s in range(total)]
    return per_second, keyframes


def test_window_stats_respects_margins_and_features():
    ps, kf = profile()
    wins = analysis.window_stats(ps, kf, 20, 600)
    head, tail = analysis.margins(600)
    assert min(w["start"] for w in wins) >= head and max(w["start"] + 20 for w in wins) <= 600 - tail
    hot = next(w for w in wins if w["start"] == 305)
    assert hot["kbps"] == 4000.0 and hot["si"] == 90.0 and hot["yavg"] == 100.0


def test_pick_candidates_no_overlap_and_covers_kinds():
    ps, kf = profile()
    cands = analysis.pick_candidates(ps, kf, 20, 600)
    starts = sorted(c["start"] for c in cands)
    assert all(b - a >= 20 for a, b in zip(starts, starts[1:])) and len(cands) <= 14
    assert any(300 <= c["start"] < 330 for c in cands)  # el pico de bitrate y SI
    assert any(c["yavg"] < 30 for c in cands)  # y la zona oscura (bitrate normal, brillo bajo)
    assert analysis.pick_candidates([], [], 20, 100) == []


def cand(start, kbps, si, ti, yavg):
    return {"start": start, "kbps": kbps, "si": si, "ti": ti, "yavg": yavg}


def test_assign_roles_distinct_and_spaced():
    cands = [cand(100, 20000, 60, 10, 90), cand(200, 32000, 80, 40, 80), cand(300, 22000, 55, 8, 35),
             cand(400, 25000, 95, 12, 100), cand(500, 24000, 58, 60, 95), cand(600, 22500, 57, 9, 88)]
    picks = analysis.assign_roles([dict(c) for c in cands], 5, 20)
    by_role = {p["role"]: p["start"] for p in picks}
    assert by_role["exigente"] == 200 and by_role["oscura"] == 300
    assert by_role["detalle"] == 400 and by_role["movimiento"] == 500
    assert len({p["start"] for p in picks}) == 5
    one = analysis.assign_roles([dict(c) for c in cands], 1, 20)
    assert [p["role"] for p in one] == ["exigente"]
    assert analysis.assign_roles([], 3, 20) == []
    many = analysis.assign_roles([dict(c) for c in cands[:2]], 5, 20)  # menos candidatas que tramos pedidos
    assert len(many) == 2
    tight = analysis.assign_roles([cand(100, 1, 1, 1, 50), cand(110, 2, 2, 2, 50)], 2, 20)  # 10 s < 2× duración
    assert len(tight) == 1


def test_summarize_window():
    frames = [{"si": 40.0, "ti": float(i), "yavg": 60.0} for i in range(10)]
    m = analysis.summarize_window(frames)
    assert m["si"] == 40.0 and m["ti"] == 8.0 and m["ti_mean"] == 4.5 and m["yavg"] == 60.0
    assert analysis.summarize_window([{"pts": 0.0}]) is None


@pytest.fixture(scope="module")
def long_src(tmp_path_factory):
    """400 s a 320×180: 0–150 normal, 150–200 ruido, 200–260 oscuro, 260–400 normal."""
    path = tmp_path_factory.mktemp("an") / "long.mkv"
    graph = ("testsrc2=s=320x180:r=6:d=150[a];nullsrc=s=320x180:r=6:d=50,geq=lum='random(1)*255':cb=128:cr=128[b];"
             "testsrc2=s=320x180:r=6:d=60,eq=brightness=-0.4[c];testsrc2=s=320x180:r=6:d=140[d];"
             "[a][b][c][d]concat=n=4:v=1:a=0,format=yuv420p")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-filter_complex", graph, "-c:v", "libx264",
                    "-preset", "ultrafast", "-crf", "23", "-g", "6", str(path)], check=True)
    return path


def run(tmp_path, src, dur=20):
    conn = db.connect(str(tmp_path / "a.db"))
    st = src.stat()
    aid = db.create_analysis(conn, src.name, st.st_size, st.st_mtime_ns, float(dur))
    row = db.claim_analysis(conn)
    analysis.run_analysis(conn, row, src.parent, tmp_path / "frames")
    return conn, aid


def test_run_analysis_finds_noise_and_dark(long_src, tmp_path):
    conn, aid = run(tmp_path, long_src)
    a = db.get_analysis(conn, aid)
    assert a["status"] == "done", a["error"]
    picks = analysis.assign_roles(json.loads(a["candidates_json"]), 5, 20)
    by_role = {p["role"]: p["start"] for p in picks}
    assert 130 <= by_role["exigente"] <= 180  # dentro del ruido (150–200 s)
    assert 200 <= min(picks, key=lambda p: p["yavg"])["start"] <= 240  # la más oscura, en la zona oscura (200–260 s)
    assert all(90 <= p["start"] <= 260 for p in picks)  # fuera de los márgenes
    assert (tmp_path / "frames" / "analysis" / str(aid) / f"{by_role['exigente']}.webp").is_file()


def test_profile_cached_and_prunes_old(long_src, tmp_path, monkeypatch):
    conn, first = run(tmp_path, long_src)
    monkeypatch.setattr(analysis, "scan_packets", lambda *a, **k: pytest.fail("repite la fase 1"))
    monkeypatch.setattr(analysis, "scan_keyframes", lambda *a, **k: pytest.fail("repite la fase 2"))
    st = long_src.stat()
    again = db.create_analysis(conn, long_src.name, st.st_size, st.st_mtime_ns, 20.0)
    assert again == first  # misma fuente y duración: se reutiliza el análisis hecho
    other = db.create_analysis(conn, long_src.name, st.st_size, st.st_mtime_ns, 30.0)
    row = db.claim_analysis(conn)
    assert row["id"] == other
    analysis.run_analysis(conn, row, long_src.parent, tmp_path / "frames")
    assert db.get_analysis(conn, other)["status"] == "done"  # otra duración: fase 3 nueva con el perfil en caché


def test_run_analysis_error_is_recorded(tmp_path):
    conn = db.connect(str(tmp_path / "e.db"))
    db.create_analysis(conn, "no-existe.mkv", 1, 1, 20.0)
    analysis.run_analysis(conn, db.claim_analysis(conn), tmp_path, tmp_path / "frames")
    a = db.get_analysis(conn, 1)
    assert a["status"] == "error" and a["error"]


def test_representativa_uses_episode_median():
    cands = [dict(cand(100 * i, 20000 + 1000 * i, 50 + i, 8 + i, 90), pct=pct) for i, pct in ((1, 95), (2, 52), (3, 99), (4, 10))]
    picks = analysis.assign_roles(cands, 2, 20)
    assert {p["role"]: p["start"] for p in picks}["representativa"] == 200
    ps, kf = profile()
    assert all("pct" in c and 0 <= c["pct"] <= 100 for c in analysis.pick_candidates(ps, kf, 20, 600))
