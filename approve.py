"""텔레그램 승인/거절 버튼을 확인하고, 승인된 영상을 인스타 릴스로 올립니다."""
import json, os, subprocess, sys, time
from datetime import datetime, timezone
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = str(os.environ["TELEGRAM_CHAT_ID"])
IG_USER = os.environ.get("IG_USER_ID", "")
IG_TOKEN = os.environ.get("IG_PAGE_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TG = f"https://api.telegram.org/bot{TOKEN}"
GV = "v26.0"


def tg(method, **data):
    try:
        return requests.post(f"{TG}/{method}", data=data, timeout=60).json()
    except Exception as e:
        print("텔레그램 오류:", e)
        return {}


def gh(*args, check=True):
    return subprocess.run(["gh", *args, "-R", REPO], capture_output=True,
                          text=True, check=check)


def graph_ok(r):
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"응답 오류 {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400 or "error" in j:
        raise RuntimeError(str(j.get("error", j))[:300])
    return j


def publish_reel(path, caption):
    size = os.path.getsize(path)
    j = graph_ok(requests.post(
        f"https://graph.facebook.com/{GV}/{IG_USER}/media",
        data={"media_type": "REELS", "upload_type": "resumable",
              "caption": caption, "share_to_feed": "true",
              "access_token": IG_TOKEN}, timeout=60))
    cid = j["id"]
    uri = j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GV}/{cid}"
    with open(path, "rb") as f:
        graph_ok(requests.post(
            uri, headers={"Authorization": f"OAuth {IG_TOKEN}",
                          "offset": "0", "file_size": str(size)},
            data=f, timeout=300))
    status = ""
    for _ in range(40):
        time.sleep(10)
        st = graph_ok(requests.get(
            f"https://graph.facebook.com/{GV}/{cid}",
            params={"fields": "status_code,status", "access_token": IG_TOKEN},
            timeout=60))
        status = st.get("status_code", "")
        print("처리 상태:", status)
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError("인스타 영상 처리 실패: " + str(st.get("status", ""))[:200])
    if status != "FINISHED":
        raise RuntimeError("인스타 영상 처리가 너무 오래 걸립니다.")
    mid = graph_ok(requests.post(
        f"https://graph.facebook.com/{GV}/{IG_USER}/media_publish",
        data={"creation_id": cid, "access_token": IG_TOKEN}, timeout=60))["id"]
    try:
        link = graph_ok(requests.get(
            f"https://graph.facebook.com/{GV}/{mid}",
            params={"fields": "permalink", "access_token": IG_TOKEN},
            timeout=60)).get("permalink", "")
    except Exception:
        link = ""
    return link


def buttons(tag):
    return json.dumps({"inline_keyboard": [[
        {"text": "✅ 승인 (업로드)", "callback_data": f"ok:{tag}"},
        {"text": "❌ 거절", "callback_data": f"no:{tag}"}]]})


def handle(cq):
    cqid = cq["id"]
    if str(cq["from"]["id"]) != CHAT:
        tg("answerCallbackQuery", callback_query_id=cqid, text="권한이 없습니다.")
        return
    action, tag = cq["data"].split(":", 1)
    mid = cq["message"]["message_id"]
    exists = gh("release", "view", tag, "--json", "body", "-q", ".body", check=False)
    if exists.returncode != 0:
        tg("answerCallbackQuery", callback_query_id=cqid, text="이미 처리된 영상입니다.")
        tg("editMessageReplyMarkup", chat_id=CHAT, message_id=mid,
           reply_markup=json.dumps({"inline_keyboard": []}))
        return
    caption = exists.stdout.strip()

    if action == "no":
        gh("release", "delete", tag, "--cleanup-tag", "-y", check=False)
        tg("answerCallbackQuery", callback_query_id=cqid, text="거절했어요.")
        tg("editMessageReplyMarkup", chat_id=CHAT, message_id=mid,
           reply_markup=json.dumps({"inline_keyboard": []}))
        tg("sendMessage", chat_id=CHAT, text="❌ 거절된 영상을 삭제했어요.")
        return

    tg("answerCallbackQuery", callback_query_id=cqid, text="업로드를 시작합니다.")
    tg("editMessageReplyMarkup", chat_id=CHAT, message_id=mid,
       reply_markup=json.dumps({"inline_keyboard": []}))
    try:
        if not (IG_USER and IG_TOKEN):
            raise RuntimeError("IG_USER_ID / IG_PAGE_TOKEN 이 설정되지 않았어요.")
        os.makedirs("dl", exist_ok=True)
        gh("release", "download", tag, "-p", "final.mp4", "-D", "dl", "--clobber")
        link = publish_reel("dl/final.mp4", caption)
        gh("release", "delete", tag, "--cleanup-tag", "-y", check=False)
        tg("sendMessage", chat_id=CHAT, text="✅ 인스타 릴스 업로드 완료!\n" + link)
    except Exception as e:
        print("업로드 실패:", e)
        tg("sendMessage", chat_id=CHAT,
           text=f"⚠️ 업로드 실패: {str(e)[:300]}\n아래 버튼을 다시 눌러 재시도할 수 있어요.",
           reply_markup=buttons(tag))


def cleanup_old():
    """3일 넘게 방치된 임시 영상은 자동으로 지웁니다."""
    r = gh("release", "list", "--json", "tagName,createdAt", check=False)
    if r.returncode != 0:
        return
    now = datetime.now(timezone.utc)
    for it in json.loads(r.stdout or "[]"):
        if not it["tagName"].startswith("p"):
            continue
        t = datetime.fromisoformat(it["createdAt"].replace("Z", "+00:00"))
        if (now - t).days >= 3:
            gh("release", "delete", it["tagName"], "--cleanup-tag", "-y", check=False)
            print("오래된 영상 삭제:", it["tagName"])


def main():
    r = requests.get(f"{TG}/getUpdates",
                     params={"timeout": 0, "allowed_updates": json.dumps(["callback_query"])},
                     timeout=60).json()
    ups = r.get("result", [])
    if ups:
        last = max(u["update_id"] for u in ups)
        # 같은 버튼을 두 번 처리하지 않도록 먼저 '읽음' 처리
        requests.get(f"{TG}/getUpdates", params={"offset": last + 1, "timeout": 0}, timeout=60)
        for u in ups:
            if "callback_query" in u:
                handle(u["callback_query"])
    else:
        print("새 버튼 입력 없음")
    cleanup_old()


if __name__ == "__main__":
    main()
