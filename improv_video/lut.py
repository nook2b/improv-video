"""Чтение и запись .cube и «запекание» поправки экспозиции I-Log во входную сторону LUT."""

from __future__ import annotations

from pathlib import Path


def read_cube(path: Path) -> tuple[int, list[tuple[float, float, float]]]:
    size, rows = 0, []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("LUT_3D_SIZE"):
            size = int(line.split()[1])
        elif line[0].isdigit() or line[0] in "-.":
            r, g, b = (float(v) for v in line.split()[:3])
            rows.append((r, g, b))
    if not size or len(rows) != size ** 3:
        raise ValueError(f"Не похоже на 3D LUT .cube: {path}")
    return size, rows


def write_cube(path: Path, size: int, rows: list[tuple[float, float, float]]) -> Path:
    lines = [f"LUT_3D_SIZE {size}"] + [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in rows]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def _sample(size, rows, r, g, b):
    """Трилинейная выборка LUT в точке (r, g, b) ∈ [0, 1]³; в .cube быстрее всего меняется r."""
    n = size - 1

    def split(v):
        v = min(max(v, 0.0), 1.0) * n
        i = min(int(v), n - 1)
        return i, v - i

    (ri, rf), (gi, gf), (bi, bf) = split(r), split(g), split(b)
    out = [0.0, 0.0, 0.0]
    for db, wb in ((0, 1 - bf), (1, bf)):
        for dg, wg in ((0, 1 - gf), (1, gf)):
            for dr, wr in ((0, 1 - rf), (1, rf)):
                w = wr * wg * wb
                if w:
                    v = rows[(ri + dr) + (gi + dg) * size + (bi + db) * size * size]
                    out[0] += w * v[0]
                    out[1] += w * v[1]
                    out[2] += w * v[2]
    return tuple(out)


def bake_offset(src: Path, dst: Path, offset: float) -> Path:
    """Новый LUT, равный исходному после сдвига входного сигнала на offset (доля диапазона 0–1).

    Для лог-кривой сдвиг сигнала = поправка экспозиции, поэтому отдельный проход
    поправки по каждому 4K-кадру больше не нужен.
    """
    size, rows = read_cube(src)
    n = size - 1
    baked = []
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                baked.append(_sample(size, rows, ri / n + offset, gi / n + offset, bi / n + offset))
    return write_cube(dst, size, baked)
