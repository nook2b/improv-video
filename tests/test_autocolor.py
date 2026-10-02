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
