import hashlib
import json
import plistlib
import subprocess
import zipfile

import pytest

from improv_video.app import updater


def test_versions():
    assert updater.parse_version("v0.1.14") == (0, 1, 14)
    assert updater.is_newer("0.1.14", "0.1.13")
    assert updater.is_newer("0.2.0", "0.1.99")
    assert not updater.is_newer("0.1.13", "0.1.13")
    assert not updater.is_newer("0.1.9", "0.1.13")  # не строками: 9 < 13
    assert not updater.is_newer("мусор", "0.1.13")


def test_latest_reads_release():
    data = {"tag_name": "v0.1.14", "assets": [
        {"name": "improv-video-mac.zip", "browser_download_url": "https://x/zip"},
        {"name": "improv-video-mac.zip.sha256", "browser_download_url": "https://x/sha"}]}
    r = updater.latest(fetch=lambda url: json.dumps(data).encode())
    assert (r.version, r.zip_url, r.sha256_url) == ("0.1.14", "https://x/zip", "https://x/sha")
    assert updater.latest(fetch=lambda url: json.dumps({"tag_name": "v1", "assets": []}).encode()) is None


def _release(tmp_path, version="0.1.14", sha=None):
    app = tmp_path / "src" / "improv-video.app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
    archive = tmp_path / "improv-video-mac.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(app / "Info.plist", "improv-video.app/Contents/Info.plist")
    digest = tmp_path / "sum"
    digest.write_text((sha or hashlib.sha256(archive.read_bytes()).hexdigest()) + "  improv-video-mac.zip\n")
    return updater.Release("0.1.14", archive.as_uri(), digest.as_uri())


def test_prepare_downloads_checks_and_unpacks(tmp_path):
    app = updater.prepare(_release(tmp_path), tmp_path / "update")
    assert app.name == "improv-video.app" and updater.bundle_version(app) == "0.1.14"
    assert updater.prepare(_release(tmp_path / "again"), tmp_path / "update") == app  # уже скачано


def test_prepare_rejects_broken_download(tmp_path):
    with pytest.raises(ValueError, match="повреждено"):
        updater.prepare(_release(tmp_path, sha="0" * 64), tmp_path / "update")


def test_prepare_rejects_wrong_version(tmp_path):
    with pytest.raises(ValueError, match="нужной версии"):
        updater.prepare(_release(tmp_path, version="0.1.13"), tmp_path / "update")


def test_swap_script_replaces_app_and_keeps_old(tmp_path):
    bundle, new, backup = tmp_path / "Apps" / "improv-video.app", tmp_path / "new" / "improv-video.app", tmp_path / "prev.app"
    bundle.mkdir(parents=True)
    (bundle / "v").write_text("old")
    new.mkdir(parents=True)
    (new / "v").write_text("new")
    script = updater.swap_script(new, bundle, backup, pid=999999).replace("open ", "true ")
    subprocess.run(["/bin/sh", "-c", script], check=True)
    assert (bundle / "v").read_text() == "new" and (backup / "v").read_text() == "old"


def test_swap_script_restores_old_if_new_missing(tmp_path):
    bundle = tmp_path / "improv-video.app"
    bundle.mkdir()
    (bundle / "v").write_text("old")
    script = updater.swap_script(tmp_path / "нет.app", bundle, tmp_path / "prev.app", pid=999999)
    subprocess.run(["/bin/sh", "-c", script.replace("open ", "true ")], check=True)
    assert (bundle / "v").read_text() == "old"
