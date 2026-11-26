import pytest

import sqlite3

from encodelab.db import (add_tests, batch_tests, claim_next, connect, counts, create_batch, delete_batch,
                          delete_test, fail_test, finish_test, get_batch, get_test, list_batches, list_results,
                          list_scenes, queue_state, reset_running, scene_tests, set_progress, speed_history)


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "t.db"))


def new_batch(conn):
    return create_batch(conn, source_path="s/e01.mkv", start_s=600, duration_s=20, source_kbps=20000,
                        episode_duration_s=2640, fps=24)


def test_queue_flow(conn):
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6"), (32, "6")])
    t = claim_next(conn)
    assert t["quality"] == 28 and t["source_path"] == "s/e01.mkv" and t["start_s"] == 600
    finish_test(conn, t["id"], {"kbps": 4000.0, "vmaf_mean": 95.0, "enc_fps": 12.0, "no_es_columna": 1})
    r = list_results(conn, sort="kbps")[0]
    assert r["status"] == "done" and r["ratio"] == 0.2 and round(r["est_gb"], 2) == 1.32
    assert round(r["est_hours"], 2) == 1.47
    assert [x["status"] for x in batch_tests(conn, b)] == ["done", "queued"]
    assert get_batch(conn, b)["source_path"] == "s/e01.mkv"


def test_fail_and_filter(conn):
    b = new_batch(conn)
    add_tests(conn, b, "av1_qsv", "-pix_fmt p010le", [(25, "slow")])
    fail_test(conn, claim_next(conn)["id"], "Código 1: boom")
    assert claim_next(conn) is None
    r = list_results(conn, source="s/e01.mkv")[0]
    assert (r["status"], r["error"], r["extra_args"]) == ("error", "Código 1: boom", "-pix_fmt p010le")
    assert list_results(conn, source="otra.mkv") == []


def test_reset_running_requeues(conn):
    add_tests(conn, new_batch(conn), "libsvtav1", "", [(30, "6")])
    t = claim_next(conn)
    assert reset_running(conn) == 1
    assert claim_next(conn)["id"] == t["id"]


def test_unknown_sort_falls_back(conn):
    assert list_results(conn, sort="vmaf_mean; DROP TABLE tests") == []
    add_tests(conn, new_batch(conn), "libsvtav1", "", [(30, "6")])
    assert len(list_results(conn, sort="vmaf_mean; DROP TABLE tests")) == 1


def test_delete_test_rules(conn):
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6"), (32, "6")])
    running = claim_next(conn)
    assert delete_test(conn, running["id"]) is False
    other = [t for t in batch_tests(conn, b) if t["id"] != running["id"]][0]
    assert delete_test(conn, other["id"]) is True and get_batch(conn, b) is not None
    finish_test(conn, running["id"], {"kbps": 1.0})
    assert delete_test(conn, running["id"]) is True
    assert get_batch(conn, b) is None  # última prueba borrada: la batería también


def test_finish_after_delete_is_noop(conn):
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6")])
    t = batch_tests(conn, b)[0]
    assert delete_test(conn, t["id"])
    finish_test(conn, t["id"], {"kbps": 1.0})
    fail_test(conn, t["id"], "x")
    assert list_results(conn) == []


def test_delete_batch_keeps_running(conn):
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6"), (32, "6")])
    r = claim_next(conn)
    assert delete_batch(conn, b) is False and [t["id"] for t in batch_tests(conn, b)] == [r["id"]]
    finish_test(conn, r["id"], {})
    assert delete_batch(conn, b) is True and get_batch(conn, b) is None


def test_scenes_group_batches(conn):
    a = new_batch(conn)
    add_tests(conn, a, "libsvtav1", "", [(28, "6")])
    other = create_batch(conn, source_path="s/e02.mkv", start_s=10, duration_s=20)
    add_tests(conn, other, "libsvtav1", "", [(30, "6")])
    c = new_batch(conn)  # misma escena que a
    add_tests(conn, c, "av1_qsv", "", [(25, "slow")])
    scenes = list_scenes(conn)
    assert [s["batch_id"] for s in scenes] == [c, other] and scenes[0]["n_tests"] == 2
    assert {t["encoder"] for t in scene_tests(conn, a)} == {"libsvtav1", "av1_qsv"}
    assert [t["encoder"] for t in scene_tests(conn, a, sort="id")] == ["av1_qsv", "libsvtav1"]
    assert scene_tests(conn, 999) == [] and counts(conn) == {"done": 0, "pending": 3}
    assert [r["n_tests"] for r in list_batches(conn)] == [1, 1, 1]


def test_scenes_carry_group_and_role(conn):
    from encodelab import db
    gid = db.create_group(conn, "s/e01.mkv", None)
    create_batch(conn, source_path="s/e01.mkv", start_s=300, duration_s=20, group_id=gid, role="exigente")
    loose = new_batch(conn)
    manual = create_batch(conn, source_path="s/e01.mkv", start_s=300, duration_s=20)  # añadida a mano al tramo del grupo
    by_id = {s["batch_id"]: s for s in list_scenes(conn)}
    assert set(by_id) == {manual, loose}
    assert (by_id[manual]["group_id"], by_id[manual]["role"]) == (gid, "exigente")
    assert (by_id[loose]["group_id"], by_id[loose]["role"]) == (None, None)


V2_SCHEMA = """
CREATE TABLE batches (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  source_path TEXT NOT NULL, start_s REAL NOT NULL, duration_s REAL NOT NULL,
  codec TEXT, width INTEGER, height INTEGER, fps REAL, field_order TEXT,
  episode_duration_s REAL, source_kbps REAL, ffmpeg_version TEXT);
CREATE TABLE tests (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  encoder TEXT NOT NULL, quality INTEGER NOT NULL, preset TEXT NOT NULL, extra_args TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued', error TEXT, ffmpeg_args TEXT,
  size_bytes INTEGER, kbps REAL, encode_s REAL, enc_fps REAL, frames INTEGER,
  vmaf_mean REAL, vmaf_min REAL, vmaf_p5 REAL, vmaf_neg_mean REAL, psnr_y REAL, ssim REAL, ms_ssim REAL,
  finished_at TEXT);
"""


def test_connect_migrates_old_schema(tmp_path):
    path = str(tmp_path / "v2.db")
    old = sqlite3.connect(path)
    old.executescript(V2_SCHEMA)
    old.execute("INSERT INTO batches (source_path, start_s, duration_s) VALUES ('s/e01.mkv', 600, 20)")
    old.execute("INSERT INTO tests (batch_id, encoder, quality, preset, status, vmaf_mean) "
                "VALUES (1, 'libsvtav1', 30, '6', 'done', 94.5)")
    old.commit()
    old.close()
    conn = connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tests)")}
    assert {"progress", "phase", "frame", "frames_total", "started_ts", "measure_s"} <= cols
    t = get_test(conn, 1)
    assert (t["encoder"], t["vmaf_mean"], t["status"]) == ("libsvtav1", 94.5, "done")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'settings'").fetchone() is not None
    connect(path)  # idempotente


def test_claim_sets_progress_fields(conn):
    b = create_batch(conn, source_path="s/e01.mkv", start_s=600, duration_s=20, fps=24.0)
    add_tests(conn, b, "libsvtav1", "", [(28, "6")])
    t = claim_next(conn)
    assert t["started_ts"] > 0 and t["progress"] == 0 and t["phase"] == "encode" and t["fps"] == 24.0


def test_set_progress_only_running(conn):
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6"), (32, "6")])
    r = claim_next(conn)
    set_progress(conn, r["id"], "metrics", 0.8, 40, 480)
    assert (get_test(conn, r["id"])["progress"], get_test(conn, r["id"])["phase"]) == (0.8, "metrics")
    other = [t for t in batch_tests(conn, b) if t["id"] != r["id"]][0]
    set_progress(conn, other["id"], "encode", 0.5, 1, 2)  # en cola: no se toca
    assert get_test(conn, other["id"])["progress"] is None


def test_queue_state(conn):
    assert queue_state(conn, 1000.0) == {"pending": 0, "version": "0:0:0:", "running": [], "queued": [],
                                         "eta_total_s": None}
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6"), (32, "6")])
    r = claim_next(conn)
    conn.execute("UPDATE tests SET started_ts = ? WHERE id = ?", (990.0, r["id"]))
    set_progress(conn, r["id"], "encode", 0.5, 240, 480)
    q = queue_state(conn, 1000.0)
    assert q["pending"] == 2 and q["running"][0]["label"] == "libsvtav1 28·6" and q["running"][0]["eta_s"] == 10
    assert [x["label"] for x in q["queued"]] == ["libsvtav1 32·6"] and q["eta_total_s"] == 10 + 20  # la de cola, como la actual
    finish_test(conn, r["id"], {"kbps": 1.0})
    assert queue_state(conn, 1000.0)["version"] != q["version"]


def test_speed_history(conn):
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6"), (32, "6")])
    t1, t2 = batch_tests(conn, b)
    finish_test(conn, t1["id"], {"encode_s": 10.0, "measure_s": 2.0, "frames": 120})
    finish_test(conn, t2["id"], {"encode_s": 20.0, "frames": 100})
    # Las filas sin measure_s (anteriores a la v3) no cuentan: solo midieron la codificación.
    assert abs(speed_history(conn)["libsvtav1|6"] - 0.1) < 1e-9


def frame_row(**kw):
    return {**dict(source_path="s/e01.mkv", start_s=600, duration_s=20, test_id=None, idx=10, role="detalle",
                   vmaf=None, bytes=100, path="/f/o10.webp", thumb="/f/o10_s.webp"), **kw}


def test_frames_flow_and_cascade(conn):
    from encodelab import db
    b = new_batch(conn)
    add_tests(conn, b, "libsvtav1", "", [(28, "6")])
    t = claim_next(conn)
    db.add_frames(conn, [frame_row(), frame_row(test_id=t["id"], vmaf=91.5, path="/f/t.webp", thumb="/f/t_s.webp")])
    assert [r["idx"] for r in db.scene_originals(conn, "s/e01.mkv", 600, 20)] == [10]
    assert len(db.scene_frames(conn, "s/e01.mkv", 600, 20)) == 2 and db.frames_usage(conn) == (2, 200)
    assert db.frame_paths(conn, [t["id"]]) == ["/f/t.webp", "/f/t_s.webp"]
    finish_test(conn, t["id"], {"kbps": 1.0})
    delete_test(conn, t["id"])  # la prueba y la batería vacía se borran; sus fotogramas, en cascada
    assert [r["test_id"] for r in db.scene_frames(conn, "s/e01.mkv", 600, 20)] == [None]
    assert db.drop_orphan_originals(conn) == ["/f/o10.webp", "/f/o10_s.webp"]
    assert db.frames_usage(conn) == (0, 0)


def test_orphan_originals_kept_while_scene_has_batches(conn):
    from encodelab import db
    new_batch(conn)
    db.add_frames(conn, [frame_row()])
    assert db.drop_orphan_originals(conn) == [] and db.clear_frames(conn) == ["/f/o10.webp", "/f/o10_s.webp"]
