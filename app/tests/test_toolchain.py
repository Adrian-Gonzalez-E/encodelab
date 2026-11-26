import json
import subprocess
from pathlib import Path

import pytest

from encodelab.media import QSV_ARGS, VMAF_FILTER


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def test_encoders_present():
    out = run(["ffmpeg", "-hide_banner", "-encoders"])
    for enc in ("libsvtav1", "libx265", "av1_qsv", "hevc_qsv"):
        assert enc in out


def test_vmaf_filter_produces_all_metrics(tmp_path):
    log = tmp_path / "v.json"
    lavfi = ("testsrc2=s=640x360:r=24:d=1,split[a][b];"
             "[a]format=yuv420p10le[dis];[b]format=yuv420p10le[ref];"
             "[dis][ref]" + VMAF_FILTER.format(log=log, threads=2))
    run(["ffmpeg", "-hide_banner", "-filter_complex", lavfi, "-f", "null", "-"])  # sin entradas: testsrc2 es la fuente
    frames = json.loads(log.read_text())["frames"]
    assert {"vmaf", "vmaf_neg", "psnr_y", "float_ssim", "float_ms_ssim"} <= frames[0]["metrics"].keys()
    # El fotograma 0 da ~97 aun idéntico (movimiento = 0); por eso se mira la media.
    assert sum(f["metrics"]["vmaf"] for f in frames) / len(frames) > 99


@pytest.mark.skipif(not Path("/dev/dri/renderD128").exists(), reason="sin GPU Intel")
def test_qsv_av1_encodes(tmp_path):
    run(["ffmpeg", "-hide_banner", *QSV_ARGS, "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=1",
         "-c:v", "av1_qsv", "-global_quality", "30", str(tmp_path / "o.mkv")])
    assert (tmp_path / "o.mkv").stat().st_size > 0
