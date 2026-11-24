"""Draws the Shard Sniper icon (1024px PNG) in the app's purple style. Used by the Mac build."""
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# rounded dark tile with violet glow
tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(tile)
d.rounded_rectangle((60, 60, S - 60, S - 60), radius=200, fill=(12, 5, 22, 255))
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse((150, -250, S - 150, 520), fill=(122, 0, 255, 120))
glow = glow.filter(ImageFilter.GaussianBlur(120))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((60, 60, S - 60, S - 60), radius=200, fill=255)
tile.paste(Image.alpha_composite(tile, glow), (0, 0), mask)
img = Image.alpha_composite(img, tile)

d = ImageDraw.Draw(img)
P, L = (154, 61, 255, 255), (201, 139, 255, 255)

# chamfered frame
c = 70
x0, y0, x1, y1 = 250, 250, S - 250, S - 250
d.line([(x0 + c, y0), (x1, y0), (x1, y1 - c), (x1 - c, y1), (x0, y1), (x0, y0 + c), (x0 + c, y0)], fill=P, width=18)

# crosshair ticks
m = S // 2
for a, b in (((m, 170), (m, 290)), ((m, S - 290), (m, S - 170)), ((170, m), (290, m)), ((S - 290, m), (S - 170, m))):
    d.line([a, b], fill=L, width=22)

# shard
shard = [(m, 330), (m + 175, m - 30), (m + 175, m + 30), (m, S - 330), (m - 175, m + 30), (m - 175, m - 30)]
d.polygon(shard, fill=(122, 0, 255, 255))
d.polygon([(m, 330), (m + 175, m - 30), (m, m - 30)], fill=L)
d.polygon([(m - 175, m - 30), (m, 330), (m, m - 30)], fill=(166, 77, 255, 255))

# slanted S
w = 34
pts = [(m + 70, m - 95), (m - 35, m - 95), (m - 75, m - 55), (m - 75, m - 15), (m + 45, m + 15),
       (m + 45, m + 55), (m + 15, m + 85), (m - 75, m + 85)]
d.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")

img.save(sys.argv[1] if len(sys.argv) > 1 else "icon.png")
