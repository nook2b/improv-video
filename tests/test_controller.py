import threading
from contextlib import contextmanager
from pathlib import Path

from improv_video.app.config import AppConfig
from improv_video.app.controller import Controller


class FakeUI:
    def __init__(self, answers):
        self.answers = answers  # подстрока вопроса → ответ
        self.asked, self.notes, self.clipboard, self.opened, self.revealed = [], [], [], [], []
        self.ready = []
        self.lock = threading.Lock()

    def dialog(self, text, buttons, default=None, title="", giving_up_after=None):
        with self.lock:
            self.asked.append(text)
        for key, answer in self.answers.items():
            if key in text:
                return answer
        return buttons[-1] if buttons else None

    def ask_first_run(self, clips, days):
        answer = self.dialog(f"На карте {clips} клипов за {days} дн. — первый запуск", [])
        return {"Обработать все": "all", "Считать обработанными": "skip"}.get(answer)

    def ask_kind(self, day, meta="", note="", default="training", timeout=None):
        answer = self.dialog(f"Что снимали {day:%d.%m.%Y}? {meta}", [])
        return {"Тренировка": "training", "Занятие": "lesson"}.get(answer)

    def show_ready(self, name, desc, file):
        with self.lock:
            self.ready.append((name, desc, Path(file)))

    def notify(self, title, text):
        self.notes.append((title, text))

    def choose_file(self, prompt, extensions=None):
        return None

    def copy_to_clipboard(self, text):
        self.clipboard.append(text)

    def reveal(self, path):
        self.revealed.append(Path(path))

    def open_path(self, target):
        self.opened.append(str(target))

    @contextmanager
    def keep_awake(self):
        yield


def make_controller(tmp_path, ui, **cfg):
    config = AppConfig(archive=str(tmp_path / "Footage"), lut="", denoise="weak", **cfg)
    c = Controller(config, ui, config_path=tmp_path / "config.json", volumes_dir=tmp_path / "Volumes")
    c.start(watch=False)
    return c


def test_card_to_manual_upload(card, tmp_path, monkeypatch):
    monkeypatch.setattr("improv_video.app.config.AppConfig.settings",
                        _fast_settings(AppConfig.settings))
    ui = FakeUI({"первый запуск": "Обработать все", "01.10.2026": "Занятие", "03.10.2026": "Тренировка"})
    c = make_controller(tmp_path, ui)
    c.submit("source", card)
    c.wait_idle(300)
    c.stop()

    assert any("Что снимали 01.10.2026" in q for q in ui.asked)
    handed = sorted(name for name, _, _ in c.manual)
    assert handed == ["Занятие 01.10.2026", "Тренировка 03.10.2026"]
    assert "Занятие 01.10.2026" in ui.clipboard
    assert "https://studio.youtube.com" in ui.opened
    assert all(p.name.startswith("video") for p in ui.revealed)
    assert AppConfig.load(tmp_path / "config.json").last_kind in ("lesson", "training")
    assert any("готов за" in t for _, t in ui.notes)
    assert any("Что снимали 01.10.2026? 18:05–" in q and "клипа" in q for q in ui.asked)
    assert sorted(n for n, _, _ in ui.ready) == ["Занятие 01.10.2026", "Тренировка 03.10.2026"]
    statuses = {it.title: it.status for it in c.videos()}
    # «Готово» в окне «Ролик готов» → передан в Studio
    assert statuses == {"Занятие 01.10.2026": "handed", "Тренировка 03.10.2026": "handed"}


def test_no_new_clips_note(card, tmp_path):
    ui = FakeUI({"первый запуск": "Считать обработанными"})
    c = make_controller(tmp_path, ui)
    c.submit("source", card)
    c.wait_idle()
    c.submit("source", card)  # всё уже обработано
    c.wait_idle()
    c.stop()
    assert ("improv-video", "Новых клипов нет") in ui.notes
    assert c.status == "Новых клипов нет" and not c.busy


def test_stop_processing_keeps_day_for_next_insert(card, tmp_path, monkeypatch):
    from improv_video import tools
    import improv_video.pipeline as pl

    monkeypatch.setattr("improv_video.app.config.AppConfig.settings", _fast_settings(AppConfig.settings))
    ui = FakeUI({"первый запуск": "Обработать все", "Что снимали": "Занятие"})
    c = make_controller(tmp_path, ui, copy_clips=False)
    calls = []

    def stopped(*a, **kw):
        calls.append(a[0])
        raise tools.Cancelled()

    monkeypatch.setattr(pl, "build_day", stopped)
    c.submit("source", card)
    c.wait_idle(300)
    c.stop()
    assert len(calls) == 1  # остальные дни после остановки не начинаются
    assert any("Обработка остановлена" in t for _, t in ui.notes)
    failed = [it for it in c.videos() if it.status == "failed"]
    assert len(failed) == 1 and "остановили" in failed[0].detail
    assert c.manual == []


def test_first_run_can_skip(card, tmp_path):
    ui = FakeUI({"первый запуск": "Считать обработанными"})
    c = make_controller(tmp_path, ui)
    c.submit("source", card)
    c.wait_idle()
    c.stop()
    assert c.manual == []
    assert any("помечены как обработанные" in t for _, t in ui.notes)


def test_forget_skipped_returns_clips(card, tmp_path):
    ui = FakeUI({"первый запуск": "Считать обработанными"})
    c = make_controller(tmp_path, ui)
    c.submit("source", card)
    c.wait_idle()
    c.submit("forget_skipped")
    c.wait_idle()
    c.stop()
    assert any("Отметка снята с 5 клипов" in t for _, t in ui.notes)


def _fast_settings(original):
    def settings(self):
        s = original(self)
        s.x265_preset = "ultrafast"
        s.encoders = ("libx265",)
        return s
    return settings


def test_no_copy_mode_reads_from_card_and_resumes(card, tmp_path, monkeypatch):
    """Без копирования: на Mac только готовое видео; без флешки день ждёт и собирается при новой вставке."""
    import shutil
    monkeypatch.setattr("improv_video.app.config.AppConfig.settings", _fast_settings(AppConfig.settings))
    # Своя копия карты, чтобы её можно было «вынуть»
    my_card = tmp_path / "CARD"
    shutil.copytree(card, my_card)
    ui = FakeUI({"первый запуск": "Обработать все", "Что снимали": "Занятие"})
    c = make_controller(tmp_path, ui, copy_clips=False, max_height=1080)

    # Сбой посреди сборки (как если флешку вынули): ролик не теряется
    calls = {"n": 0}
    import improv_video.pipeline as pl
    real_build = pl.build_day

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("флешка извлечена")
        return real_build(*a, **kw)

    monkeypatch.setattr(pl, "build_day", flaky)
    c.submit("source", my_card)
    c.wait_idle(300)
    archive = tmp_path / "Footage"
    assert not list(archive.rglob("VID_*.mp4"))  # клипы на Mac не копируются
    first = sorted(n for n, _, _ in c.manual)

    c.submit("source", my_card)  # вставили флешку снова
    c.wait_idle(300)
    c.stop()
    names = sorted(n for n, _, _ in c.manual)
    assert names == ["Занятие 01.10.2026", "Занятие 03.10.2026"], (first, names)
    assert not list(archive.rglob("VID_*.mp4"))
    assert len(list(archive.rglob("video*.mp4"))) == 2
