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


def share_button(jpeg):
    """Centre (x, y) in 0-1000 of the blue "Share" button on an open truck card, or None.

    Found by colour, not guessed: a wide, bright blue button in the lower part of the screen. "Plunder!" next to
    it is orange, so this can never return the Plunder! button."""
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = 160, int(160 * img.height / img.width)
    px = img.resize((W, H)).load()
    y0, y1 = int(H * 0.55), int(H * 0.97)
    mask = [[False] * W for _ in range(H)]
    for y in range(y0, y1):
        for x in range(W):
            r, g, b = px[x, y]
            # button blue: strong blue, clearly more green than red (purple reward tiles have g < r)
            if b >= 170 and b > r + 90 and g > r + 35 and g >= 110:
                mask[y][x] = True
    seen = [[False] * W for _ in range(H)]
    best = None
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
            # a button: wide and short, solidly filled
            if w >= W * 0.12 and h >= 3 and w >= h * 2 and len(pts) >= w * h * 0.5:
                cy = sum(ys) / len(ys)
                # only the truck card has the orange "Plunder!" button right beside Share, on the same row:
                # this rules out blue gates in mini-games, "Skip" buttons and the blue header of B-rank cards
                left_end, left_start = min(xs) - 1, max(0, int(min(xs) - w * 1.4))
                row = [px[lx, int(cy)] for lx in range(left_start, left_end)]
                orange = sum(1 for r, g, b in row if r >= 200 and 120 <= g <= 225 and b <= 110)
                if row and orange >= len(row) * 0.35:
                    if best is None or len(pts) > best[0]:
                        best = (len(pts), sum(xs) / len(xs), cy)
    if not best:
        return None
    return int(best[1] * 1000 / W), int(best[2] * 1000 / H)


def is_blue_button(jpeg, x, y, radius=30):
    """True if the spot (0-1000 coords) sits on a blue button. Inside the Share popup the blue buttons are the
    "send to World / Alliance / chat" buttons, so a tap there must never happen."""
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = img.size
    cx, cy = x * W / 1000.0, y * H / 1000.0
    r = radius * W / 1000.0
    box = img.crop((int(max(0, cx - r)), int(max(0, cy - r)), int(min(W, cx + r)), int(min(H, cy + r))))
    px = list(box.resize((12, 12)).getdata())
    blue = sum(1 for R, G, B in px if B >= 150 and B > R + 60)
    return blue >= len(px) * 0.12


def is_popup_row(jpeg, x, y, radius=30):
    """True if the spot is on the light beige panel of the Share popup (its channel rows) rather than on the
    dark header where the X is."""
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = img.size
    cx, cy = x * W / 1000.0, y * H / 1000.0
    r = radius * W / 1000.0
    box = img.crop((int(max(0, cx - r)), int(max(0, cy - r)), int(min(W, cx + r)), int(min(H, cy + r))))
    px = list(box.resize((12, 12)).getdata())
    light = sum(1 for R, G, B in px if R >= 185 and G >= 180 and B >= 160)
    return light >= len(px) * 0.55


def main_and_card(jpeg_big, main_width=900):
    """From one high-res screenshot: a normal-size image for the AI plus an enlarged crop of the lower part
    (where the truck card and its reward icons are), so small S-fragment icons are easy to read."""
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg_big)).convert("RGB")
    main = img.resize((main_width, round(img.height * main_width / img.width)), Image.LANCZOS) if img.width > main_width else img
    card = img.crop((0, int(img.height * 0.55), img.width, int(img.height * 0.97)))
    if card.width > 1000:  # still large enough to read the reward icons, but quicker for the AI
        card = card.resize((1000, round(card.height * 1000 / card.width)), Image.LANCZOS)
    out = []
    for im in (main, card):
        b = io.BytesIO()
        im.save(b, "JPEG", quality=85)
        out.append(b.getvalue())
    return out[0], out[1]