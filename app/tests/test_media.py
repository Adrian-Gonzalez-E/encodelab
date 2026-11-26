import subprocess
from pathlib import Path

import pytest

from encodelab import media

from encodelab.media import (codec_args, encode_cmd, expand_matrix, metrics_cmd, parse_extra_args,
                             parse_probe, safe_media_path, summarize_vmaf)


def test_safe_media_path_rejects_escape(tmp_path):
    (tmp_path / "a.mkv").touch()
    assert safe_media_path(tmp_path, "a.mkv") == tmp_path / "a.mkv"
    (tmp_path / "b.m2ts").touch()
    (tmp_path / "c.ts").touch()
    # MPEG-TS: seek inexacto y offsets de PTS dan métricas basura; hay que remuxar a MKV.
    for bad in ("../etc/passwd", "/etc/passwd", "a.txt", "no.mkv", "b.m2ts", "c.ts"):
        with pytest.raises(ValueError):
            safe_media_path(tmp_path, bad)


def test_safe_media_path_dirs(tmp_path):
    (tmp_path / "s").mkdir()
    assert safe_media_path(tmp_path, "s", must_be_video=False) == tmp_path / "s"
    assert safe_media_path(tmp_path, "", must_be_video=False) == tmp_path
    with pytest.raises(ValueError):
        safe_media_path(tmp_path, "..", must_be_video=False)


def test_parse_extra_args():
    assert parse_extra_args("-svtav1-params tune=0:film-grain=8") == ["-svtav1-params", "tune=0:film-grain=8"]
    assert parse_extra_args("") == []
    assert parse_extra_args("-x265-params aq-mode=3:psy-rd=2.0") == ["-x265-params", "aq-mode=3:psy-rd=2.0"]
    for bad in ("-i x.mkv", "-y", "-f null /data/encodelab.db", "'sin cerrar", "-f rawvideo ../data/encodelab.db",
                "-f rawvideo file:pwned", "-f rawvideo tcp:qbittorrent:8080", "-vf movie=../etc/passwd"):
        with pytest.raises(ValueError):
            parse_extra_args(bad)


def test_expand_matrix():
    assert expand_matrix("28, 32", "6,8") == [(28, "6"), (28, "8"), (32, "6"), (32, "8")]
    for q, p in (("", "6"), ("x", "6"), ("300", "6"), ("1,2,3,4,5", "1,2,3,4,5"), ("30", "6;rm")):
        with pytest.raises(ValueError):
            expand_matrix(q, p)


def test_codec_args_and_encode_cmd():
    c = codec_args("libsvtav1", 30, "6", ["-pix_fmt", "yuv420p10le"])
    assert c == ["-c:v", "libsvtav1", "-crf", "30", "-preset", "6", "-pix_fmt", "yuv420p10le"]
    cmd = encode_cmd(Path("/media/x.mkv"), 600.0, 20.0, c, Path("/work/o.mkv"), qsv=False)
    assert cmd[:3] == ["nice", "-n", "15"] and cmd[-1] == "/work/o.mkv"
    assert cmd[-4:-1] == ["-progress", "pipe:1", "-nostats"]
    m = cmd.index("-map")
    assert cmd.index("-ss") < cmd.index("-i") and cmd[m:m + 5] == ["-map", "0:v:0", "-an", "-sn", "-dn"]
    assert "-init_hw_device" not in cmd
    q = encode_cmd(Path("/media/x.mkv"), 0, 5, codec_args("av1_qsv", 25, "slow", []), Path("/work/o.mkv"), qsv=True)
    assert q.index("-init_hw_device") < q.index("-i")
    assert codec_args("av1_qsv", 25, "slow", [])[2:4] == ["-global_quality", "25"]


def test_metrics_cmd_distorted_first():
    cmd = metrics_cmd(Path("/media/x.mkv"), 600.0, 20.0, Path("/work/o.mkv"), Path("/work/v.json"), 6)
    assert cmd[cmd.index("-i") + 1] == "/work/o.mkv"  # entrada 0 = distorsionada
    g = cmd[cmd.index("-lavfi") + 1]
    assert "[0:v]settb=AVTB,setpts=N,format=yuv420p10le[dis]" in g and "[dis][ref]libvmaf" in g
    assert "log_path=/work/v.json:n_threads=6" in g
    assert cmd[-6:] == ["-progress", "pipe:1", "-nostats", "-f", "null", "-"]


def test_summarize_vmaf():
    frames = [{"metrics": {"vmaf": float(i), "vmaf_neg": 50.0, "psnr_y": 40.0, "float_ssim": 0.9,
                           "float_ms_ssim": 0.95}} for i in range(101)]
    s = summarize_vmaf({"frames": frames})
    assert s == {"frames": 101, "vmaf_mean": 50.0, "vmaf_min": 0.0, "vmaf_p5": 5.0, "vmaf_neg_mean": 50.0,
                 "psnr_y": 40.0, "ssim": 0.9, "ms_ssim": 0.95}


def test_parse_probe_interlaced():
    d = {"streams": [{"codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "30000/1001",
                      "field_order": "tt", "pix_fmt": "yuv420p"}], "format": {"duration": "2640.5"}}
    p = parse_probe(d)
    assert p["interlaced"] is True and round(p["fps"], 3) == 29.97 and p["duration_s"] == 2640.5
    d["streams"][0]["field_order"] = "progressive"
    assert parse_probe(d)["interlaced"] is False
    del d["streams"][0]["field_order"]
    assert parse_probe(d)["interlaced"] is False


def test_detect_qsv_without_device(tmp_path):
    assert media.detect_qsv(str(tmp_path / "nope")) == ([], f"sin dispositivo {tmp_path / 'nope'}")


def test_detect_qsv_ffmpeg_fails(tmp_path, monkeypatch):
    dev = tmp_path / "renderD128"
    dev.touch()
    monkeypatch.setattr(media.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "x\nPermission denied\n"))
    assert media.detect_qsv(str(dev)) == ([], "av1_qsv: Permission denied; hevc_qsv: Permission denied")


def test_detect_qsv_ok_and_partial(tmp_path, monkeypatch):
    dev = tmp_path / "renderD129"
    dev.touch()
    seen = []
    def fake(cmd, **kw):
        seen.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 1 if "av1_qsv" in cmd else 0, "", "Current resolution is unsupported")
    monkeypatch.setattr(media.subprocess, "run", fake)
    assert media.detect_qsv(str(dev)) == (["hevc_qsv"], "av1_qsv: Current resolution is unsupported")
    cmd, kw = seen[0]
    assert f"vaapi=va:{dev}" in cmd and "qsv=hw@va" in cmd and kw.get("timeout") == 15


def test_detect_qsv_timeout(tmp_path, monkeypatch):
    dev = tmp_path / "renderD128"
    dev.touch()
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 15)
    monkeypatch.setattr(media.subprocess, "run", boom)
    ok, reason = media.detect_qsv(str(dev))
    assert ok == [] and "15 s" in reason


def test_available_encoders():
    assert media.available_encoders([]) == ["libsvtav1", "libx265"]
    assert media.available_encoders(["hevc_qsv"]) == ["libsvtav1", "libx265", "hevc_qsv"]
