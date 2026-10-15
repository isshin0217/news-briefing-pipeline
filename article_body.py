"""
article_body.py
기사 링크를 열어 본문을 추출한다. 1_fetch_news.py 가 중복 제거·상한 적용을 마친
뒤(=최종 60건 이내)에만 호출한다.

왜 필요한가 (2026-08-24 실측):
  RSS·네이버 스니펫은 문장 중간에서 끊긴다. '[기고] 센텀2지구' 기사는
  "국제해사기구(IMO)는 지난달 1일부터 자율운항선박(MASS) 코드 적용을..." 에서
  동사째로 잘려 있었고, 5_analyze 의 모델이 그 뒤를 "적용을 시작함"이라고 지어
  한 줄 결론으로 내보냈다. (본문을 열어보니 실제로 "적용을 시작했고"가 맞았지만,
  맞았다는 건 결과론이고 그때 모델에게는 근거가 없었다.)
  요약 길이를 늘려도 이건 해결되지 않는다 — 스니펫 원문 자체가 그 길이다.

설계:
  - 본문은 summary 를 덮어쓰지 않고 별도 필드 body 에 담는다.
    카드에 보이는 요약(summary_ko)과 중복 판정 로직은 그대로 두고,
    LLM 에게 주는 텍스트만 본문으로 바꾸기 위해서다.
  - 번역하지 않는다. 영문 기사는 영문 본문 그대로 둔다 — 분석 모델은 영문을 읽고,
    무료 번역기로 본문 전체를 돌리면 느린 데다 rate limit 에 걸린다.
  - 실패는 정상이다. 못 가져오면 body 를 비워두고, 쓰는 쪽이 스니펫으로 되돌아간다.

주의: 이 스크립트는 주 1회 60건 이하만 읽는다. 도메인당 간격을 두고 순차로 돈다.
"""

import json
import re
import time
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# 본문 저장 상한. 분석 단계는 어차피 앞부분 수백 자만 쓰지만, 여유를 두고 담아둔다.
BODY_MAX_CHARS = 2500
REQUEST_TIMEOUT = 15
PER_DOMAIN_DELAY = 0.8       # 같은 도메인 연속 요청 간 최소 간격(초)
MIN_BODY_CHARS = 200         # 이보다 짧으면 추출 실패로 본다 (메뉴·안내문만 긁힌 것)

# 본문·이미지를 포기하는 호스트. 지금은 비어 있다.
# 2026-09-17 이전에는 news.google.com 이 여기 있었다 — 아래 resolve_google_news 참고.
SKIP_HOSTS: set[str] = set()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 403 일 때 다시 시도하는 정직한 봇 UA (fetch_article 의 2026-10-12 주석 참고)
BOT_UA = "Acme MarineNewsletterBot/1.0 (+newsletter@example.com)"

# ── 구글 뉴스 링크 복원 ────────────────────────────────────────────────────
# 구글 뉴스 RSS 링크(news.google.com/rss/articles/CBMi...)에는 원문 주소가 들어
# 있지 않다. URL 의 base64 를 풀어도, 리다이렉트 페이지 마크업을 뒤져도 없다
# (2026-09-17 재확인: 그냥 GET 하면 200 에 최종 호스트가 news.google.com 그대로고
# 페이지 안에는 구글 자체 자산 URL 만 있다).
#
# 다만 구글 뉴스 페이지가 자기 자바스크립트로 원문을 열 때 쓰는 내부 RPC
# (batchexecute) 는 호출할 수 있다. 기사 페이지에 박혀 있는 서명(data-n-a-sg)과
# 타임스탬프(data-n-a-ts)를 그대로 되돌려주면 원문 주소가 돌아온다.
#
# 왜 이걸 감수하는가 (2026-09-14 메일 실측):
#   뉴스레터에 실린 39건 중 14건(36%)이 구글 뉴스 링크였고, 크롤을 건너뛰니
#   전부 사진이 없었다. 팀에서 "사진 없는 기사가 있다"고 지적받은 세 건이 모두
#   여기에 해당한다. 복원을 붙이면 14건 전부 원문을 찾고 그중 11건이 사진을,
#   11건이 본문을 얻는다(지적받은 세 건은 모두 사진을 얻는다).
#   남는 3건은 safety4sea 2건(우리 UA 에 403)과 newsworks 1건(og:image 자체가 없음)
#   이라 복원과 무관하다 — 이쪽은 메일 빌더의 플레이스홀더가 받는다.
#
# 주의: 문서화되지 않은 비공개 엔드포인트다. 구글이 바꾸면 조용히 실패한다.
# 그래서 실패를 정상 경로로 다룬다 — 빈 문자열을 돌려주고, 호출부는 기존의
# '본문 없음' 과 똑같이 처리한다. 파이프라인은 절대 여기서 멈추지 않는다.
GNEWS_HOST = "news.google.com"
GNEWS_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
GNEWS_ID = re.compile(r"/(?:rss/)?articles/([^?/]+)")
GNEWS_SG = re.compile(r'data-n-a-sg="([^"]+)"')
GNEWS_TS = re.compile(r'data-n-a-ts="([^"]+)"')

_gnews_session: "requests.Session | None" = None


def _gnews_sess() -> "requests.Session":
    """복원용 세션. 쿠키를 물고 가야 배치 호출이 안정적이라 재사용한다."""
    global _gnews_session
    if _gnews_session is None:
        _gnews_session = requests.Session()
        _gnews_session.headers.update({"User-Agent": UA})
    return _gnews_session


def resolve_google_news(url: str) -> str:
    """구글 뉴스 RSS 링크 → 원문 기사 주소. 실패하면 빈 문자열(예외 없음)."""
    m = GNEWS_ID.search(url)
    if not m:
        return ""
    try:
        sess = _gnews_sess()
        _polite_wait(GNEWS_HOST)
        r = sess.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        sg = GNEWS_SG.search(r.text)
        ts = GNEWS_TS.search(r.text)
        if not (sg and ts):
            return ""
        # 구글 뉴스 프런트엔드가 보내는 것과 같은 모양의 요청. 자리표시자("X", 1 …)는
        # 응답에 영향을 주지 않는 로캘·실험 플래그라 고정값으로 둔다.
        payload = ["Fbv4je", json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
              None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            m.group(1), int(ts.group(1)), sg.group(1)])]
        _polite_wait(GNEWS_HOST)
        pr = sess.post(GNEWS_BATCH, data={"f.req": json.dumps([[payload]])},
                       timeout=REQUEST_TIMEOUT,
                       headers={"Content-Type":
                                "application/x-www-form-urlencoded;charset=UTF-8"})
        pr.raise_for_status()
        # 응답은 )]}' 로 시작하는 청크형 JSON 이라 정식 파싱이 번거롭다.
        # 구글이 아닌 첫 번째 주소가 원문이다.
        #
        # 먼저 이스케이프를 푼다. 이걸 빼먹으면 국내 기사가 조용히 깨진다 —
        # 응답이 '=' 를 \\u003d 로 내보내는데, 역슬래시에서 주소를 끊으면
        # pinpointnews.co.kr/news/articleView.html?idxno 처럼 쿼리 값이 날아간
        # 주소가 나오고, 그게 200 을 주는 빈 페이지라 실패로도 안 잡힌다.
        # 국내 언론사 CMS 는 대부분 ?idxno= 로 기사를 가르므로 거의 전부가 해당된다.
        #
        # 역슬래시가 두 겹인 게 핵심이다. 응답이 JSON 안에 JSON 문자열을 담은
        # 구조라 안쪽에서 한 번, 바깥에서 또 한 번 이스케이프된다. 실제 바이트는
        # ?idxno\\u003d486061 (역슬래시 두 개) 이므로 겹친 역슬래시를 먼저 접고
        # 나서 \uXXXX 를 푼다. 순서를 바꾸면 앞 역슬래시가 남아 \= 가 되고,
        # 주소는 여전히 같은 자리에서 잘린다.
        text = pr.text.replace("\\\\", "\\")
        text = re.sub(r"\\u([0-9a-fA-F]{4})",
                      lambda m: chr(int(m.group(1), 16)), text)
        text = text.replace("\\/", "/")
        for cand in re.findall(r'https?://[^\s\\"\']{15,300}', text):
            host = urlparse(cand).netloc
            if host and "google" not in host and "gstatic" not in host:
                return cand
        return ""
    except requests.RequestException:
        return ""
    except Exception:
        # 응답 모양이 바뀌어 파싱이 터져도 뉴스레터는 나가야 한다
        return ""

# 본문 컨테이너 셀렉터. 위에서부터 먼저 맞는 것을 쓴다.
# 국내 언론사 CMS 는 몇 가지 틀을 돌려쓰므로 이 목록이 대부분을 덮는다.
CONTENT_SELECTORS = [
    "#dic_area", "#newsct_article",                 # 네이버 뉴스
    "#article-view-content-div",                    # 다수 지역지(뉴스와이어 계열)
    "#articleBody", "#article_body", "#articleBodyContents",
    ".article_body", ".article-body", ".articleBody",
    ".news_text", ".news_content", ".news_body",    # 아시아투데이 등
    ".view_con", ".view_text", "#textinput",
    ".entry-content", ".post-content", ".td-post-content",   # 워드프레스 계열(해외)
    "article",
]

# 본문에 섞여 들어오는 공유버튼·저작권·기자정보 줄. 줄 단위로 걸러낸다.
JUNK_LINE = re.compile(
    r"(무단\s*전재|재배포\s*금지|저작권자|ⓒ|©|All rights reserved|Copyright"
    r"|URL\s*복사|글씨\s*키우기|글자\s*크기|프린트|메일\s*보내기|기사\s*공유"
    r"|페이스북|카카오톡|네이버\s*밴드|트위터|텔레그램|공유하기|스크랩"
    r"|구독하기|구독\s*신청|좋아요|댓글|관련\s*기사|많이\s*본|인기\s*기사"
    r"|Subscribe|Share this|Related articles|Follow us|Sign up)",
    re.IGNORECASE)

# 기자 서명 줄 ("홍길동 기자 hong@xxx.co.kr")
BYLINE_LINE = re.compile(r"^[^\s]{2,10}\s*기자\s*[\w.\-]+@[\w.\-]+$")

_last_hit: dict[str, float] = {}


def _polite_wait(host: str) -> None:
    """같은 도메인에 연속으로 붙지 않도록 간격을 둔다."""
    prev = _last_hit.get(host)
    if prev is not None:
        gap = PER_DOMAIN_DELAY - (time.monotonic() - prev)
        if gap > 0:
            time.sleep(gap)
    _last_hit[host] = time.monotonic()


def _link_density(node) -> float:
    """전체 글자 중 링크 글자의 비율. 본문은 낮고 메뉴·목록은 높다."""
    text = node.get_text(strip=True)
    if not text:
        return 1.0
    anchor = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
    return anchor / len(text)


def _best_block(soup) -> str:
    """셀렉터로 못 찾았을 때 쓰는 일반 휴리스틱.

    글자 수가 많으면서 링크 비율이 낮은 블록을 고른다. 그냥 '가장 긴 블록'을
    고르면 페이지 전체를 감싼 #wrap 이 뽑히므로 링크 비율로 눌러준다.
    """
    best, best_score = None, 0.0
    for node in soup.find_all(["div", "section", "article", "td"]):
        text = node.get_text(strip=True)
        if len(text) < MIN_BODY_CHARS:
            continue
        score = len(text) * (1.0 - _link_density(node))
        if score > best_score:
            best, best_score = node, score
    return best.get_text("\n", strip=True) if best is not None else ""


def _clean(text: str) -> str:
    """줄 단위로 공유버튼·저작권·기자서명 같은 껍데기를 걷어낸다."""
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        # 짧은 줄은 대부분 버튼·라벨이다. 본문 문단은 이보다 길다.
        if len(line) < 12:
            continue
        if JUNK_LINE.search(line) or BYLINE_LINE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()[:BODY_MAX_CHARS]


def extract(html: str) -> str:
    """기사 HTML 에서 본문 텍스트를 뽑는다. 실패하면 빈 문자열."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "aside", "form", "figure", "iframe", "noscript"]):
        tag.decompose()

    for sel in CONTENT_SELECTORS:
        for node in soup.select(sel):
            body = _clean(node.get_text("\n", strip=True))
            if len(body) >= MIN_BODY_CHARS:
                return body

    return _clean(_best_block(soup))


# ── 대표 이미지 ──────────────────────────────────────────────────────────
# 2026-09-07 피드백: 경쟁사 동향을 '썸네일 + 태그 + 제목' 카드로 바꾸자는 요청.
# 본문을 긁을 때 페이지를 이미 파싱하고 있으므로, 같은 응답에서 대표 이미지도 뽑는다
# (요청을 더 보내지 않는다). 본문 크롤이 실패하는 기사는 이미지도 없다 —
# 렌더링 쪽에서 이미지 없는 카드를 견디게 만들어야 한다.
IMAGE_META = [
    ("meta", {"property": "og:image"}),
    ("meta", {"property": "og:image:url"}),
    ("meta", {"name": "twitter:image"}),
    ("meta", {"name": "twitter:image:src"}),
]
# 로고·아이콘·1픽셀 추적자를 거른다. 크기를 알려면 이미지를 또 받아야 하므로
# URL 에 드러난 힌트만 본다.
IMAGE_JUNK = re.compile(r"(logo|icon|favicon|blank|spacer|1x1|pixel|default|noimg|no_image)",
                        re.I)


def extract_image(soup, base_url: str) -> str:
    """기사 대표 이미지 URL. 없으면 빈 문자열."""
    for tag, attrs in IMAGE_META:
        node = soup.find(tag, attrs=attrs)
        url = (node.get("content") or "").strip() if node else ""
        if not url:
            continue
        url = urljoin(base_url, url)
        if not url.startswith(("http://", "https://")) or IMAGE_JUNK.search(url):
            continue
        return url
    return ""


def fetch_article(url: str) -> tuple[str, str]:
    """기사 한 건의 (본문, 대표 이미지). 실패는 빈 문자열로 돌려준다(예외 없음)."""
    if not url:
        return "", ""
    host = urlparse(url).netloc
    if not host or host in SKIP_HOSTS:
        return "", ""
    # 구글 뉴스 링크는 원문 주소로 되돌린 뒤에 긁는다. 이미 복원된 주소를 받은
    # 경우(add_bodies)는 여기 걸리지 않으므로 두 번 복원하지 않는다.
    if host == GNEWS_HOST:
        url = resolve_google_news(url)
        host = urlparse(url).netloc if url else ""
        if not host or host in SKIP_HOSTS:
            return "", ""
    _polite_wait(host)
    headers = {"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}
    try:
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.SSLError:
            # 인증서가 만료된 매체가 있다(2026-10-06 실측: 해사신문 haesanews.com).
            # 공개 기사 본문을 읽기만 하므로 검증 없이 한 번 더 시도한다 — 그러지 않으면
            # 그 매체 기사는 본문이 늘 비어 리드 기반 태그 보완(retag_from_body)도 못 받는다.
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, verify=False)
        if r.status_code == 403:
            # 일부 해외 전문지는 '브라우저인 척하는' 요청을 막고, 자기를 밝힌 봇은 통과시킨다
            # (서버 IP 에서 온 브라우저 UA 를 스크레이퍼로 본다). 실측(2026-10-12, 피드백):
            #                         Chrome UA   UA 없음   봇 UA
            #   Smart Maritime Network   403        403      200
            #   The Digital Ship         403        403      200
            #   전자신문·한국일보          200        200      200
            # 그 매체 기사는 본문·사진이 늘 비어 카드가 회색 상자로 나갔다.
            r = requests.get(url, headers={**headers, "User-Agent": BOT_UA},
                             timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        # 인코딩 헤더가 없는 국내 사이트가 있어 본문 기준으로 재판정한다
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        # 이미지는 원본 soup 에서 뽑는다 — extract() 가 태그를 decompose 하기 전에.
        image = extract_image(BeautifulSoup(r.text, "html.parser"), r.url)
        return extract(r.text), image
    except requests.RequestException:
        return "", ""
    except Exception:
        # 파서가 이상한 마크업에서 터져도 파이프라인을 멈추지 않는다
        return "", ""


def fetch_body(url: str) -> str:
    """본문만 필요한 호출부를 위한 얇은 래퍼."""
    return fetch_article(url)[0]


def add_bodies(articles: list[dict]) -> tuple[int, int]:
    """기사 목록에 body / image 필드를 채운다(제자리 수정).

    반환: (본문 성공 건수, 시도 건수) — 이미지는 있으면 덤이라 집계에 넣지 않는다.
    """
    targets = [a for a in articles if urlparse(a.get("link", "")).netloc not in SKIP_HOSTS]
    ok = img_ok = 0
    gnews_tried = gnews_ok = 0
    for i, art in enumerate(articles, 1):
        link = art.get("link", "")
        if urlparse(link).netloc in SKIP_HOSTS:
            art["body"] = ""
            art["image"] = ""
            continue
        # 구글 뉴스는 여기서 먼저 복원한다. fetch_article 에 맡겨도 되지만,
        # 복원 성공/실패를 집계에 남기려면 결과를 직접 봐야 한다.
        mark_prefix = ""
        if urlparse(link).netloc == GNEWS_HOST:
            gnews_tried += 1
            real = resolve_google_news(link)
            if not real:
                art["body"] = ""
                art["image"] = ""
                print(f"  [{i}/{len(articles)}] 구글뉴스 복원실패 "
                      f"- {art.get('title', '')[:45]}")
                continue
            gnews_ok += 1
            # 원문 주소는 참고용으로만 남긴다. link 를 바꾸면 이미 끝난 중복 제거의
            # 기준이 흔들리고 메일 링크도 함께 바뀌어, 이번 수정 범위를 넘는다.
            art["link_original"] = real
            link = real
            mark_prefix = "복원 "
        body, image = fetch_article(link)
        art["body"] = body
        art["image"] = image
        if body:
            ok += 1
        if image:
            img_ok += 1
        mark = f"{len(body)}자" if body else "실패"
        mark += " +이미지" if image else ""
        print(f"  [{i}/{len(articles)}] {mark_prefix}{mark} "
              f"- {art.get('title', '')[:45]}")
    print(f"  본문 {ok}/{len(targets)}건 · 대표 이미지 {img_ok}건")
    # 구글 뉴스는 예전에 집계에서 통째로 빠져 있었다. 36% 가 사진 없이 나가도
    # 로그만 봐서는 알 수 없었던 원인이라 따로 찍는다 (2026-09-14 건).
    if gnews_tried:
        print(f"  구글뉴스 링크 {gnews_tried}건 중 원문복원 {gnews_ok}건")
    return ok, len(targets)


# ── 본문 날짜 대조 ────────────────────────────────────────────────────────
# 피드가 주는 pubDate 를 못 믿는 경우가 있다. Google News RSS 는 기사량이 적은
# 검색어의 정원을 옛 기사로 채우면서 재색인 시점을 pubDate 로 찍어 보낸다
# (실측 2026-08-24: 2019년 기사에 'Thu, 20 Aug 2026' → 7일 필터 통과).
# 본문을 확보한 기사에 한해, 기사 머리에 찍힌 발행 일시와 피드 날짜를 대조한다.
#
# 주의: 본문 어디서나 날짜를 찾으면 안 된다. "2025년 부산항은 2488만 TEU를 처리했으며"
# 같은 본문 속 연도가 발행일로 오인된다. 그래서 (1) 기사 머리 300자만 보고,
# (2) 연·월·일이 모두 갖춰진 날짜만 인정한다. 이 두 조건이면 본문 서술형 연도는
# 거의 걸리지 않는다(실측: 본문 보유 43건 중 오탐 0건).
BODY_HEAD_CHARS = 300
STALE_TOLERANCE_DAYS = 30    # 이만큼 이상 과거면 '옛 기사'로 본다
FULL_DATE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def body_publish_date(body: str) -> date | None:
    """기사 머리에서 발행 일시로 보이는 날짜를 찾는다. 없으면 None."""
    found = []
    for y, mo, d in FULL_DATE.findall(body[:BODY_HEAD_CHARS]):
        try:
            found.append(date(int(y), int(mo), int(d)))
        except ValueError:
            continue          # 2026.13.45 같은 오탐
    return min(found) if found else None


def drop_stale_by_body(articles: list[dict]) -> list[dict]:
    """본문 발행일이 피드 날짜보다 한참 과거인 기사를 걸러낸다.

    본문이 없는 기사는 대조할 근거가 없으므로 그대로 둔다.

    2026-09-17: 구글 뉴스 링크도 원문을 복원해 본문을 갖게 되면서 이 검사를 받는다.
    위 주석이 적어둔 '재색인 시점을 pubDate 로 찍는' 출처가 바로 구글 뉴스인데,
    정작 그 기사들은 본문이 없어 여태 검사에서 빠져 있었다. 9/14 자료로 확인해보니
    14건 전부 통과해 이번 주 결과는 달라지지 않았지만, 앞으로는 걸릴 수 있다.
    """
    kept = []
    for art in articles:
        body, published = art.get("body") or "", art.get("published") or ""
        if not body or not published:
            kept.append(art)
            continue
        real = body_publish_date(body)
        if real is None:
            kept.append(art)
            continue
        try:
            feed = datetime.fromisoformat(published).astimezone(timezone.utc).date()
        except ValueError:
            kept.append(art)
            continue
        gap = (feed - real).days
        if gap > STALE_TOLERANCE_DAYS:
            print(f"  [옛 기사 제외] 피드 {feed} vs 본문 {real} ({gap}일 차) "
                  f"- {art.get('title', '')[:45]}")
            continue
        kept.append(art)
    return kept


# ── 같은 토픽 안에서 겹치는 기사 솎아내기 ──────────────────────────────
# 2026-08-25 피드백
#
# 왜 기존 중복 판정으로 안 걸리나: 제목 토큰도 본문 유사도도 구간이 겹친다(실측).
#   팬스타 출항 4건 서로:      본문 겹침 0.19~0.36
#   '기후변화의 역설'(유지 대상): 0.22    ← 같은 구간
# 임계값 하나로는 "같은 사건"과 "같은 주제 다른 각도"를 못 가른다.
#
# 그래서 '제거'가 아니라 '다양성 선택'으로 푼다. 점수 상위부터 담되, 이미 담은 기사와
# 본문이 많이 겹치면 건너뛰고 다음 각도를 집는다. 판정이 틀려도 기사가 사라지는 게
if __name__ == "__main__":
    # 단독 실행: raw_news.json 에 body 를 채워 넣는다
    import json
    from pathlib import Path

    path = Path("raw_news.json")
    news = json.loads(path.read_text(encoding="utf-8"))
    print(f"\n본문 수집 시작 - {len(news)}건\n")
    ok, tried = add_bodies(news)
    path.write_text(json.dumps(news, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n본문 수집 완료: {ok}/{tried}건 (구글뉴스 링크 {len(news) - tried}건은 대상 외)")
