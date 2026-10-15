"""
official_sources.py
규제기관·수로국의 '공식 발표'를 직접 읽어온다. 2차 보도(뉴스 기사)가 아니라 원문이다.

배경 (2026-08-25 요청):
  기관별로 실제 제공 여부를 재봤다.
    UKHO Admiralty  HTML 파싱 가능 — 날짜·제목·링크가 서버 렌더 HTML 에 그대로 있다
    EMSA            RSS 정상 (다만 오염방제 훈련·연안경비 행사 위주라 제품과 거리가 있다)
    IMO             페이지는 열리지만 목록이 HTML 에 없다(SharePoint + JS 렌더).
                    정적 수집으로는 좌측 메뉴만 잡힌다. 헤드리스 브라우저가 있어야 한다.
    ClassNK         링크 0개 — 완전 JS 렌더
    DNV             HTTP 403 (봇 차단)
    IHO/Lloyd's/한국선급/해수부   RSS 없음 또는 404
  그래서 지금 넣는 것은 UKHO 하나다. 아크메 제품(ENC·ECDIS·S-100)과 정확히 겹친다.

주의: UKHO 는 월 1~2건꼴로 발행한다. 수집 기간이 7일이므로 대부분의 주에는 0건이다.
      그게 정상이다 — 그 주에 공식 발표가 없었다는 뜻이다.
"""

import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25

UKHO_URL = "https://www.admiralty.co.uk/news"
UKHO_NAME = "UKHO Admiralty (공식)"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_DATE = re.compile(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(20\d{2})")

# 카드 안에 섞여 들어오는 UI 문구. 제목 앞뒤에서 걷어낸다.
_UI_NOISE = re.compile(r"(Search\s+Title|Article\s+description|Read\s+more|Insight|"
                       r"News|Press\s+release)", re.IGNORECASE)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def fetch_ukho() -> list[dict]:
    """UKHO Admiralty 뉴스 목록 → [{title, summary, link, published}].

    실패는 빈 목록으로 돌려준다(예외 없음). 공식 소스 하나가 죽어도
    파이프라인 전체가 멈추면 안 된다.
    """
    try:
        r = requests.get(UKHO_URL, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        print(f"  [공식소스] UKHO 수집 실패: {type(exc).__name__}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for card in soup.select("article"):
        text = _clean(card.get_text(" ", strip=True))
        match = _DATE.search(text)
        if not match:
            continue                      # 날짜 없는 블록(필터 UI 등)은 목록 항목이 아니다
        day, mon, year = match.groups()
        try:
            pub = datetime(int(year), _MONTHS[mon], int(day), tzinfo=timezone.utc)
        except ValueError:
            continue

        # 링크: 카드 안의 첫 실제 링크. '#'(앵커)만 있는 카드는 목록 헤더라 건너뛴다.
        href = ""
        for a in card.find_all("a", href=True):
            if a["href"].strip() not in ("#", ""):
                href = urljoin(UKHO_URL, a["href"])
                break
        if not href or href in seen:
            continue
        seen.add(href)

        # 카드 본문은 "<날짜> Search Title <제목> Article description <요약>" 형태다.
        # 'Article description' 을 먼저 경계로 쓰고 나서 UI 문구를 걷어낸다 —
        # 순서를 바꾸면 경계가 사라져 제목과 요약이 붙어버린다.
        after = text[match.end():]
        parts = re.split(r"Article\s+description", after, maxsplit=1,
                         flags=re.IGNORECASE)
        title = _clean(_UI_NOISE.sub(" ", parts[0]))[:120]
        summary = _clean(parts[1])[:400] if len(parts) > 1 else title
        if len(title) < 12:
            continue
        out.append({"title": title, "summary": summary,
                    "link": href, "published": pub})

    print(f"  수집 중: {UKHO_NAME} ... {len(out)}건")
    return out


def fetch_all_official() -> list[dict]:
    """공식 소스 전체. 항목은 {title, summary, link, published(datetime)}."""
    return fetch_ukho()


if __name__ == "__main__":
    for item in fetch_all_official():
        print(f"  {item['published']:%Y-%m-%d} | {item['title'][:70]}")
        print(f"     {item['link']}")
