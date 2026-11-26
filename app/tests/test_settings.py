import pytest

from encodelab import settings
from encodelab.db import connect, get_settings, save_settings

VALID = {"default_duration": "30", "default_start_pct": "40", "default_encoder": "av1_qsv",
         "extras_libsvtav1": "-pix_fmt yuv420p10le", "extras_av1_qsv": "-pix_fmt p010le", "extras_libx265": "",
         "extras_hevc_qsv": "", "best_vmaf": "95", "default_view": "baterias", "refresh_s": "5",
         "vmaf_warn": "85", "card_style": "gauges", "background": "aurora"}


def test_settings_roundtrip(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    assert settings.load(conn) == settings.DEFAULTS
    save_settings(conn, settings.parse(VALID))  # sin "animations": casilla desmarcada
    s = settings.load(conn)
    assert (s["default_duration"], s["default_encoder"], s["refresh_s"], s["default_view"]) == (30, "av1_qsv", 5, "baterias")
    assert s["animations"] is False and s["best_vmaf"] == 95
    assert s["vmaf_warn"] == 85 and s["card_style"] == "gauges" and s["background"] == "aurora"
    assert get_settings(conn)["default_start_pct"] == "40"


@pytest.mark.parametrize("bad", [{"default_duration": "500"}, {"default_encoder": "libaom-av1"},
                                 {"extras_libsvtav1": "-i /etc/passwd"}, {"refresh_s": "3"}, {"best_vmaf": "abc"},
                                 {"default_view": "tabla"}, {"default_start_pct": "99"},
                                 {"vmaf_warn": "96"}, {"vmaf_warn": "abc"}, {"card_style": "tabla"},
                                 {"background": "<script>"}])
def test_settings_parse_rejects(bad):
    with pytest.raises(ValueError):
        settings.parse({**VALID, **bad})
