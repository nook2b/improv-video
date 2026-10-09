# PyInstaller: improv-video.app для macOS (Apple Silicon), приложение только в менюбаре.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = [("../resources", "resources")]
datas += collect_data_files("googleapiclient")
datas += copy_metadata("keyring") + copy_metadata("google-api-python-client")
hiddenimports = collect_submodules("keyring.backends") + ["rumps"]

a = Analysis(["main.py"], pathex=[".."], datas=datas, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="improv-video", console=False,
          target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="improv-video")
app = BUNDLE(
    coll,
    name="improv-video.app",
    bundle_identifier="com.nook2b.improv-video",
    icon="icon/AppIcon.icns",  # из icon/source.png: python packaging/icon/make_icon.py
    info_plist={
        "LSUIElement": True,  # только иконка в менюбаре, без Dock
        "CFBundleShortVersionString": "0.1.22",
        "LSMinimumSystemVersion": "13.0",
        "NSRemovableVolumesUsageDescription": "improv-video копирует клипы с карты камеры.",
    },
)
