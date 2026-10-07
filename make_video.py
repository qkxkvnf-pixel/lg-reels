import asyncio, json, os, subprocess, sys, time
import edge_tts, requests
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
VOICE = "ko-KR-SunHiNeural"
CHANNEL = "가전 NEWS"
FONT_PATHS = [
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
OUT = "out"


def font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def wrap(draw, text, fnt, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=fnt) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch.lstrip()
    if cur:
        lines.append(cur)
    return lines


def render(headline, narration, idx, total, path):
    img = Image.new("RGB", (W, H), (20, 22, 30))
    d = ImageDraw.Draw(img)
    # 배경 그라데이션
    for y in range(H):
        c = int(20 + 25 * y / H)
        d.line([(0, y), (W, y)], fill=(c, c + 2, c + 14))
    # 상단 바
    d.rectangle([0, 0, W, 170], fill=(165, 0, 52))
    d.text((60, 55), CHANNEL, font=font(64), fill="white")
    d.text((W - 60, 70), "BREAKING", font=font(44), fill=(255, 220, 230), anchor="ra")
    # 헤드라인 박스
    d.rectangle([0, 620, W, 900], fill=(255, 255, 255))
    d.rectangle([0, 620, 24, 900], fill=(165, 0, 52))
    f = font(84)
    y = 650
    for line in wrap(d, headline, f, W - 140):
        d.text((70, y), line, font=f, fill=(20, 20, 20))
        y += 105
    # 자막
    f2 = font(58)
    lines = wrap(d, narration, f2, W - 160)
    box_h = len(lines) * 80 + 60
    top = 1380
    d.rectangle([40, top, W - 40, top + box_h], fill=(0, 0, 0))
    y = top + 30
    for line in lines:
        d.text((80, y), line, font=f2, fill="white")
        y += 80
    # 하단 진행바
    d.rectangle([0, H - 40, W, H], fill=(60, 60, 70))
    d.rectangle([0, H - 40, int(W * (idx + 1) / total), H], fill=(165, 0, 52))
    img.save(path)


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


async def tts(text, path):
    await edge_tts.Communicate(text, VOICE, rate="+8%").save(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    data = json.load(open("scenes.json", encoding="utf-8"))
    scenes = data["scenes"]
    clips = []
    for i, s in enumerate(scenes):
        png, mp3, mp4 = f"{OUT}/s{i}.png", f"{OUT}/s{i}.mp3", f"{OUT}/s{i}.mp4"
        render(s["headline"], s["narration"], i, len(scenes), png)
        asyncio.run(tts(s["narration"], mp3))
        dur = duration(mp3) + 0.4
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", png, "-i", mp3,
             "-t", f"{dur:.2f}", "-r", "30", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-af", "apad", mp4],
            check=True, capture_output=True)
        clips.append(mp4)
    with open(f"{OUT}/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '{os.path.basename(c)}'\n")
    final = f"{OUT}/final.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"{OUT}/list.txt",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-ar", "44100", "-ac", "2", "-movflags", "+faststart", final],
        check=True, capture_output=True)
    print("영상 완성:", final, f"({duration(final):.1f}초)")

    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("텔레그램 키가 없어 전송은 건너뜀")
        return

    caption = data.get("caption", "")
    tag = "p" + time.strftime("%Y%m%d-%H%M%S")
    # 승인 전까지 영상을 GitHub 릴리스에 임시 보관
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
