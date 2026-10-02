"""Замер скорости цветовой обработки 4K на этом компьютере.

improv-video.app/Contents/MacOS/improv-video --bench [lut.cube]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .tools import ffmpeg, find_tool, resources_dir
from .video import decode_args

FRAMES = 120

CHAINS = {
    "только декод": "null",
    "сейчас (3 пересчёта)": (
        "scale=in_range=pc:out_range=tv:in_color_matrix=bt709:out_color_matrix=bt709,format=yuv420p10le,"
        "lutyuv=y='clip(val+48,minval,maxval)',scale=in_color_matrix=bt709:in_range=tv,format=gbrp10le,"
        "lut3d=file=lut.cube:interp=tetrahedral,"
        "scale=3840:2160:force_original_aspect_ratio=decrease:flags=lanczos:in_color_matrix=bt709:"
        "out_color_matrix=bt709:out_range=tv,pad=3840:2160:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=p010le"),
    "2 пересчёта, gbrp10": (
        "scale=in_range=pc:in_color_matrix=bt709,format=gbrp10le,lut3d=file=lut.cube:interp=tetrahedral,"
        "scale=out_color_matrix=bt709:out_range=tv,format=p010le"),
    "2 пересчёта, float": (
        "scale=in_range=pc:in_color_matrix=bt709,format=gbrpf32le,lut3d=file=lut.cube:interp=tetrahedral,"
        "scale=out_color_matrix=bt709:out_range=tv,format=p010le"),
    "zscale + float": (
        "zscale=rangein=full:matrixin=709:range=full,format=gbrpf32le,lut3d=file=lut.cube:interp=tetrahedral,"
        "zscale=matrix=709:range=limited,format=p010le"),
}


def _run(clip: Path, vf: str, cwd: Path) -> float:
    t0 = time.monotonic()
    ffmpeg([*decode_args(), "-i", str(clip), "-frames:v", str(FRAMES), "-vf", vf, "-an", "-f", "null", "-"], cwd=cwd)
    return FRAMES / (time.monotonic() - t0)


def main(argv: list[str]) -> int:
    lut = Path(argv[0]) if argv else None
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if lut and lut.exists():
            (work / "lut.cube").write_bytes(lut.read_bytes())
        else:  # тождественный LUT 33³ — по скорости как настоящий
            n = 33
            rows = [f"{r / (n - 1):.6f} {g / (n - 1):.6f} {b / (n - 1):.6f}"
                    for b in range(n) for g in range(n) for r in range(n)]
            (work / "lut.cube").write_text(f"LUT_3D_SIZE {n}\n" + "\n".join(rows) + "\n")
        clip = work / "t4k.mp4"
        ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=3840x2160:r=30000/1001:d=5", "-vf", "scale=out_range=pc",
                "-pix_fmt", "yuvj420p", "-c:v", "libx265", "-preset", "ultrafast",
                "-x265-params", "log-level=error", str(clip)])
        print("ffmpeg:", find_tool("ffmpeg"))
        best = None
        for name, vf in CHAINS.items():
            try:
                fps = _run(clip, vf, work)
            except Exception as e:  # noqa: BLE001
                print(f"{name}: ошибка {str(e).splitlines()[-1][:120]}")
                continue
            print(f"{name}: {fps:.1f} к/с", flush=True)
            if name != "только декод" and (best is None or fps > best[1]):
                best = (name, fps)
        # Несколько записей одновременно: суммарная скорость лучшей цепочки
        if best:
            vf = CHAINS[best[0]]
            for n in (2, 3, 4):
                t0 = time.monotonic()
                procs = [subprocess.Popen([find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", *decode_args(),
                                           "-i", str(clip), "-frames:v", str(FRAMES), "-vf", vf, "-an", "-f", "null", "-"],
                                          cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                         for _ in range(n)]
                for p in procs:
                    p.wait()
                print(f"{best[0]} × {n} параллельно: {n * FRAMES / (time.monotonic() - t0):.1f} к/с суммарно", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
