from encodelab import media
from encodelab.glossary import BY_KEY, ENCODER_INFO, with_thresholds


def test_glossary_covers_ui():
    keys = {"vmaf_mean", "vmaf_neg_mean", "vmaf_p5", "vmaf_min", "psnr_y", "ssim", "ms_ssim",
            "kbps", "ratio", "est_gb", "enc_fps", "est_hours"}
    assert set(BY_KEY) == keys and all(d["name"] and d["short"] and d["long"] for d in BY_KEY.values())
    assert all(media.ENCODERS[e]["codec"] in ("av1", "hevc") for e in media.ENCODERS)


def test_encoder_info_complete():
    assert set(ENCODER_INFO) == set(media.ENCODERS)
    for enc, e in ENCODER_INFO.items():
        assert e["title"] and e["text"] and e["codec"] in ("AV1", "HEVC") and e["engine"] in ("CPU", "Intel QSV"), enc
        assert set(e["meters"]) == {"compresion", "velocidad", "compat"}
        assert all(e["quality"][k] for k in ("flag", "range", "start", "default", "hint"))
        assert all(e["preset"][k] for k in ("range", "start", "default", "hint")) and e["extras_hint"]
        media.expand_matrix(e["quality"]["default"], e["preset"]["default"])  # los valores por defecto son válidos


def test_vmaf_scale_follows_thresholds():
    g = with_thresholds(85, 95)
    assert [x[0] for x in g["vmaf_p5"]["scale"]] == [0, 85, 95] and [x[0] for x in g["vmaf_min"]["scale"]] == [0, 85, 95]
    assert g["psnr_y"]["scale"] == BY_KEY["psnr_y"]["scale"]
    assert [x[0] for x in BY_KEY["vmaf_p5"]["scale"]] == [0, 80, 93]  # el original no se toca
