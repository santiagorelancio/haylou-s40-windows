"""Genera icon.ico. Usa icon_source.jpg si existe; si no, dibuja un icono genérico de auriculares."""
import os
from PIL import Image, ImageDraw

SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]

if os.path.exists("icon_source.jpg"):
    src = Image.open("icon_source.jpg").convert("RGBA").resize((1024, 1024), Image.LANCZOS)
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 1023, 1023), 230, fill=255)
    src.putalpha(mask)
    img = src.resize((256, 256), Image.LANCZOS)
else:
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, S - 8, S - 8), 56, fill="#0A84FF")
    d.arc((52, 50, S - 52, S - 10), 180, 360, fill="white", width=18)      # diadema
    d.rounded_rectangle((42, 120, 88, 190), 14, fill="white")               # almohadillas
    d.rounded_rectangle((S - 88, 120, S - 42, 190), 14, fill="white")

img.save("icon.ico", sizes=SIZES)
