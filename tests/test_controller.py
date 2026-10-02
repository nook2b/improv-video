import threading
from contextlib import contextmanager
from pathlib import Path

from improv_video.app.config import AppConfig
from improv_video.app.controller import Controller


class FakeUI:
    def __init__(self, answers):
        self.answers = answers  # подстрока вопроса → ответ
        self.asked, self.notes, self.clipboard, self.opened, self.revealed = [], [], [], [], []
        self.lock = threading.Lock()

    def dialog(self, text, buttons, default=None, title="", giving_up_after=None):
        with self.lock:
            self.asked.append(text)
        for key, answer in self.answers.items():
            if key in text:
                return answer
        return buttons[-1]

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


def test_first_run_can_skip(card, tmp_path):
    ui = FakeUI({"первый запуск": "Считать обработанными"})
    c = make_controller(tmp_path, ui)
    c.submit("source", card)
    c.wait_idle()
    c.stop()
    assert c.manual == []
    assert any("помечены как обработанные" in t for _, t in ui.notes)


def _fast_settings(original):
    def settings(self):
        s = original(self)
        s.x265_preset = "ultrafast"
        s.encoders = ("libx265",)
        return s
    return settings
