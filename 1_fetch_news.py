"""
1_fetch_news.py
업계 뉴스 RSS/검색 수집 스크립트 (검색어·키워드는 profile.json)
실행: python 1_fetch_news.py
결과: raw_news.json 저장
"""

import feedparser
import json
import os
import re
import requests
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

from profile_loader import PROFILE, QUERIES, KEYWORDS as KW, CUSTOMERS, COMPETITORS as COMPETITOR_LIST

# 네이버 검색 API 자격증명 (developers.naver.com 에서 발급, 없으면 네이버 수집 생략)
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")


def gnews(query: str, lang: str = "ko") -> str:
    """Google News RSS 검색 피드 URL 생성."""
    q = quote(query)
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# Google News / 네이버 검색어 (주제/규제/경쟁사) — profile.json 의 queries 에서 로드
GOOGLE_QUERIES_KO = QUERIES.get("topic_ko", [])
GOOGLE_QUERIES_EN = QUERIES.get("topic_en", [])
COMPLIANCE_QUERIES_KO = QUERIES.get("compliance_ko", [])
COMPLIANCE_QUERIES_EN = QUERIES.get("compliance_en", [])
COMPETITOR_QUERIES_KO = QUERIES.get("competitor_ko", [])
COMPETITOR_QUERIES_EN = QUERIES.get("competitor_en", [])

# 고객사 이름도 자동으로 검색어에 포함 (고객사 소식을 놓치지 않도록)
GOOGLE_QUERIES_EN = GOOGLE_QUERIES_EN + [c["name_en"] for c in CUSTOMERS if c.get("name_en")]

# ── 수집할 RSS 소스 목록 ──────────────────────────────────────────────────
STATIC_SOURCES = PROFILE.get("sources", [])

# 정적 소스 + Google News 검색 피드(한/영) 를 합쳐 최종 SOURCES 구성
def _gfeeds(queries, lang):
    return [{"name": f"Google News({lang.upper()}) - {q}", "url": gnews(q, lang), "lang": lang} for q in queries]

SOURCES = (
    STATIC_SOURCES
    + _gfeeds(GOOGLE_QUERIES_KO, "ko") + _gfeeds(GOOGLE_QUERIES_EN, "en")
    + _gfeeds(COMPLIANCE_QUERIES_KO, "ko") + _gfeeds(COMPLIANCE_QUERIES_EN, "en")
    + _gfeeds(COMPETITOR_QUERIES_KO, "ko") + _gfeeds(COMPETITOR_QUERIES_EN, "en")
)

# 수집 기간
DAYS_BACK = 7
CUTOFF = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)

# 중복 제거 후 최대 보존 기사 수 (번역 부하/rate-limit 보호).
# 관련성 점수 상위 N건만 남기므로, 값을 키우면 더 많은 기사가 뉴스레터에 실림.
MAX_ARTICLES = 80

# 키워드 (profile.json 의 keywords 에서 로드)
KEYWORDS = KW.get("general", [])              # 일반 키워드 (+1, 수집 필터)
CORE_RELEVANCE_KEYWORDS = KW.get("core", [])    # 핵심어: 제품/사업 직결 (+3)
RELATED_RELEVANCE_KEYWORDS = KW.get("related", [])  # 인접어: 사업 인접 (+2)
# AI 접목 신호: 두 그룹이 한 기사에 함께 있으면 결합 보너스(+3)
AI_SIGNAL_KEYWORDS = KW.get("ai_signal", [])
AI_TARGET_KEYWORDS = KW.get("ai_target", [])

# 저품질/사내소식 노이즈 키워드 (점수 감점 → 뒤로 밀림)
NOISE_KEYWORDS = [
    "사옥", "이전", "인사발령", "임원인사", "주주총회", "주가", "증시", "실적발표",
    "부고", "동정", "채용", "창립", "기념식", "후원", "기부", "봉사", "표창",
    # 인물 프로필 / 인사 / PR·홍보성 (연관성 낮음 → 감점 강화)
    "who is", "[인사]", "인사]", "승진", "취임", "선임", "내정", "위촉",
    "회장]", "부회장]", "사장]", "[게시판]", "게시판", "수여식", "간담회",
    "매거진", "발간", "출판", "신년사", "특징주",
    "obituary", "appointment", "share price", "dividend", "quarterly results",
]

# 순수 투자/주가성 뉴스 (투자자 대상 → 기술 브리핑과 무관 → 제외).
# 주의: 매각·인수·합병 등 M&A 용어는 넣지 않음 (고객사 매각 등 중요 소식이라 통과시켜야 함)
STOCK_NOISE_KEYWORDS = [
    "주가", "증시", "코스피", "코스닥", "목표주가", "목표가",
    "배당", "시가총액", "공매도", "실적발표", "어닝", "투자자", "자사주",
    "유상증자", "무상증자", "현금 요새", "밸류업", "저평가",
    "투자의견", "매수의견", "매도의견", "상한가", "하한가", "특징주",
    "dividend", "earnings", "share price", "shareholder",
    "target price", "stock market", "market cap", "ipo",
]


def is_stock_noise(text: str) -> bool:
    """순수 투자/주가성 기사인지 판별 → True면 수집에서 제외 (M&A는 제외 대상 아님)."""
    lower = text.lower()
    return any(kw in lower for kw in STOCK_NOISE_KEYWORDS)


def clean_html(text: str) -> str:
    # 태그 제거 후 HTML 엔티티(&nbsp; &amp; &#39; 등) 디코딩
    return unescape(re.sub(r"<[^>]+>", "", text or "")).replace("\xa0", " ").strip()


def parse_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def matches_keywords(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in KEYWORDS)


def normalize_title(title: str) -> str:
    """중복 판정을 위한 제목 정규화 (소문자화 + 영숫자·한글만 남김)."""
    t = title.lower()
    t = re.sub(r"[^0-9a-z가-힣]+", "", t)
    return t


def relevance_score(text: str, customer: str | None) -> int:
    """관련성 점수 (제품 밀착도 기준 차등).
    핵심어 +3 / 인접어 +2 / 일반어 +1, AI×스마트십·선박관리 결합 +3, 노이즈 -4."""
    lower = text.lower()
    score = 0
    if customer:
        score += 5
    # 제품 직결 핵심어(+3) — 밀착도 높은 주제를 확실히 상위로
    score += sum(3 for kw in CORE_RELEVANCE_KEYWORDS if kw in lower)
    # 사업 인접어(+2)
    score += sum(2 for kw in RELATED_RELEVANCE_KEYWORDS if kw in lower)
    # 일반 키워드(+1)
    score += sum(1 for kw in KEYWORDS if kw in lower)
    # AI 접목 결합 보너스: AI 신호 + (스마트십/선박관리/자율운항 등) 대상이 함께 있을 때만
    if any(k in lower for k in AI_SIGNAL_KEYWORDS) and any(k in lower for k in AI_TARGET_KEYWORDS):
        score += 3
    # 노이즈(인물·인사·PR·주식성) 감점 강화(-4)
    score -= sum(4 for kw in NOISE_KEYWORDS if kw in lower)
    return score


# 고객사 감지 규칙 (키워드 그룹 → 표기명) — profile.json 의 customers 에서 로드
CUSTOMER_RULES: list[tuple[list[str], str]] = [
    ([a.lower() for a in c.get("aliases", [])] or [c["name"].lower()], c["name"]) for c in CUSTOMERS
]

def detect_customer(text: str) -> str | None:
    """고객사 감지 → 표기명 반환. 여러 고객사가 등장하면 '가장 먼저 등장한' 회사 선택.
    제목에 적용하면 한국어 헤드라인 특성상(주어가 맨 앞) 기사 주제와 배지가 일치한다.
    거래 상대로 스쳐 언급된 회사(뒤쪽 등장)에 잘못 매칭되지 않는다."""
    lower = text.lower()
    best_name, best_pos = None, len(lower) + 1
    for kws, name in CUSTOMER_RULES:
        pos = min((lower.find(k) for k in kws if k in lower), default=-1)
        if 0 <= pos < best_pos:
            best_pos, best_name = pos, name
    return best_name


# 경쟁사 감지 (키워드 → 표기명) — profile.json 의 competitors 에서 로드
COMPETITORS = [
    ([a.lower() for a in c.get("aliases", [])] or [c["name"].lower()], c["name"]) for c in COMPETITOR_LIST
]

# 글로벌 컴플라이언스(규제/인증 등) 감지 키워드
COMPLIANCE_KEYWORDS = KW.get("compliance", [])

def detect_competitor(text: str) -> str | None:
    """경쟁사 키워드 감지 → 경쟁사명 반환"""
    lower = text.lower()
    for kws, name in COMPETITORS:
        if any(kw in lower for kw in kws):
            return name
    return None


def is_compliance(text: str) -> bool:
    """IMO/선급 등 글로벌 컴플라이언스 소식인지 판별"""
    lower = text.lower()
    return any(kw in lower for kw in COMPLIANCE_KEYWORDS)


def make_article(title, summary, link, pub, lang, source):
    """공통 기사 생성기: 필터/태깅/점수 적용 후 dict 반환 (제외 시 None)."""
    title = (title or "").strip()
    summary = (summary or "").strip()
    if not title:
        return None
    full_text = title + " " + summary

    # 순수 투자/주가성 기사 제외 (매각·인수 등 M&A는 필터 대상에서 뺐으므로 통과됨)
    if is_stock_noise(full_text):
        return None

    # 태깅: 고객사 > 경쟁사 > 컴플라이언스 (우선순위대로 하나만)
    # 고객사 배지는 반드시 '제목'에서만 감지 → 카드 제목과 배지가 항상 일치.
    # (본문에만 등장하는 거래 상대에 잘못 태깅되던 문제 방지.)
    customer = detect_customer(title)
    competitor = None if customer else detect_competitor(full_text)
    compliance = bool(not customer and not competitor and is_compliance(full_text))

    # 키워드 필터 (일반 키워드 OR 고객사/경쟁사/컴플라이언스)
    if not matches_keywords(full_text) and not customer and not competitor and not compliance:
        return None

    score = (relevance_score(full_text, customer)
             + (3 if competitor else 0) + (2 if compliance else 0))
    return {
        "source": source,
        "lang": lang,
        "title": title,
        "summary": summary[:800],
        "link": link,
        "published": pub.isoformat() if pub else "",
        "customer": customer,        # 고객사명 또는 None
        "competitor": competitor,    # 경쟁사명 또는 None
        "compliance": compliance,    # 컴플라이언스 여부
        "relevance_score": score,
    }


def fetch_naver(queries) -> list[dict]:
    """네이버 검색 API로 한국어 뉴스 수집 (자격증명 없으면 생략)."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("  (네이버 API 키 없음 → 네이버 수집 생략)")
        return []
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID,
               "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    out = []
    for q in queries:
        print(f"  수집 중: 네이버 - {q} ...", end=" ")
        try:
            r = requests.get("https://openapi.naver.com/v1/search/news.json",
                             params={"query": q, "display": 30, "sort": "date"},
                             headers=headers, timeout=15)
            r.raise_for_status()
            count = 0
            for it in r.json().get("items", []):
                title = clean_html(it.get("title", ""))
                summary = clean_html(it.get("description", ""))
                link = it.get("link") or it.get("originallink", "")
                try:
                    pub = parsedate_to_datetime(it.get("pubDate", "")).astimezone(timezone.utc)
                except Exception:
                    pub = None
                if pub and pub < CUTOFF:
                    continue
                art = make_article(title, summary, link, pub, "ko", "네이버뉴스")
                if art:
                    out.append(art)
                    count += 1
            print(f"{count}건")
        except Exception as e:
            print(f"오류: {e}")
    return out


def fetch_all() -> list[dict]:
    articles = []
    for src in SOURCES:
        print(f"  수집 중: {src['name']} ...", end=" ")
        try:
            feed = feedparser.parse(src["url"])
            count = 0
            for entry in feed.entries:
                pub = parse_date(entry)
                if pub and pub < CUTOFF:
                    continue
                art = make_article(
                    clean_html(entry.get("title", "")),
                    clean_html(entry.get("summary", entry.get("description", ""))),
                    entry.get("link", ""), pub, src["lang"], src["name"])
                if art:
                    articles.append(art)
                    count += 1
            print(f"{count}건")
        except Exception as e:
            print(f"오류: {e}")

    # 네이버 검색 API 수집 (한국어 쿼리 재사용)
    articles += fetch_naver(GOOGLE_QUERIES_KO + COMPLIANCE_QUERIES_KO + COMPETITOR_QUERIES_KO)

    return dedup(articles)


def clean_title_for_dup(title: str) -> str:
    """근접 중복 판정 전 제목에서 언론사명/부제를 제거해 핵심만 남긴다.
    (대괄호 안에 핵심 엔티티가 들어있는 경우가 많아 내용은 보존)"""
    t = title
    if " - " in t:               # Google News "제목 - 언론사" 꼬리 제거
        t = t.rsplit(" - ", 1)[0]
    t = re.split(r"[…]|\.\.\.", t)[0]   # 말줄임 부제 이후 제거
    return t


def title_tokens(title: str) -> set:
    """제목을 문자 바이그램 집합으로 변환 (한국어 조사/동의어에 강건한 근접 중복 판정용)."""
    t = re.sub(r"[^0-9a-z가-힣]", "", clean_title_for_dup(title).lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def is_near_duplicate(tokens: set, kept_tokens: list[set]) -> bool:
    """같은 사건의 다른 헤드라인인지 판정. overlap(포함율) 또는 jaccard 기준."""
    if len(tokens) < 5:          # 너무 짧은 제목은 판정에서 제외
        return False
    for kt in kept_tokens:
        if len(kt) < 5:
            continue
        inter = len(tokens & kt)
        overlap = inter / min(len(tokens), len(kt))
        jaccard = inter / len(tokens | kt)
        if overlap >= 0.5 or jaccard >= 0.4:
            return True
    return False


def dedup(articles: list[dict]) -> list[dict]:
    """제목/링크 완전일치 + 제목 토큰 유사도 기반 근접 중복 제거.
    관련성 점수가 높은 기사를 대표로 보존한다."""
    # 점수 높은 순으로 먼저 정렬해야 중복 그룹에서 대표 기사가 살아남음
    articles.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    seen_titles: set[str] = set()
    seen_links: set[str] = set()
    kept_tokens: list[set] = []
    unique = []
    for art in articles:
        key = normalize_title(art["title"])
        link = art.get("link", "")
        if key and key in seen_titles:
            continue
        if link and link in seen_links:
            continue
        tokens = title_tokens(art["title"])
        if is_near_duplicate(tokens, kept_tokens):
            continue

        seen_titles.add(key)
        if link:
            seen_links.add(link)
        kept_tokens.append(tokens)
        unique.append(art)
    # 관련성 상위 MAX_ARTICLES건만 보존 (unique는 점수 내림차순 상태)
    unique = unique[:MAX_ARTICLES]
    # 최종 출력은 최신순
    unique.sort(key=lambda x: x["published"], reverse=True)
    return unique


if __name__ == "__main__":
    print(f"\n[1단계] 뉴스 수집 시작 (최근 {DAYS_BACK}일)\n")
    news = fetch_all()

    customer_news = [a for a in news if a.get("customer")]
    general_news = [a for a in news if not a.get("customer")]

    out = Path("raw_news.json")
    out.write_text(json.dumps(news, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {len(news)}건 수집 완료 → {out}")
    print(f"  일반 뉴스: {len(general_news)}건")
    print(f"  고객사 관련 뉴스: {len(customer_news)}건")
    if customer_news:
        from collections import Counter
        counts = Counter(a["customer"] for a in customer_news)
        for name, cnt in counts.most_common():
            print(f"    - {name}: {cnt}건")
