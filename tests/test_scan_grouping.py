from datetime import date

from improv_video.grouping import Part, group_days, group_recordings
from improv_video.probe import probe
from improv_video.scan import find_camera_dirs, list_clips


def test_only_video_clips(card):
    dirs = find_camera_dirs(card)
    assert [d.name for d in dirs] == ["Camera01"]
    names = [c.name for c in list_clips(dirs[0])]
    assert names == [
        "VID_20261001_180500_00_578.mp4",
        "VID_20261001_180500_00_579.mp4",
        "VID_20261001_183000_00_580.mp4",
        "VID_20261002_013000_00_581.mp4",
        "VID_20261003_120000_00_582.mp4",
    ]


def test_probe_vertical_and_silent(card):
    cam = card / "DCIM" / "Camera01"
    v = probe(cam / "VID_20261003_120000_00_582.mp4")
    assert (v.width, v.height) == (360, 640)
    assert not probe(cam / "VID_20261001_183000_00_580.mp4").has_audio


def test_recordings_and_days(card):
    clips = list_clips(card / "DCIM" / "Camera01")
    recs = group_recordings([Part(c, probe(c.path)) for c in clips])
    assert [len(r.parts) for r in recs] == [2, 1, 1, 1]
    days = group_days(recs)
    assert list(days) == [date(2026, 10, 1), date(2026, 10, 3)]
    assert len(days[date(2026, 10, 1)]) == 3  # запись в 01:30 2 октября — в дне 1 октября


def test_sequential_names_also_group(tmp_path):
    """Запасной признак: файл начинается сразу после конца предыдущего."""
    from datetime import datetime
    from fractions import Fraction
    from improv_video.probe import Media
    from improv_video.scan import Clip

    def part(name, start, dur):
        return Part(Clip(tmp_path / name, name, 1, start), Media(dur, 1920, 1080, Fraction(30), True))

    recs = group_recordings([
        part("a", datetime(2026, 10, 1, 18, 0, 0), 600),
        part("b", datetime(2026, 10, 1, 18, 10, 1), 300),
        part("c", datetime(2026, 10, 1, 18, 30, 0), 60),
    ])
    assert [len(r.parts) for r in recs] == [2, 1]
    assert recs[0].end == datetime(2026, 10, 1, 18, 15, 0)
