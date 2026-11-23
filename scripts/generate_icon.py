"""Genera el icono personalizado de la aplicacion en formato PNG e ICO multi-resolucion."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "assets"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw_app_icon(size: int = 1024) -> Image.Image:
    """Dibuja un icono vectorial supersampleado estilo gaming/WhatsApp con tijeras y play."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Fondo redondeado oscuro con borde sutil esmeralda
    pad = int(size * 0.05)
    radius = int(size * 0.22)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=radius,
        fill=(13, 22, 30, 255),
        outline=(37, 211, 102, 220),
        width=int(size * 0.028),
    )

    # Halo interno suave verde WhatsApp
    inner_pad = int(size * 0.11)
    draw.rounded_rectangle(
        [inner_pad, inner_pad, size - inner_pad, size - inner_pad],
        radius=int(size * 0.17),
        fill=(19, 32, 43, 255),
        outline=(25, 158, 75, 95),
        width=int(size * 0.012),
    )

    # Cinta / marco de video central (verde WhatsApp)
    frame_box = [int(size * 0.20), int(size * 0.26), int(size * 0.80), int(size * 0.68)]
    draw.rounded_rectangle(
        frame_box,
        radius=int(size * 0.07),
        fill=(37, 211, 102, 255),
    )

    # Perforaciones laterales estilo fotograma
    sprocket_w = int(size * 0.045)
    sprocket_h = int(size * 0.055)
    for y_frac in (0.31, 0.44, 0.57):
        y0 = int(size * y_frac)
        draw.rounded_rectangle(
            [int(size * 0.235), y0, int(size * 0.235) + sprocket_w, y0 + sprocket_h],
            radius=int(size * 0.015),
            fill=(13, 22, 30, 235),
        )
        draw.rounded_rectangle(
            [int(size * 0.765) - sprocket_w, y0, int(size * 0.765), y0 + sprocket_h],
            radius=int(size * 0.015),
            fill=(13, 22, 30, 235),
        )

    # Triangulo Play blanco en el centro del fotograma
    play_pts = [
        (int(size * 0.43), int(size * 0.36)),
        (int(size * 0.43), int(size * 0.58)),
        (int(size * 0.63), int(size * 0.47)),
    ]
    draw.polygon(play_pts, fill=(248, 252, 255, 255))

    # Linea de corte discontinua inferior y tijeras estilizadas (color cyan claro)
    cut_y = int(size * 0.75)
    for x_start in range(int(size * 0.19), int(size * 0.68), int(size * 0.08)):
        draw.rounded_rectangle(
            [x_start, cut_y - int(size * 0.01), x_start + int(size * 0.045), cut_y + int(size * 0.01)],
            radius=int(size * 0.008),
            fill=(56, 189, 248, 240),
        )

    # Anillos de las tijeras (derecha abajo)
    ring_r = int(size * 0.055)
    ring_w = int(size * 0.022)
    c1 = (int(size * 0.74), int(size * 0.72))
    c2 = (int(size * 0.74), int(size * 0.85))
    draw.ellipse([c1[0] - ring_r, c1[1] - ring_r, c1[0] + ring_r, c1[1] + ring_r], outline=(248, 250, 252, 255), width=ring_w)
    draw.ellipse([c2[0] - ring_r, c2[1] - ring_r, c2[0] + ring_r, c2[1] + ring_r], outline=(248, 250, 252, 255), width=ring_w)

    # Hojas cruzadas de las tijeras
    draw.line([(int(size * 0.70), int(size * 0.74)), (int(size * 0.56), int(size * 0.84))], fill=(248, 250, 252, 255), width=int(size * 0.028))
    draw.line([(int(size * 0.70), int(size * 0.83)), (int(size * 0.56), int(size * 0.73))], fill=(56, 189, 248, 255), width=int(size * 0.028))

    return img


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    master = draw_app_icon(1024)

    png_path = ASSETS_DIR / "app_icon.png"
    master.resize((256, 256), Image.Resampling.LANCZOS).save(png_path, format="PNG")

    ico_path = ASSETS_DIR / "app_icon.ico"
    master.save(ico_path, format="ICO", sizes=ICO_SIZES)

    print(f"Iconos generados: {png_path} y {ico_path}")


if __name__ == "__main__":
    main()
