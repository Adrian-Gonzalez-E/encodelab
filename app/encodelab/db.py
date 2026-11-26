"""Cola de pruebas y resultados en SQLite."""
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  source_path TEXT NOT NULL, start_s REAL NOT NULL, duration_s REAL NOT NULL,
  codec TEXT, width INTEGER, height INTEGER, fps REAL, field_order TEXT,
  episode_duration_s REAL, source_kbps REAL, ffmpeg_version TEXT);
CREATE TABLE IF NOT EXISTS tests (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  encoder TEXT NOT NULL, quality INTEGER NOT NULL, preset TEXT NOT NULL, extra_args TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued', error TEXT, ffmpeg_args TEXT,
  size_bytes INTEGER, kbps REAL, encode_s REAL, enc_fps REAL, frames INTEGER,
  vmaf_mean REAL, vmaf_min REAL, vmaf_p5 REAL, vmaf_neg_mean REAL, psnr_y REAL, ssim REAL, ms_ssim REAL,
  finished_at TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  source_path TEXT NOT NULL, analysis_id INTEGER);
CREATE TABLE IF NOT EXISTS frames (
  id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, start_s REAL NOT NULL, duration_s REAL NOT NULL,
  test_id INTEGER REFERENCES tests(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL, role TEXT, vmaf REAL, bytes INTEGER, path TEXT NOT NULL, thumb TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_profiles (
  source_path TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  per_second_json TEXT NOT NULL, keyframes_json TEXT NOT NULL,
  PRIMARY KEY (source_path, size_bytes, mtime_ns));
CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  source_path TEXT NOT NULL, size_bytes INTEGER, mtime_ns INTEGER, duration_s REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', phase TEXT, progress REAL, error TEXT,
  candidates_json TEXT, finished_at TEXT);
CREATE INDEX IF NOT EXISTS frames_scene ON frames (source_path, start_s, duration_s, idx);
"""

# Columnas añadidas en la UI v3: connect() las crea en bases de datos anteriores sin tocar los datos.
NEW_TEST_COLS = {"progress": "REAL", "phase": "TEXT", "frame": "INTEGER", "frames_total": "INTEGER",
                 "started_ts": "REAL", "measure_s": "REAL"}

# Columnas de batches añadidas con la selección automática de tramos.
NEW_BATCH_COLS = {"group_id": "INTEGER", "role": "TEXT"}

RESULT_COLS = {"ffmpeg_args", "size_bytes", "kbps", "encode_s", "enc_fps", "frames", "vmaf_mean", "vmaf_min",
               "vmaf_p5", "vmaf_neg_mean", "psnr_y", "ssim", "ms_ssim", "measure_s"}

# Los ASC llevan NULLS LAST para que las pruebas sin terminar no tapen los resultados.
SORTS = {"id": "t.id DESC", "kbps": "t.kbps NULLS LAST", "ratio": "ratio NULLS LAST",
         "vmaf_mean": "t.vmaf_mean DESC", "vmaf_p5": "t.vmaf_p5 DESC", "enc_fps": "t.enc_fps DESC"}

SELECT_RESULTS = """
SELECT t.*, b.source_path, b.start_s, b.duration_s, b.created_at,
  t.kbps / b.source_kbps AS ratio,
  t.kbps * b.episode_duration_s / 8 / 1e6 AS est_gb,
  b.episode_duration_s * b.fps / t.enc_fps / 3600 AS est_hours
FROM tests t JOIN batches b ON b.id = t.batch_id
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tests)")}
    for name, kind in NEW_TEST_COLS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE tests ADD COLUMN {name} {kind}")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
    for name, kind in NEW_BATCH_COLS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {name} {kind}")
    conn.commit()
    return conn


def create_batch(conn: sqlite3.Connection, **cols) -> int:
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO batches ({names}) VALUES ({marks})", list(cols.values()))
    conn.commit()
    return cur.lastrowid


def add_tests(conn: sqlite3.Connection, batch_id: int, encoder: str, extra_args: str,
              matrix: list[tuple[int, str]]) -> None:
    conn.executemany("INSERT INTO tests (batch_id, encoder, quality, preset, extra_args) VALUES (?, ?, ?, ?, ?)",
                     [(batch_id, encoder, q, p, extra_args) for q, p in matrix])
    conn.commit()


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute("UPDATE tests SET status='running', started_ts=?, progress=0, phase='encode' "
                       "WHERE id=(SELECT id FROM tests WHERE status='queued' ORDER BY id LIMIT 1) RETURNING id",
                       (time.time(),)).fetchone()
    conn.commit()
    if row is None:
        return None
    return conn.execute("SELECT t.*, b.source_path, b.start_s, b.duration_s, b.fps FROM tests t "
                        "JOIN batches b ON b.id = t.batch_id WHERE t.id = ?", (row["id"],)).fetchone()


def finish_test(conn: sqlite3.Connection, test_id: int, result: dict) -> None:
    cols = [k for k in result if k in RESULT_COLS]
    sets = "".join(f", {k} = ?" for k in cols)
    conn.execute(f"UPDATE tests SET status='done', finished_at=datetime('now','localtime'){sets} WHERE id = ?",
                 [result[k] for k in cols] + [test_id])
    conn.commit()


def fail_test(conn: sqlite3.Connection, test_id: int, error: str) -> None:
    conn.execute("UPDATE tests SET status='error', error=?, finished_at=datetime('now','localtime') WHERE id=?",
                 (error, test_id))
    conn.commit()


def reset_running(conn: sqlite3.Connection) -> int:
    n = conn.execute("UPDATE tests SET status='queued' WHERE status='running'").rowcount
    conn.commit()
    return n


def list_results(conn: sqlite3.Connection, source: str | None = None, sort: str = "id") -> list[sqlite3.Row]:
    where, args = ("WHERE b.source_path = ?", [source]) if source else ("", [])
    order = SORTS.get(sort, SORTS["id"])
    return conn.execute(f"{SELECT_RESULTS} {where} ORDER BY {order}", args).fetchall()


def get_test(conn: sqlite3.Connection, test_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()


def get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def batch_tests(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(f"{SELECT_RESULTS} WHERE t.batch_id = ? ORDER BY t.id", (batch_id,)).fetchall()


# Escena = mismo tramo de la misma fuente; sus baterías se comparan juntas.
def list_scenes(conn: sqlite3.Connection, best_vmaf: float = 93) -> list[sqlite3.Row]:
    # Con MAX(), SQLite toma las columnas sueltas (created_at) de la fila del máximo.
    # El grupo sale de cualquier batería del tramo (el más reciente), aunque la última se añadiera a mano sin grupo.
    same = "FROM batches g WHERE g.source_path = b.source_path AND g.start_s = b.start_s AND g.duration_s = b.duration_s"
    return conn.execute(
        "SELECT MAX(b.id) AS batch_id, b.source_path, b.start_s, b.duration_s, b.created_at, COUNT(t.id) AS n_tests, "
        "COALESCE(SUM(t.status = 'done'), 0) AS n_done, COALESCE(SUM(t.status IN ('queued', 'running')), 0) AS n_pending, "
        "MIN(CASE WHEN t.status = 'done' AND t.vmaf_mean >= ? THEN t.kbps / b.source_kbps END) AS best_ratio, "
        f"(SELECT MAX(g.group_id) {same}) AS group_id, "
        f"(SELECT g.role {same} AND g.group_id IS NOT NULL ORDER BY g.group_id DESC, g.id LIMIT 1) AS role "
        "FROM batches b LEFT JOIN tests t ON t.batch_id = b.id "
        "GROUP BY b.source_path, b.start_s, b.duration_s ORDER BY batch_id DESC", (best_vmaf,)).fetchall()


def scene_tests(conn: sqlite3.Connection, batch_id: int, sort: str = "id") -> list[sqlite3.Row]:
    b = get_batch(conn, batch_id)
    if b is None:
        return []
    order = SORTS.get(sort, SORTS["id"])
    return conn.execute(f"{SELECT_RESULTS} WHERE b.source_path = ? AND b.start_s = ? AND b.duration_s = ? "
                        f"ORDER BY {order}", (b["source_path"], b["start_s"], b["duration_s"])).fetchall()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT b.*, COUNT(t.id) AS n_tests FROM batches b LEFT JOIN tests t ON t.batch_id = b.id "
                        "GROUP BY b.id ORDER BY b.id DESC").fetchall()


def counts(conn: sqlite3.Connection) -> dict:
    done, pending = conn.execute("SELECT COALESCE(SUM(status = 'done'), 0), "
                                 "COALESCE(SUM(status IN ('queued', 'running')), 0) FROM tests").fetchone()
    return {"done": done, "pending": pending}


def _drop_if_empty(conn: sqlite3.Connection, batch_id: int) -> None:
    conn.execute("DELETE FROM batches WHERE id = ? AND NOT EXISTS (SELECT 1 FROM tests WHERE batch_id = ?)",
                 (batch_id, batch_id))


def delete_test(conn: sqlite3.Connection, test_id: int) -> bool:
    """Borra la prueba salvo si está en curso; una batería que se queda vacía se borra también."""
    row = conn.execute("DELETE FROM tests WHERE id = ? AND status != 'running' RETURNING batch_id",
                       (test_id,)).fetchone()
    if row is not None:
        _drop_if_empty(conn, row["batch_id"])
    conn.commit()
    return row is not None


def delete_batch(conn: sqlite3.Connection, batch_id: int) -> bool:
    """Borra las pruebas que no estén en curso; True si la batería ya no existe."""
    conn.execute("DELETE FROM tests WHERE batch_id = ? AND status != 'running'", (batch_id,))
    _drop_if_empty(conn, batch_id)
    conn.commit()
    return get_batch(conn, batch_id) is None


def set_progress(conn: sqlite3.Connection, test_id: int, phase: str, frac: float, frame: int | None,
                 total: int | None) -> None:
    conn.execute("UPDATE tests SET phase = ?, progress = ?, frame = ?, frames_total = ? "
                 "WHERE id = ? AND status = 'running'", (phase, frac, frame, total, test_id))
    conn.commit()


def queue_state(conn: sqlite3.Connection, now: float) -> dict:
    """Cola para la píldora y /api/queue; version cambia cuando cambia algún estado."""
    rows = conn.execute("SELECT id, encoder, quality, preset, status, progress, phase, frame, frames_total, "
                        "started_ts FROM tests WHERE status IN ('running', 'queued') ORDER BY id").fetchall()
    done, max_id = conn.execute("SELECT COALESCE(SUM(status = 'done'), 0), COALESCE(MAX(id), 0) FROM tests").fetchone()
    running, queued = [], []
    for r in rows:
        item = {"id": r["id"], "label": f'{r["encoder"]} {r["quality"]}·{r["preset"]}'}
        if r["status"] == "queued":
            queued.append(item)
            continue
        p = r["progress"] or 0
        elapsed = now - r["started_ts"] if r["started_ts"] else None
        eta = elapsed / p * (1 - p) if p > 0 and elapsed is not None else None
        running.append({**item, "progress": p, "phase": r["phase"], "frame": r["frame"],
                        "frames_total": r["frames_total"], "eta_s": eta, "elapsed_s": elapsed})
    # Las de la cola se estiman como la que está en curso (misma batería casi siempre).
    first = running[0] if running else None
    eta_total = (first["eta_s"] + len(queued) * first["elapsed_s"] / first["progress"]
                 if first and first["eta_s"] is not None else None)
    # La fase entra en la versión: el cambio Codificar → Medir refresca los pasos de la tarjeta.
    run_id = ",".join(f'{r["id"]}@{r["phase"]}' for r in running)
    return {"pending": len(rows), "version": f"{done}:{len(rows)}:{max_id}:{run_id}", "running": running,
            "queued": queued, "eta_total_s": eta_total}


def speed_history(conn: sqlite3.Connection) -> dict[str, float]:
    """Segundos por fotograma (codificar + medir) medidos, por "encoder|preset"."""
    # Sin measure_s (pruebas anteriores a la v3) solo se midió la codificación: no cuentan.
    rows = conn.execute("SELECT encoder || '|' || preset AS k, AVG((encode_s + measure_s) / frames) AS spf "
                        "FROM tests WHERE status = 'done' AND frames > 0 AND encode_s IS NOT NULL AND measure_s IS NOT NULL "
                        "GROUP BY encoder, preset").fetchall()
    return {r["k"]: r["spf"] for r in rows}


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def save_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", list(values.items()))
    conn.commit()


# ---------- fotogramas de comparación (test_id NULL = original de la escena) ----------
def add_frames(conn: sqlite3.Connection, rows: list[dict]) -> None:
    cols = "source_path, start_s, duration_s, test_id, idx, role, vmaf, bytes, path, thumb"
    conn.executemany(f"INSERT INTO frames ({cols}) VALUES (:source_path, :start_s, :duration_s, :test_id, :idx, "
                     ":role, :vmaf, :bytes, :path, :thumb)", rows)
    conn.commit()


def scene_originals(conn: sqlite3.Connection, source_path: str, start: float, dur: float) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM frames WHERE test_id IS NULL AND source_path = ? AND start_s = ? "
                        "AND duration_s = ? ORDER BY idx", (source_path, start, dur)).fetchall()


def scene_frames(conn: sqlite3.Connection, source_path: str, start: float, dur: float) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM frames WHERE source_path = ? AND start_s = ? AND duration_s = ? "
                        "ORDER BY idx, test_id", (source_path, start, dur)).fetchall()


def test_frames(conn: sqlite3.Connection, test_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM frames WHERE test_id = ? ORDER BY idx", (test_id,)).fetchall()


def get_frame(conn: sqlite3.Connection, frame_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM frames WHERE id = ?", (frame_id,)).fetchone()


def frames_usage(conn: sqlite3.Connection) -> tuple[int, int]:
    n, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM frames").fetchone()
    return n, size


def frame_paths(conn: sqlite3.Connection, test_ids: list[int]) -> list[str]:
    """Rutas (imagen y miniatura) de las pruebas dadas: se piden antes de borrarlas (el borrado es en cascada)."""
    if not test_ids:
        return []
    marks = ",".join("?" for _ in test_ids)
    rows = conn.execute(f"SELECT path, thumb FROM frames WHERE test_id IN ({marks})", test_ids).fetchall()
    return [p for r in rows for p in (r["path"], r["thumb"])]


def batch_test_ids(conn: sqlite3.Connection, batch_id: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM tests WHERE batch_id = ? AND status != 'running'",
                                          (batch_id,))]


def drop_orphan_originals(conn: sqlite3.Connection) -> list[str]:
    """Quita los originales de escenas que ya no tienen ninguna batería; devuelve sus ficheros."""
    rows = conn.execute("SELECT id, path, thumb FROM frames WHERE test_id IS NULL AND NOT EXISTS ("
                        "SELECT 1 FROM batches b WHERE b.source_path = frames.source_path AND "
                        "b.start_s = frames.start_s AND b.duration_s = frames.duration_s)").fetchall()
    conn.executemany("DELETE FROM frames WHERE id = ?", [(r["id"],) for r in rows])
    conn.commit()
    return [p for r in rows for p in (r["path"], r["thumb"])]


def clear_frames(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT path, thumb FROM frames").fetchall()
    conn.execute("DELETE FROM frames")
    conn.commit()
    return [p for r in rows for p in (r["path"], r["thumb"])]


# ---------- análisis automático de tramos ----------
def create_analysis(conn: sqlite3.Connection, source_path: str, size: int, mtime_ns: int, duration: float) -> int:
    """Id de un análisis del mismo fichero (tamaño y mtime) y duración si ya hay uno vivo; si no, uno nuevo."""
    row = conn.execute("SELECT id FROM analyses WHERE source_path = ? AND size_bytes = ? AND mtime_ns = ? AND "
                       "duration_s = ? AND status IN ('queued', 'running', 'done') ORDER BY id DESC LIMIT 1",
                       (source_path, size, mtime_ns, duration)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO analyses (source_path, size_bytes, mtime_ns, duration_s) VALUES (?, ?, ?, ?)",
                       (source_path, size, mtime_ns, duration))
    conn.commit()
    return cur.lastrowid


def claim_analysis(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute("UPDATE analyses SET status = 'running', phase = 'paquetes', progress = 0 WHERE id = "
                       "(SELECT id FROM analyses WHERE status = 'queued' ORDER BY id LIMIT 1) RETURNING *").fetchone()
    conn.commit()
    return row


def set_analysis_progress(conn: sqlite3.Connection, analysis_id: int, phase: str, progress: float) -> None:
    conn.execute("UPDATE analyses SET phase = ?, progress = ? WHERE id = ? AND status = 'running'",
                 (phase, progress, analysis_id))
    conn.commit()


def finish_analysis(conn: sqlite3.Connection, analysis_id: int, candidates_json: str) -> None:
    conn.execute("UPDATE analyses SET status = 'done', progress = 1, candidates_json = ?, "
                 "finished_at = datetime('now','localtime') WHERE id = ?", (candidates_json, analysis_id))
    conn.commit()


def fail_analysis(conn: sqlite3.Connection, analysis_id: int, error: str) -> None:
    conn.execute("UPDATE analyses SET status = 'error', error = ?, finished_at = datetime('now','localtime') "
                 "WHERE id = ?", (error, analysis_id))
    conn.commit()


def get_analysis(conn: sqlite3.Connection, analysis_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()


def reset_running_analyses(conn: sqlite3.Connection) -> int:
    n = conn.execute("UPDATE analyses SET status = 'queued' WHERE status = 'running'").rowcount
    conn.commit()
    return n


def get_profile(conn: sqlite3.Connection, source_path: str, size: int, mtime_ns: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM file_profiles WHERE source_path = ? AND size_bytes = ? AND mtime_ns = ?",
                        (source_path, size, mtime_ns)).fetchone()


def save_profile(conn: sqlite3.Connection, source_path: str, size: int, mtime_ns: int, per_second_json: str,
                 keyframes_json: str) -> None:
    # Una sola versión por fichero: si cambió (tamaño o mtime) la anterior ya no sirve.
    conn.execute("DELETE FROM file_profiles WHERE source_path = ?", (source_path,))
    conn.execute("INSERT INTO file_profiles VALUES (?, ?, ?, ?, ?)",
                 (source_path, size, mtime_ns, per_second_json, keyframes_json))
    conn.commit()


def prune_analyses(conn: sqlite3.Connection, keep_id: int) -> list[int]:
    """Quita los análisis anteriores del mismo fichero y duración (ids devueltos para borrar sus miniaturas)."""
    cur = conn.execute("SELECT source_path, duration_s FROM analyses WHERE id = ?", (keep_id,)).fetchone()
    rows = conn.execute("SELECT id FROM analyses WHERE source_path = ? AND duration_s = ? AND id < ? AND "
                        "status IN ('done', 'error')", (cur["source_path"], cur["duration_s"], keep_id)).fetchall()
    conn.executemany("DELETE FROM analyses WHERE id = ?", [(r["id"],) for r in rows])
    conn.commit()
    return [r["id"] for r in rows]


# ---------- grupos: varias baterías (una por tramo) de una misma selección automática ----------
def create_group(conn: sqlite3.Connection, source_path: str, analysis_id: int | None) -> int:
    cur = conn.execute("INSERT INTO groups (source_path, analysis_id) VALUES (?, ?)", (source_path, analysis_id))
    conn.commit()
    return cur.lastrowid


def get_group(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def group_batches(conn: sqlite3.Connection, group_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT b.*, COUNT(t.id) AS n_tests, COALESCE(SUM(t.status = 'done'), 0) AS n_done, "
                        "COALESCE(SUM(t.status IN ('queued', 'running')), 0) AS n_pending "
                        "FROM batches b LEFT JOIN tests t ON t.batch_id = b.id WHERE b.group_id = ? "
                        "GROUP BY b.id ORDER BY b.start_s", (group_id,)).fetchall()


def group_summary(conn: sqlite3.Connection, group_id: int) -> list[sqlite3.Row]:
    """Una fila por configuración hecha en TODOS los tramos del grupo (peor tramo y medias)."""
    return conn.execute(
        "SELECT t.encoder, t.quality, t.preset, t.extra_args, COUNT(*) AS n, MAX(t.ffmpeg_args) AS ffmpeg_args, "
        "AVG(t.vmaf_mean) AS vmaf_avg, MIN(t.vmaf_mean) AS vmaf_worst, MIN(t.vmaf_p5) AS p5_worst, "
        "MIN(t.vmaf_min) AS min_worst, AVG(t.kbps / b.source_kbps) AS ratio, MAX(t.kbps / b.source_kbps) AS ratio_worst, "
        "AVG(t.kbps * b.episode_duration_s / 8 / 1e6) AS est_gb, AVG(t.enc_fps) AS enc_fps, "
        "AVG(b.episode_duration_s * b.fps / t.enc_fps / 3600) AS est_hours "
        "FROM tests t JOIN batches b ON b.id = t.batch_id WHERE b.group_id = ? AND t.status = 'done' "
        "GROUP BY t.encoder, t.quality, t.preset, t.extra_args "
        "HAVING COUNT(*) = (SELECT COUNT(*) FROM batches WHERE group_id = ?) ORDER BY ratio", (group_id, group_id)).fetchall()


def nav_targets(conn: sqlite3.Connection) -> dict:
    """Destinos de las pestañas que solo existen con datos: último grupo y última escena con imágenes."""
    group = conn.execute("SELECT MAX(g.id) FROM groups g WHERE EXISTS (SELECT 1 FROM batches b WHERE b.group_id = g.id)").fetchone()[0]
    compare = conn.execute("SELECT MAX(b.id) FROM batches b JOIN frames f ON f.test_id IS NULL AND f.source_path = b.source_path "
                           "AND f.start_s = b.start_s AND f.duration_s = b.duration_s").fetchone()[0]
    return {"group": group, "compare": compare}


def compare_scenes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Escenas con imágenes guardadas (para elegir tramo en el visor), la más reciente primero."""
    return conn.execute("SELECT MAX(b.id) AS batch_id, b.source_path, b.start_s, b.duration_s, MAX(b.role) AS role "
                        "FROM batches b JOIN frames f ON f.test_id IS NULL AND f.source_path = b.source_path "
                        "AND f.start_s = b.start_s AND f.duration_s = b.duration_s "
                        "GROUP BY b.source_path, b.start_s, b.duration_s ORDER BY batch_id DESC").fetchall()


def list_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT g.id, g.source_path, g.created_at, COUNT(b.id) AS n FROM groups g "
                        "JOIN batches b ON b.group_id = g.id GROUP BY g.id ORDER BY g.id DESC").fetchall()
