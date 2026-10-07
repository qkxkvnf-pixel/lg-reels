"""최신 가전 뉴스를 찾아서 30초 뉴스 대본(scenes.json)을 자동으로 만듭니다."""
import json, os, random, re, sys, time
import xml.etree.ElementTree as ET
from urllib.parse import quote
import requests

QUERIES = [
    "LG전자 베스트샵",
    "LG전자 신제품 가전",
    "혼수 가전 LG전자",
    "LG전자 가전 트렌드",
]
API = "https://generativelanguage.googleapis.com/v1beta"


def fetch_news(query):
    url = (f"https://news.google.com/rss/search?q={quote(query + ' when:7d')}"
           "&hl=ko&gl=KR&ceid=KR:ko")
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        # 제목 끝의 " - 언론사" 제거
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        items.append({"title": title, "source": source,
                      "date": (it.findtext("pubDate") or "")[:16]})
    return items


def list_models(key):
    """쓸 수 있는 flash 모델을 좋은 순서(최신, 안정판 우선)로 돌려줍니다."""
    forced = os.environ.get("GEMINI_MODEL")
    if forced:
        return [forced]
    r = requests.get(f"{API}/models", params={"key": key, "pageSize": 200}, timeout=30)
    r.raise_for_status()
    bad = ("image", "tts", "live", "audio", "embed", "robotics",
           "computer", "aqa", "imagen", "veo", "gemma", "learnlm")
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


PROMPT = """너는 한국어 뉴스 쇼츠 작가다. 아래 뉴스 제목들을 바탕으로 30초 분량의 뉴스 영상 대본을 JSON으로만 써라.

[메인 기사]
제목: {title}
언론사: {source} ({date})

[같은 주제의 다른 기사 제목]
{others}

규칙:
- 제목들에 나온 사실만 사용한다. 숫자, 날짜, 인물, 제품명을 지어내지 않는다.
- 제목만으로 알 수 없는 내용은 쓰지 않는다. 추측, 과장, 광고 문구를 쓰지 않는다.
- 장면은 4개. 각 장면의 headline은 18자 이내, narration은 45자 이내의 자연스러운 뉴스 구어체.
- 전체 narration 합계는 170자 안팎이다.
- 첫 장면은 시선을 끄는 한 문장, 마지막 장면은 정리 한 문장이다.
- 이 영상은 LG전자 공식 채널이 아니므로 공식 입장처럼 말하지 않는다.
- caption에는 한 줄 요약, 줄바꿈, "출처: {source}", "AI로 제작된 영상입니다", 해시태그 5개를 넣는다.

출력 형식(JSON만, 설명 금지):
{{"caption": "...", "scenes": [{{"headline": "...", "narration": "..."}}]}}
"""


def ask_gemini(key, model, prompt):
    r = requests.post(
        f"{API}/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json",
                                   "temperature": 0.6}},
        timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini 오류 {r.status_code}: {r.text[:300]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return json.loads(text.replace("```json", "").replace("```", "").strip())


def valid(d):
    try:
        sc = d["scenes"]
        return (3 <= len(sc) <= 5
                and all(s["headline"] and s["narration"] for s in sc)
                and sum(len(s["narration"]) for s in sc) <= 260
                and bool(d["caption"]))
    except Exception:
        return False


def main():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY 가 없습니다. Secrets에 추가해 주세요.")
    used = json.load(open("used.json", encoding="utf-8")) if os.path.exists("used.json") else []

    queries = QUERIES[:]
    random.shuffle(queries)
    chosen, others = None, []
    for q in queries:
        try:
            items = fetch_news(q)
        except Exception as e:
            print("뉴스 수집 실패:", q, e)
            continue
        fresh = [i for i in items if i["title"] not in used]
        if fresh:
            chosen = fresh[0]
            others = [i["title"] for i in items if i["title"] != chosen["title"]][:6]
            print("선택한 검색어:", q)
            break
    if not chosen:
        sys.exit("새 뉴스를 찾지 못했습니다.")
    print("선택한 기사:", chosen["title"], "-", chosen["source"])

    models = list_models(key)
    print("사용할 모델 순서:", models)
    prompt = PROMPT.format(title=chosen["title"], source=chosen["source"] or "언론사",
                           date=chosen["date"], others="\n".join("- " + o for o in others) or "(없음)")
    data = None
    for model in models:
        for attempt in range(3):
            try:
                print(f"시도: {model} ({attempt + 1}/3)")
                d = ask_gemini(key, model, prompt)
                if valid(d):
                    data = d
                    break
                print("형식이 맞지 않아 다시 시도합니다.")
            except Exception as e:
                print("실패:", str(e)[:200])
            time.sleep(10 * (attempt + 1))
        if data:
            break
    if not data:
        sys.exit("대본 생성에 실패했습니다. 잠시 후 다시 실행해 주세요.")

    json.dump(data, open("scenes.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    used.append(chosen["title"])
    json.dump(used[-200:], open("used.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
