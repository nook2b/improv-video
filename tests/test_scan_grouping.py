from datetime import date

from improv_video.grouping import Part, group_days, group_recordings
from improv_video.probe import probe
from improv_video.scan import find_camera_dirs, list_clips


def test_only_video_clips(card):
    dirs = find_camera_dirs(card)
    assert [d.name for d in dirs] == ["Camera01"]
    names = [c.name for c in list_clips(dirs[0])]
    assert names == [
        "VID_20261001_180500_00_001.mp4",
        "VID_20261001_180503_00_002.mp4",
        "VID_20261001_183000_00_003.mp4",
        "VID_20261002_013000_00_004.mp4",
        "VID_20261003_120000_00_005.mp4",
    ]


def test_probe_vertical_and_silent(card):
    cam = card / "DCIM" / "Camera01"
    v = probe(cam / "VID_20261003_120000_00_005.mp4")
    assert (v.width, v.height) == (360, 640)
    assert not probe(cam / "VID_20261001_183000_00_003.mp4").has_audio


def test_recordings_and_days(card):
    clips = list_clips(card / "DCIM" / "Camera01")
    recs = group_recordings([Part(c, probe(c.path)) for c in clips])
    assert [len(r.parts) for r in recs] == [2, 1, 1, 1]
    days = group_days(recs)
    assert list(days) == [date(2026, 10, 1), date(2026, 10, 3)]
    assert len(days[date(2026, 10, 1)]) == 3  # запись в 01:30 2 октября — в дне 1 октября
