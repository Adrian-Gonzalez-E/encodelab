import re
import subprocess

import pytest

from encodelab import db, frames, media


def test_matrix_for():
    assert frames.matrix_for({"color_space": "bt709", "height": 480}) == "bt709"
    assert frames.matrix_for({"color_space": "bt470bg", "height": 1080}) == "bt470bg"
    assert frames.matrix_for({"color_space": None, "height": 1080}) == "bt709"
    assert frames.matrix_for({"color_space": "unknown", "height": 576}) == "smpte170m"
    assert frames.range_for({"color_range": "pc"}) == "pc" and frames.range_for({"color_range": None}) == "tv"


def test_parse_metadata_skips_nan():
    text = ("frame:0    pts:0       pts_time:0\\nlavfi.siti.si=nan\\nlavfi.siti.ti=0.00\\n"
            "frame:1    pts:1       pts_time:0.0417\\nlavfi.siti.si=72.39\\nlavfi.siti.ti=14.69\\n"
            "lavfi.signalstats.YAVG=88.5\\n").replace("\\n", "\n")
    f = media.parse_metadata(text)
    assert f[0] == {"pts": 0.0, "ti": 0.0} and f[1]["si"] == 72.39 and f[1]["yavg"] == 88.5


def series(n=240):
    return [{"si": 50 + (10 if i == 100 else 0), "ti": float(i % 20) + (500 if i == 30 else 0)} for i in range(n)]


def test_pick_indexes_roles_and_spacing():
    picks = frames.pick_indexes(series(), 3, 24)
    roles = dict((r, i) for i, r in picks)
    assert roles["detalle"] == 100 and roles["medio"] != roles["detalle"]
    assert roles["movimiento"] != 30  # el máximo de TI es un corte, no el percentil 90
    assert [i for i, _ in picks] == sorted(i for i, _ in picks)
    assert all(b - a >= 24 for (a, _), (b, _) in zip(picks, picks[1:]))
    assert len(frames.pick_indexes(series(), 1, 24)) == 1 and frames.pick_indexes([], 3, 24) == []
    assert len(frames.pick_indexes(series(), 6, 24)) == 6


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    path = tmp_path_factory.mktemp("src") / "tag.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24:d=4",
                    "-c:v", "libx264", "-crf", "10", "-colorspace", "bt709", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-g", "24", str(path)], check=True)
    return path


def psnr(a, b):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b), "-lavfi", "psnr", "-f", "null", "-"],
                       capture_output=True, text=True)
    return float(re.search(r"average:(\S+)", r.stderr).group(1))


def test_frames_same_color_and_frame(src, tmp_path):
    info = media.probe_source(src)
    assert info["color_space"] == "bt709"
    idx = [10, 40]
    originals = [tmp_path / f"o{i}.webp" for i in idx]
    thumbs = [tmp_path / f"o{i}_s.webp" for i in idx]
    frames.extract(src, idx, originals, thumbs, frames.matrix_for(info), frames.range_for(info), start=1.0, dur=2.0)
    plan = [(i, tmp_path / f"e{i}.webp", tmp_path / f"e{i}_s.webp") for i in idx]
    work = tmp_path / "w"
    work.mkdir()
    # El encode (SVT-AV1) sale sin etiqueta de color: aun así debe verse igual que el original.
    r = media.run_test(src, 1.0, 2.0, "libsvtav1", 10, "12", [], work,
                       2, frames={"items": plan, "matrix": frames.matrix_for(info), "range": "tv"})
    assert set(r["frame_vmaf"]) == set(idx) and all(v > 90 for v in r["frame_vmaf"].values())
    for i, enc, thumb in plan:
        assert psnr(originals[idx.index(i)], enc) > 33  # otro fotograma o otra matriz darían ~20 dB
        assert thumb.stat().st_size < 60_000
    assert psnr(originals[0], plan[1][1]) < 25  # y no es el mismo fotograma que el otro índice
    assert list(work.iterdir()) == []


def test_extract_failure_does_not_fail_test(src, tmp_path):
    plan = [(10, tmp_path / "x.webp", tmp_path / "x_s.webp")]
    r = media.run_test(src, 1.0, 2.0, "libsvtav1", 10, "12", [], tmp_path, 2,
                       frames={"items": plan, "matrix": "no-existe", "range": "tv"})
    assert r["vmaf_mean"] > 90 and r["frame_vmaf"] == {}


def test_ensure_originals_reuses_rows(src, tmp_path):
    conn = db.connect(str(tmp_path / "f.db"))
    info = media.probe_source(src)
    root = tmp_path / "frames"
    first = frames.ensure_originals(conn, root, src.parent, src.name, 0.0, 3.0, 3, info)
    assert len(first) == 3 and all((root / frames.scene_key(src.name, 0.0, 3.0)).exists() for _ in first)
    again = frames.ensure_originals(conn, root, src.parent, src.name, 0.0, 3.0, 5, info)
    assert [r["idx"] for r in again] == [r["idx"] for r in first]  # índices fijos por escena


def test_remove_files_only_under_root(tmp_path):
    root = tmp_path / "frames"
    (root / "a").mkdir(parents=True)
    inside, outside = root / "a" / "x.webp", tmp_path / "keep.txt"
    inside.write_bytes(b"x")
    outside.write_text("x")
    frames.remove_files(root, [str(inside), str(outside), str(root / "../keep.txt")])
    assert not inside.exists() and not (root / "a").exists() and outside.exists()
