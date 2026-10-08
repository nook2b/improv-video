"""Значок приложения: source.png (красный квадрат на белом) → AppIcon.icns по сетке значков macOS.

Белый фон вокруг убирается (по краю — плавно, без белой каймы), квадрат вписывается в 824 из 1024 px.
python packaging/icon/make_icon.py
"""

from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = Path(__file__).resolve().parent


def main() -> None:
    rgb = np.asarray(Image.open(HERE / "source.png").convert("RGB")).astype(np.float32)
    whiteish = (rgb.min(axis=2) > 235)
    labels, _ = ndimage.label(whiteish)
    outside = np.isin(labels, np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    outside &= whiteish
    # Кайма: несколько пикселей от фона — прозрачность по тому, насколько пиксель «белее» красного
    edge = ndimage.binary_dilation(outside, iterations=4) & ~outside
    alpha = np.where(outside, 0.0, 1.0)
    g = rgb[..., 1]
    edge_alpha = np.clip((255 - g) / (255 - 25), 0, 1)
    alpha[edge] = edge_alpha[edge]
    a = np.maximum(alpha, 1e-3)[..., None]
    color = np.clip((rgb - (1 - alpha[..., None]) * 255) / a, 0, 255)  # убрать примесь белого
    rgba = np.dstack([color, alpha * 255]).astype(np.uint8)
    img = Image.fromarray(rgba, "RGBA")
    img = img.crop(img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox())
    side = 824
    img = img.resize((side, round(img.height * side / img.width)), Image.LANCZOS)
    canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((1024 - img.width) // 2, (1024 - img.height) // 2))
    canvas.save(HERE / "AppIcon.png")
    canvas.save(HERE / "AppIcon.icns", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512),
                                              (1024, 1024)])
    print(img.size, "→", HERE / "AppIcon.icns")


if __name__ == "__main__":
    main()
