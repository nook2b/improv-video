from datetime import date

from improv_video.state import State


def test_parts_and_assignment(tmp_path):
    s = State(tmp_path / "s.sqlite")
    d = date(2026, 10, 1)
    assert s.is_empty()
    s.add_clip("a.mp4", 10, d)
    s.add_clip("b.mp4", 20, d)
    assert s.is_known("a.mp4", 10) and not s.is_known("a.mp4", 11)
    vid, part = s.create_video(d, ["a.mp4", "b.mp4"], "training")
    assert part == 1 and s.unassigned_clips(d) == []
    s.add_clip("c.mp4", 30, d)  # досняли в тот же день
    vid2, part2 = s.create_video(d, s.unassigned_clips(d), "training")
    assert part2 == 2 and vid2 != vid
    s.update_video(vid, status="uploaded", youtube_id="xyz")
    assert s.video(vid)["youtube_id"] == "xyz"
