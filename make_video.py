import asyncio, json, os, random, subprocess, sys, time
import edge_tts, requests
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
BW, BH = 1620, 2880          # 배경 사진은 1.5배 크기로 준비(확대 효과용)
VOICE = "ko-KR-SunHiNeural"
CHANNEL = "가전 NEWS"
FONT_PATHS = [
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
OUT = "out"
RED = (165, 0, 52)


def font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def wrap(draw, text, fnt, max_w):
    """띄어쓰기 단위로 줄바꿈합니다(한 단어가 너무 길 때만 글자 단위)."""
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if draw.textlength(word, font=fnt) <= max_w:
            cur = word
        else:
            for ch in word:
                if draw.textlength(cur + ch, font=fnt) <= max_w:
                    cur += ch
                else:
                    lines.append(cur)
                    cur = ch
    if cur:
        lines.append(cur)
    return lines


def cover_resize(path):
    """사진을 BW x BH 크기로 꽉 채워 자릅니다."""
    im = Image.open(path).convert("RGB")
    s = max(BW / im.width, BH / im.height)
    im = im.resize((int(im.width * s) + 1, int(im.height * s) + 1), Image.LANCZOS)
    x, y = (im.width - BW) // 2, (im.height - BH) // 2
    im.crop((x, y, x + BW, y + BH)).save(path, quality=92)


def gradient_bg(path):
    """사진이 없을 때 쓰는 어두운 그라데이션 배경."""
    im = Image.new("RGB", (BW, BH))
    d = ImageDraw.Draw(im)
    for y in range(BH):
        t = y / BH
        d.line([(0, y), (BW, y)], fill=(int(24 + 40 * t), int(26 + 12 * t), int(40 + 30 * t)))
    im.save(path, quality=92)


_used_photo_ids = set()


def _download(url, path):
    img = requests.get(url, timeout=60)
    img.raise_for_status()
    open(path, "wb").write(img.content)
    cover_resize(path)


def fetch_pixabay(query, path):
    """Pixabay(무료)에서 세로 사진을 가져옵니다."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return False
    base = {"key": key, "q": query, "image_type": "photo", "orientation": "vertical",
            "safesearch": "true", "per_page": 20}
    hits = []
    for extra in ({"min_width": 720, "min_height": 1100}, {}):   # 큰 사진 우선
        r = requests.get("https://pixabay.com/api/", params={**base, **extra}, timeout=30)
        r.raise_for_status()
        hits = [h for h in r.json().get("hits", [])
                if ("px", h["id"]) not in _used_photo_ids]
        if hits:
            break
    if not hits:
        return False
    h = random.choice(hits[:8])
    _used_photo_ids.add(("px", h["id"]))
    _download(h.get("largeImageURL") or h["webformatURL"], path)
    return True


def fetch_pexels(query, path):
    """Pexels 키가 있을 때만 쓰는 예비 방법입니다."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return False
    r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": key},
                     params={"query": query, "orientation": "portrait",
                             "size": "large", "per_page": 12}, timeout=30)
    r.raise_for_status()
    photos = [p for p in r.json().get("photos", [])
              if ("pe", p["id"]) not in _used_photo_ids]
    if not photos:
        return False
    p = random.choice(photos[:5])
    _used_photo_ids.add(("pe", p["id"]))
    _download(p["src"]["large2x"], path)
    return True


def fetch_photo(query, path):
    """성공하면 사진 출처 이름('Pixabay' 등)을, 실패하면 None을 돌려줍니다."""
    if not query:
        return None
    for name, fn in (("Pixabay", fetch_pixabay), ("Pexels", fetch_pexels)):
        try:
            if fn(query, path):
                return name
        except Exception as e:
            print(f"{name} 사진 실패:", query, str(e)[:120])
    return None


def render_overlay(headline, narration, idx, total, path):
    """사진 위에 얹을 투명한 뉴스 화면(상단 바, 헤드라인, 자막)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 아래쪽 어두운 그라데이션(글자가 잘 보이게)
    for y in range(780, H):
        a = int(210 * (y - 780) / (H - 780))
        d.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    # 상단 바
    d.rectangle([0, 0, W, 170], fill=RED + (235,))
    d.text((60, 55), CHANNEL, font=font(64), fill="white")
    d.text((W - 60, 70), "NEWS BRIEF", font=font(44), fill=(255, 225, 232), anchor="ra")
    # 헤드라인(하단 1/3 위쪽)
    f = font(72)
    hl = wrap(d, headline, f, W - 150)
    h_h = len(hl) * 92 + 50
    top = 1040
    d.rectangle([0, top, W, top + h_h], fill=(255, 255, 255, 245))
    d.rectangle([0, top, 24, top + h_h], fill=RED + (255,))
    y = top + 25
    for line in hl:
        d.text((70, y), line, font=f, fill=(20, 20, 20))
        y += 92
    # 자막
    f2 = font(56)
    lines = wrap(d, narration, f2, W - 170)
    box_h = len(lines) * 78 + 56
    st = top + h_h + 50
    d.rounded_rectangle([40, st, W - 40, st + box_h], radius=24, fill=(0, 0, 0, 175))
    y = st + 28
    for line in lines:
        d.text((80, y), line, font=f2, fill="white")
        y += 78
    # 진행바
    d.rectangle([0, H - 36, W, H], fill=(255, 255, 255, 70))
    d.rectangle([0, H - 36, int(W * (idx + 1) / total), H], fill=RED + (255,))
    img.save(path)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise RuntimeError("ffmpeg 실패")
    return r


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def make_clip(bg, overlay, mp3, mp4, dur, zoom_in):
    """사진을 천천히 확대/축소하면서 뉴스 화면을 얹고 음성을 붙입니다."""
    frames = int(dur * 30) + 3
    z = ("min(1.0+0.0006*on,1.15)" if zoom_in else "max(1.0,1.15-0.0006*on)")
    fc = (f"[0:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={W}x{H}:fps=30[bg];"
          f"[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v]")
    run(["ffmpeg", "-y", "-i", bg, "-loop", "1", "-framerate", "30", "-i", overlay,
         "-i", mp3, "-filter_complex", fc, "-map", "[v]", "-map", "2:a",
         "-t", f"{dur:.2f}", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-af", "apad", mp4])


async def tts(text, path):
    await edge_tts.Communicate(text, VOICE, rate="+8%").save(path)


def main():
    if not os.path.exists("scenes.json"):
        print("오늘은 만들 대본이 없어서 건너뜁니다.")
        return
    os.makedirs(OUT, exist_ok=True)
    data = json.load(open("scenes.json", encoding="utf-8"))
    scenes = data["scenes"]
    clips, credits = [], set()
    for i, s in enumerate(scenes):
        bg = f"{OUT}/bg{i}.jpg"
        ov, mp3, mp4 = f"{OUT}/ov{i}.png", f"{OUT}/s{i}.mp3", f"{OUT}/s{i}.mp4"
        src = fetch_photo(s.get("image_query"), bg)
        if src:
            credits.add(src)
        else:
            gradient_bg(bg)
        render_overlay(s["headline"], s["narration"], i, len(scenes), ov)
        asyncio.run(tts(s["narration"], mp3))
        make_clip(bg, ov, mp3, mp4, duration(mp3) + 0.4, zoom_in=(i % 2 == 0))
        clips.append(mp4)
    with open(f"{OUT}/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '{os.path.basename(c)}'\n")
    final = f"{OUT}/final.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"{OUT}/list.txt",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-ar", "44100", "-ac", "2", "-movflags", "+faststart", final])
    print("영상 완성:", final, f"({duration(final):.1f}초)")

    caption = data.get("caption", "")
    if credits:
        caption += "\n사진: " + ", ".join(sorted(credits))

    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("텔레그램 키가 없어 전송은 건너뜀")
        return

    tag = "p" + time.strftime("%Y%m%d-%H%M%S")
    subprocess.run(
        ["gh", "release", "create", tag, final, "--title", tag,
         "--notes", caption, "--prerelease"], check=True)
    print("임시 보관:", tag)

    buttons = {"inline_keyboard": [[
        {"text": "✅ 승인 (업로드)", "callback_data": f"ok:{tag}"},
        {"text": "❌ 거절", "callback_data": f"no:{tag}"},
    ]]}
    tg_caption = ("📝 업로드 대기 중 (승인 후 5~15분 안에 올라가요)\n\n" + caption)[:1000]
    with open(final, "rb") as v:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendVideo",
            data={"chat_id": chat, "caption": tg_caption,
                  "reply_markup": json.dumps(buttons)},
            files={"video": v}, timeout=120)
    print("텔레그램 전송:", r.status_code)
    if r.status_code != 200:
        sys.exit(r.text)


if __name__ == "__main__":
    main()
