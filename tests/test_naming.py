from datetime import date, datetime, time

import pytest

from improv_video.naming import description, parse_start, shooting_day, title


def test_parse_start():
    assert parse_start("VID_20261001_180500_00_001.mp4") == datetime(2026, 10, 1, 18, 5, 0)
    assert parse_start("LRV_20261001_180500_01_001.lrv") is None
    assert parse_start("VID_20261399_999999_00_001.mp4") is None


def test_shooting_day_boundary():
    assert shooting_day(datetime(2026, 10, 2, 1, 30)) == date(2026, 10, 1)
    assert shooting_day(datetime(2026, 10, 2, 4, 0)) == date(2026, 10, 2)
    assert shooting_day(datetime(2026, 10, 2, 5, 0), time(6, 0)) == date(2026, 10, 1)


def test_titles():
    assert title("training", date(2026, 10, 1)) == "Тренировка 01.10.2026"
    assert title("lesson", date(2026, 3, 9)) == "Занятие 09.03.2026"
    assert title("lesson", date(2026, 3, 9), part=2) == "Занятие 09.03.2026 (часть 2)"
    with pytest.raises(ValueError):
        title("party", date(2026, 3, 9))


def test_description():
    assert description(datetime(2026, 10, 1, 18, 5), datetime(2026, 10, 1, 20, 40)) == "Снято 01.10.2026, 18:05–20:40"
    assert (description(datetime(2026, 10, 1, 23, 10), datetime(2026, 10, 2, 1, 30))
            == "Снято 01.10.2026 23:10 – 02.10.2026 01:30")


def test_show_and_masterclass_titles():
    from datetime import date

    from improv_video.naming import title

    assert title("show", date(2026, 10, 8)) == "Шоу 08.10.2026"
    assert title("masterclass", date(2026, 10, 8), 2) == "Мастер-класс 08.10.2026 (часть 2)"
