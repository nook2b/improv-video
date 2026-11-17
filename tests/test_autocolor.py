from improv_video import autocolor, brightness

from conftest import make_clip

LUT = "calibration/AcePro2_I-Log_To_Rec.709_V1.0.cube"


def _stats(px):
    lum = sorted(autocolor._luma(p) for p in px)
    mean = sum(lum) / len(lum)
    return mean, autocolor._pct(lum, 0.01), autocolor._pct(lum, 0.995)


def test_auto_hits_targets(tmp_path):
    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 3, vf="eq=contrast=0.45:brightness=-0.22:saturation=0.5")
    samples = brightness.sample_frames([clip], tmp_path / "s.mkv", 1.0)
    _, _, white0 = _stats(autocolor._pixels(samples, LUT))
    final, grade = autocolor.solve(samples, LUT, tmp_path)
    mean, black, white = _stats(autocolor._pixels(samples, final))
    assert abs(mean - autocolor.TARGET_MEAN) < 0.03, grade.describe()
    assert abs(black - autocolor.TARGET_BLACK) < 0.03
    # Белые тянутся к цели, но не больше чем в 1.5 раза — плоскую картинку не «задираем»
    assert abs(white - autocolor.TARGET_WHITE) < abs(white0 - autocolor.TARGET_WHITE)
    assert "экспозиция" in grade.describe()


def test_white_balance_reduces_blue_cast(tmp_path):
    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 3,
                     vf="hue=s=0,eq=contrast=0.45:brightness=-0.15,colorbalance=bs=0.03:bm=0.03:bh=0.03")
    samples = brightness.sample_frames([clip], tmp_path / "s.mkv", 1.0)
    _, grade = autocolor.solve(samples, LUT, tmp_path)
    r, g, b = grade.gains
    assert b < 1.0 and r >= 1.0 - 1e-6
    assert b >= 1 - autocolor.MAX_WB


def test_bright_scene_stays_bright(tmp_path):
    """Светлая комната (EV 0) не затемняется до яркости чёрного зала."""
    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 3, vf="eq=contrast=0.45:brightness=0.02:saturation=0.5")
    samples = brightness.sample_frames([clip], tmp_path / "s.mkv", 1.0)
    mean0, _, _ = _stats(autocolor._pixels(samples, LUT))
    assert mean0 > 0.4  # сцена и правда светлая
    final, grade = autocolor.solve(samples, LUT, tmp_path)
    mean, _, white = _stats(autocolor._pixels(samples, final))
    assert abs(mean - mean0) < 0.05, grade.describe()
    assert white > 0.85


def test_overexposed_scene_is_pulled_down(tmp_path):
    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 3, vf="eq=contrast=0.45:brightness=0.10:saturation=0.5")
    samples = brightness.sample_frames([clip], tmp_path / "s.mkv", 1.0)
    final, grade = autocolor.solve(samples, LUT, tmp_path)
    mean, _, _ = _stats(autocolor._pixels(samples, final))
    assert abs(mean - autocolor.TARGET_MEAN_BRIGHT) < 0.04, grade.describe()


def test_reported_exposure_is_the_one_baked(tmp_path):
    """Поправка в описании — та же, что запечена в LUT (раньше последний шаг терялся)."""
    from improv_video import lut as lutlib

    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 3, vf="eq=contrast=0.45:brightness=0.10:saturation=0.5")
    samples = brightness.sample_frames([clip], tmp_path / "s.mkv", 1.0)
    final, grade = autocolor.solve(samples, LUT, tmp_path)
    offset = grade.stops * brightness.ILOG_CODES_PER_STOP / 876
    size, rows = lutlib.read_cube(lutlib.bake_offset(LUT, tmp_path / "check.cube", offset))
    expected = [tuple(grade.curve(v, c) for c, v in enumerate(row)) for row in rows]
    _, got = lutlib.read_cube(final)
    assert max(abs(a - b) for e, g in zip(expected, got) for a, b in zip(e, g)) < 1e-5
