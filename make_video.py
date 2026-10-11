import asyncio, base64, json, os, random, re, subprocess, sys, time
import edge_tts, requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
WIN_Y, WIN_W, WIN_H = 440, 1080, 920      # 사진이 보이는 '창' (위아래는 흰 바탕에 글자)
BW, BH = 1620, 1380                        # 사진은 1.5배 크기로 준비(확대 효과용)
VOICE = "ko-KR-SunHiNeural"
CHANNEL = (os.environ.get("CHANNEL_NAME") or "").strip() or "가전 NEWS"
# 제목은 굵고 꽉 찬 글씨, 본문은 둥글고 부드러운 글씨 (없으면 아래 예비 폰트 사용)
TITLE_FONTS = [
    "fonts/BlackHanSans-Regular.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicExtraBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
BODY_FONTS = [
    "fonts/Jua-Regular.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
OUT = "out"
RED = (165, 0, 52)
YELLOW = (255, 226, 0)
INK = (20, 20, 20)


def font(size, title=False):
    for p in (TITLE_FONTS if title else BODY_FONTS):
        try:
            if os.path.exists(p) and os.path.getsize(p) > 1000:
                return ImageFont.truetype(p, size)
        except Exception:
            continue
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
    """사진을 BW x BH 크기로 꽉 채워 자릅니다. (위쪽을 조금 더 살려서 자름)"""
    im = Image.open(path).convert("RGB")
    s = max(BW / im.width, BH / im.height)
    im = im.resize((int(im.width * s) + 1, int(im.height * s) + 1), Image.LANCZOS)
    x, y = (im.width - BW) // 2, int((im.height - BH) * 0.4)
    im.crop((x, y, x + BW, y + BH)).save(path, quality=92)


def gradient_bg(path):
    """사진이 없을 때 쓰는 부드러운 파스텔 배경."""
    im = Image.new("RGB", (BW, BH))
    d = ImageDraw.Draw(im)
    for y in range(BH):
        t = y / BH
        d.line([(0, y), (BW, y)], fill=(int(235 - 40 * t), int(240 - 30 * t), int(250 - 25 * t)))
    im.save(path, quality=92)


_used_photo_ids = set()


def fit_user_photo(src, dst):
    """사진을 영상의 사진 창에 맞춥니다. 세로 사진은 양옆을 흐리게 채워서 잘리지 않게 해요."""
    im = Image.open(src).convert("RGB")
    if im.width / im.height >= 0.9:          # 가로/정사각 사진: 꽉 채우기
        im.save(dst, quality=92)
        cover_resize(dst)
        return
    cover = im.copy()
    s = max(BW / cover.width, BH / cover.height)
    cover = cover.resize((int(cover.width * s) + 1, int(cover.height * s) + 1), Image.LANCZOS)
    x, y = (cover.width - BW) // 2, (cover.height - BH) // 2
    bg = cover.crop((x, y, x + BW, y + BH)).filter(ImageFilter.GaussianBlur(45))
    bg = Image.blend(bg, Image.new("RGB", (BW, BH), (255, 255, 255)), 0.25)
    fh = BH
    fw = int(im.width * fh / im.height)
    fg = im.resize((fw, fh), Image.LANCZOS)
    bg.paste(fg, ((BW - fw) // 2, 0))
    bg.save(dst, quality=92)


def download_user_photos(ids):
    """텔레그램으로 보낸 사진을 받아옵니다."""
    token = os.environ.get("TELEGRAM_TOKEN")
    paths = []
    if not token:
        return paths
    for i, fid in enumerate(ids[:5]):
        try:
            j = requests.get(f"https://api.telegram.org/bot{token}/getFile",
                             params={"file_id": fid}, timeout=30).json()
            fp = j["result"]["file_path"]
            data = requests.get(f"https://api.telegram.org/file/bot{token}/{fp}", timeout=60)
            data.raise_for_status()
            raw = f"{OUT}/user_raw{i}"
            open(raw, "wb").write(data.content)
            Image.open(raw).verify()              # 사진이 맞는지 확인
            paths.append(raw)
        except Exception as e:
            print("내 사진 받기 실패:", str(e)[:100])
    print(f"내 사진 {len(paths)}장 사용")
    return paths


def _download(url, path):
    img = requests.get(url, timeout=60)
    img.raise_for_status()
    open(path, "wb").write(img.content)
    cover_resize(path)


def fetch_pixabay(query, path):
    """Pixabay(무료)에서 검색어와 가장 잘 맞는 세로 사진을 가져옵니다."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return False
    base = {"key": key, "q": query, "image_type": "photo", "orientation": "horizontal",
            "safesearch": "true", "per_page": 20}
    hits = []
    for extra in ({"min_width": 1000, "min_height": 700}, {}):   # 큰 사진 우선
        r = requests.get("https://pixabay.com/api/", params={**base, **extra}, timeout=30)
        r.raise_for_status()
        hits = [h for h in r.json().get("hits", [])
                if ("px", h["id"]) not in _used_photo_ids]
        if hits:
            break
    if not hits:
        return False
    words = [w for w in query.lower().split() if len(w) > 2]

    def score(h):   # 사진 태그에 검색어 단어가 많이 들어 있을수록 내용과 잘 맞음
        tags = str(h.get("tags", "")).lower()
        return sum(1 for w in words if w in tags)

    top = sorted(hits[:12], key=score, reverse=True)
    best = [h for h in top if score(h) == score(top[0])][:4]
    h = random.choice(best)
    _used_photo_ids.add(("px", h["id"]))
    _download(h.get("largeImageURL") or h["webformatURL"], path)
    return True


def fetch_pexels(query, path):
    """Pexels 키가 있을 때만 쓰는 예비 방법입니다."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return False
    r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": key},
                     params={"query": query, "orientation": "landscape",
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


def fetch_photo(queries, path):
    """검색어를 순서대로 시도합니다. 성공하면 사진 출처 이름을, 실패하면 None을 돌려줍니다."""
    if isinstance(queries, str):
        queries = [queries]
    for q in [x for x in queries if x]:
        for name, fn in (("Pixabay", fetch_pixabay), ("Pexels", fetch_pexels)):
            try:
                if fn(q, path):
                    return name
            except Exception as e:
                print(f"{name} 사진 실패:", q, str(e)[:120])
    return None


# ---------------- AI 이미지 생성 (Gemini) ----------------
# 그림 스타일: make-video.yml 의 AI_STYLE_NAME 값으로 고릅니다 (flat / watercolor / cartoon / 3d)
STYLES = {
    "flat": ("Flat vector illustration, clean geometric shapes, soft pastel color palette, "
             "minimal detail, modern editorial news illustration"),
    "watercolor": ("Soft watercolor illustration, gentle brush strokes, warm pastel colors, "
                   "light paper texture, hand-painted look"),
    "cartoon": ("Cute cartoon illustration, bold clean outlines, bright cheerful colors, "
                "simple friendly shapes"),
    "3d": ("Clean modern semi-realistic 3D illustration, soft natural lighting, "
           "warm bright colors, shallow depth of field"),
}
AI_STYLE = STYLES.get(os.environ.get("AI_STYLE_NAME", "flat"), STYLES["flat"])
AI_RULES = ("Landscape-leaning 5:4 composition. Main subject centered in the frame, "
            "simple uncluttered background. No text, no letters, no logos, "
            "no brand names, no real people, no watermarks.")
_GAPI = "https://generativelanguage.googleapis.com/v1beta"
_ai_models = None
_ai_off = False


def list_image_models(key):
    """쓸 수 있는 Gemini 이미지 모델을 좋은 순서로 찾습니다."""
    forced = os.environ.get("GEMINI_IMAGE_MODEL")
    if forced:
        return [forced]
    r = requests.get(f"{_GAPI}/models", params={"key": key, "pageSize": 200}, timeout=30)
    r.raise_for_status()
    flash, other = [], []
    for m in r.json().get("models", []):
        name = m["name"].split("/")[-1]
        if ("generateContent" not in m.get("supportedGenerationMethods", [])
                or "image" not in name or "imagen" in name or "pro" in name):
            continue
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        item = (float(ver.group(1)) if ver else 0, "preview" not in name, name)
        (flash if "flash" in name else other).append(item)
    return [n for _, _, n in sorted(flash, reverse=True)] + \
           [n for _, _, n in sorted(other, reverse=True)]


def _extract_image(resp_json):
    for cand in resp_json.get("candidates", []):
        for p in cand.get("content", {}).get("parts", []):
            d = p.get("inlineData") or p.get("inline_data")
            if d and d.get("data"):
                return base64.b64decode(d["data"])
    return None


def _gemini_image(image_prompt, path):
    """(선택) Gemini 로 그림을 만듭니다. 무료 한도가 없어서 기본은 꺼져 있어요."""
    global _ai_models, _ai_off
    key = os.environ.get("GEMINI_API_KEY")
    if _ai_off or not key or not image_prompt:
        return False
    try:
        if _ai_models is None:
            _ai_models = list_image_models(key)
            print("이미지 모델 순서:", _ai_models)
        prompt = f"{AI_STYLE}. {image_prompt}. {AI_RULES}"
        hard = False
        for model in _ai_models[:3]:
            for use_ratio in (True, False):
                cfg = {"responseModalities": ["TEXT", "IMAGE"]}
                if use_ratio:
                    cfg["imageConfig"] = {"aspectRatio": "5:4"}
                r = requests.post(
                    f"{_GAPI}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": cfg}, timeout=120)
                if r.status_code == 200:
                    raw = _extract_image(r.json())
                    if raw:
                        open(path, "wb").write(raw)
                        fit_user_photo(path, path)
                        return True
                    print("AI가 그림을 돌려주지 않았어요(안전 규칙 등):", model)
                    break
                msg = r.text[:200].replace("\n", " ")
                print(f"AI 이미지 오류 {r.status_code} ({model}):", msg)
                if r.status_code == 400 and use_ratio:
                    continue                     # 비율 설정 없이 한 번 더
                if r.status_code in (401, 403, 429):
                    hard = True                  # 무료 한도/권한 문제 → 다음 모델
                break
        # 한도/권한 문제로 막혔다면 이번 실행에서는 AI 이미지를 더 시도하지 않음
        # (그림 하나가 안전 규칙으로 거절된 경우는 그 장면만 무료 사진으로 대체)
        if hard:
            _ai_off = True
    except Exception as e:
        print("AI 이미지 실패:", str(e)[:150])
        _ai_off = True
    return False


# ---------------- Cloudflare Workers AI (무료 하루 10,000 Neurons) ----------------
CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"
# FLUX 는 '~하지 마'라는 문장을 잘 못 알아들어서, 원하는 모습만 긍정문으로 적어요
FLUX_RULES = ("Wide 6:5 composition, the main subject centered in the frame, simple uncluttered "
              "background, soft warm lighting, clean and tidy illustration.")
_cf_off = False


def _cf_image(image_prompt, path):
    global _cf_off
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    tok = os.environ.get("CLOUDFLARE_API_TOKEN")
    if _cf_off or not (acct and tok):
        return False
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{CF_MODEL}"
    prompt = f"{AI_STYLE}. {image_prompt}. {FLUX_RULES}"
    try:
        for size in ((1152, 960), (1024, 832), None):       # 가로로 약간 넓게 → 더 작게 → 기본(정사각)
            body = {"prompt": prompt, "steps": 4}
            if size:
                body["width"], body["height"] = size
            r = requests.post(url, headers={"Authorization": f"Bearer {tok}"},
                              json=body, timeout=120)
            if r.status_code == 200:
                raw = None
                if str(r.headers.get("content-type", "")).startswith("image/"):
                    raw = r.content
                else:
                    b64 = ((r.json() or {}).get("result") or {}).get("image")
                    raw = base64.b64decode(b64) if b64 else None
                if raw:
                    open(path, "wb").write(raw)
                    fit_user_photo(path, path)
                    print("Cloudflare AI 그림 만들기 성공", size or "기본 크기")
                    return True
                print("Cloudflare AI 가 그림을 돌려주지 않았어요")
                return False
            print(f"Cloudflare AI 오류 {r.status_code}:", r.text[:200].replace("\n", " "))
            if r.status_code in (400, 422) and size:
                continue                              # 크기 설정 문제 → 더 작은 크기로 재시도
            if r.status_code in (401, 403, 429):
                _cf_off = True                        # 키/한도 문제 → 이번 실행에서는 더 시도 안 함
            return False
    except Exception as e:
        print("Cloudflare AI 실패:", str(e)[:150])
        _cf_off = True
    return False


def generate_ai_image(image_prompt, path):
    """AI 그림 만들기: Cloudflare(무료) 먼저, 설정했다면 Gemini. 실패하면 False."""
    if not image_prompt:
        return False
    if _cf_image(image_prompt, path):
        return True
    if os.environ.get("AI_GEMINI") == "1":
        return _gemini_image(image_prompt, path)
    return False


def _fit_lines(d, text, title, sizes, max_w, max_lines):
    for size in sizes:
        f = font(size, title)
        lines = wrap(d, text, f, max_w)
        if len(lines) <= max_lines:
            return f, lines, size
    f = font(sizes[-1], title)
    return f, wrap(d, text, f, max_w)[:max_lines], sizes[-1]


def render_overlay(headline, narration, idx, total, path, note=""):
    """숏폼 느낌: 위에는 큰 제목, 가운데는 사진 창, 아래는 큰 자막. (사진 창은 투명)"""
    img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, WIN_Y, W, WIN_Y + WIN_H - 1], fill=(0, 0, 0, 0))     # 사진이 보일 창

    # 채널 이름 (노란 글씨 + 검은 테두리)
    d.text((54, 40), CHANNEL, font=font(66, True), fill=YELLOW,
           stroke_width=7, stroke_fill=(0, 0, 0))

    # 큰 제목 + 형광펜 효과
    f, lines, size = _fit_lines(d, headline, True, [104, 92, 80, 68], W - 110, 2)
    y = 148
    lh = int(size * 1.12)
    for line in lines:
        bb = d.textbbox((W // 2, y), line, font=f, anchor="ma")
        d.rectangle([bb[0] - 10, y + int(size * 0.55), bb[2] + 10, bb[3] + 2], fill=YELLOW + (255,))
        d.text((W // 2, y), line, font=f, fill=INK, anchor="ma")
        y += lh
    if note:
        d.text((W // 2, WIN_Y - 46), note, font=font(30), fill=(120, 120, 120), anchor="ma")

    # 아래 큰 자막
    top = WIN_Y + WIN_H + 50
    f2, lines2, size2 = _fit_lines(d, narration, False, [76, 70, 64, 58, 52], W - 130, 5)
    block = len(lines2) * int(size2 * 1.3)
    y = top + max(0, (H - 70 - top - block) // 2)
    for line in lines2:
        d.text((W // 2, y), line, font=f2, fill=INK, anchor="ma")
        y += int(size2 * 1.3)

    # 진행바
    d.rectangle([0, H - 26, W, H], fill=(225, 225, 225, 255))
    d.rectangle([0, H - 26, int(W * (idx + 1) / total), H], fill=RED + (255,))
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


def make_clip(bg, overlay, mp3, mp4, dur, zoom_in, pan=0):
    """사진을 창 안에서 천천히 확대/축소하고, 위아래 글자 화면을 얹은 뒤 음성을 붙입니다."""
    frames = int(dur * 30) + 3
    z = ("min(1.0+0.0006*on,1.15)" if zoom_in else "max(1.0,1.15-0.0006*on)")
    xs = ["iw/2-(iw/zoom/2)", "(iw-iw/zoom)*0.2", "(iw-iw/zoom)*0.8"][pan % 3]
    fc = (f"[0:v]zoompan=z='{z}':x='{xs}':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={WIN_W}x{WIN_H}:fps=30[img];"
          f"[1:v][img]overlay=0:{WIN_Y}[base];"
          f"[base][2:v]overlay=0:0:format=auto,format=yuv420p[v]")
    run(["ffmpeg", "-y", "-i", bg, "-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:r=30",
         "-loop", "1", "-framerate", "30", "-i", overlay, "-i", mp3,
         "-filter_complex", fc, "-map", "[v]", "-map", "3:a",
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
    ids = [x.strip() for x in os.environ.get("PHOTO_IDS", "").split(",") if x.strip()]
    mine = download_user_photos(ids) if ids else []
    cf_ready = bool(os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN"))
    gem_ready = os.environ.get("AI_GEMINI") == "1" and bool(os.environ.get("GEMINI_API_KEY"))
    ai_on = os.environ.get("AI_IMAGES", "0") == "1" and (cf_ready or gem_ready)
    print("AI 그림 사용:", ai_on, "(Cloudflare:", cf_ready, ", Gemini:", gem_ready, ")")
    for i, s in enumerate(scenes):
        bg = f"{OUT}/bg{i}.jpg"
        ov, mp3, mp4 = f"{OUT}/ov{i}.png", f"{OUT}/s{i}.mp3", f"{OUT}/s{i}.mp4"
        note = ""
        if mine:
            fit_user_photo(mine[i % len(mine)], bg)           # 내가 보낸 사진
        elif ai_on and generate_ai_image(s.get("image_prompt"), bg):
            credits.add("AI")                                  # AI가 만든 그림
            note = "이해를 돕기 위한 AI 일러스트가 포함되어 있어요"
        else:
            src = fetch_photo([s.get("image_query"), s.get("image_query_alt"),
                               "home appliances"], bg)
            if src:
                credits.add(src)
            else:
                gradient_bg(bg)
        render_overlay(s["headline"], s["narration"], i, len(scenes), ov, note)
        asyncio.run(tts(s["narration"], mp3))
        make_clip(bg, ov, mp3, mp4, duration(mp3) + 0.4, zoom_in=(i % 2 == 0), pan=i)
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
    notes = []
    if "AI" in credits:
        notes.append("이미지: AI 생성")
    stock = sorted(credits - {"AI"})
    if stock:
        notes.append("사진: " + ", ".join(stock))
    if notes:
        caption += "\n" + "\n".join(notes)

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
