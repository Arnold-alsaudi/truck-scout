"""Local image checks that don't need the AI: finds the yellow/golden (rank S) trucks on the trade road."""
import io


def yellow_trucks(jpeg, max_items=12):
    """Centres of yellow/orange truck bodies as (x, y) in 0-1000 of the image, top to bottom.

    Looks only in the road area (it skips the top bar and the bottom card / tabs) and keeps blobs that are
    truck-sized, so yellow buttons, the big S letter and icons are ignored."""
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = 120, int(120 * img.height / img.width)
    small = img.resize((W, H))
    hsv = small.convert("HSV")
    px, hx = small.load(), hsv.load()
    y0, y1 = int(H * 0.12), int(H * 0.72)  # stop above the truck card's Plunder!/Share buttons
    mask = [[False] * W for _ in range(H)]
    for y in range(y0, y1):
        for x in range(W):
            h, s, v = hx[x, y]
            r, g, b = px[x, y]
            # truck yellow/orange: hue ~20-45 deg (PIL 0-255 scale ~14-32), saturated and bright, red > blue clearly
            if 12 <= h <= 34 and s >= 120 and v >= 150 and r > b + 70 and g > b + 30:
                mask[y][x] = True

    seen = [[False] * W for _ in range(H)]
    blobs = []
    for y in range(y0, y1):
        for x in range(W):
            if not mask[y][x] or seen[y][x]:
                continue
            stack, pts = [(x, y)], []
            seen[y][x] = True
            while stack:
                cx, cy = stack.pop()
                pts.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < W and y0 <= ny < y1 and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            # truck body: wider than tall, a reasonable share of the screen width
            if len(pts) >= 10 and W * 0.06 <= w <= W * 0.20 and h <= H * 0.22 and w >= h * 1.4:
                blobs.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    blobs.sort(key=lambda b: (b[1], b[0]))
    return [(int(bx * 1000 / W), int(by * 1000 / H)) for bx, by in blobs[:max_items]]


def main_and_card(jpeg_big, main_width=900):
    """From one high-res screenshot: a normal-size image for the AI plus an enlarged crop of the lower part
    (where the truck card and its reward icons are), so small S-fragment icons are easy to read."""
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg_big)).convert("RGB")
    main = img.resize((main_width, round(img.height * main_width / img.width)), Image.LANCZOS) if img.width > main_width else img
    card = img.crop((0, int(img.height * 0.55), img.width, int(img.height * 0.97)))
    if card.width > 800:  # still large enough to read the reward icons, but quicker for the AI
        card = card.resize((800, round(card.height * 800 / card.width)), Image.LANCZOS)
    out = []
    for im in (main, card):
        b = io.BytesIO()
        im.save(b, "JPEG", quality=85)
        out.append(b.getvalue())
    return out[0], out[1]