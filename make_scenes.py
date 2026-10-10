"""고양·안양·인천·시흥 지역의 최근 7일 이내 긍정적인 베스트샵·가전 행사 뉴스를 골라
30초 뉴스 대본(scenes.json)을 만듭니다."""
import json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
import requests
from bs4 import BeautifulSoup

MAX_AGE_DAYS = 7

# ====== 지역 설정 (여기만 고치면 지역을 바꿀 수 있어요) ======
REGIONS = ["고양", "안양", "인천", "시흥"]
# 제목에 이 글자 중 하나는 들어 있어야 후보가 됩니다 (동네 이름 포함)
REGION_KEYWORDS = ["고양", "일산", "덕양", "안양", "평촌", "범계",
                   "인천", "부평", "계양", "검단", "시흥", "배곧", "정왕"]
ALIASES = "일산 OR 덕양 OR 평촌 OR 범계 OR 부평 OR 계양 OR 검단 OR 배곧 OR 정왕"
ALL = "(" + " OR ".join(REGIONS) + ")"

GROUPS = [
    # 1순위: 해당 지역의 LG전자 베스트샵 매장·행사 소식
    ("지역 베스트샵 매장·행사",
     [f"LG전자 베스트샵 {r}" for r in REGIONS]
     + [f"베스트샵 ({ALIASES})", f"LG베스트샵 {ALL} 행사"]),
    # 2순위: 해당 지역의 혼수 가전·LG전자 행사 소식
    ("지역 혼수 가전·행사",
     [f"{r} 혼수 가전" for r in REGIONS]
     + [f"{ALL} 혼수 가전 행사", f"{ALL} LG전자 행사"]),
]
API = "https://generativelanguage.googleapis.com/v1beta"


def notify(text):
    t, c = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if t and c:
        try:
            requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                          data={"chat_id": c, "text": text}, timeout=30)
        except Exception as e:
            print("알림 실패:", e)


def fetch_news(query):
    url = (f"https://news.google.com/rss/search?q={quote(query + ' when:7d')}"
           "&hl=ko&gl=KR&ceid=KR:ko")
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue  # 날짜를 모르면 쓰지 않는다
        items.append({"title": title, "source": source, "dt": dt})
    return items


def collect_candidates(used):
    """그룹별로 최근 7일 이내, 아직 안 쓴 기사만 모읍니다."""
    limit = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    cands, seen = [], set()
    for gi, (gname, queries) in enumerate(GROUPS):
        pool = []
        for q in queries:
            try:
                pool += fetch_news(q)
            except Exception as e:
                print("뉴스 수집 실패:", q, e)
        pool.sort(key=lambda x: x["dt"], reverse=True)
        n = 0
        for it in pool:
            if it["dt"] < limit or it["title"] in used or it["title"] in seen:
                continue
            if not any(k in it["title"] for k in REGION_KEYWORDS):
                continue   # 대상 지역이 제목에 없으면 제외
            seen.add(it["title"])
            it["group"] = gi
            it["gname"] = gname
            cands.append(it)
            n += 1
            if n >= 8:
                break
        print(f"[{gname}] 후보 {n}개")
    return cands



def fetch_article(url):
    """사용자가 보낸 기사 링크에서 제목, 언론사, 본문을 읽어옵니다."""
    if not re.match(r"^https?://", url):
        raise ValueError("http(s) 링크만 사용할 수 있어요.")
    r = requests.get(url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9"})
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = meta("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    desc = meta("og:description", "description")
    site = meta("og:site_name") or urlparse(r.url).netloc
    date = (meta("article:published_time", "og:article:published_time") or "")[:10]
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        t.decompose()
    box = (soup.find("article") or soup.find(id=re.compile("article|newsct|content", re.I))
           or soup.body or soup)
    paras = [p.get_text(" ", strip=True) for p in box.find_all("p")]
    body = "\n".join(p for p in paras if len(p) > 30)
    if len(body) < 200:
        body = (desc + "\n" + body).strip()
    if not title and not body:
        raise ValueError("기사 내용을 읽지 못했어요.")
    return {"title": title, "source": site, "date": date or "날짜 미상",
            "body": body[:6000], "url": r.url}


def list_models(key):
    forced = os.environ.get("GEMINI_MODEL")
    if forced:
        return [forced]
    r = requests.get(f"{API}/models", params={"key": key, "pageSize": 200}, timeout=30)
    r.raise_for_status()
    bad = ("image", "tts", "live", "audio", "embed", "robotics", "computer",
           "aqa", "imagen", "veo", "gemma", "learnlm")
    main, lite = [], []
    for m in r.json().get("models", []):
        name = m["name"].split("/")[-1]
        if ("generateContent" in m.get("supportedGenerationMethods", [])
                and "flash" in name and not any(b in name for b in bad)):
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            if v:
                item = (float(v.group(1)), "preview" not in name, name)
                (lite if "lite" in name else main).append(item)
    ordered = [n for _, _, n in sorted(main, reverse=True)] + \
              [n for _, _, n in sorted(lite, reverse=True)]
    if not ordered:
        sys.exit("사용 가능한 Gemini flash 모델을 찾지 못했습니다.")
    return ordered[:5]


def ask_gemini(key, model, prompt):
    r = requests.post(
        f"{API}/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json",
                                   "temperature": 0.5}},
        timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini 오류 {r.status_code}: {r.text[:300]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return json.loads(text.replace("```json", "").replace("```", "").strip())


def run_gemini(key, models, prompt, check):
    """모델을 바꿔가며, 점점 길게 기다리며 재시도합니다."""
    for model in models:
        for attempt in range(3):
            try:
                print(f"시도: {model} ({attempt + 1}/3)")
                d = ask_gemini(key, model, prompt)
                if check(d):
                    return d
                print("형식이 맞지 않아 다시 시도합니다.")
            except Exception as e:
                print("실패:", str(e)[:200])
            time.sleep(10 * (attempt + 1))
    return None


SELECT_PROMPT = """너는 지역 뉴스 편집자다. 아래 후보 기사 제목 중 영상으로 만들 기사 한 개를 골라라.

대상 지역은 경기 고양시, 경기 안양시, 인천시, 경기 시흥시 네 곳이다. (고양의 일산·덕양, 안양의 평촌·범계, 인천의 부평·계양·검단, 시흥의 배곧·정왕 같은 동네 포함)

선택 기준 (중요한 순서):
1. 지역 조건: 제목에 대상 지역이 분명히 나오고, 그 지역의 매장이나 행사 소식이어야 한다.
   대구, 부산, 대전, 광주, 서울 등 다른 지역 소식, 지역이 불분명한 기사, 전국 단위 기사(전국 매장, 전국 행사, 전국 동시 진행 등)는 반드시 제외한다.
   제목에 대상 지역과 다른 지역이 함께 나오더라도 대상 지역이 중심이 아니면 제외한다.
2. 긍정적이거나 호의적인 소식만 고른다. 예: 매장 오픈·리뉴얼, 행사·체험·프로모션, 고객 혜택, 수상, 신제품 전시, 혼수 가전 상담·행사.
   리콜, 결함, 사고, 소송, 논란, 파업, 실적 악화, 가격 인상, 불매, 안전·품질 문제 등 부정적 소식은 반드시 제외한다.
3. 주제 우선순위: (1순위) LG전자 베스트샵 매장·행사 → (2순위) 해당 지역의 혼수 가전·LG전자 행사.
   높은 순위에 적합한 기사가 있으면 반드시 그 기사를 고른다.
4. 위 조건에 맞는 기사가 하나도 없으면 -1을 답한다. 억지로 고르지 않는다.

후보:
{lines}

출력(JSON만): {{"index": 번호}}"""


SCRIPT_PROMPT = """너는 한국어 뉴스 쇼츠 작가다. 아래 뉴스 제목들을 바탕으로 30초 분량의 뉴스 영상 대본을 JSON으로만 써라.

[메인 기사]
제목: {title}
언론사: {source} ({date})

[같은 주제의 다른 기사 제목]
{others}

규칙:
- 제목들에 나온 사실만 사용한다. 숫자, 날짜, 인물, 제품명을 지어내지 않는다.
- 제목만으로 알 수 없는 내용은 쓰지 않는다. 추측, 과장, 광고 문구를 쓰지 않는다. 밝고 긍정적인 톤으로 쓴다.
- 장면은 4개. 각 장면의 headline은 18자 이내, narration은 45자 이내의 자연스러운 뉴스 구어체.
- 전체 narration 합계는 170자 안팎이다.
- 첫 장면은 시선을 끄는 한 문장, 마지막 장면은 정리 한 문장이다.
- 이 영상은 LG전자 공식 채널이 아니므로 공식 입장처럼 말하지 않는다.
- 각 장면에 image_query를 넣는다. 그 장면에 어울리는 사진을 찾기 위한 영어 검색어 2~4단어다.
  일반적인 장면을 묘사한다. 예: "modern kitchen appliances", "couple new home living room", "washing machine laundry room", "electronics store showroom".
  브랜드명, 글자, 특정 인물 이름은 넣지 않는다. 장면마다 서로 다른 검색어를 쓴다.
- 제목에 나온 지역(예: 안양, 인천)을 첫 장면과 마지막 장면에서 자연스럽게 언급한다.
- caption에는 한 줄 요약, 줄바꿈, "출처: {source}", "AI로 제작된 영상입니다", 해시태그 5개를 넣는다. 해시태그 중 2개는 기사의 지역과 관련된 것(예: #안양, #인천베스트샵)으로 한다.

출력 형식(JSON만, 설명 금지):
{{"caption": "...", "scenes": [{{"headline": "...", "narration": "...", "image_query": "..."}}]}}
"""


ARTICLE_PROMPT = """너는 한국어 뉴스 쇼츠 작가다. 아래 기사 내용을 바탕으로 30초 분량의 뉴스 영상 대본을 JSON으로만 써라.

[기사]
제목: {title}
언론사: {source} ({date})
본문:
{body}

규칙:
- 기사에 나온 사실만 사용한다. 숫자, 날짜, 인물, 제품명을 지어내지 않는다. 기사에 없는 내용은 쓰지 않는다.
- 기사 문장을 그대로 옮기지 말고, 완전히 새로운 문장으로 요약해서 쓴다.
- 추측, 과장, 광고 문구를 쓰지 않는다. 기사의 분위기를 왜곡하지 않는다.
- 장면은 4개. 각 장면의 headline은 18자 이내, narration은 45자 이내의 자연스러운 뉴스 구어체.
- 전체 narration 합계는 170자 안팎이다.
- 첫 장면은 시선을 끄는 한 문장, 마지막 장면은 정리 한 문장이다.
- 이 영상은 해당 기업의 공식 채널이 아니므로 공식 입장처럼 말하지 않는다.
- 각 장면에 image_query를 넣는다. 그 장면에 어울리는 사진을 찾기 위한 영어 검색어 2~4단어다.
  일반적인 장면을 묘사한다. 예: "modern kitchen appliances", "couple new home living room", "electronics store showroom".
  브랜드명, 글자, 특정 인물 이름은 넣지 않는다. 장면마다 서로 다른 검색어를 쓴다.
- caption에는 한 줄 요약, 줄바꿈, "출처: {source}", "AI로 제작된 영상입니다", 해시태그 5개를 넣는다.

출력 형식(JSON만, 설명 금지):
{{"caption": "...", "scenes": [{{"headline": "...", "narration": "...", "image_query": "..."}}]}}
"""


def valid_script(d):
    try:
        sc = d["scenes"]
        return (3 <= len(sc) <= 5
                and all(s["headline"] and s["narration"] for s in sc)
                and sum(len(s["narration"]) for s in sc) <= 260
                and bool(d["caption"]))
    except Exception:
        return False


def skip(msg):
    print(msg)
    if os.path.exists("scenes.json"):
        os.remove("scenes.json")
    notify("ℹ️ " + msg)
    sys.exit(0)


def main():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY 가 없습니다. Secrets에 추가해 주세요.")
    if os.path.exists("scenes.json"):
        os.remove("scenes.json")  # 지난번 대본이 남아 있지 않게
    used = json.load(open("used.json", encoding="utf-8")) if os.path.exists("used.json") else []

    models = list_models(key)
    print("사용할 모델 순서:", models)

    link = os.environ.get("ARTICLE_URL", "").strip()
    if link:
        # ---- 내가 보낸 기사 링크로 만들기 ----
        print("링크 모드:", link)
        try:
            art = fetch_article(link)
        except Exception as e:
            skip(f"기사 링크를 읽지 못했어요 ({str(e)[:80]}). "
                 "기사 제목과 본문을 복사해서 알려주시면 다른 방법을 찾아볼게요.")
        print("기사 제목:", art["title"], "-", art["source"])
        prompt = ARTICLE_PROMPT.format(title=art["title"], source=art["source"],
                                       date=art["date"], body=art["body"] or "(본문을 읽지 못함)")
        used_title = art["title"]
    else:
        # ---- 자동으로 기사 고르기 ----
        cands = collect_candidates(used)
        if not cands:
            skip("고양·안양·인천·시흥 지역의 최근 7일 이내 새 기사가 없어서 이번 영상은 건너뛰었어요.")
        lines = "\n".join(
            f"[{i}] ({c['group'] + 1}순위: {c['gname']}) {c['title']} - {c['source']} ({c['dt'].strftime('%m/%d')})"
            for i, c in enumerate(cands))
        sel = run_gemini(key, models, SELECT_PROMPT.format(lines=lines),
                         lambda d: isinstance(d.get("index"), int)
                         and -1 <= d["index"] < len(cands))
        if sel is None:
            sys.exit("기사 선택에 실패했습니다. 잠시 후 다시 실행해 주세요.")
        if sel["index"] == -1:
            skip("지역(고양·안양·인천·시흥)에 맞는 긍정적인 새 기사가 없어서 이번 영상은 건너뛰었어요.")
        chosen = cands[sel["index"]]
        print("선택한 기사:", chosen["title"], "-", chosen["source"], f"({chosen['gname']})")
        others = [c["title"] for c in cands
                  if c["title"] != chosen["title"] and c["group"] == chosen["group"]][:6]
        prompt = SCRIPT_PROMPT.format(
            title=chosen["title"], source=chosen["source"] or "언론사",
            date=chosen["dt"].strftime("%Y-%m-%d"),
            others="\n".join("- " + o for o in others) or "(없음)")
        used_title = chosen["title"]

    data = run_gemini(key, models, prompt, valid_script)
    if not data:
        sys.exit("대본 생성에 실패했습니다. 잠시 후 다시 실행해 주세요.")

    json.dump(data, open("scenes.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    used.append(used_title)
    json.dump(used[-200:], open("used.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
