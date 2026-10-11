import asyncio, base64, json, os, random, re, subprocess, sys, time
import edge_tts, requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


def fit_user_photo(src, dst):
    """내가 보낸 사진을 영상 배경으로 만듭니다. 가로 사진은 흐린 배경 위에 선명하게 얹어요."""
    im = Image.open(src).convert("RGB")
    if im.width / im.height <= 0.9:          # 세로에 가까운 사진: 꽉 채우기
        im.save(dst, quality=92)
        cover_resize(dst)
        return
    cover = im.copy()
    s = max(BW / cover.width, BH / cover.height)
    cover = cover.resize((int(cover.width * s) + 1, int(cover.height * s) + 1), Image.LANCZOS)
    x, y = (cover.width - BW) // 2, (cover.height - BH) // 2
    bg = cover.crop((x, y, x + BW, y + BH)).filter(ImageFilter.GaussianBlur(45))
    bg = Image.blend(bg, Image.new("RGB", (BW, BH), (0, 0, 0)), 0.35)
    fw = BW
    fh = int(im.height * fw / im.width)
    fg = im.resize((fw, fh), Image.LANCZOS)
    bg.paste(fg, (0, int(BH * 0.38) - fh // 2))
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
AI_RULES = ("Vertical 9:16 composition. Main subject in the upper-middle of the frame, "
            "simple calm area in the lower third. No text, no letters, no logos, "
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
                    cfg["imageConfig"] = {"aspectRatio": "9:16"}
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
FLUX_RULES = ("Vertical composition, the main subject centered in the upper-middle of the frame, "
              "calm simple empty space in the lower third, soft warm lighting, clean and tidy illustration.")
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
        for size in ((1024, 1792), (768, 1344), None):      # 큰 세로 → 작은 세로 → 기본(정사각)
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


def make_clip(bg, overlay, mp3, mp4, dur, zoom_in, pan=0):
    """사진을 천천히 확대/축소하면서 뉴스 화면을 얹고 음성을 붙입니다."""
    frames = int(dur * 30) + 3
    z = ("min(1.0+0.0006*on,1.15)" if zoom_in else "max(1.0,1.15-0.0006*on)")
    xs = ["iw/2-(iw/zoom/2)", "(iw-iw/zoom)*0.2", "(iw-iw/zoom)*0.8"][pan % 3]
    fc = (f"[0:v]zoompan=z='{z}':x='{xs}':y='ih/2-(ih/zoom/2)'"
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
    ids = [x.strip() for x in os.environ.get("PHOTO_IDS", "").split(",") if x.strip()]
    mine = download_user_photos(ids) if ids else []
    cf_ready = bool(os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN"))
    gem_ready = os.environ.get("AI_GEMINI") == "1" and bool(os.environ.get("GEMINI_API_KEY"))
    ai_on = os.environ.get("AI_IMAGES", "0") == "1" and (cf_ready or gem_ready)
    print("AI 그림 사용:", ai_on, "(Cloudflare:", cf_ready, ", Gemini:", gem_ready, ")")
    for i, s in enumerate(scenes):
        bg = f"{OUT}/bg{i}.jpg"
        ov, mp3, mp4 = f"{OUT}/ov{i}.png", f"{OUT}/s{i}.mp3", f"{OUT}/s{i}.mp4"
        if mine:
            fit_user_photo(mine[i % len(mine)], bg)           # 내가 보낸 사진
        elif ai_on and generate_ai_image(s.get("image_prompt"), bg):
            credits.add("AI")                                  # AI가 만든 그림
        else:
            src = fetch_photo([s.get("image_query"), s.get("image_query_alt"),
                               "home appliances"], bg)
            if src:
                credits.add(src)
            else:
                gradient_bg(bg)
        render_overlay(s["headline"], s["narration"], i, len(scenes), ov)
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
