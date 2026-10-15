"""
6_send_telegram.py
weekly_analysis.json + summarized_news.json → 텔레그램으로 요약 브리핑 전송
+ 생성된 HTML 뉴스레터 파일 첨부

실행:
  python 6_send_telegram.py            # 요약 + HTML 첨부 전송
  python 6_send_telegram.py --getid    # chat_id 확인 (봇과 대화/그룹초대 후 실행)

필요: .env 에 아래 두 값
  TELEGRAM_BOT_TOKEN=...   (@BotFather 에서 /newbot 으로 발급)
  TELEGRAM_CHAT_ID=...     (--getid 로 확인)
"""

import os
import sys
import json
import glob
import html
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API = f"https://api.telegram.org/bot{TOKEN}"
MSG_LIMIT = 4000  # 텔레그램 메시지 최대 4096자 (여유분)


def esc(s: str) -> str:
    return html.escape(s or "")


def latest_html() -> Path | None:
    files = glob.glob("weekly_newsletter_*.html")
    return Path(max(files, key=lambda f: Path(f).stat().st_mtime)) if files else None


def headline(item: dict) -> str:
    title = esc((item.get("title_ko") or item.get("title", ""))[:80])
    link = esc(item.get("link", ""))
    src = esc(item.get("source", ""))
    return f'• <a href="{link}">{title}</a> <i>({src})</i>'


def top_by_tag(articles: list, tag: str, n: int = 3) -> list:
    xs = sorted([x for x in articles if x.get(tag)],
                key=lambda x: x.get("relevance_score", 0), reverse=True)
    seen, out = set(), []
    for x in xs:
        key = (x.get("title_ko") or x.get("title", ""))[:20]
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
        if len(out) >= n:
            break
    return out


def build_digest() -> str:
    analysis = json.loads(Path("weekly_analysis.json").read_text(encoding="utf-8"))
    articles = []
    if Path("summarized_news.json").exists():
        articles = json.loads(Path("summarized_news.json").read_text(encoding="utf-8"))

    lines = ["<b>⚓ 아크메 주간 해사 브리핑</b>",
             f"<i>{esc(analysis.get('period', ''))}</i>", ""]

    concl = analysis.get("one_line_conclusion_ko") or analysis.get("one_line_conclusion", "")
    if concl:
        lines += [f"💡 <b>한 줄 결론</b>", esc(concl), ""]

    sections = [
        ("🔬 <b>기술 동향</b>", analysis.get("tech_news", [])[:5]),
        ("🏢 <b>고객사 동향</b>", analysis.get("customer_news", [])[:5]),
        ("🌐 <b>글로벌 컴플라이언스</b>", top_by_tag(articles, "compliance", 3)),
        ("🏁 <b>경쟁사 동향</b>", top_by_tag(articles, "competitor", 3)),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.append(title)
        lines += [headline(x) for x in items]
        lines.append("")

    lines.append("📎 전체 뉴스레터는 첨부된 HTML 파일을 열어보세요.")

    # 4096자 제한: 초과 시 뒤 줄부터 제거 (태그 중간 잘림 방지)
    while lines and len("\n".join(lines)) > MSG_LIMIT:
        lines.pop()
    return "\n".join(lines)


def get_updates():
    """봇과 최근 대화한 chat 목록을 출력해 chat_id를 찾도록 돕는다."""
    if not TOKEN:
        print(".env에 TELEGRAM_BOT_TOKEN이 없습니다.")
        return
    r = requests.get(f"{API}/getUpdates", timeout=30)
    data = r.json()
    seen = {}
    for upd in data.get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat")
        if chat:
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
    if not seen:
        print("최근 대화가 없습니다. 봇에게 메시지를 보내거나 그룹에 초대한 뒤 다시 실행하세요.")
        return
    print("발견된 chat_id (이 값을 .env의 TELEGRAM_CHAT_ID에 넣으세요):")
    for cid, name in seen.items():
        print(f"  {cid}  →  {name}")


def send() -> bool:
    if not TOKEN or not CHAT_ID:
        print(".env에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID를 설정하세요. (chat_id는 --getid로 확인)")
        return False

    text = build_digest()
    r = requests.post(f"{API}/sendMessage", data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }, timeout=30)
    if not r.ok:
        print("요약 메시지 전송 실패:", r.text)
        return False
    print("텔레그램 요약 전송 완료")

    # 전체 HTML 뉴스레터 첨부
    fp = latest_html()
    if fp:
        with open(fp, "rb") as f:
            r2 = requests.post(f"{API}/sendDocument",
                               data={"chat_id": CHAT_ID},
                               files={"document": (fp.name, f, "text/html")},
                               timeout=60)
        print("HTML 첨부 전송:", "완료" if r2.ok else f"실패 {r2.text}")
    else:
        print("첨부할 HTML을 찾지 못했습니다. 3_build_html.py를 먼저 실행하세요.")
    return True


if __name__ == "__main__":
    if "--getid" in sys.argv:
        get_updates()
    else:
        send()
