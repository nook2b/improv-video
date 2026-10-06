from datetime import date
from fractions import Fraction

from improv_video import brightness, lut as lutlib
from improv_video.pipeline import Settings, build_day
from improv_video.probe import probe
from improv_video.video import plan_chunks

from conftest import make_clip

LUT = "calibration/AcePro2_I-Log_To_Rec.709_V1.0.cube"


def test_plan_chunks_exact():
    fps = Fraction(30000, 1001)
    chunks = plan_chunks(3600, fps, 300)
    assert sum(n for _, n in chunks) == round(3600 * fps)
    assert all(s2 == s1 + n1 for (s1, n1), (s2, _) in zip(chunks, chunks[1:]))
    assert plan_chunks(200, fps, 300) == [(0, round(200 * fps))]


def test_baked_lut_matches_separate_offset(tmp_path):
    """Сдвиг, запечённый в LUT, даёт ту же яркость, что отдельная поправка lutyuv перед LUT."""
    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 2, vf="eq=contrast=0.45:saturation=0.5")
    samples = brightness.sample_frames([clip], tmp_path / "s.mkv", 1.0)
    stops = 0.8
    separate = brightness.measure(samples, LUT, "ilog", stops)
    baked = lutlib.bake_offset(LUT, tmp_path / "baked.cube", stops * brightness.ILOG_CODES_PER_STOP / 876)
    together = brightness.measure(samples, baked, "ilog", 0)
    assert abs(separate - together) < 0.01, (separate, together)


def test_chunked_parallel_encode_keeps_sync(tmp_path):
    a = make_clip(tmp_path / "VID_20261001_180000_00_001.mp4", 4)
    b = make_clip(tmp_path / "VID_20261001_180000_00_002.mp4", 3, audio_seconds=2.6)
    settings = Settings(archive=tmp_path, lut=LUT, profile="ilog", denoise="off", x265_preset="ultrafast",
                        encoders=("libx265",), workers=3, chunk_seconds=2)
    r = build_day(date(2026, 10, 1), [a, b], "training", 1, settings, tmp_path / "out.mp4", notify=lambda _: None)
    m = probe(r.file)
    frames = round(7 * Fraction(30))  # тестовые клипы 30 к/с
    assert abs(m.duration - frames / 30) < 0.05
    import json, subprocess
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                         str(r.file)], capture_output=True, text=True).stdout)["streams"]
    durs = {s["codec_type"]: float(s["duration"]) for s in streams}
    assert abs(durs["video"] - durs["audio"]) < 0.05
