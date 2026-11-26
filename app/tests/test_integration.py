import os
import subprocess

import pytest

from encodelab.media import EncodeError, ffmpeg_version, probe_source, run_test, segment_kbps


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    path = tmp_path_factory.mktemp("src") / "src.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=1280x720:r=24:d=4", "-c:v", "libx264", "-crf", "18", "-g", "48", str(path)],
                   check=True)
    return path


@pytest.fixture(scope="module")
def src_23976(tmp_path_factory):
    # Caso Blu-ray: 24000/1001 en MKV, timestamps redondeados a 1 ms.
    path = tmp_path_factory.mktemp("src") / "src23976.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:r=24000/1001:d=30", "-c:v", "libx264", "-crf", "18", "-g", "24", str(path)],
                   check=True)
    return path


@pytest.mark.parametrize("start", [17.77, 25.0])
def test_aligned_segment_23976(src_23976, tmp_path, start):
    r = run_test(src_23976, start, 2.0, "libsvtav1", 10, "12", [], tmp_path, 2)
    assert r["vmaf_mean"] > 95


def test_aligned_segment_scores_high(src, tmp_path):
    r = run_test(src, 1.0, 2.0, "libsvtav1", 10, "12", [], tmp_path, 2)
    assert r["frames"] == 48 and r["vmaf_mean"] > 95 and r["kbps"] > 0 and r["enc_fps"] > 0
    assert r["ffmpeg_args"] == "-c:v libsvtav1 -crf 10 -preset 12"
    assert list(tmp_path.iterdir()) == []


def test_run_test_reports_progress(src, tmp_path):
    calls = []
    r = run_test(src, 1.0, 2.0, "libsvtav1", 30, "12", [], tmp_path, 2,
                 on_progress=lambda phase, frac, frame: calls.append((phase, frac, frame)))
    phases = [c[0] for c in calls]
    fracs = [c[1] for c in calls]
    assert phases[0] == "encode" and phases[-1] == "metrics" and fracs[-1] == 1.0
    assert fracs == sorted(fracs) and all(0 <= f <= 1 for f in fracs)
    assert all(f <= 0.7 for p, f, _ in calls if p == "encode")
    assert any(0.71 < f < 1 for p, f, _ in calls if p == "metrics"), "la medida no avanza"
    assert r["measure_s"] > 0 and calls[-1][2] == 48


def test_progress_callback_error_kills_ffmpeg(src, tmp_path):
    def boom(phase, frac, frame):
        raise RuntimeError("callback roto")

    with pytest.raises(RuntimeError):
        run_test(src, 0, 2.0, "libsvtav1", 30, "4", [], tmp_path, 2, on_progress=boom)
    with pytest.raises(ChildProcessError):  # ningún ffmpeg hijo vivo ni sin recoger
        os.waitpid(-1, os.WNOHANG)


@pytest.mark.parametrize("extra", [["-svtav1-params", "nonsense=1"], ["-nonsense", "1"]])
def test_bad_option_raises_encode_error(src, tmp_path, extra):
    with pytest.raises(EncodeError, match="nonsense"):
        run_test(src, 0, 2.0, "libsvtav1", 30, "12", extra, tmp_path, 2)
    assert list(tmp_path.iterdir()) == []


def test_segment_kbps_and_probe(src):
    assert segment_kbps(src, 1.0, 2.0) > 0
    assert probe_source(src)["width"] == 1280
    assert ffmpeg_version().startswith("ffmpeg version")
