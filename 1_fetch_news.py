"""
1_fetch_news.py
해사/쉬핑 뉴스 RSS 수집 스크립트
실행: python 1_fetch_news.py
결과: raw_news.json 저장
"""

import feedparser
import json
import os
import re
import requests
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv

import keyword_match as km
import official_sources
from article_body import add_bodies, drop_stale_by_body
from event_dedup import merge_same_event

load_dotenv()

# 네이버 검색 API 자격증명 (developers.naver.com 에서 발급, 없으면 네이버 수집 생략)
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")


# 수집 기간. gnews() 가 검색어에 붙이므로 이 파일 위쪽에 둔다.
DAYS_BACK = 7


def gnews(query: str, lang: str = "ko") -> str:
    """Google News RSS 검색 피드 URL 생성.

    검색어에 'when:7d' 를 붙인다(2026-08-24). 구글이 기사량이 적은 검색어의 100건
    정원을 옛 기사로 채우는 것을 서버 쪽에서 막는 게 목적이다. 우리 7일 필터는
    구글이 준 pubDate 를 믿을 수밖에 없는데, 그 값이 재색인 시점으로 찍혀 오는
    경우가 있어 옛 기사가 통과했다(2019년 대우조선해양 기사 사례).

    실측(2026-08-24) — 검색어 '대우조선해양':
        기본     100건 (2026:15, 2025:27, 2024:5, 2023:32, 2022:20, 2021:1)
        when:7d   37건 (2026:37)
    기사량이 충분한 검색어는 손실이 없다('북극항로' 92건 → 100건, 'HD현대중공업' 100건 유지).
    """
    q = quote(f"{query} when:{DAYS_BACK}d")
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# Google News 검색어 (주제/제품/경쟁사/규제/고객사) - 최대한 넓게 수집
#
# 중요: Google News RSS 와 네이버 검색 API 모두 공백을 AND 로 해석한다.
# 회사 검색어에 한정어를 붙이면(예: "HMM 현대상선") 그 단어가 없는 기사는 통째로
# 빠진다. 실측(2026-08-10): 'HMM, 전방위 AI 이식'(뉴스저널리즘 2026-08-06)이
# 본문에 '현대상선'이 없어 누락됐다. 점수를 매겼다면 25점으로 HMM 최상위였다.
# 부작용도 있었다 — "HMM 현대상선" 결과 100건 중 8월 기사는 16건뿐(옛 기사가
# 피드 정원을 잠식)이었으나 "HMM" 단독은 94건이었다.
# 따라서 이름만으로 식별되는 회사는 단독어로 검색한다.
# 다만 일반명사와 겹치는 짧은 이름(ExampleLine4 등)은 한정어를 유지한다 — 오수집이 더 크다.
GOOGLE_QUERIES_KO = [
    # 직접 고객 해운사
    # "현대상선"은 검색어에서 뺐다(2026-08-24). 2020년 4월 HMM 으로 사명이 바뀌어,
    # 이 이름으로 잡히는 기사는 전부 2020년 이전 것이다. 최근 기사가 "HMM(옛 현대상선)"
    # 처럼 병기하는 경우는 "HMM" 검색어가 이미 잡으므로 커버리지 손실도 없다.
    # (감지용 키워드로는 CUSTOMER_RULES 에 그대로 남겨둔다 — 병기 기사 태깅에 쓰인다.)
    "HMM", "에이치라인해운", "현대LNG해운", "Ocean Network Express",
    "HMM오션서비스", "예시해운1", "예시해운2", "예시해양기술", "ExampleLine3", "ExampleLine4 해운",
    # ── 일회성 검색어: HMM-발레(Vale) 철광석 장기운송계약 ──────────────────
    # 2026-09-08 요청: 이 건은 9/14 뉴스레터에 반드시 실어야 한다.
    #
    # "HMM" 단독 검색만으로는 놓칠 수 있다. 위 주석대로 구글·네이버 모두 100건
    # 상한이라 "HMM" 처럼 기사량이 많은 검색어는 실질적으로 최근 3~5일치만 덮는다.
    # 계약 기사가 7일 창의 앞부분(월요일 빌드 기준 9/8~9/9)에 나오면 그 100건 밖으로
    # 밀려난다. 한정어를 붙이면 결과가 적어 창 전체를 덮는다 — 아래
    # _NARROWING_TOPICS(HMM AI 등)와 같은 원리이고, 단독 "HMM" 은 그대로 두므로
    # 커버리지는 줄지 않고 더해지기만 한다.
    #
    # 표기를 둘 다 둔다: 국문 매체가 '발레'로도 'Vale'로도 쓴다. 공백은 AND 이므로
    # 한 줄로 합치면("HMM 발레 Vale") 두 표기를 모두 쓴 기사만 잡혀 오히려 좁아진다.
    #
    # 점수는 손대지 않았다. 장기운송계약은 화주·용선 소식이라 항법·선박관리 제품과
    # 접점이 없고(LINER_SERVICE_KEYWORDS 와 같은 성격), 이번 건만 예외로 싣는
    # 것이므로 관련성 키워드에는 넣지 않는다. 고객사 태그(+6)로 9점 안팎이면
    # 고객사 동향 카드에는 오른다(회사당 1건 규칙). 다만 같은 주에 점수가 더 높은
    # HMM 기사가 있으면 카드는 그쪽이 가져가고 이 기사는 전체 뉴스 목록에 실린다.
    #
    # 발레 건이 나가고 나면 이 두 줄은 지워도 된다.
    "HMM 발레", "HMM Vale",
    # 관련 해운사 (직접 고객 아님)
    "팬오션", "대한해운", "SK해운", "폴라리스쉬핑", "장금상선", "현대글로비스",
    "KSS해운", "KLCSM", "시도상선", "동원산업",
    # 극지·특수선 (쇄빙연구선 — 항법·통합브리지 수요처)
    "극지연구소 쇄빙연구선", "차세대 쇄빙연구선", "쇄빙선 건조",
    # 북극항로 (2026-08-24 피드백: 관련 기사가 거의 안 잡힌다는 지적).
    # 그전에는 전용 검색어가 없어, 다른 검색어에 우연히 딸려 오는 기사만 들어왔다.
    # 실측: 이번 주 56건 중 북극항로가 제목에 있는 기사는 인재양성 기사 1건뿐이었는데,
    # 정작 '한국 컨테이너선 첫 북극항로 시범운항'(팬스타 아크로호 부산신항 출항)이라는
    # 큰 기사가 통째로 빠져 있었다. 항로가 열리면 해도(ENC)·항법·빙해 항행 지원 수요가
    # 직결되므로 전용 검색어를 둔다.
    #
    # 검색어를 3개로 줄였다. 실측에서 '북극항로'·'북극항로 개척'·'북극해 항로'·
    # '극지 운항 선박'이 사실상 같은 기사 묶음을 돌려줬다(단독어만으로 7일내 83건).
    # 지금은 기사량이 많은 주제라 단독어 + 좁힌 검색어 하나면 충분하다.
    "북극항로", "북극항로 시범운항", "북극해 항로",
    # 주요 조선소
    "HD현대중공업", "HD한국조선해양", "한화오션", "삼성중공업",
    "HJ중공업", "케이조선",
    # 플랫폼팀/솔루션팀 관심 기술 (자율운항·스마트십·선박관리·항법)
    "자율운항 선박", "스마트십 플랫폼", "전자해도", "선박관리 소프트웨어",
    "IMO 자율운항 규제", "선박 암모니아 추진", "선박 메탄올 추진", "선박 탈탄소 규제",
    "아비커스", "선박 자율운항 실증", "선박 통합항해시스템", "신조선 발주",
    # AI × 스마트십 / 선박관리(해운 ERP) 접목 모니터링
    # 해운사의 AI 도입 기사는 '스마트십·자율운항' 같은 표준 용어 대신 '항로 최적화·
    # 화주 챗봇'처럼 자유롭게 쓰는 경우가 많아, 업종어 + AI 조합도 함께 둔다.
    "AI 스마트십", "AI 선박관리 솔루션", "AI 해운 ERP", "인공지능 자율운항 선박",
    "해운 인공지능 도입", "선사 AI 전환",
    # AI·AX × 조선/해양/방산 (2026-10-06 피드백). 위 검색어는 해운·선박관리 쪽뿐이라
    # 조선·해양 산업의 AX 기사는 검색 결과에 아예 없었다. 실측: '조선기자재도 AI 전환
    # 시대…국립한국해양대 집결'(해사신문 9/30)은 어느 검색어로도 수집되지 않았다.
    # '방산 AX'는 넣지 않는다 — 실측(2026-10-06) 육군·제조·대학 기사만 돌아왔다.
    "조선 AX", "조선 AI 전환", "해양 AX", "해운 AX", "함정 AI",
    # 자사(아크메) — 전용 검색어가 없었다(2026-09-01 피드백).
    # OWN_COMPANY_KEYWORDS 는 '들어온 기사에 배지를 다는' 감지용일 뿐이라, 우리 회사
    # 기사를 실제로 가져오는 경로가 어디에도 없었다. 다른 검색어에 우연히 딸려 오는 것
    # 말고는 수집될 방법이 없었던 셈이다.
    #
    # 주의 — 네이버 검색 API 로는 아크메 기사를 못 가져온다. 실측(2026-09-01):
    #   네이버 뉴스 검색 API   '아크메'/'(주)아크메'/'Acme Marine'  전부 0건
    #   Google News RSS        '아크메'  44건, 'when:7d' 5건
    # naver.com 웹 검색에는 보이지만 개방 API 의 뉴스 색인에는 없다(제휴 매체 위주라
    # 해사 전문지가 빠진다). 그래서 이 검색어는 구글 쪽이 유일한 수집 경로다.
    # 국문 사명이 둘이라 검색어도 둘이다. 감지 키워드만 늘리면 소용없다 —
    # 가져오는 경로가 없으면 태깅할 기사 자체가 안 들어온다(2026-09-01 아크메 사례).
    #
    # '아크메전자'는 반드시 따옴표로 묶는다. 구글이 '아크메 + 전자'로 쪼개서 찾으면
    # LG전자 마린밸런스·HD현대마린엔진·LS마린솔루션이 쏟아지고 정작 우리 기사는 0건이다.
    # 실측(2026-09-01): 무따옴표 69건 중 제목에 '아크메전자'가 든 기사 0건,
    #                   따옴표 9건 중 3건.
    # '아크메전자상사'는 따로 둔다. '아크메전자'로도 감지는 되지만(부분 문자열),
    # 검색은 별개다 — 실측(2026-09-01) '아크메전자상사'로만 잡히는 기사가 3건 있었다
    # (K-SeaQ 공동브랜드 출범, 조선해양기자재 해외인증 55개사 등 컨소시엄 기사).
    "아크메", '"아크메전자"', '"아크메전자상사"',
    # 경쟁사 모니터링 (국내)
    "HD현대마린솔루션", "경쟁사7",
]

# 기사량이 많은 회사명은 단독 검색만으로는 7일 창을 못 덮는다.
# Google News RSS 와 네이버 API 모두 한 번에 100건까지만 주는데, "HMM" 처럼 하루
# 수십 건씩 쏟아지는 검색어는 그 100건이 최근 3~5일치로 차버린다.
# 실측(2026-08-12): "HMM" 단독은 네이버가 8/7~8/12, 구글이 최근 며칠만 덮어
# 8/6 자 기사가 이미 창 밖으로 밀려났다. 반면 "HMM AI"는 같은 시점에도 잡혔다.
# → 대용량 회사명에는 관심 주제를 붙인 '좁은' 검색어를 함께 돌려 창을 뒤로 넓힌다.
#   (단독 검색어는 그대로 두므로 커버리지는 줄지 않고 더해지기만 한다.)
_HIGH_VOLUME_COMPANIES = ["HMM", "HD현대중공업", "한화오션", "삼성중공업", "팬오션"]
_NARROWING_TOPICS = ["AI", "인공지능", "디지털 전환", "스마트십", "자율운항"]
GOOGLE_QUERIES_KO += [f"{c} {t}" for c in _HIGH_VOLUME_COMPANIES
                      for t in _NARROWING_TOPICS]
GOOGLE_QUERIES_EN = [
    "Ocean Network Express", "WECDIS navigation",
    "autonomous ship OR MASS maritime",
    "IACS UR E26 OR E27", "S-100 ENC electronic chart",
    "smart ship platform", "Avikus autonomous navigation",
    "ammonia OR methanol newbuilding vessel", "maritime decarbonization CII FuelEU",
    "integrated bridge system ship", "shipbuilding order Korea",
    "Hanwha Ocean OR Samsung Heavy order", "HD Hyundai shipbuilding",
    "HJ Shipbuilding OR K Shipbuilding Korea",
    # 직접 고객 해운사 (영문 표기)
    "HMM shipping OR HMM Ocean Service",
    "H-Line Shipping OR Hyundai LNG Shipping",
    # 관련 해운사 (영문 표기)
    "Pan Ocean OR Korea Line Shipping",
    "SK Shipping OR Sinokor OR Sido Shipping",
    "Polaris Shipping OR KSS Line", "Hyundai Glovis vessel",
    # AI × 스마트십 / 선박관리(해운 ERP) 접목 모니터링
    "AI ship management software", "generative AI shipping",
    "AI smart ship platform OR fleet management", "machine learning vessel performance",
    # 자사(아크메) 영문 표기
    "Acme Marine maritime OR shipping",
    # 경쟁사 / 연관사 모니터링 (해외)
    "NAVTOR", "MEGURI 2040 autonomous ship", "ShipDC ship data center",
    # 콩스버그는 MASS·자율운항으로 한정한다(2026-10-06 피드백). 'maritime digital' 로
    # 받으면 방산(미사일)·인사·실적 기사가 함께 온다. make_article 의 KONGSBERG 관문 참고.
    "Kongsberg autonomous",
    # 북극항로 (국문 검색어와 짝) — 해외 매체가 훨씬 자주 다룬다.
    # 'Arctic LNG carrier voyage'는 7일내 0건, 'Polar Code ice navigation'은 스발바르
    # 관광·크루즈 기사만 돌려줘 뺐다.
    "Northern Sea Route shipping", "Arctic shipping route",
]

# 글로벌 컴플라이언스: IMO + 주요 선급 규제 + 선박평가/베팅(TMSA·Rightship) 소식
COMPLIANCE_QUERIES_KO = [
    "IMO 해사 규제", "한국선급 KR", "선급 규정 해운", "IMO 탄소중립 규제",
    "TMSA 선박관리 평가", "Rightship 선박 평가", "SIRE 베팅 검사",
    # 협약·배출규제·표준 (개별 감지 키워드에 대응하는 능동 검색)
    "SOLAS MARPOL 협약 개정", "선박 탄소집약도 CII 등급",
    "EU ETS 해운 배출권 FuelEU", "IMO MASS Code 자율운항 규정",
    # S-100 은 솔루션팀 제품(ENC/WECDIS)과 직결이라 단독어까지 폭을 넓힌다.
    # 3단어 AND("IHO S-100 전자해도 표준")만 두면 정작 S-100 전환 소식이 빠진다.
    "IHO S-100 전자해도 표준", "S-100", "S-100 전자해도", "UKHO 해도",
]
COMPLIANCE_QUERIES_EN = [
    "IMO regulation shipping", "DNV OR ABS classification rules",
    "Lloyd's Register OR Bureau Veritas OR ClassNK maritime",
    "IACS unified requirements", "IMO MEPC OR MSC decarbonization",
    "TMSA tanker management self assessment", "RightShip vetting rating",
    "SIRE 2.0 vessel inspection", "OCIMF vetting tanker",
    # 협약·배출규제·표준
    "SOLAS OR MARPOL amendment IMO", "CII carbon intensity indicator ship",
    "EU ETS shipping OR FuelEU Maritime", "IMO MASS Code autonomous regulation",
    # S-100 / UKHO — 3단어 AND 만으로는 전환 로드맵·제품 출시 소식이 빠진다
    "IHO S-100 hydrographic standard", "S-100 ENC", "S-100 implementation",
    "UKHO Admiralty", "UKHO S-100 chart",
]

# 경쟁사: 국내 스마트십/항법(Competitor1/Competitor2/경쟁사4/경쟁사5/경쟁사6 등) + 해운 ERP 벤더
#
# 회사명 단독어로 검색한다(2026-10-06 피드백).
# 예전 검색어는 '경쟁사2 Competitor2 스마트십' 처럼 세 단어를 이어 붙였는데, 위
# GOOGLE_QUERIES_KO 주석대로 공백은 AND 라서 세 단어를 모두 쓴 기사만 잡혔다.
# 실측(2026-10-06, 네이버 total / 구글 30일):
#   '경쟁사2 Competitor2 스마트십'  0 / 0      '경쟁사2'  351 / 4
#   '경쟁사1 Competitor1 스마트십'      0 / 0      'Competitor1'    218, '경쟁사1 Competitor1' 187 (전부 회사 기사)
#   '경쟁사5 Competitor5'    0 / 0      '"경쟁사5"' 1045
# 즉 경쟁사 전용 수집 경로가 통째로 죽어 있었고, 경쟁사 기사는 다른 검색어에 우연히
# 딸려 온 것뿐이었다(9/17 경쟁사2 베슬링크 기사도 그렇게 들어왔다).
# '경쟁사1' 단독은 쓰지 않는다 — 1,225건이 대학 과정명 등 무관 기사다. 언론은 이 회사를
# '경쟁사1'로 쓰는데 '경쟁사1'는 일반명사라 Competitor1 와 함께 묶는다.
# '경쟁사5'은 따옴표로 묶는다 — 안 묶으면 네이버가 '마리나'+'체인'으로 쪼갠다.
COMPETITOR_QUERIES_KO = [
    "경쟁사2", "Competitor1", "경쟁사1 Competitor1", "경쟁사3", '"경쟁사5"',
    "경쟁사4 선박", "경쟁사6 선박관리",
    "선박관리 소프트웨어 ERP",
]
COMPETITOR_QUERIES_EN = [
    "Competitor1", "Competitor2", "Competitor3",
    "Competitor5 OR Competitor5 maritime", "Competitor6 ship management",
    "Furuno OR JRC marine electronics", "Sperry Marine OR Raytheon Anschutz navigation",
    "Wartsila Voyage",
    "SpecTec AMOS ship management", "BASSnet ship management software",
    # OR 묶음은 커버리지가 얕아 회사별로 분리
    "ShipNet ship management", "Hanseaticsoft Cloud Fleet Manager",
    "ABS Nautical Systems software", "Helm CONNECT maritime",
    "Kongsberg MASS",
    # 전용 검색어가 없던 벤더 보강
    "MESPAS ship management", "Veson Nautical IMOS voyage management",
    "MeSoft ship management software",
]

# ── 수집할 RSS 소스 목록 ──────────────────────────────────────────────────
# 실측(2026-08-10): 해외 매체 8곳 중 4곳이 0건을 반환하고 있었다. 해외 기사 비중이
# 낮았던 1차 원인이다 (수집 606건 중 영문은 34건뿐).
#   Maritime Executive  404 — 피드 경로가 /rss/articles → /articles.rss 로 바뀜
#   Safety4Sea          403 — Cloudflare 차단 (User-Agent 를 바꿔도 동일)
#   Shipping News Net   200이나 XML 아님 — /?feed=rss2 → /rss/allArticle.xml
#   Korea Shipping Gazette 404 — 사이트에 RSS 링크 자체가 없어짐
# 직접 접근이 막힌 두 곳은 Google News 의 site: 검색으로 우회한다.
STATIC_SOURCES = [
    {"name": "Maritime Executive",      "url": "https://maritime-executive.com/articles.rss",          "lang": "en"},
    {"name": "Splash247",               "url": "https://splash247.com/feed/",                          "lang": "en"},
    {"name": "The Loadstar",            "url": "https://theloadstar.com/feed/",                        "lang": "en"},
    {"name": "Smart Maritime Network",  "url": "https://smartmaritimenetwork.com/feed/",               "lang": "en"},
    {"name": "The Digital Ship",        "url": "https://thedigitalship.com/feed/",                     "lang": "en"},
    {"name": "Thetius",                 "url": "https://thetius.com/feed/",                            "lang": "en"},
    # Shipping News Net(쉬핑뉴스넷)은 한국어 매체다 — 기존에 lang="en" 으로 잘못 지정돼
    # 있었다. 요약·번역 단계가 언어를 보고 갈리므로 표기를 바로잡는다.
    {"name": "Shipping News Net",       "url": "https://www.shippingnewsnet.com/rss/allArticle.xml",   "lang": "ko"},
    # 직접 RSS 가 막힌 매체 (Google News site: 우회)
    {"name": "Safety4Sea",              "url": gnews("site:safety4sea.com", "en"),                     "lang": "en"},
    {"name": "Korea Shipping Gazette",  "url": gnews("site:ksg.co.kr", "ko"),                          "lang": "ko"},
    # 직접 RSS 가 얕은 매체 (2026-10-06 피드백: SpecTec·HarborMax AMOS, Flexport AI,
    # Ulysses AI Finder 누락). 피드가 최신 몇 건만 주는데 수집은 주 1회라 주 초반 기사가
    # 수집 시점에 이미 피드 밖이었다. 실측(2026-10-05 월요일):
    #   Smart Maritime Network  9건 — 전부 10/5 (하루치)
    #   The Digital Ship       10건 — 10/1~10/5 (4일치)
    # Google News site: 검색은 when:7d 로 7일 창 전체를 준다(같은 날 21건씩).
    # 직접 RSS 는 그대로 둔다 — 요약문이 붙어 오고, 겹치는 기사는 dedup 이 합친다.
    {"name": "Smart Maritime Network",  "url": gnews("site:smartmaritimenetwork.com", "en"),       "lang": "en"},
    {"name": "The Digital Ship",        "url": gnews("site:thedigitalship.com", "en"),             "lang": "en"},
    {"name": "Thetius",                 "url": gnews("site:thetius.com", "en"),                    "lang": "en"},
]

# 해사 전문지 — 매체 자체가 해사 주제를 보장한다(2026-10-06).
# 해사 키워드 필터·제목 해사 게이트는 '이게 해사 기사인가'를 단어로 추정하는 장치인데,
# 전문지 기사는 그 추정이 필요 없다. 실측: 'Flexport launches AI automated freight
# booking and tracking'(Smart Maritime Network)은 제목·요약에 vessel·shipping 이 없어
# 수집 필터에서 탈락했다. 점수 경쟁은 그대로 거친다 — 관문만 면제한다.
# The Loadstar 는 넣지 않는다: 항공화물·트럭 등 해사 밖 물류도 많이 다룬다.
TRADE_PRESS_SOURCES = {
    "Maritime Executive", "Splash247", "Smart Maritime Network", "The Digital Ship",
    "Thetius", "Shipping News Net", "Safety4Sea", "Korea Shipping Gazette",
}

# 정적 소스 + Google News 검색 피드(한/영) 를 합쳐 최종 SOURCES 구성
def _gfeeds(queries, lang):
    return [{"name": f"Google News({lang.upper()}) - {q}", "url": gnews(q, lang), "lang": lang} for q in queries]

SOURCES = (
    STATIC_SOURCES
    + _gfeeds(GOOGLE_QUERIES_KO, "ko") + _gfeeds(GOOGLE_QUERIES_EN, "en")
    + _gfeeds(COMPLIANCE_QUERIES_KO, "ko") + _gfeeds(COMPLIANCE_QUERIES_EN, "en")
    + _gfeeds(COMPETITOR_QUERIES_KO, "ko") + _gfeeds(COMPETITOR_QUERIES_EN, "en")
)

CUTOFF = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)

# 중복 제거 후 최대 보존 기사 수 (번역 부하/rate-limit 보호).
# 관련성 점수 상위 N건만 남기므로, 값을 키우면 더 많은 기사가 뉴스레터에 실림.
# 60건으로 축소: 이메일 크기(Gmail 100KB 제한)와 가독성 우선.
# 대신 relevance_score 의 변별력을 높여, 60건이 '점수 동점 무리에서 임의로 뽑힌
# 60건'이 아니라 '실제 상위 60건'이 되도록 했다.
#
# AI 융합 기사(10점 이상)는 이 상한에 포함되지 않는다 — UNCAPPED 참고(2026-10-06).
MAX_ARTICLES = 60

# 상한 밖에서 '점수 하한만 넘으면 전부' 싣는 주제 (기준, 점수 하한).
#
# 2026-10-06 피드백.
# 실측(그 주 후보 505건): 슬롯 밖은 사실상 12점이 컷이었다.
#   '수주 밀려오자 AI 심는다…한화 방산3사 AX 전환'   10점 — 제품 밀착도가 낮아 기본 3점
#   'Flexport launches AI automated freight booking'  12점
# 슬롯을 8 → 20 으로 늘려도 같은 10점 무리에서 동점으로 밀렸다. 자리 수로 다투게 두면
# 매주 같은 일이 생긴다. 상한 안에 넣으면 반대로 고객사 기사가 5건까지 밀려났다
# (AI 융합 48건이 75칸 중 48칸을 차지). 그래서 상한 '밖'에 둔다.
#
# 하한 10: 융합 가점(7)에 해사 맥락 점수가 3점은 있어야 한다. 실측 건수(중복 제거 후)
#   하한 8 → 48건 / 10 → 39건 / 12 → 22건. 10 이 방산3사를 지키는 가장 높은 값이다.
# 메일 본문에는 이 중 상위 15건만 싣고 전체는 첨부 HTML 에 실었다(사용자 결정, 아래에서 되돌림).
#
# 2026-10-12 비웠다 (사용자 결정 "예전처럼 되돌리기"). 상한 밖으로 받자 AI 융합 기사가
# 주 50건 안팎 들어와 AI 칸이 넘쳤고("AI/자율운항/스마트십 이 카테고리에 왜이렇게
# 많아갑자기?"), 지자체 성과 발표·산업전 개막 같은 기사까지 맨 위에 올랐다. 늘어난
# 기사만큼 번역·요약 호출이 늘어 Gemini 하루 한도에도 걸렸다. AI 융합은 이제 상한 안의
# 최소 슬롯(MIN_SLOTS 'ai_marine' 8건)으로만 보장한다. 장치는 남겨 둔다.
UNCAPPED: list[tuple[str, int]] = []

# 일반 해사 키워드
KEYWORDS = [
    "autonomous", "smart ship", "cyber", "imo", "newbuilding", "lng",
    "decarbonization", "ammonia", "methanol", "retrofit", "ais",
    "dnv", "abs", "classification", "shipping", "maritime",
    "radar", "ibs", "wecdis", "vdr", "iacs",
    # 영문 매체 표기 보강. 영국식 철자(-isation)와 기본 명사(vessel/fleet)가 빠져 있어
    # 영문 기사가 '해사 뉴스 최소 신호'(상한 2점)조차 못 받고 있었다.
    "decarbonisation", "vessel", "fleet", "seafarer", "shipyard", "bunkering",
    "자율운항", "스마트십", "사이버", "신조", "탈탄소", "해운", "선박",
    "hhi", "hyundai heavy", "현대중공업",
    # 극지·특수선: '쇄빙연구선'은 '선박'을 포함하지 않아 이 항목이 없으면 필터에 걸린다
    "쇄빙", "icebreaker", "polar research vessel",
    # 북극항로: 이 항목이 없으면 '북극항로 협력 논의'처럼 일반 해사 키워드가 하나도
    # 없는 제목이 수집 필터에서 통째로 탈락한다
    "북극항로", "북극해", "arctic", "northern sea route", "polar code",
    # 연관사(경쟁사 아님) — 태깅은 안 하지만 수집 대상에는 포함
    "shipdc", "ship data center",
    # 선종·사고·해양기관 어휘 (2026-09-01). 이 목록에 선종어가 통째로 없어서,
    # 회사명 태그가 없는 선박 기사는 수집조차 안 됐다.
    #
    # 주의: 이 목록은 '수집 대상에 넣는' 어휘일 뿐 사고 기사를 싣겠다는 뜻이 아니다.
    # 처음 넣을 때는 '해양 사고는 실어야 한다'는 판단이었는데, 같은 날 오후에 뒤집혔다
    # ("해양사고 기사도 미수집 되는 게 맞다"). 인명피해·수색구조 보도는 이제
    # km.is_marine_casualty 가 제외한다 — 어휘를 지우는 대신 제외 필터를 둔 이유는,
    # 어휘를 지우면 사고와 무관한 선종 기사('여객선 신조 발주')까지 같이 죽기 때문이다.
    "여객선", "화물선", "어선", "유람선", "낚싯배", "급유선", "관공선",
    # 상선 선종 (2026-09-21 피드백 — 현대글로비스 소유
    # VLCC 'VL 프로스페리티' 이란 해킹 의혹 기사, n.news.naver.com/article/016/0002699577).
    #
    # 위 줄에 여객선·어선·급유선은 있는데 정작 상선의 주력 선종이 통째로 빠져 있었다.
    # 그래서 회사명이 제목에 없는 상선 기사는 수집조차 안 됐다. 실측: 제목
    # '텍사스 입항 유조선 이란 해킹 수사에 국내 기업 포함' 은 해사 어휘가 하나도 없는
    # 것으로 판정됐다(주체인 현대글로비스·HMM오션서비스는 본문에만 나온다).
    "유조선", "탱커", "tanker", "vlcc", "vlgc", "vlac",
    "컨테이너선", "컨테이너 선", "벌크선", "벌커", "산적화물선", "운반선",
    "container ship", "containership", "bulk carrier", "bulker", "car carrier",
    "자동차운반선", "자동차 운반선", "ro-ro", "로로선",
    # 선박 사이버 사고 어휘는 여기 넣지 않는다 (2026-09-21, 회귀 테스트가 잡아냈다).
    #
    # 처음에는 '해킹'을 이 목록에 넣었다. 국내 매체가 사건을 '사이버'가 아니라
    # '해킹'으로 쓰기 때문인데(위 VLCC 기사도 그렇다), 그 순간 구멍이 생긴다:
    # TITLE_MARINE_KEYWORDS 가 이 목록을 그대로 포함하므로, '해킹'이 여기 있으면
    # 제목에 해사 어휘가 하나도 없는 일반 보안 기사가 '해사 기사'로 인정된다.
    # 실측: 감시 케이스 '국내 대기업 해킹 피해 확산…고객 개인정보 유출 조사'가 통과했다.
    #
    # 선박 해킹 기사는 제목에 선종·해운 어휘가 함께 오므로(위 기사는 '유조선') 이
    # 목록의 선종어로 이미 수집된다. 사고 어휘는 점수에만 쓴다
    # — CYBER_INCIDENT_KEYWORDS 참고.
    "침몰", "좌초", "전복", "해난", "조난", "표류",
    "해경", "해양경찰", "해수부", "해양수산부",
    "ferry", "capsize", "capsized", "aground", "grounding",
    "coast guard", "salvage",
    # 조선·해양 산업어 (2026-10-06 피드백: AI×조선/해양 융합 기사 누락).
    # 이 목록에 '조선'·'해양' 계열이 하나도 없어서, 제목·요약에 '선박'·'해운'이 없는
    # 조선·해양 기사가 수집 필터에서 통째로 탈락했다. 실측:
    #   'K조선, 200조 일감 안고 배 밖으로…AI·MRO 새 성장축'   (구글 제목만 → 탈락)
    #   '조선기자재도 AI 전환 시대…55개사 170명 국립한국해양대 집결' (요약까지 → 탈락)
    # '조선'·'해양' 단독은 넣지 않는다 — '조선'은 조선일보·조선왕조, '해양'은 해양레저·
    # 해양바이오에 걸린다. 산업을 가리키는 복합어만 넣는다.
    "조선업", "조선소", "조선사", "조선해양", "조선·해양", "조선 해양", "k조선", "k-조선",
    "조선기자재", "조선 3사", "조선3사", "해양산업", "해양플랜트", "해사",
    # '함정' 단독은 '함정수사'·'함정에 빠지다'에 걸린다
    "해군 함정", "함정 건조", "함정 정비", "함정 mro", "함정사업",
    "vessels", "ships", "shipbuilding", "shipbuilder",
    # 친환경·탈탄소 (2026-10-06 피드백).
    # 영문은 ammonia·methanol 이 있는데 국문 표기가 없었다. '친환경'은 넓은 말이지만
    # 여기는 수집 후보에 넣는 단계라 괜찮다 — 제목 해사 게이트가 자동차·식품 기사를 막는다.
    "친환경", "암모니아", "메탄올", "탄소포집", "선상탄소포집", "occs",
    "carbon capture", "무탄소", "수소선박", "풍력보조", "wind-assisted",
]

# 고객사 키워드 (별도 태깅)
CUSTOMER_KEYWORDS = [
    "hmm", "hyundai merchant marine", "현대상선",
    "h-line", "hline", "에이치라인", "h line shipping",
    "hyundai lng", "현대lng", "현대 lng",
    "pan ocean", "팬오션",
    "one ", "ocean network express",
    "avance gas",
]

# 아크메 제품/사업과 직결되는 관련성 키워드 (관련성 점수 가중, 2단계 차등)
# 플랫폼팀(스마트십·자율운항 플랫폼)·솔루션팀(항법·ENC·선박관리 ERP) 업무에 가중.
# 사이버보안은 축소(핵심어에서 제외) — 여전히 수집은 되지만 상위 노출 우선순위는 낮춤.

# ── 핵심어(제품 직결, +3): 아크메 제품/사업과 바로 연결되는 주제 ──
CORE_RELEVANCE_KEYWORDS = [
    # 항법/솔루션팀
    "wecdis", "s-100", "s100", "enc", "전자해도", "통합브리지", "ukho",
    "ship management", "fleet management", "선박관리", "선대관리", "fleethub",
    # 자율운항/스마트십(플랫폼팀)
    "autonomous ship", "autonomous vessel", "mass code", "smart ship",
    "스마트십", "자율운항", "kass",
    # 항해 최적화 — 스마트십 플랫폼의 핵심 기능이라 제품 직결이다.
    # 그동안 AI 결합 조건(AI_TARGET)에만 있어, AI 없이 항해 최적화만 다룬 기사는
    # 가점을 전혀 못 받았다. 실측: 'ZeroNorth launches agentic AI for voyage
    # optimisation'(경쟁 제품 출시 소식)이 2점으로 최하위권이었다.
    "voyage optimization", "voyage optimisation",
    "route optimization", "route optimisation", "항로 최적화", "항해 최적화",
    "e-navigation", "digital twin", "remote pilotage",
    # 연관사(경쟁사 아님) — 관련 선박 데이터 플랫폼
    "shipdc", "ship data center",
    # 아이스네비게이션(빙해 항행 지원) — 아크메 사업이다(2026-08-24 확인).
    # 그동안 북극 관련어는 인접어(+2)에만 있었다. 사업 우선순위와 점수가 어긋나
    # 북극 기사가 5~7점에 머물렀고(고객사·조선소 기사는 15점 이상), 그걸 메우려고
    # '북극 최소 4건' 슬롯으로 자리를 억지로 떼어두고 있었다 — 우선순위를 점수가
    # 아니라 예외 규칙으로 표현한 셈이다. 제품 직결로 올려 정공법으로 바꾼다.
    # 실측(2026-08-24, 57건): 영향 5건뿐이고 전부 방향이 맞다.
    #   팬스타 아크로호 첫 북극항로 시범운항  11 → 22점
    #   한화오션 '세계 유일 내빙 성능' 가스운반선 인도  10 → 20점
    # (아래 ARCTIC_CORE_KEYWORDS 에 모아 두고 여기에 합친다 — 점수에서 '1종'으로 센다)
]

# 북극·빙해 계열 핵심어. 아크메 아이스네비게이션 사업이라 제품 직결(+5)이 맞지만,
# 동의어가 11개라 한 주제만 다룬 기사가 손쉽게 3종 상한을 채워 +15를 벌었다
# (2026-09-01 피드백).
# 그래서 목록은 그대로 두되 relevance_score 에서 '한 종류'로 센다 —
# 우선순위(핵심어 +5)는 유지하고 중복 계산만 없앤다. keyword_match.count_kinds 참고.
# 실측(2026-08-31 58건 재계산): 팬스타 아크로호 28 → 18, '첫발 뗐지만' 26 → 16.
ARCTIC_CORE_KEYWORDS = [
    "북극항로", "북극해 항로", "northern sea route", "polar code",
    "ice navigation", "아이스 네비게이션", "아이스네비게이션",
    "빙해", "내빙", "ice class", "쇄빙",
]
CORE_RELEVANCE_KEYWORDS += ARCTIC_CORE_KEYWORDS

# 선박 사이버 '사고' 어휘 (2026-09-21). 실제로 공격을 받았다는 보도만 가리킨다.
#
# 이 파일에는 "사이버보안은 축소(핵심어에서 제외)"라는 결정이 남아 있고, 그 결정은
# 유지한다 — 'cyber'·'사이버' 단독은 지금도 핵심어가 아니다. 낮추기로 한 대상은
# 사이버보안 일반론·솔루션 홍보 기사였다.
#
# 반면 선박이 실제로 해킹당한 사건은 성격이 다르다. 아크메 제품이 선박 항해·관리
# 소프트웨어라, 그 소프트웨어가 붙어 있는 배가 공격받았다는 소식은 제품 직결 리스크다.
# 계기(2026-09-21): 현대글로비스 소유·HMM오션서비스 관리 VLCC 가 이란에 해킹당해
# FBI 가 수사 중이라는 기사가 5점에 머물러 뉴스레터에서 빠졌다.
#
# 아래 어휘는 사건 보도에만 쓰인다. '보안'·'security' 같은 넓은 말은 넣지 않는다.
# 북극 목록과 같은 이유로 '한 종류'로 묶어 센다 — 동의어가 많아 표기를 바꿔 가며
# 점수를 벌지 못하게 한다(count_kinds).
CYBER_INCIDENT_KEYWORDS = [
    "해킹", "랜섬웨어", "악성코드", "사이버공격", "사이버 공격", "사이버 침해",
    "hacking", "hacked", "ransomware", "malware", "cyberattack", "cyber attack",
    "cyber incident", "cyber breach",
]
CORE_RELEVANCE_KEYWORDS += CYBER_INCIDENT_KEYWORDS

# 점수에서 '한 종류'로 묶어 셀 동의어 그룹. 같은 주제를 여러 표기로 적었다는 이유만으로
# 점수를 벌지 못하게 한다(count_kinds). 새 그룹을 추가할 때는 CORE 목록의 부분집합이어야 한다.
CORE_KEYWORD_GROUPS = [ARCTIC_CORE_KEYWORDS, CYBER_INCIDENT_KEYWORDS]
# ── 인접어(사업 인접, +2): 관련은 있으나 밀착도는 한 단계 낮은 주제 ──
RELATED_RELEVANCE_KEYWORDS = [
    "radar", "ibs", "erp", "avikus",
    "iacs", "ur e26", "ur e27", "e26", "e27",
    "cii", "fueleu", "eu ets", "s-100 전환", "선급",
    # IMO 온실가스 규제 (2026-08-25 피드백).
    # 실측: 'Op-Ed: Alternative GHG Proposals Would Derail IMO Climate...' 가 7점,
    # 'US FMC DiBella calls for rethinking IMO Net-Zero Framework' 가 9점이었다.
    # 선박 연료 전환·배출 규제는 항법·선박관리 제품의 운항 데이터 요구와 맞닿는다.
    "ghg", "온실가스", "net-zero", "net zero", "넷제로", "탄소중립",
    "decarbonisation", "decarbonization", "탈탄소 규제", "배출규제",
    # 스마트십 인접 기술 — 영문 매체에서 흔히 쓰는 표기
    "vessel performance", "berth planning", "port call optimisation",
    "port call optimization", "vsat", "connectivity", "situational awareness",
    # 북극 관련어는 CORE_RELEVANCE_KEYWORDS 로 옮겼다(2026-08-24) — 아이스네비게이션이
    # 아크메 사업이라 인접(+2)이 아니라 제품 직결(+5)이 맞다. 그쪽 주석 참고.
]
# ── AI 접목 신호: 아래 두 그룹이 한 기사에 함께 있으면 결합 보너스(+3) ──
AI_SIGNAL_KEYWORDS = ["ai ", " ai", "artificial intelligence", "machine learning",
                       "머신러닝", "인공지능", "생성형", "genai", "generative ai"]
AI_TARGET_KEYWORDS = ["smart ship", "스마트십", "ship management", "선박관리",
                       "fleet", "선대", "자율운항", "autonomous", "erp", "해운 erp",
                       "vessel performance", "항해 최적화", "voyage optimization",
                       # 실무에서는 '항해'보다 '항로'가 훨씬 흔하게 쓰인다. 이 표기가
                       # 빠져 있어 'HMM, 선박 항로 최적화에서 화주 챗봇까지'(2026-08-06)가
                       # AI 결합 보너스(+4)를 못 받고 9점에 머물러 상한 컷에서 잘렸다.
                       "항로 최적화", "항로최적화", "route optimization",
                       "운항 최적화", "연료 최적화", "최적 항로"]

# ── AI·AX × 조선/해양 융합 (2026-10-06 피드백) ─────────────────────────────
# "AI와 조선소, 선박, 함정, 방산, 해운, 해양 등의 결합·융합 기사 필요, AI·AX 흐름".
#
# 위 AI 결합 보너스는 '스마트십·선박관리·항로 최적화' 같은 우리 제품 영역에만 붙는다.
# 그래서 조선소 AI 전환·방산 AX·해양기관 AI 지원처럼 산업 전반의 AI 흐름은 가점이
# 없었고, 제목에 '선박'·'해운'이 없으면 수집 필터에서부터 탈락했다. 실측(피드백 기사):
#   'K조선, 200조 일감 안고 배 밖으로…AI·MRO 새 성장축'       수집 필터 탈락
#   '수주 밀려오자 AI 심는다···한화 방산3사 AX 전환 본격화'      수집 필터 탈락
#   '해양산업 AI 전환 지원…해진공 AX 사이트 개통'              구글(제목만)에서 탈락
#
# 판정은 '제목'으로 한다 — 제목에 AI 신호와 산업어가 함께 있어야 융합 기사다.
# 요약까지 보면 AI 를 한 줄 스친 수주 기사가 다 걸린다.
# '에이전트'는 넣지 않는다: 해운에서는 선박대리점(ship agent)을 뜻한다.
AI_CONVERGENCE_SIGNAL = AI_SIGNAL_KEYWORDS + [
    "ax", "ai 전환", "ai전환", "인공지능 전환", "피지컬 ai", "agentic", "llm",
]
# '조선' 단독은 쓰지 않는다 — '조선대 AI중심대학 선정'(조선대학교)이 걸렸다(2026-10-06 실측).
AI_CONVERGENCE_DOMAIN = [
    "조선소", "조선업", "조선사", "조선해양", "조선·해양", "조선 해양", "k조선", "k-조선",
    "조선 3사", "조선3사", "조선기자재", "조선 빅3", "수리조선", "첨단 조선", "조선 산업",
    "조선산업", "스마트야드", "스마트 야드",
    "선박", "함정", "해운", "해양", "항만", "해사", "선사", "선급", "해군",
    "maritime", "shipping", "shipyard", "shipbuilding", "vessel", "vessels", "ship",
    "ships", "naval", "navy", "port", "ports", "seafarer", "offshore", "fleet",
]
# '방산'은 조선·해군 맥락이 요약에 있을 때만 융합으로 본다 (2026-10-06 실측).
# 단독으로 두면 'KT, 충청 제조·방산기업 AI 전환 돕는다'·'방산 특화 AX대학원 유치'
# 같은 육·공군·제조 기사가 쏟아졌다. 피드백 기사 '한화 방산3사 AX 전환'은 요약에
# "한화오션도 AX사업부를 중심으로 조선소 생산 현장의 AI 전환을…"이 있다.
AI_DEFENSE_TITLE = ["방산"]
AI_DEFENSE_MARINE_CONTEXT = AI_CONVERGENCE_DOMAIN + [
    "한화오션", "hd현대", "현대중공업", "삼성중공업", "hj중공업", "케이조선",
    "잠수함", "구축함", "호위함", "군함",
]
# 4 → 7 (2026-10-06 피드백).
# +4 일 때 실측: 후보 505건 중 60건만 남는데 슬롯 밖에서는 사실상 12점이 컷이라
# '수주 밀려오자 AI 심는다…한화 방산3사 AX 전환'(11점)이 잘렸다. 피드백이 직접 요구한
# 주제라 고객사 가점(+6)과 같은 급으로 올린다.
#
# 7 → 4 로 되돌렸다 (2026-10-12 사용자 결정 "예전처럼 되돌리기"). +7 에서는 'AI·해양'
# 두 단어만 있는 지자체·행사 기사가 20점대로 고객사 기사보다 위에 섰다.
AI_CONVERGENCE_BONUS = 4
# 해사 전문지 가점. 전문지는 업계 독자를 상대로 쓴 기사라 같은 사안이라도 밀도가 높다.
# 실측: 'Flexport launches AI automated freight booking and tracking'(Smart Maritime
# Network)이 6점으로 잘렸다 — 일반 해사어가 없어 점수를 벌 데가 없었다.
TRADE_PRESS_BONUS = 3


# 조선소 생산 자동화(용접·도장 로봇)는 AI 융합으로 보지 않는다 (2026-10-12 피드백:
# 'Kawasaki Heavy unveils four-legged AI shipyard robot'·'야드 작업용 AI 용접·도장 로봇'
# 관련없음). 아크메 제품은 운항·선박관리 소프트웨어라 건조 공정 로봇과 접점이 없다.
# 수집에서 빼지는 않는다 — 가점만 안 준다.
AI_CONVERGENCE_EXCLUDE = ["로봇", "용접", "도장", "robot", "robots", "welding", "painting"]


def is_ai_marine(title: str, source: str = "", summary: str = "") -> bool:
    """제목에 AI·AX 신호와 조선·해양 산업어가 함께 있는가.
    해사 전문지 기사는 매체가 산업을 보장하므로 AI 신호만 본다
    (실측: 'Flexport launches AI automated freight booking' — 산업어가 'freight' 뿐).
    제목의 '방산'은 요약에 조선·해군 맥락이 있을 때만 인정한다(AI_DEFENSE_TITLE 주석)."""
    t = km.strip_quotes(km.strip_outlet(title or "")).lower()
    if not km.any_keyword(t, AI_CONVERGENCE_SIGNAL) or km.any_keyword(t, AI_CONVERGENCE_EXCLUDE):
        return False
    if source in TRADE_PRESS_SOURCES or km.any_keyword(t, AI_CONVERGENCE_DOMAIN):
        return True
    return (km.any_keyword(t, AI_DEFENSE_TITLE)
            and km.any_keyword(km.strip_quotes(summary or "").lower(),
                               AI_DEFENSE_MARINE_CONTEXT))


# ── 친환경·탈탄소 (2026-10-06 피드백) ─────────
# 최소 슬롯 판정용. 제목 기준이다(주제는 제목에 있다).
GREEN_TITLE_KEYWORDS = [
    "탈탄소", "친환경", "저탄소", "무탄소", "탄소중립", "탄소포집", "온실가스", "넷제로",
    "암모니아", "메탄올", "수소", "lng 추진", "이중연료", "풍력보조", "배출",
    "decarbon", "decarbonisation", "decarbonization", "ammonia", "methanol",
    "hydrogen", "carbon", "emission", "emissions", "ghg", "net-zero", "net zero",
    "dual-fuel", "dual fuel", "wind-assisted", "occs", "fueleu", "cii",
]


def is_green(art: dict) -> bool:
    return km.any_keyword(km.strip_outlet(art.get("title", "")).lower(),
                          GREEN_TITLE_KEYWORDS)


# 하위호환: 외부에서 HIGH_RELEVANCE_KEYWORDS 를 참조할 경우 대비
HIGH_RELEVANCE_KEYWORDS = CORE_RELEVANCE_KEYWORDS + RELATED_RELEVANCE_KEYWORDS

# ── 제목 해사 게이트: '이게 해사 기사이긴 한가'를 제목으로 한 번 확인한다 ──
#
# 배경(2026-09-01 피드백).
# 수집 필터와 점수는 제목+요약을 한 덩어리(full_text)로 보는데, 방송 종합뉴스처럼
# 여러 꼭지가 한 기사에 들어가면 다른 꼭지의 단어로 점수를 번다. 실측:
#   제목  '대홍수' 한국인 9명은 어디에...필사의 수색      ← 해사어 0개
#   요약  위성통신과 항해 정보, 쇄빙 내빙 능력 ... 폴라코드  ← 북극항로 꼭지가 섞여 있다
#   점수  핵심어 '내빙'+'쇄빙' 2종 +10, 일반어 +2, 최신성 +3 = 15점 → 뉴스레터 게재
# 고객사·경쟁사 배지를 제목으로만 감지하는 것과 같은 이유다 — 기사의 주제는 제목에 있다.
#
# 게이트는 '품질 필터'가 아니라 '주제 밖 기사 차단기'라서 어휘를 넉넉히 잡는다.
# 점수용 KEYWORDS 에는 없지만 해사 헤드라인에 흔한 말(조선·항만·해양·상선·port 등)을
# 더해, 진짜 해사 기사가 어휘 누락으로 잘리지 않게 한다.
# 실측(과거 raw_news 6개 파일, 고유 기사 186건): 12건 제외, 전부 비해사 기사였다
# (팁스 선정 / 오늘아침 정치 / AI 팩토리 / 라운드테이블 성료 / 대홍수). 오제외 0건.
TITLE_MARINE_EXTRA = [
    "조선", "조선소", "조선업", "항로", "항만", "해상", "해사", "해양", "바다",
    "선사", "선주", "화주", "운반선", "컨테이너선", "유조선", "탱커", "예인선",
    "상선", "함정", "해군", "항해", "항행", "선원", "선급", "입항", "출항", "기항",
    "ship", "ships", "port", "ports", "navigation", "nautical", "marine",
    "ocean", "cargo", "container", "tanker", "voyage", "sea route", "satcom",
    "inmarsat", "harbour", "harbor", "offshore", "hull",
    # 선종·해양 사고 어휘는 KEYWORDS 에 있다 (거기 주석 참고) — 이 목록은
    # KEYWORDS 를 이미 포함하므로 여기에 또 적지 않는다.
    "부두", "선착장", "뱃길",
]
# KEYWORDS 중 '해사 기사'의 증거가 못 되는 말은 제목 게이트에서 뺀다(2026-10-06).
# 친환경 계열은 수집 후보로는 넣되(KEYWORDS 주석 참고), 제목에 '친환경'만 있다고
# 해사 기사로 인정하면 '친환경 자동차 출시' 같은 기사가 게이트를 넘는다.
_TITLE_GATE_EXCLUDE = {"친환경", "암모니아", "메탄올", "탄소포집", "carbon capture", "무탄소"}
TITLE_MARINE_KEYWORDS = ([k for k in KEYWORDS if k not in _TITLE_GATE_EXCLUDE]
                         + CORE_RELEVANCE_KEYWORDS
                         + RELATED_RELEVANCE_KEYWORDS + TITLE_MARINE_EXTRA)


def has_marine_title(title: str) -> bool:
    """제목만으로 해사 기사임을 알 수 있는가 (주제 밖 기사 차단용).

    연재 코너명을 떼고 본다(2026-09-01 피드백).
    코너명은 그 기사의 주제가 아니라 지면의 이름이다. 코너명에 든 해사어로 이 게이트를
    통과하면, 정작 기사 제목에는 아무 내용이 없는 꼭지가 실린다.
      실측: '[자유항행시대의 종말과 과제] ③ 전문가 인터뷰' 가 코너명의 '항행'으로
      통과해 13점으로 실렸다. 코너명을 떼면 남는 제목은 '전문가 인터뷰' 뿐이다.
    번역 단계는 이미 같은 이유로 코너명을 뗀다(km.strip_corner 주석) — 판정도 같은
    제목을 봐야 한다.
    """
    core = km.strip_corner(km.strip_outlet(title or ""))
    return km.any_keyword(km.strip_quotes(core).lower(), TITLE_MARINE_KEYWORDS)


# 회사명에 흔히 붙는 법인격·업종 접미어. 회사명만 남은 제목을 판정할 때 함께 지운다.
_CORP_SUFFIXES = [
    "industries", "industry", "heavy", "shipbuilding", "marine", "ocean",
    "corporation", "company", "holdings", "group", "co", "ltd", "inc", "corp",
    "중공업", "해운", "조선", "그룹", "주식회사", "㈜",
]


def is_bare_entity_title(title: str) -> bool:
    """제목이 회사명 하나뿐이라 기사 내용이 없는가.

    2026-09-01 피드백: 'HD Hyundai Heavy Industries - RealClearDefense'.
    언론사명을 떼면 제목이 회사명 그 자체다 — 무슨 일이 있었는지가 없다.
    구글 뉴스는 요약도 제목 재탕이라 본문 신호도 없는데, 고객사 태그(+6)와
    회사명이 머금은 일반 키워드로 16점을 받아 상위에 올라왔다.

    '남은 글자가 있는가'로만 판정한다 — '남은 말이 해사어인가'로 보면 안 된다.
    실측: '삼성重도 피지컬 AI 도입 빨라진다'는 회사명을 떼면 해사어도 사업어도
    없지만 멀쩡한 기사다(고객사 카드에 실렸다). 회사명 말고 아무것도 없을 때만 건다.
    """
    core = km.strip_quotes(km.strip_outlet(title or "")).lower()
    for kws, _name in CUSTOMER_RULES + COMPETITORS:
        for kw in kws:
            core = core.replace(kw, " ")
    for suffix in _CORP_SUFFIXES:
        core = core.replace(suffix, " ")
    # 남은 글자(기호·공백 제외)가 거의 없으면 회사명뿐인 제목이다.
    # 4자 기준: 'hd' 같은 약칭 조각은 남아도 통과시키지 않되, 짧은 실제 제목은 살린다.
    return len(re.sub(r"[^0-9a-z가-힣]+", "", core)) < 4

# 저품질/사내소식 노이즈 키워드 (점수 감점 → 뒤로 밀림)
NOISE_KEYWORDS = [
    "사옥", "이전", "인사발령", "임원인사", "주주총회", "주가", "증시", "실적발표",
    "부고", "동정", "채용", "창립", "기념식", "후원", "기부", "봉사", "표창",
    # 인물 프로필 / 인사 / PR·홍보성 (연관성 낮음 → 감점 강화)
    "who is", "[인사]", "인사]", "승진", "취임", "선임", "내정", "위촉",
    "회장]", "부회장]", "사장]", "[게시판]", "게시판", "수여식", "간담회",
    "매거진", "발간", "출판", "신년사", "특징주",
    # 기업 소개 연재·탐방 (2026-09-01 피드백). 한 회에 한 업체를 소개하는 기획물은 사실상 홍보물이라
    # 상단에 올릴 성격이 아니다. 제외까지는 안 한다 — 경쟁사 동향으로는 볼 값어치가 있다.
    "유니콘", "기업 탐방", "스타트업 탐방", "창업 스토리", "성공 스토리",
    # 인물·오너 프로필 기획물 (제품·기술 소식이 아님)
    "[the ceo]", "오너진단", "차세대 오너", "인물 프로필",
    "obituary", "appointment", "share price", "dividend", "quarterly results",
]

# 행사·전시·수상·홍보성 (실체 있는 기술/사업 소식이 아님 → 감점).
# 실측 배경: "해양 보안 전문가들 한자리에 모인다"(행사 안내)가 일반 키워드를 잔뜩
# 머금어 전체 1위(24점)를 차지했다. 정작 제품 직결인 UKHO S-100 기사는 최하위였다.
# 조선·해운과 무관한 사업부문 (2026-08-25 피드백). 국내 중공업사는 조선 외에 건설·토목·플랜트·전자 부문을 함께 갖고 있어,
# 회사명만 보고 태깅하면 그쪽 기사가 고객사 동향에 실린다.
# 실측: '조선에 가렸던 삼성重 토건…반도체 훈풍에 삼성전자 관련 매출 50% 급증'(15점),
#       'HJ중공업, 1740억 규모 제2경춘국도 4공구 공사계약 체결'(17점).
#
# 제목으로만 판정한다 — 본문에 '반도체'가 스치는 정상 해사 기사를 막지 않기 위해서다.
# 실측 오탐 0건('LNG운반선 4척 건조 계약'·'해상풍력 설치선 수주'·'엔진공장 증설' 통과).
# 키워드 목록은 keyword_match.NONMARINE_KEYWORDS 에 있다(카드 선정 단계와 공유).


def is_nonmarine(title: str) -> bool:
    """조선·해운이 아닌 사업부문(건설·토목·전자) 기사인가."""
    return km.any_keyword(km.strip_quotes(km.strip_outlet(title)).lower(),
                          km.NONMARINE_KEYWORDS)


# 노사 소식 — 임금·성과급·파업. 수집 단계에서 제외한다(2026-08-25 피드백). 처음에는 -5점 감점으로 뒀는데
# 실측에서 19점·17점으로 여전히 상위에 남았다. 제품·기술과 무관하고 매주 비슷한
# 내용이 반복되므로 아예 받지 않는다.
LABOR_NOISE_KEYWORDS = [
    "노조", "노사", "파업", "쟁의", "임단협", "임금협상", "단체교섭",
    "성과급", "성과공유", "하투", "총파업", "부분파업",
    # 영문 표기 (2026-09-01 피드백).
    # 이 목록이 한글뿐이라 국문 기사만 걸리고 영문판은 그대로 통과했다. 실측:
    # 'HD현대重 노조 파업권 확보'는 제외됐는데 'Strike looms as HD Hyundai Heavy
    # union backs industrial action'은 11점으로 통과했다.
    #
    # 'union' 단독은 절대 넣지 않는다 — European Union 이 걸려 EU 규제 기사가 통째로
    # 날아간다. 노사 맥락이 분명한 표현만 쓴다.
    "strike", "walkout", "industrial action", "labour union", "labor union",
    "trade union", "union backs", "union votes", "union demands",
    "collective bargaining", "wage talks", "wage negotiation", "picket",
    # 임금·퇴직금 소송 (2026-09-21 피드백).
    # 2026-09-01 LLM 검수가 지적해 승격됐지만 거르는 규칙이 없어 3주째 '미해결'로
    # 남아 있던 건이다. 실측 제목: 'HD Hyundai Heavy retirees lose lawsuit seeking to
    # include bonuses in severance pay'. 이 목록은 단체행동(노조·파업)만 담고 있어
    # 개인·집단의 임금 소송은 하나도 안 걸렸다. 같은 성격의 사내 노사 이슈다.
    #
    # '소송'·'lawsuit' 단독은 절대 넣지 않는다 — 사업상 분쟁이 통째로 사라진다.
    # 실측: '러시아측 "한화오션 1.37조 물어내라"'(Arctic LNG 2 국제중재)는 지난주
    # 뉴스레터에 실린 정상 기사다. 임금·퇴직금을 직접 가리키는 말만 넣는다.
    "퇴직금", "퇴직급여", "통상임금", "체불임금", "임금 소송", "임금소송",
    "근로자지위", "부당해고", "정리해고", "희망퇴직", "임금체불",
    "severance pay", "retirement pay", "retirees lose", "retirees win",
    "unpaid wages", "back pay", "wrongful dismissal", "unfair dismissal",
    "labour court", "labor court",
    # 임금협상 줄임말·결과 표현 (2026-09-28 피드백). 목록에 '임단협'·'임금협상'은 있었지만
    # 제목이 줄임말 '임협'과 결과('기본급 … 인상')만 써서 하나도 안 걸렸다.
    # 실측 제목: '삼성중공업, 추석 전 임협 최종 타결…기본급 13만원↑'(14점, 고객사 카드).
    # '임금'·'임금 인상'·'pay rise'는 넣지 않는다 — '선원 최저임금 인상 합의'(ILO·MLC
    # 규제 기사)가 부분 문자열로 걸린다. watch_cases.json 에 그 짝 케이스가 있다.
    "임협", "기본급", "임금 타결", "임금교섭", "임금 교섭",
    "wage deal", "pay deal",
    # 잠정합의안 부결·가결 (2026-10-06 피드백). 영문 제목이 'union
    # rejects'·'wage agreement'라 위 표현에 하나도 안 걸려 14점으로 신조/조선 칸에 실렸다.
    # 'provisional agreement'·'tentative agreement' 단독은 넣지 않는다 — EU 의회·이사회
    # 잠정합의(EU ETS·FuelEU)를 가리키는 규제 기사에 그대로 쓰인다.
    "union rejects", "union rejected", "union approves", "union ratifies",
    "union members reject", "wage agreement", "provisional wage", "tentative wage",
    "합의안 부결", "합의안 가결",
    # 농성 (2026-10-19 피드백). 실측 제목: 'Union Leader Launches Crane Sit-in at Hyundai
    # Heavy Industries' Ulsan Yard'(20점, 전체 뉴스 게재). 위 영문 목록에 농성·노조 간부
    # 표현이 없었다. 번역 후 재판정(2_summarize.drop_translated_noise)도 함께 생겼지만,
    # 여기서 먼저 걸러야 번역·본문 수집 비용을 아낀다.
    # 'sit in' (하이픈 없음)은 넣지 않는다 — 'vessels sit in port' 같은 문장에 걸린다.
    "농성", "sit-in", "union leader", "union chief", "union head", "labor protest",
    "labour protest", "hunger strike",
    # 직원 급여·연봉 공시 기사 (2026-09-28 피드백). 반기보고서 급여 항목을 풀어쓴 기사로,
    # 노사 소식과 같은 사내 보수 이슈다. 실측: 8점으로 경쟁사 카드에 실렸다(업체당 1건
    # 규칙이라 그 업체의 유일한 기사면 점수와 무관하게 자리를 받는다).
    # '급여' 단독은 넣지 않는다 — '급여수'(보일러 급수) 같은 기술 용어와 선원 급여를
    # 다루는 MLC 규제 기사가 걸릴 수 있다. 직원 보수 공시에만 쓰는 말로 좁힌다.
    "평균급여", "평균 급여", "평균연봉", "평균 연봉", "1인당 급여", "1인당 연봉",
    "직원 급여", "직원 연봉", "연봉킹", "연봉 1위", "보수총액", "보수 총액",
    # 2026-10-12 피드백(관련없는 기사): '한화오션 "안전·생산 위협은 타협안해"…고공농성
    # 중단 촉구'(15점, 고객사 카드 후보). 제목에 '노조'가 없어 위 목록을 비껴갔다.
    "고공농성", "고공 농성", "농성",
]

# 조직·거점 신설 발표 (2026-08-25 피드백).
# 기관을 세운다·협약을 맺는다는 발표 자체는 제품·기술 소식이 아니고 후속이 없다.
# '협약'·'mou' 단독은 넣지 않는다 — 북극항로 기항 협약처럼 실질이 있는 건이 걸린다.
# 실측: '삼성중공업-KAIST 차세대 선박연구센터 출범'(18점),
#       '국립한국해양대-한국조선해양기자재연구원 해양 AX 혁신허브 구축'(18점).
ORG_LAUNCH_KEYWORDS = [
    "센터 출범", "센터 가동", "센터 구축", "센터 개소", "센터 설립", "센터 신설",
    "연구소 설립", "연구소 개소", "허브 구축", "허브 조성", "허브 업무협약",
    "혁신허브", "혁신 허브", "거점 구축", "거점 조성", "혁신 거점",
    # 국가 간 '녹색 해운 회랑' 협약 (2026-09-01 피드백: "싱가포르와 브라질의 디지털
    # 운송 통로 협약, 이것도 별로 쓸데없는 기사 같음). 나라끼리 MOU 를 맺었다는
    # 발표 자체이고 후속이 없다 — 센터 출범 발표와 같은 성격이라 여기에 둔다.
    # 실측: 'Singapore adds Brazil to green shipping corridor network'(9점).
    #
    # 국내 정책 기사와는 다르다. '녹색해운항로법 시행'처럼 실제로 시행되는 제도는
    # 이 표현들에 안 걸린다(실측 확인).
    "shipping corridor", "green corridor", "green and digital",
    "해운 회랑", "그린 회랑", "운송 통로", "녹색 회랑",
    # 지자체 특구·거점 지정 (2026-09-07 피드백). 센터 출범과 같은 성격이다 —
    # 무엇을 지었다는 게 아니라 어디에 무엇을 만들기로 했다는 발표이고 후속이 없다.
    # 실측: '부산 청사포~신항 해역 신해양레저 규제자유특구 지정'(자율운항 레저선박),
    #       '목포, 무인수상정 자율운항 거점 추진…해상실증·R&D 연계'.
    "규제자유특구", "규제 자유특구", "규제특구", "특구 지정", "특구로 지정",
    "거점 추진", "거점 마련", "거점화",   # '거점 구축'·'거점 조성'은 위에 이미 있다
]

# 위 목록은 제목만 본다. 연구 조직 신설은 제목에서 그 말을 안 쓰고 본문에서만
# 밝히는 경우가 있다 — 2026-09-07 실측: '삼성중공업, 미래선박 R&D 투트랙…AI부터
# 차세대 연료까지'는 제목에 '센터'가 한 번도 없고, 요약 첫 문장이
# "지난 8월 문을 연 'SHI-KAIST 차세대 선박연구센터'는…" 이다.
# 요약은 스치는 말이 많아 넓은 키워드를 쓰면 멀쩡한 기사가 떨어지므로,
# '연구 조직 신설' 외에는 쓰이지 않는 말만 넣는다.
ORG_LAUNCH_IN_SUMMARY = [
    "연구센터", "협력센터", "연구소 설립", "연구원 설립", "r&d센터", "r&d 센터",
]


# 정기선 서비스 개편 (2026-09-01 피드백). 선사가 항로·기항지를 손보는 소식은 화주·포워더용 정보이고
# 항법·선박관리 제품과 접점이 없다. 고객사 태그(+6)가 붙어 점수는 잘 나온다.
#
# 새 항로 '개척'과는 다르다는 점에 주의 — 북극항로 시범운항·취항은 남아야 한다
# (아이스네비게이션 사업 직결). 그래서 '취항'·'항로 개설' 같은 넓은 말은 안 쓰고,
# 기존 서비스를 늘리고 줄이는 표현만 좁게 잡는다.
# 실측 확인: '팬스타, 국내 최초 컨테이너선 북극항로 취항'은 이 목록에 안 걸린다.
LINER_SERVICE_KEYWORDS = [
    "서비스 확대", "서비스 개설", "서비스 개편", "서비스 축소", "서비스 중단",
    "노선 확대", "노선 개편", "노선 축소", "기항지 추가", "기항 확대",
    "루프 추가", "선복 확대", "증편",
    "expands service", "service expansion", "new loop", "dedicated loop",
    "adds port call", "service upgrade", "network reshuffle",
]


def is_liner_service(title: str) -> bool:
    """정기선 서비스 개편 소식인가 (제목 기준)."""
    return km.any_keyword(km.strip_quotes(km.strip_outlet(title)).lower(),
                          LINER_SERVICE_KEYWORDS)


def is_labor_noise(title: str) -> bool:
    """노사 소식인가 (제목 기준)."""
    return km.any_keyword(km.strip_quotes(km.strip_outlet(title)).lower(),
                          LABOR_NOISE_KEYWORDS)


def is_org_launch(title: str, summary: str = "") -> bool:
    """센터·허브·특구 신설 발표인가.

    제목으로 판정하되, 연구 조직 신설은 요약까지 본다(ORG_LAUNCH_IN_SUMMARY 주석).
    """
    if km.any_keyword(km.strip_quotes(km.strip_outlet(title)).lower(),
                      ORG_LAUNCH_KEYWORDS):
        return True
    return bool(summary) and km.any_keyword(
        km.strip_quotes(summary).lower(), ORG_LAUNCH_IN_SUMMARY)

PROMO_NOISE_KEYWORDS = [
    "한자리에", "한자리", "개최", "컨퍼런스", "콘퍼런스", "세미나", "포럼", "엑스포",
    "박람회", "전시회", "설명회", "심포지엄", "워크숍", "시상", "수상", "선정됐",
    "대상 수상", "브랜드평판", "인재양성", "미리보기", "conference", "exhibition",
    "webinar", "award", "expo",
    # 행사 종료 보도 ('성료'). 2026-08-24 실측: 북극 가점이 붙으면서 '법무법인 세종,
    # 제1회 유라시아 디플로매틱 라운드테이블 성료'가 15점으로 올라왔다.
    "성료", "개최했다", "성황리",
    # 센터·허브 신설 발표는 make_article 에서 제외한다(ORG_LAUNCH_KEYWORDS 참고).
    # 인재·인력 기사 (2026-08-24 피드백).
    # '인재양성'은 진작 있었는데 붙여쓴 표기만 있어 띄어쓴 제목을 통째로 놓쳤다
    # ('국립목포해양대, 해운업계 50억 지원 업고 북극항로 인재 허브 도약' 15점).
    # 대학 기사를 싸잡아 빼지는 않는다 — 기술 개발 기사는 남아야 한다
    # (실측: '경상국립대, AI 기반 선박 자율 계류·묘박 시스템 개발 착수'는 안 걸린다).
    "인재 양성", "인재 허브", "인재허브", "인재 확보", "인재확보",
    "인력난", "인력 양성", "채용", "취업", "장학",
    # 시상·전시 기획전 소개 기사 (사실상 기업 홍보물)
    "4차 산업혁명", "ai korea",
]

# 주기 발행 종합·전망 기사 (개별 사안이 없는 묶음 기사 → 수집 제외).
# 실측: '7월 5주 주요 제조업 전망'이 요약에 자율운항 단어가 섞여 자율운항 카테고리로
# 분류됐다. 여러 산업을 훑는 정기 코너라 뉴스레터에 실을 내용이 없다.
DIGEST_NOISE_KEYWORDS = [
    "제조업 전망", "주간 전망", "주요 산업 전망", "산업 동향 브리프",
    "데이터 뉴스룸", "위클리 에너지 리포트", "이주의 산업",
    # 시각별 종합·헤드라인 묶음 (개별 사안이 없다). 2026-08-24 실측: 북극 가점이
    # 붙으면서 '李, 민주당 지도부·의원단 연쇄 회동[오늘아침]'이 15점으로 올라왔다.
    "[오늘아침]", "오늘아침", "이 시각 주요뉴스", "이 시각 헤드라인",
    "주요 신문 톱뉴스", "조간 브리핑", "뉴스 브리핑",
    # 기사가 아니라 '이 기사는 유료 매체에 실렸습니다' 안내문. 본문이 없으므로
    # 요약도 불릿도 만들 수 없다 — 이번 주 LLM 검수도 이걸 무관으로 지적했고,
    # 승격된 회귀 케이스이기도 하다(watch_cases.json).
    # 2026-09-07 피드백.
    # 실측 제목: "이 기사는 M.의 자본시장 전문 유료언론 '레이더M'에 보도되었습니다. - 매일경제"
    "레이더m", "유료언론", "유료 언론", "에 보도되었습니다", "유료 기사",
    "paid media outlet", "subscribers only", "this article was reported on",
]

# 증권가 분석·투자 관점 기사 (STOCK_NOISE 로 제외되진 않지만 우선순위는 낮춤)
ANALYST_NOISE_KEYWORDS = [
    "해운주", "조선주", "수혜주", "공략법", "급등", "급락", "강세", "약세", "장중",
    "리포트", "커버리지", "밸류에이션", "모멘텀", "주목", "기대감", "증권",
]

# 순수 투자/주가성 뉴스 (투자자 대상 → 기술개발팀 무관 → 제외).
# 주의: 매각·인수·합병 등 M&A 용어는 넣지 않음 (고객사 매각 등 중요 소식이라 통과시켜야 함)
STOCK_NOISE_KEYWORDS = [
    "주가", "증시", "코스피", "코스닥", "목표주가", "목표가",
    "배당", "시가총액", "공매도", "실적발표", "어닝", "투자자", "자사주",
    "유상증자", "무상증자", "현금 요새", "밸류업", "저평가",
    "투자의견", "매수의견", "매도의견", "상한가", "하한가", "특징주",
    # 종목 분석 블로그·리포트 (2026-08-31 실측: 'HD현대중공업 투자분석 2026. 08. 29 -
    # 주달' 이 18점으로 고객사 카드에 실렸다. 기술개발팀과 무관한 투자자용 콘텐츠다)
    "투자분석", "종목분석", "기업분석 리포트",
    # 공시 묶음 기사 (개별 사안이 없는 목록). 실측(2026-09-01): '[개장 전 주요 공시]
    # HD현대중공업·코오롱·한화엔진·JW중외제약 등'이 15점으로 신조/조선 칸에 실렸다.
    # '공시' 단독은 넣지 않는다 — '수주 공시'처럼 실질이 있는 기사가 걸린다.
    "주요 공시", "개장 전", "장 마감", "공시 종합", "공시 브리핑",
    "초고수", "개장 선택", "매매 동향",
    "dividend", "earnings", "share price", "shareholder",
    "target price", "stock market", "market cap", "ipo",
    # 2026-09-07 피드백. 원문은 'HMM stock holds steady as Middle East disruptions reshape
    # shipping routes' 인데, 이 목록의 영문은 'stock market'·'share price' 처럼 두 단어
    # 짜리뿐이라 홑 단어 'stock' 이 빠져나갔다. 한글 '주가'에 대응하는 영문이 없던 셈이다.
    # 수집 단계는 번역 전이라 영문 제목 그대로 판정한다(번역은 2_summarize.py).
    #
    # 영문 키워드는 단어경계로 판정되므로 livestock(가축운반선)·feedstock(연료 원료)·
    # stockpile·Stockholm 에는 걸리지 않는다. 다만 그래서 'stock' 하나로는 'stocks' 를
    # 못 잡아 복수형도 함께 적는다. 같은 이유로 기존 'shareholder' 는 'shareholders' 를
    # 놓치고 있었다.
    "stock", "stocks", "shareholders", "share prices", "stock exchange",
    "buyback", "kospi", "kosdaq", "nasdaq", "nyse",
    # 회계·재무제표 분석 기사 (기술개발팀 무관).
    # 실측: '동부건설, HJ중공업 효과 지분법이익 47억원…재무제표에' 기사가 제목의
    # 'HJ중공업'으로 고객사 태깅돼 +6점을 받고 통과했다. 주어는 동부건설이고
    # 내용은 관계기업 투자손익이라 뉴스레터에 실을 이유가 없다.
    "지분법", "재무제표", "관계기업", "당기순이익", "부채비율", "자본잠식",
    "손익계산서", "감사보고서", "분기보고서", "영업권", "재무구조",
]

# 자금 조달·부동산 기사 — '제목'으로만 판정해 제외 (2026-09-28 피드백).
#   "해수부·HMM 부산행…부산역 '비스퀘어' 매각 주목 - Naver Blog 이런것도 안들어왔으면"
#   "10조 쓴 HMM, 현대무벡스 지분도 매각···'실탄 확보' 본격화 이런것도 굳이 없어도"
# 둘 다 HMM 고객사 태그(+6)로 13점대를 받아 신조/조선 칸에 실렸다. 회사가 돈을 어떻게
# 마련하고 건물을 어떻게 파는지는 항법·선박관리 제품과 접점이 없다.
#
# '매각'·'지분 매각' 단독은 절대 넣지 않는다 — 고객사의 매각·인수는 반드시 실어야
# 하는 소식이다(5_analyze 프롬프트). 실측으로 걸리면 안 되는 제목:
#   '산은·해진공, HMM 지분 매각 재추진'   ← HMM 자체의 주인이 바뀌는 건
#   'HD현대, 한화오션 지분 매각 검토'      ← 조선소 지배구조
# 그래서 '보유 자산을 팔아 현금을 만든다'는 뜻으로만 쓰이는 말로 좁힌다.
CORP_FINANCE_TITLE_KEYWORDS = [
    # 자금 조달
    "실탄 확보", "실탄확보", "실탄 마련", "자금 확보", "자금확보", "자금 조달",
    "자금조달", "유동성 확보", "현금 확보", "현금확보", "재원 마련", "재원 확보",
    "지분도 매각", "보유 지분 매각", "보유지분 매각", "투자자산 매각", "자산 유동화",
    "블록딜", "회사채", "영구채", "신종자본증권", "기업어음",
    # 부동산 (사옥·오피스 빌딩 매매·임대)
    "사옥 매각", "사옥 매입", "사옥 이전", "신사옥", "오피스 빌딩", "오피스빌딩",
    "빌딩 매각", "빌딩 매입", "부동산", "리츠", "임대차", "임차 계약",
    "sale-and-leaseback", "sale and leaseback", "office building", "headquarters building",
    "bond issue", "bond sale",
    # 지분 인수 (2026-10-12 피드백: 'HD현대마린솔루션, 3314억원 규모 해외 선박 유지보수사
    # 지분 인수' — "별 상관 없을 것 같아"). 투자·재무 거래라 제품·기술 소식이 아니다.
    "지분 인수", "지분 취득", "지분 매입", "stake acquisition", "acquires stake",
    # 같은 날 두 번째: 'HD현대마린솔루션, 글로벌 엔진 MRO 기업 골텐스 3300억에 인수' 필요없음
    "억에 인수", "억원에 인수", "억 원에 인수", "억 규모 인수", "억원 규모 인수",
]

# 블로그·개인 게시글 — 언론 기사가 아니다. Google News 가 섞어서 준다.
# 실측(2026-09-28): '해수부·HMM 부산행…부산역 '비스퀘어' 매각 주목 - Naver Blog'.
# 제목 꼬리의 매체명(strip_outlet 이 떼는 부분)과 링크 주소 둘 다 본다.
BLOG_OUTLETS = ["naver blog", "네이버 블로그", "네이버블로그", "tistory", "티스토리",
                "brunch", "브런치", "daum blog", "다음 블로그", "medium", "velog",
                "blogspot", "wordpress.com", "substack"]
BLOG_HOSTS = ["blog.naver.com", "m.blog.naver.com", "tistory.com", "brunch.co.kr",
              "blog.daum.net", "medium.com", "velog.io", "blogspot.com", "substack.com"]


def is_corp_finance_noise(title: str) -> bool:
    """자금 조달·부동산 기사인가 (제목 기준)."""
    return km.any_keyword(km.strip_quotes(km.strip_outlet(title)).lower(),
                          CORP_FINANCE_TITLE_KEYWORDS)


def is_blog(title: str, link: str = "") -> bool:
    """블로그 게시글인가 — 제목 꼬리 매체명 또는 링크 주소로 판정."""
    title = title or ""
    if " - " in title:
        outlet = title.rsplit(" - ", 1)[1].strip().lower()
        if any(b in outlet for b in BLOG_OUTLETS):
            return True
    return any(h in (link or "").lower() for h in BLOG_HOSTS)

# 증권가 코너·종목 시황 기사 — '제목'으로만 판정해 제외 (2026-09-08 피드백).
#
# STOCK_NOISE_KEYWORDS 와 나눠 놓은 이유가 핵심이다. 그쪽은 제목+요약(full_text)을
# 보는데, 여기 있는 말들은 요약에 한 번 스치기만 해도 정상 기사를 죽인다
# ('증권'·'장중'이 특히 그렇다). 그래서 이 목록은 제목만 본다.
#
# 실측(2026-09-08, 제목 801개): 주식 신호어가 있는 29건 중 17건이 기존 필터를
# 빠져나갔다. 감점(ANALYST_NOISE_KEYWORDS)으로는 못 막는다 — '조선주'·'장중'은
# 거기 있는데도 -3점일 뿐이라 고객사 태그(+6)가 붙으면 그대로 올라온다.
#   실측 누락 예: '조선주 장중 혼조…HD현대중공업·HD한국조선해양 상승'
#                 '[마감분석] 팬오션, 뚜렷한 호재 없이 7%대 급등 마감 : 기업주식정보'
#                 'LS증권 "HD현대중공업 힘센 엔진으로 성장 가속"'
#
# 전수 검토에서 뺀 말 (오제외를 만든다):
#   '악재' — '러시아측 "한화오션 1.37조 물어내라"…대형 악재'가 걸린다.
#            Arctic LNG 2 국제중재 기사로, 지난주 뉴스레터에 실린 정상 기사다.
#   '급락'·'강세' — 'HMM 정시운항률 14%로 급락'(운항 실적),
#                   '원화 강세에 조선사 3분기 실적 복병'(환율)이 걸린다.
#   '호재'·'종목' 단독 — 위 기사들이 '주식정보'·'핫종목'으로 이미 걸려 얻는 게 없다.
STOCK_TITLE_NOISE_KEYWORDS = [
    # 종목 묶음 표현
    "조선주", "해운주", "수혜주", "테마주", "관련주", "핫종목", "테마 강세",
    # 증권 매체의 고정 코너명 (제목에 그대로 박혀 나온다)
    "주식정보", "코주부", "마감분석", "공시 pick", "공시pick", "버핏",
    # 증권사 리포트 인용 기사 / 장중 시황
    "증권", "장중",
]


def is_stock_title_noise(title: str) -> bool:
    """증권가 코너·종목 시황 기사인가 (제목 기준)."""
    return km.any_keyword(km.strip_quotes(km.strip_outlet(title)).lower(),
                          STOCK_TITLE_NOISE_KEYWORDS)


# 스포츠·연예 후원 기사 (회사명이 '스폰서'로만 등장 → 사업 소식 아님 → 제외).
# 실측: '[골프청사진] 나이스 샷 HJ중공업 조아연, 2026 오로라월드 챔피언십 무빙데이'가
# 제목의 'HJ중공업'(골프단 후원사)으로 고객사 태깅돼 +6점을 받고 통과했다.
# 일반 명사와 겹치는 말('경기', '우승', '대회')은 넣지 않는다 — 오제외 위험.
SPORTS_NOISE_KEYWORDS = [
    "골프", "챔피언십", "무빙데이", "나이스 샷", "버디", "이글", "언더파", "타수",
    "lpga", "klpga", "kpga", "pga", "kbo", "프로야구", "프로축구", "프로배구",
    "야구단", "축구단", "농구단", "배구단", "구단", "선수단", "감독 선임",
    "올림픽", "월드컵", "아시안게임", "리그 개막", "시즌 개막",
    "드라마", "예능", "아이돌", "콘서트", "영화제",
    # 영문 표기 (2026-10-12 피드백 — 'Yoo Hyun-jo leads
    # HJ Shipbuilding & Construction Championship' 이 국문 목록만 있어 통과했다)
    "championship", "golf", "golfer", "tournament",
]

# 기부·사회공헌 기사 (2026-09-08 피드백).
#
# 왜 감점이 아니라 제외인가: 보도자료가 여러 매체에 그대로 뿌려져 보도량이 크게
# 잡힌다. 보도량을 '사건의 크기'로 쓰는 이상(coverage_bonus) 감점으로는 계속 올라온다.
# 실측(2026-09-08): '한국선급, 부산 초중고에 발전기금 1억1천만원 기탁'이 11개 매체에
# 실려 가점 +2 를 받고 글로벌 컴플라이언스 카드에 올랐다. 그 자리에서 밀려난 것이
# 'CMA CGM·Bureau Veritas, AI 지원 컨테이너선' 기사였다.
#
# 오제외를 피하려고 뺀 말들:
#   '기부' 단독  — '기부채납'(항만 부지를 국가에 넘기는 법률 용어)이 걸린다.
#                  그래서 아래 predicate 에서 그 말을 지우고 판정한다.
#   '선행'       — '선행 연구'·'선행 기술' 이 걸린다. 기술 기사가 통째로 죽는다.
#   '후원' 단독  — 기업 스폰서십·연구 후원에 두루 쓰인다('후원금'만 넣는다).
#   '장학' 단독  — PROMO_NOISE_KEYWORDS 에 이미 있다(감점). 여기서는 금품 전달을
#                  뜻하는 '장학금'만 잡는다.
DONATION_NOISE_KEYWORDS = [
    "기탁", "성금", "기부", "후원금", "장학금", "사회공헌", "사회 공헌",
    # '발전기금' (2026-09-21 피드백). 이 목록의 '기탁'으로 잡히던 유형인데, 이번 제목은 전달 동사 없이
    # '발전기금'만 쓰고 뒤를 '…40년 산학협력으로 해운 인재 육성'으로 이었다.
    # 실측: '한국선급, 부산 초중고에 발전기금 1억1천만원 기탁'(2026-09-08)은 '기탁'으로
    # 걸렸지만 '대한해운, 목포해양대에 발전기금…'은 그대로 통과해 고객사 카드에 실렸다.
    # '기금' 단독은 넣지 않는다 — '해운산업 지원기금 조성'처럼 정책 기사가 걸린다.
    "발전기금", "발전 기금", "기금 전달", "기금 출연",
    "이웃돕기", "이웃 돕기", "사랑의 열매", "나눔 행사", "나눔행사",
    "연탄 나눔", "김장 나눔", "온정", "구호품", "위문품",
    # 영문은 단어경계로 판정되므로 활용형을 다 적는다(LABOR_NOISE 주석과 같은 이유).
    "donation", "donations", "donate", "donates", "donated",
    "charity", "charitable", "philanthropy", "fundraiser", "fundraising",
    # 명절 전 협력사 대금 조기 지급 (2026-09-28 피드백). 기부와 같은 상생 홍보 보도자료로, 조선사마다 설·추석 직전에 한꺼번에
    # 낸다. 실측: '케이조선, 협력사 대금 253억원 조기 지급'(13점, 고객사 카드).
    # '대금' 단독은 넣지 않는다 — '선박 건조대금'·'중도금 수령'처럼 수주 기사가 걸린다.
    "조기 지급", "조기지급", "대금 조기", "조기 집행", "상생결제", "상생 결제",
    # 인도적 지원 (2026-10-12 피드백: 'HMM, 레바논 난민에 겨울 의류 운송 지원…20억원 상당
    # 구호물품 해상운송' 관련없음). '해상운송'이 있어 해운 기사처럼 보이지만 사회공헌이다.
    "구호물품", "구호 물품", "난민", "이재민", "인도적 지원",
    # 2026-10-12 피드백: 'Ocean Network Express expands partnership with Project Maji to
    # Nigeria'(식수 지원 사회공헌) 빼라
    "project maji", "charity", "humanitarian", "clean water",
]

# '기부'를 품고 있지만 기부가 아닌 말. 판정 전에 지운다(_mask_confusables 와 같은 방식).
DONATION_CONFUSABLES = ["기부채납"]

# 인물 임명·인사 발령 기사 (기술·사업 소식이 아님 → 제외).
# NOISE_KEYWORDS 의 -4점 감점만으로는 부족하다. 실측: '선박해양플랜트연구소 신임 소장에
# 정종진 전 현대중공업 상무'가 16점(고객사 +6 + 제품 키워드)으로 고객사 동향에 실렸다.
# 주어는 연구소이고 '현대중공업'은 인물의 전 직장으로 스쳤을 뿐이다.
# 행사·전시·시상 안내 기사 (실체 있는 사업·기술 소식이 아님 → 제외).
# 반드시 '제목'으로만 판정한다. 본문 판정은 오제외를 낳는다 — 실측: '삼성중공업, 美 방산
# 새 길' 본문의 '수상'(수상함/水上艦)이 시상식으로 오인돼 PROMO 키워드에 걸렸다.
# '개최'는 넣지 않는다: 'IMO MEPC 회의 개최'처럼 규제 뉴스 제목에도 쓰인다(감점은 유지).
EVENT_NOISE_TITLE_KEYWORDS = [
    "한자리에", "세미나", "포럼", "엑스포", "박람회", "전시회", "설명회",
    "심포지엄", "워크숍", "워크샵", "간담회", "좌담회", "기념식", "시상식", "수여식",
    "출품", "참가 신청", "참관", "미리보기", "사전등록", "부스",
    "conference", "exhibition", "webinar", "expo", "symposium", "workshop",
    # 전시회 부대 행사·기념 (2026-09-01 피드백). 주제(S-100)는 맞지만 내용이
    # '전시회에서 1주년을 기념한다'라 사업·기술 소식이 없다.
    # '기념' 단독은 넣지 않는다 — '창립 기념 수주' 같은 실질 있는 제목이 걸린다.
    "주년 기념", "1주년", "기념 행사", "기념행사", "anniversary",
    # 주요 해사 전시회 이름. 이름이 제목에 있으면 대개 참가·부스·발표 안내다.
    "smm 2026", "nor-shipping", "posidonia", "marintec", "kormarine", "seatrade",
    # 수출 상담회·판로 개척 (2026-09-01 피드백). 기업 개별 소식이 아니라 지자체 지원사업 홍보다.
    "수출길", "수출 상담", "수출상담", "판로 개척", "판로개척", "시장 공략",
    "해외 진출 지원", "무역사절단", "통상촉진단",
    # 인재·인력 양성 (2026-08-24 피드백).
    # PROMO_NOISE_KEYWORDS 에도 있지만 그쪽은 감점(-5)일 뿐이라 못 막았다. 실측
    # (2026-09-01 회귀 테스트): '국립목포해양대, 해운업계 50억 지원 업고 북극항로 인재
    # 허브 도약'이 감점을 받고도 5점으로 통과했고, 8/24 당시엔 15점으로 게재됐다.
    # 피드백은 "점수를 낮춰라"가 아니라 "필요 없다"였으므로 제목 기준 제외로 올린다.
    # 기술 개발 기사는 그대로 남는다('경상국립대, AI 기반 선박 자율 계류 시스템 개발').
    "인재 양성", "인재양성", "인재 허브", "인재허브", "인력 양성", "인력양성",
    "인재 확보", "인재확보", "실무인재", "인력난", "인력 수급",
    # '육성' 표기 (2026-09-21). 위 목록이 '양성'·'허브'·'확보'만 담고 있어
    # '40년 산학협력으로 해운 인재 육성'이 그대로 통과했다. 같은 성격의 기사다.
    # '육성' 단독은 넣지 않는다 — '조선 기자재 산업 육성 대책'처럼 정책 기사가 걸린다.
    "인재 육성", "인재육성", "인력 육성", "인력육성", "해기사 양성", "해기사 육성",
    # 채용·공개채용 (2026-10-06 피드백). 실측 제목: '한국선급, 미래 해사기술 인재 57명 뽑는다'(13점,
    # 컴플라이언스 태그). '채용'·'뽑' 계열 표현이 하나도 없어 그대로 통과했다.
    # '채용' 단독은 넣지 않는다 — 해사 기사에서 '채택'의 뜻으로 흔히 쓴다
    # ('메탄올 추진 엔진 채용', '신기술 채용 선박'). 인력 모집을 가리키는 말만 넣는다.
    "공개채용", "공개 채용", "공채", "신입사원", "신입 채용", "경력 채용", "경력직",
    "채용 공고", "채용공고", "채용 박람회", "채용박람회", "채용 설명회", "인턴 채용",
    "인재 채용", "인력 채용", "직원 채용", "명 채용", "명 뽑", "명을 뽑", "인재 모집",
    "인력 모집", "hiring", "recruitment drive", "job openings", "graduate programme",
    # 창간·축사 (같은 날 7_watch LLM 검수가 '홍보'로 지적: '<현대해양>창간 57주년 축사').
    # 위 '주년 기념'은 '57주년 축사'를 못 잡는다.
    # '창간' 단독은 넣지 않는다 — '[창간특집][HMM] 바다 위 테슬라 꿈꾼다…AI 자율운항'
    # (23점)처럼 창간 기획으로 실린 정상 기사가 걸린다(실측).
    "축사", "창간 기념", "창간기념", "기념사",
    # 정부 지원사업·공모 선정 (2026-09-01 피드백).
    # 지원 대상으로 뽑혔다는 발표는 아직 아무 일도 일어나지 않은 소식이다.
    # PROMO 의 '선정됐'(감점)만으로는 못 막았다 — 실측: '안개·어둠 뚫는 선박의 AI 눈,
    # 시흥 한국공학대 교원창업 TIPS 선정'이 감점을 받고도 통과했다.
    # '선정' 단독은 넣지 않는다 — '우선협상대상자 선정'처럼 실질이 있는 건이 걸린다.
    "팁스", "tips 선정", "지원사업 선정", "지원 사업 선정", "특성화대학",
    "교원창업", "창업기업 선정", "공모 선정", "공모에 선정", "사업자 선정",
    "지원 대상 선정", "지원대상 선정", "선정 사업", "국책과제 선정",
    # 같은 기사가 표현만 바꿔 다시 들어왔다(2026-09-01). '한국공학대 교원창업 TIPS
    # 선정'을 막았더니 '한국공학대 교원기업 아이케이에랩'으로 되돌아와 기술 동향
    # TOP4 를 차지했다. 매체마다 부르는 말이 달라 표기를 같이 적어 둔다.
    "교원기업", "교원 기업", "교원 창업", "실험실 창업", "창업지원단",
    "시드 투자 유치", "시리즈a 유치", "액셀러레이팅",
    # 2026-10-12 피드백(관련없는 기사). 행사·지자체 홍보가 'AI·해양' 두 단어로 AI 융합
    # 가점을 받아 20점대로 AI 칸 맨 위에 올랐다:
    #   '조선·해양에 AI·에너지 더했다…국제해양에너지플랜트산업전 14일 개막'   20점
    #   '부산시장, 민선 9기 출범 100일…해양수도·AI 대전환으로 부산 미래'     24점
    "산업전", "박람회 개막", "전시회 개막",
    # 기관의 스타트업 지원 소식 (2026-10-12 피드백: '인천항만공사, 공공데이터로 스타트업
    # AI 선박 출항시간 예측 서비스 출시 지원' 필요 없는 기사, 13점·기술 동향 TOP2).
    # 주체가 기관이고 내용이 '지원'이라 업계 기술 동향이 아니다.
    "스타트업 지원", "출시 지원", "스타트업 키운다", "스타트업 육성", "기술사업화 지원",
    "민선", "출범 100일", "취임 100일", "취임 1년", "시정 성과", "도정 성과",
    # 같은 날 추가 피드백: '[부산광역시] 아시아 창업 엑스포 FLY ASIA 2026', 'HD현대
    # 조선사업 총괄 부회장 선임 및 조직 개편' 빼라
    "엑스포", "expo", "fly asia", "플라이 아시아", "창업 박람회",
    "부회장 선임", "사장 선임", "조직 개편", "조직개편", "임원 인사",
]

# 학교·교육 프로그램 기사 (해사 뉴스가 아님 → 제외).
# 실측: '부산교육청 여름방학 AI·디지털 체험 교육'(14점), '[교육소식]부산SW·AI교육거점센터,
# 디지털 어드벤처 운영'(14점)이 'AI·디지털' 키워드로 점수를 얻어 통과했고, 한 줄 결론에
# "부산·교육청·AI·디지털 체험 교육 실시"로 인용되기까지 했다.
# 주의: '연수원'·'교육원'은 넣지 않는다 — 해양수산연수원·해경교육원 같은 교육기관도 해사 분야 수요처다.
EDU_NOISE_TITLE_KEYWORDS = [
    "교육청", "교육소식", "교육거점", "체험 교육", "여름방학", "겨울방학",
    "초등학교", "중학교", "고등학교", "특성화고", "마이스터고", "동아리",
    "진로체험", "특강", "캠프", "장학금", "입시", "학예", "방과후",
    # 학생 경진대회 수상 (2026-09-21 피드백). 주제(자율운항)는 맞지만 학생 팀의 대회 우승 소식이라 업계에 미치는
    # 영향이 없다. PROMO_NOISE 의 '수상'(감점 -5)만으로는 못 막았다 — 실측으로 15점을
    # 받아 2주 연속 실렸고, LLM 검수도 2주 연속 '홍보'로 지적했다.
    #
    # 'challenge'·'competition'·'award' 단독은 절대 넣지 않는다 — 정상 기사가 죽는다
    # ('decarbonisation challenge', '수주 경쟁', 'award 수주'). 학생·대회를 직접
    # 가리키는 말만 넣고, 그 밖은 아래 STUDENT_WIN_PATTERN 이 형태로 잡는다.
    "경진대회", "경연대회", "공모전", "해커톤", "hackathon",
    "young achievers", "student team", "student teams", "대학생 팀", "학생팀",
    # 2026-10-12 피드백: '해수부, 제13회 여수 해양법 아카데미 개최' 관련없음(20점)
    "아카데미",
]

# '학생이 무엇을 이겼다' 형태. 위 목록이 못 잡는 영문 표기를 형태로 잡는다.
# 실측 제목이 'students win Njord Autonomous Ship Challenge' 라 'students'+'win'
# 사이가 붙어 있지 않아도 걸리게 두되, 사이 거리를 짧게 제한해 오탐을 막는다.
STUDENT_WIN_PATTERN = re.compile(
    r"\b(students?|undergraduates?|cadets?)\b[^.]{0,30}\b(wins?|won|takes?|"
    r"claims?|sweeps?)\b", re.I)

# 사진 캡션·짧은 영상 클립 기사 — 본문이 없어 실을 내용이 없다.
# '[영상]'만으로는 거르지 않는다: '[영상]LNG 다음은 암모니아?…삼성중공업이 뛰어든 이유'처럼
# 제대로 된 기사에도 붙는 표기다. 클립 전용 표기('짤막영상' 등)만 잡는다.
PHOTO_CAPTION_PATTERN = re.compile(
    r"\[[^\]]{0,10}(포토|화보|사진구성|그래픽뉴스|짤막영상|영상뉴스|movie)[^\]]{0,10}\]")

# 기업 홍보성 '경영 슬로건' 기사 (실체 있는 사업·기술 소식이 아님 → 제외).
# 실측: '수시 점검부터 실시간 감시까지…HJ중공업 안전 경영 강화[짤막영상]'(9점) —
# 해사 키워드 0개인데 사업맥락 '안전' 하나로 통과했다.
PR_NOISE_TITLE_KEYWORDS = [
    "안전 경영", "안전경영", "esg 경영", "esg경영", "상생 경영", "상생경영",
    "윤리 경영", "윤리경영", "투명 경영", "책임 경영", "동반성장", "사회공헌",
    "봉사활동", "나눔 활동", "캠페인 전개", "임직원 참여", "무재해",
    # 브랜드 평판·순위 기사 (2026-09-01 피드백).
    # 사업·기술 소식이 아니라 순위 나열이다. 고객사 태그로 17점까지 받았다.
    #
    # '빅데이터' 단독은 절대 넣지 않는다 — 정상 기사가 통째로 날아간다.
    # 실측: 'AI·빅데이터 공급망 가시성 확보, 해운·항만 생존 조건'(13점)이 걸린다.
    "브랜드평판", "브랜드 평판", "평판지수", "평판 지수", "브랜드 순위",
    "빅데이터 분석결과", "빅데이터 분석 결과", "브랜드 지수",
    # 2026-10-12 피드백: '올포랜드, 선박 최적운항 돕는 AI 해양 솔루션 눈길' 관련없음.
    # '눈길'은 제품 소개 기사(보도자료)의 상투 표현이다.
    "눈길", "눈길 끌어", "주목받는",
]

# 솔루션 도입 '절감액 홍보' 기사 (2026-09-21 피드백).
#
# 사건이 아니라 도입사·공급사가 내놓은 기대 효과 수치를 제목에 건 보도자료다. 본문도
# 플랫폼 기능 소개로 이어진다(실측: 2,382자 전부 Milahi 플랫폼 설명·CTO 인용).
# 기술 동향 카드는 '무슨 일이 있었는가'를 싣는 자리라 이런 기사가 TOP 에 오르면
# 광고를 읽는 셈이 된다 — 이번 주 실측에서 TOP2 를 차지했다.
#
# 키워드 목록이 아니라 정규식인 이유: 'savings'·'절감' 단독은 정상 기사가 통째로
# 죽는다('EU ETS 로 연료비 절감 압박', 'IMO 규제로 절감 의무'). '주체가 절감액을
# 내세운다'는 형태 — 동사(targets/saves/cuts) + 금액·절감 명사 — 일 때만 잡는다.
#
# 실측 오탐 점검(2026-09-21, 이번 주 47건): 걸리는 기사는 Noatum 1건뿐이다.
VENDOR_PROMO_PATTERNS = [
    # targets USD 27 million in savings / saves $3m with … / cuts costs by 20% with …
    re.compile(r"\b(targets?|eyes|saves?|cuts?|slashes|unlocks?|delivers?)\b"
               r"[^.]{0,50}\b(savings|cost savings|cost reduction)\b", re.I),
    re.compile(r"\b(million|billion|[0-9]+%)\b[^.]{0,20}\b(in savings|savings)\b", re.I),
    # 국문: '연 30억 절감 기대', '비용 20% 절감 효과'
    re.compile(r"(억|만원|%)\s*[^.]{0,12}(절감 효과|절감 기대|절감한다|아낀다)"),
]


def is_vendor_promo(title: str) -> bool:
    """솔루션 도입 절감액을 내세운 홍보성 기사인가 (제목 기준)."""
    t = km.strip_outlet(title or "")
    return any(p.search(t) for p in VENDOR_PROMO_PATTERNS)

# '사업 맥락' 신호. 해사 키워드가 하나도 없는 기사는 회사명 태그(+6점)만으로 통과하는데,
# 골프단 후원처럼 회사명이 스폰서로만 등장한 기사가 이 경로로 샌다.
# 실측: '[MT포토] 조아연, HJ중공업 멋져요'(9점) — 해사 키워드 0개, 고객사 태그 HJ중공업.
# 스포츠 단어를 계속 늘리는 대신, 회사명만으로 들어온 기사에 사업 맥락을 요구한다.
BUSINESS_CONTEXT_KEYWORDS = [
    "수주", "계약", "발주", "인수", "매각", "합병", "출범", "설립", "투자", "지분",
    "건조", "인도", "진수", "명명식", "공장", "야드", "도크", "생산", "개발",
    "실적", "영업이익", "매출", "수출", "협력", "협약", "mou", "파트너십",
    "진출", "확대", "증설", "사업", "기술", "특허", "인증", "승인", "면허",
    # '안전'은 뺐다: 해사 키워드가 0개인데 '안전'만 있는 기사는 사실상 전부 안전경영
    # 홍보물이다(실측: 'HJ중공업 안전 경영 강화[짤막영상]' 9점). 진짜 안전 뉴스라면
    # '선박·조선소·해운' 등 해사 키워드가 함께 있어 이 게이트를 거치지 않는다.
    "노조", "파업", "사고", "규제", "정책", "지원", "공급",
    "order", "contract", "delivery", "newbuild", "merger", "acquisition",
    "partnership", "investment", "launch", "supply", "expansion",
]

# 옛 사명 게이트(is_legacy_name_article)는 제거했다(2026-08-24).
#
# 문제였던 건 '옛 사명으로 쓰인 기사'가 아니라 '실제 작성일이 옛날인 기사'다
# (2019년 대우조선해양 기사가 pubDate 'Thu, 20 Aug 2026'로 들어왔다). 사명은
# 그 상황에서 쓸 수 있었던 프록시일 뿐이고, 사명이 안 바뀐 회사의 옛 기사는 못 잡는다.
# 지금은 원인을 직접 막는 장치가 둘 있다:
#   ㉠ gnews() 의 'when:7d'  — 구글이 옛 기사로 정원을 채우는 것을 서버에서 차단
#   ㉡ drop_stale_by_body()  — 본문 머리의 발행일과 피드 날짜를 대조
# 남은 사각지대는 news.google.com 링크(원문 주소·메타데이터 모두 막혀 있어 ㉡ 불가)이며,
# 그쪽은 ㉠ 이 유일한 방어선이다.

_TITLES = ("소장|원장|사장|대표|회장|부회장|본부장|센터장|이사장|단장|지사장|청장"
           "|위원장|상무|전무|부사장|학장|총장|대표이사|본부장")
# 영문 직함. 약어는 단어경계로만 맞춘다('cto'가 'sector'에, 'md'가 'mdo'에 걸리지 않게).
_TITLES_EN = (r"\b(ceo|cfo|coo|cto|cio|cco|cso|chro|chief\s+\w+(\s+\w+)?\s+officer"
              r"|president|vice[\s-]president|chairman|chairwoman|chair|board\s+member"
              r"|managing\s+director|general\s+manager|executive\s+director"
              r"|non-executive\s+director|head\s+of\s+\w+|country\s+manager"
              r"|regional\s+director|svp|evp)\b")
PERSONNEL_NOISE_PATTERNS = [
    re.compile(r"\[\s*인\s*사\s*\]"),
    re.compile(r"(신임|차기|초대|후임)\s*" + _TITLES),
    re.compile(_TITLES + r"\s*(에|으로)\s+\S+\s*(선임|임명|내정|취임|위촉|승진)"),
    re.compile(r"(선임|임명|내정|취임|위촉)\s*(했다|됐다|한다|된다)?\s*$"),
    # 영문 인사 기사 (2026-09-28 피드백). 위 패턴이 전부 한국어라 영문판은 하나도 안 걸렸다. 실측:
    # 'Jan Erik Hoff appointed CFO of Kongsberg Maritime'(9점)이 경쟁사 카드 1번에 실렸다.
    #
    # 동사만으로는 판정하지 않는다 — 'appointed as authorised service partner',
    # 'named preferred bidder' 처럼 사업 기사에도 같은 동사가 쓰인다. 반드시 직함이
    # 가까이 붙어 있을 때만 인사로 본다.
    # 'welcomes'·'former' 는 뺐다 — 'Port welcomes President …'(대통령 방문),
    # 'former president'(정치 기사)처럼 인사가 아닌 제목에 흔하다.
    re.compile(r"\b(appoint(s|ed)?|names?|named|promot(es|ed)|elevat(es|ed)|hires?|hired"
               r"|taps?|tapped|joins?)\b.{0,50}?\b" + _TITLES_EN, re.I),
    re.compile(r"\b(new|incoming|interim|outgoing)\s+" + _TITLES_EN, re.I),
    re.compile(_TITLES_EN + r".{0,30}?\b(steps? down|to step down|resigns?|retires?"
               r"|departs?|to leave|succession|successor)\b", re.I),
]


# 아크메 자체 언급 (우리 회사 소식 → 전용 배지 + 기술 동향에 우선 노출)
OWN_COMPANY_KEYWORDS = [
    "아크메", "acme marine", "acme electronics",
    # 국문 사명 표기 (2026-09-01 요청). 영문명은 이미 있었지만
    # 국문으로 '아크메전자'라고 쓰는 매체가 있어 그 기사를 통째로 놓치고 있었다.
    # '아크메전자상사'도 같은 회사다(2026-09-01 확인) — '아크메전자'가 부분 문자열이라
    # 따로 적지 않아도 걸리지만, 같은 회사임을 남겨 둔다.
    "아크메전자",
]


def is_stock_noise(text: str) -> bool:
    """순수 투자/주가성 기사인지 판별 → True면 수집에서 제외 (M&A는 제외 대상 아님)."""
    lower = text.lower()
    return km.any_keyword(lower, STOCK_NOISE_KEYWORDS)


def is_digest_noise(text: str) -> bool:
    """주기 발행 종합·전망 코너인지 판별 → True면 수집에서 제외."""
    return km.any_keyword(text.lower(), DIGEST_NOISE_KEYWORDS)


def is_sports_noise(text: str) -> bool:
    """스포츠·연예 후원 기사인지 판별 → True면 수집에서 제외."""
    return km.any_keyword(text.lower(), SPORTS_NOISE_KEYWORDS)


# 의학·임상 기사 (2026-10-06 피드백). 'S-100' 검색어에 뇌손상 표지자인
# 'S100 단백'이 걸려 들어왔고, 's100' 이 핵심어라 제목 해사 게이트도 통과했다(9점,
# 컴플라이언스 태그). 검색어·핵심어는 그대로 둔다 — S-100 전자해도 기사를 놓치면 안 된다.
# 제목 기준. '의료'·'환자'는 넣지 않는다 — 선원 원격의료·응급환자 이송 같은 해사 기사가 있다.
MEDICAL_NOISE_TITLE_KEYWORDS = [
    "단백", "임상", "뇌손상", "바이오마커", "진단검사", "전문의", "의학", "질환",
    "protein", "clinical", "biomarker", "brain injury",
]


def is_medical_noise(title: str) -> bool:
    """의학·임상 기사인지 판별 → True면 수집에서 제외 (제목 기준)."""
    return km.any_keyword(km.strip_outlet(title or "").lower(),
                          MEDICAL_NOISE_TITLE_KEYWORDS)


def is_donation_noise(title: str) -> bool:
    """기부·사회공헌 기사인지 판별 (제목 기준) → True면 수집에서 제외.

    반드시 '제목'으로만 본다. 본문 판정은 오제외를 낳는다 — 회사 소개 문단에
    사회공헌 활동을 곁들이는 기사가 흔하다.
    """
    lower = km.strip_quotes(km.strip_outlet(title)).lower()
    for word in DONATION_CONFUSABLES:
        lower = lower.replace(word, " ")
    return km.any_keyword(lower, DONATION_NOISE_KEYWORDS)


def has_business_context(text: str) -> bool:
    """사업·기술 맥락이 있는 기사인지 판별 (회사명만 스친 기사를 걸러내는 데 사용)."""
    return km.any_keyword(text.lower(), BUSINESS_CONTEXT_KEYWORDS)


def is_photo_caption(title: str) -> bool:
    """사진 캡션 기사인지 판별 → True면 수집에서 제외."""
    return PHOTO_CAPTION_PATTERN.search(km.strip_outlet(title or "")) is not None


def is_event_noise(title: str) -> bool:
    """행사·전시·시상 안내, 학교 교육 프로그램, 기업 홍보성 경영 슬로건 기사인지 판별
    → True면 수집에서 제외. 모두 제목 기준이다."""
    lower = km.strip_outlet(title or "").lower()
    return (km.any_keyword(lower, EVENT_NOISE_TITLE_KEYWORDS)
            or km.any_keyword(lower, EDU_NOISE_TITLE_KEYWORDS)
            or km.any_keyword(lower, PR_NOISE_TITLE_KEYWORDS)
            # 학생 경진대회 수상 (2026-09-21 피드백) — EDU 목록 주석 참고
            or bool(STUDENT_WIN_PATTERN.search(km.strip_outlet(title or ""))))


# 인사 기사라도 남기는 고객사 (2026-09-28 요청). 직접 고객 선사의 경영진 교체는
# 영업 창구·의사결정 라인이 바뀌는 소식이라 우리에게는 사업 정보다.
# 다른 회사를 여기에 더하려면 CUSTOMER_RULES 의 표기명을 그대로 적으면 된다.
PERSONNEL_KEEP_CUSTOMERS = {"ONE"}


def is_personnel_noise(title: str) -> bool:
    """인물 임명·인사 발령 기사인지 판별 → True면 수집에서 제외.
    본문에는 '신임 대표가 밝혔다' 같은 표현이 흔하므로 제목만 검사한다.
    PERSONNEL_KEEP_CUSTOMERS 의 고객사 인사는 제외하지 않는다."""
    t = km.strip_outlet(title or "")
    if not any(p.search(t) for p in PERSONNEL_NOISE_PATTERNS):
        return False
    return detect_customer(title or "") not in PERSONNEL_KEEP_CUSTOMERS


# 자사명을 부분 문자열로 품고 있는 '다른 것'의 이름.
#
# 2026-09-01: 자사명에 국문 표기를 추가하면서 필요해졌다. 업계 전시회 이름이 자사명을
# 부분 문자열로 품는 경우가 있다. 자사 태그는 노이즈 필터를 전부 면제받고 슬롯까지
# 보장받으므로, 오탐 하나가 전시회 홍보 기사를 매주 1면에 올려놓게 된다.
# 감지 전에 이 이름들을 지운다. 자사명을 바꾸면 이 목록도 함께 점검할 것.
# (기사가 진짜로 우리 회사도 다루면 사명이 따로 또 등장하므로 그때는 정상 태깅된다.)
OWN_CONFUSABLES = [
    "kormarine", "코마린",
]


def detect_own(text: str) -> bool:
    """아크메가 직접 언급된 기사인지 판별 (제목·본문 모두 검사)."""
    lower = text.lower()
    for name in OWN_CONFUSABLES:
        lower = lower.replace(name, " ")
    return km.any_keyword(lower, OWN_COMPANY_KEYWORDS)


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
    return km.any_keyword(lower, KEYWORDS)


def normalize_title(title: str) -> str:
    """중복 판정을 위한 제목 정규화 (소문자화 + 영숫자·한글만 남김)."""
    t = title.lower()
    t = re.sub(r"[^0-9a-z가-힣]+", "", t)
    return t


def relevance_score(text: str, customer: str | None, pub=None) -> int:
    """아크메 관련성 점수. 상한 60건만 남기므로 '변별력'이 핵심이다.

    설계 원칙 (기존 방식의 문제를 실측으로 확인해 개선):
      1. 일반 키워드 개수로 점수를 벌지 못하게 상한을 둔다.
         예전엔 일반어 +1 × 개수여서, 업계용어를 많이 머금은 행사 안내 기사가
         제품 직결 기사를 앞질렀다(99건 중 41건이 6점 동점 → 컷이 사실상 임의).
      2. 점수의 주축은 '제품 밀착도'(핵심어)로 옮긴다. 종류당 +5, 3종까지 인정.
      3. 같은 값이 쌓이지 않도록 최신성·감점 축을 추가해 해상도를 높인다.
    """
    # 따옴표를 지우고 판정한다 — 여러 단어로 된 한글 키워드가 따옴표에 깨진다
    # (km.strip_quotes 주석 참고). 실측: '차세대 선박연구센터' 출범 → '센터 출범' 미매칭.
    lower = km.strip_quotes(text).lower()
    score = 0

    # ── 주체: 누구 소식인가 ──────────────────────────────
    if customer:
        score += 3 if customer in RELATED_COMPANIES else 6

    # ── 제품 밀착도: 무엇에 관한 소식인가 (점수의 주축) ──
    # 종류 수만 인정하고 3종에서 포화 → 같은 주제 반복 언급으로 점수를 벌 수 없다
    # 동의어 묶음(CORE_KEYWORD_GROUPS)은 1종으로 센다 — 같은 주제를 여러 표기로
    # 적었다는 이유만으로 점수를 벌던 문제(북극 계열 11개 동의어) 방지.
    score += 5 * min(km.count_kinds(lower, CORE_RELEVANCE_KEYWORDS,
                                    CORE_KEYWORD_GROUPS), 3)
    score += 2 * min(km.count_keywords(lower, RELATED_RELEVANCE_KEYWORDS), 3)

    # ── 일반 해사 키워드: '해사 뉴스인지' 확인용 최소 신호 (상한 2) ──
    score += min(km.count_keywords(lower, KEYWORDS), 2)

    # ── AI 접목: 신호 + 적용대상이 함께 있을 때만 ──
    if (km.any_keyword(lower, AI_SIGNAL_KEYWORDS)
            and km.any_keyword(lower, AI_TARGET_KEYWORDS)):
        score += 4

    # ── 최신성: 주간 뉴스레터이므로 최근 소식을 앞세운다 ──
    if pub is not None:
        days = (datetime.now(timezone.utc) - pub).days
        if days <= 2:
            score += 3
        elif days <= 4:
            score += 2
        elif days <= 6:
            score += 1

    # ── 감점 ────────────────────────────────────────────
    # 노사 소식은 make_article 에서 아예 제외한다(is_labor_noise 참고) —
    # 여기서 감점만 하던 때는 19점·17점으로 여전히 상위에 남았다.

    score -= 4 * km.count_keywords(lower, NOISE_KEYWORDS)          # 인사·사내소식
    score -= 5 * km.count_keywords(lower, PROMO_NOISE_KEYWORDS)    # 행사·전시·수상
    score -= 3 * km.count_keywords(lower, ANALYST_NOISE_KEYWORDS)  # 증권가 분석
    return score


# 고객사/관련사 감지 규칙 (키워드 그룹 → 표기명).
# 조선소/해운사 구분은 build_html 의 CUSTOMER_TYPE 매핑에서 처리.
# 주의: 'hmm오션서비스'가 'hmm'보다 먼저 와야 같은 위치 매칭 시 HMM으로 잡히지 않는다.
CUSTOMER_RULES: list[tuple[list[str], str]] = [
    # ── 직접 고객 해운사 (가점 +5) ──────────────────────────
    (["hmm오션서비스", "hmm 오션서비스", "hmm ocean service"], "HMM오션서비스"),
    (["hmm", "hyundai merchant marine", "현대상선"], "HMM"),
    (["h-line", "hline", "에이치라인", "h line shipping"], "에이치라인해운"),
    (["hyundai lng", "현대lng", "현대 lng"], "현대LNG해운"),
    (["ocean network express", "one container", "one shipping",
      "오션네트워크익스프레스", "오션 네트워크 익스프레스"], "ONE"),
    (["예시해운1", "exampleshipping1"], "예시해운1"),
    (["예시해운2", "example shipping2"], "예시해운2"),
    (["예시해양기술"], "예시해양기술"),
    (["exampleline3", "예시선사3"], "ExampleLine3"),
    (["exampleline4"], "ExampleLine4"),
    # ── 관련 해운사 (직접 고객 아님, 가점 +3 — RELATED_COMPANIES 등재) ──
    (["pan ocean", "팬오션"], "팬오션"),
    (["korea line", "대한해운"], "대한해운"),
    (["sk shipping", "sk해운", "sk 해운"], "SK해운"),
    (["sinokor", "장금상선"], "장금상선"),
    (["hyundai glovis", "현대글로비스"], "현대글로비스"),
    (["polaris shipping", "폴라리스쉬핑", "폴라리스 쉬핑"], "폴라리스쉬핑"),
    (["kss해운", "kss 해운", "kss line"], "KSS해운"),
    (["klcsm"], "KLCSM"),
    (["시도상선", "sido shipping"], "시도상선"),
    # ── 주요 조선소 (가점 +5) ───────────────────────────────
    (["hyundai heavy", "hd hyundai heavy", "현대중공업", "현대重",
      "hd현대중공업"], "HD현대중공업"),
    (["hd한국조선해양", "한국조선해양", "korea shipbuilding"], "HD한국조선해양"),
    (["hanwha ocean", "한화오션", "대우조선"], "한화오션"),
    (["samsung heavy", "삼성중공업", "삼성重"], "삼성중공업"),
    (["hj중공업", "hj 중공업", "hj heavy", "hj shipbuilding"], "HJ중공업"),
    # "k조선"은 제외: 한국 조선업계를 통칭하는 표현("K조선 슈퍼사이클", "美훈풍 올라탄
    # K조선")이라 케이조선(구 STX조선해양)과 무관한 기사가 대량 오태깅된다.
    (["케이조선", "k shipbuilding"], "케이조선"),
]

# 직접 고객이 아닌 '관련 해운사' — 감지/배지는 유지하되 가점을 +3으로 낮춤
RELATED_COMPANIES = {"팬오션", "대한해운", "SK해운", "장금상선", "현대글로비스",
                     "폴라리스쉬핑", "KSS해운", "KLCSM", "시도상선"}


# 고객사명을 부분 문자열로 품고 있는 '다른 기관' 이름.
#
# 한글 키워드는 단어경계를 못 쓴다(조사가 붙어 쓰이므로 부분 문자열 판정이다.
# keyword_match.py 상단 주석 참고). 그래서 '한국조선해양기자재연구원'이
# '한국조선해양'을 품어 HD한국조선해양으로 태깅됐다.
# 실측(2026-08-25 피드백): '국립한국해양대-한국조선해양기자재연구원, 해양 AX 혁신허브
# 구축' 기사에 HD한국조선해양 배지가 붙어 배지와 내용이 어긋났다.
#
# 감지 전에 이 이름들을 지워서 안쪽의 고객사명이 안 보이게 한다.
# (기사가 진짜로 고객사도 함께 다루면 회사명이 따로 또 등장하므로 그때는 정상 태깅된다.)
CUSTOMER_CONFUSABLES = [
    "한국조선해양기자재연구원", "한국조선해양기자재조합",
    "한국조선해양플랜트협회", "한국조선해양기술",
    "국립한국해양대", "한국해양대학교", "한국해양대",
    "한국해양수산연수원", "한국해양진흥공사",
]


def _mask_confusables(text: str) -> str:
    for name in CUSTOMER_CONFUSABLES:
        text = text.replace(name.lower(), " ")
    return text


# ONE 선사는 헤드라인에서 대부분 'ONE' 세 글자로만 쓴다('ONE appoints new CEO',
# 'ONE, 신임 CEO에 …'). CUSTOMER_RULES 는 소문자로 비교하므로 'one' 을 넣으면 수사
# 'one' 이 전부 걸린다(2_summarize 주석 참고). 그래서 대문자 단독 'ONE' 만 따로 본다.
# 제목 전체가 대문자인 경우는 제외한다 — 'ONE STEP CLOSER TO …' 같은 제목이 걸린다.
_ONE_UPPER = re.compile(r"(?<![A-Za-z])ONE(?![A-Za-z])")


def _mentions_one(title: str) -> bool:
    t = km.strip_outlet(title or "")
    # 대문자 비율은 영문 '단어'가 충분할 때만 본다. 'ONE, 신임 CEO에 …' 는 영문자가
    # 전부 대문자지만 약어 두 개뿐인 한국어 제목이다.
    words = re.findall(r"[A-Za-z]+", t)
    mostly_upper = (len(words) >= 4
                    and sum(w.isupper() for w in words) > len(words) * 0.6)
    return bool(_ONE_UPPER.search(t)) and not mostly_upper


def detect_customer(text: str) -> str | None:
    """고객사 감지 → 표기명 반환. 여러 고객사가 등장하면 '가장 먼저 등장한' 회사 선택.
    제목에 적용하면 한국어 헤드라인 특성상(주어가 맨 앞) 기사 주제와 배지가 일치한다.
    거래 상대로 스쳐 언급된 회사(뒤쪽 등장)에 잘못 매칭되지 않는다."""
    lower = _mask_confusables(km.strip_outlet(text).lower())
    best_name, best_pos = None, len(lower) + 1
    for kws, name in CUSTOMER_RULES:
        pos = km.first_position(lower, kws)
        if 0 <= pos < best_pos:
            best_pos, best_name = pos, name
    if _mentions_one(text):
        pos = _ONE_UPPER.search(km.strip_outlet(text)).start()
        if pos < best_pos:
            best_name = "ONE"
    return best_name


# 경쟁사 감지 (키워드 → 표기명). 스마트십/자율운항 + 해운 ERP 벤더
COMPETITORS = [
    # 스마트십 / 자율운항 / 상황인지
    (["competitor1", "경쟁사1", "경쟁사1"], "Competitor1(경쟁사1)"),
    (["competitor2", "경쟁사2"], "Competitor2(경쟁사2)"),
    (["경쟁사4"], "경쟁사4"),
    (["competitor5", "competitor5", "competitor5", "경쟁사5"], "경쟁사5"),
    (["competitor3", "경쟁사3"], "Competitor3"),
    (["avikus", "아비커스"], "Avikus(아비커스)"),
    (["nautical systems"], "ABS Nautical Systems"),
    (["kongsberg"], "Kongsberg"),
    (["meguri"], "MEGURI 2040"),
    (["navtor"], "NAVTOR"),
    (["hd현대마린솔루션", "현대마린솔루션", "hyundai marine solution"], "HD현대마린솔루션"),
    # 해운 ERP / 선박관리 소프트웨어
    (["경쟁사7", "marineoffice"], "경쟁사7"),
    (["competitor6", "경쟁사6"], "경쟁사6"),
    (["spectec", "amos "], "SpecTec (AMOS)"),
    (["bassnet", "bass software"], "BASSnet"),
    (["shipnet"], "ShipNet"),
    (["hanseaticsoft", "cloud fleet manager"], "Hanseaticsoft"),
    (["helm connect"], "Helm CONNECT"),
    (["mespas"], "MESPAS"),
    (["veson", "imos"], "Veson"),
    (["mesoft"], "MeSoft"),
]

# 글로벌 컴플라이언스(규제/선급/선박평가·베팅) 감지 키워드
COMPLIANCE_KEYWORDS = [
    "imo", "iacs", "dnv", "lloyd's register", "lloyds register", "bureau veritas",
    "classnk", "class nk", "korean register", "한국선급", "선급",
    "mepc", "solas", "marpol", "unified requirement", "ur e26", "ur e27",
    # 단어경계 판정이므로 예전처럼 " cii " 같은 공백 트릭이 필요 없다
    "eu ets", "fueleu", "cii", "imo 규제", "해사 규제", "환경규제",
    # IMO 온실가스 감축 체계 (2026-08-25 피드백) — 규제 섹션에서 다뤄야 할 사안이다
    "ghg", "온실가스", "net-zero framework", "net zero framework", "넷제로",
    # 선박관리 평가 / 베팅 (탱커·선사 대상)
    "tmsa", "rightship", "right ship", "vetting", "sire", "ocimf",
    # 해도·데이터 표준 / 자율운항 코드 ("s-100" 표기가 일반적이라 둘 다 등재)
    "iho", "국제수로기구", "ukho", "s-100", "s100", "mass code",
    # 극지 운항 규정 (북극항로 확대와 함께 따라오는 IMO 규정)
    "polar code", "극지운항", "극지 운항 규정",
    # 해도 관련 '규제' 용어만 좁게 넣는다 (2026-08-24 피드백: ENC 포함 여부 확인 요청).
    #
    # 'enc'·'전자해도'·'ecdis' 자체는 일부러 넣지 않았다. 이 목록에 걸리면 기사에
    # compliance 태그가 붙는데, 5_analyze.candidate_articles() 가 compliance 태그
    # 기사를 기술 동향 후보에서 통째로 제외한다. ENC/ECDIS 는 전사 주력 제품이라
    # (CORE_RELEVANCE_KEYWORDS 에 등재) 제품 기사가 기술 동향에서 빠지고 규제 섹션으로
    # 밀려나는 역효과가 난다. 표준·의무화처럼 규제 성격이 분명한 표현만 잡는다.
    "s-101", "전자해도 표준", "전자해도 의무", "해도 탑재 의무",
    "ecdis 의무", "ecdis carriage", "chart carriage", "enc standard",
    # '탑재'가 끼면 위 표기가 부분문자열로 안 걸린다. 실측: 'ECDIS 탑재 의무 확대'가
    # 'ecdis 의무'·'해도 탑재 의무' 둘 다 비껴갔다('전자해도 탑재 의무'는 걸린다).
    # 의무·의무화까지 붙은 표현만 넣는다 — 'ECDIS 탑재 선박 증가' 같은 제품 기사에
    # compliance 태그가 붙으면 기술 동향 후보에서 빠지기 때문(위 주석 참고).
    "ecdis 탑재 의무", "ecdis 의무화", "전자해도 의무화",
]


def detect_competitor(title: str) -> str | None:
    """경쟁사 키워드 감지 → 경쟁사명 반환. 고객사·컴플라이언스와 같이 '제목'으로 판정한다.

    본문까지 보면 기사 주체가 아닌 회사가 태깅된다. 실측: 'HD현대, 영업이익 4조원 돌파'
    (그룹 실적 기사)가 본문에 스친 'HD현대마린솔루션'으로 경쟁사 태깅돼, 정작 해당 업체
    기사인 'HD현대마린솔루션, 2분기 영업익 976억'을 점수로 제치고 대표로 올라갔다."""
    lower = km.strip_outlet(title).lower()
    for kws, name in COMPETITORS:
        if km.any_keyword(lower, kws):
            return name
    return None


# 본문 언급만으로 경쟁사 태그를 줄 회사 (2026-10-06 피드백).
#
# 경쟁사는 제목으로만 감지한다(detect_competitor 주석 — 그룹 실적 기사가 본문에 스친
# HD현대마린솔루션으로 태깅된 사례). 그런데 국내 스타트업 경쟁사의 '동정'은 거의 언제나
# 남의 행사·과제 기사에 참여사로 나온다. 실측:
#   '선박관리업협회, 부산-이마바리 해운산업 교류의 날 행사 개최' (물류신문 9/23)
#   네이버 요약: "△경쟁사2 강민욱 이사가 '당신의 AX는 어디서 시작되나요'를 ..."
# 제목에 회사명이 없어 태그·가점(+3)·경쟁사 최소 슬롯을 모두 못 받고 4점으로 잘렸다.
#
# 이름이 고유해서 오태깅 위험이 없는 회사만 둔다. 대기업 계열(HD현대마린솔루션·
# Kongsberg·Avikus)은 기사마다 스치듯 나오므로 제외하고, '경쟁사1'는 일반명사라 제외한다.
# 판정 근거는 요약(네이버 description)이다 — 네이버는 검색어 주변 문장을 요약으로
# 주므로 본문 언급이 요약에 그대로 드러난다.
COMPETITOR_MENTION_NAMES = {"Competitor2(경쟁사2)", "Competitor1(경쟁사1)", "Competitor3", "경쟁사5"}
_MENTION_KEYWORDS = {
    "Competitor2(경쟁사2)": ["competitor2", "경쟁사2"],
    "Competitor1(경쟁사1)": ["competitor1"],
    "Competitor3": ["competitor3", "경쟁사3"],
    "경쟁사5": ["competitor5", "competitor5", "경쟁사5"],
}


def detect_competitor_mention(text: str) -> str | None:
    lower = (text or "").lower()
    for name in COMPETITOR_MENTION_NAMES:
        if km.any_keyword(lower, _MENTION_KEYWORDS[name]):
            return name
    return None


# 콩스버그는 MASS·자율운항 기사만 싣는다 (2026-10-06 피드백). 콩스버그는 방산(NSM 미사일)·인사·실적 기사가
# 훨씬 많아 회사명만으로 받으면 경쟁사 칸이 그쪽으로 찬다.
# 실측: 9/28 회차 'Jan Erik Hoff appointed CFO of Kongsberg Maritime' 수집.
KONGSBERG_KEYWORDS = ["kongsberg", "콩스버그"]
KONGSBERG_AUTONOMY_KEYWORDS = [
    "autonomous", "autonomy", "mass", "mass code", "uncrewed", "unmanned",
    "remote operation", "remotely operated", "자율운항", "무인선", "원격운항",
]


def is_kongsberg_off_topic(title: str, full_text: str) -> bool:
    """제목에 콩스버그가 있는데 자율운항 이야기가 아니면 True (수집 제외)."""
    if not km.any_keyword(km.strip_outlet(title).lower(), KONGSBERG_KEYWORDS):
        return False
    return not km.any_keyword(full_text.lower(), KONGSBERG_AUTONOMY_KEYWORDS)


# 지자체가 주체인 협약·투자협약 (2026-10-12 피드백: '전남광주통합특별시, 영암 조선업에
# AI 접목…6개 기관 투자협약' 관련없음, 21점). 지자체·기관끼리의 협약은 정책 홍보라
# 업계 동향이 아니다. 고객사가 제목에 함께 있으면 거래 당사자라 남긴다(호출부 참고).
_LOCAL_GOV_SUBJECT = re.compile(
    r"^[가-힣·]{1,12}(?:특별자치시|특별자치도|특별시|광역시|시|도|군|구)(?:청)?\s*[,，·\-]")
_AGREEMENT_WORDS = ["협약", "mou", "업무협약", "투자협약", "협력 협약"]


def is_local_gov_agreement(title: str) -> bool:
    t = km.strip_corner(km.strip_outlet(title or ""))
    return bool(_LOCAL_GOV_SUBJECT.match(t)) and km.any_keyword(t.lower(), _AGREEMENT_WORDS)


def is_compliance(title: str) -> bool:
    """IMO/선급 등 글로벌 컴플라이언스 소식인지 판별 — 반드시 '제목'으로 판정한다.

    본문까지 보면 규제와 무관한 기사가 대량 오태깅된다. 실측: 컴플라이언스 태그 4건 중
    3건이 본문에 '선급'·'DNV'·'IACS'가 스쳤을 뿐인 기사였다
    ('제이제이앤컴퍼니스 MRO 시장 참여', '해양 보안 전문가들 한자리에', 'K-조선 SW 국산화').
    고객사 배지를 제목에서만 감지하는 것과 같은 이유다."""
    return km.any_keyword(km.strip_outlet(title).lower(), COMPLIANCE_KEYWORDS)


def make_article(title, summary, link, pub, lang, source):
    """공통 기사 생성기: 필터/태깅/점수 적용 후 dict 반환 (제외 시 None)."""
    title = (title or "").strip()
    summary = (summary or "").strip()
    if not title:
        return None
    # 판정용 텍스트는 따옴표를 지운다(표시용 title/summary 는 그대로 둔다).
    # km.strip_quotes 주석 참고 — 따옴표가 여러 단어 키워드 매칭을 깨뜨린다.
    full_text = km.strip_quotes(title + " " + summary)

    # 자사(아크메) 언급 기사는 아래 노이즈 필터를 면제한다
    # (2026-09-01 피드백).
    # 우리 회사 소식은 대부분 세미나·전시·기술설명회 형태로 보도되는데, 그게 전부
    # is_event_noise 에 걸린다. 실측(2026-09-01, Google News 'when:7d' 5건): 아크메가
    # 발표사로 참여한 '한국해운협회 해운업AX 전략 기술설명회' 기사 5건이 전부 제목의
    # '설명회'로 제외될 자리였다. 남의 회사면 홍보물이지만 우리 회사면 실을 소식이다.
    own = detect_own(full_text)

    # 순수 투자/주가성, 주기 발행 종합·전망, 스포츠·연예 후원, 인사 발령, 사진 캡션 제외
    # (옛 기사는 여기서 안 본다 — gnews() when:7d + drop_stale_by_body 가 맡는다)
    if not own and (is_stock_noise(full_text) or is_digest_noise(full_text)
                    or is_sports_noise(full_text) or is_personnel_noise(title)
                    or is_photo_caption(title) or is_event_noise(title)
                    or is_medical_noise(title)
                    or is_nonmarine(title) or is_labor_noise(title)
                    or is_org_launch(title, summary)
                    # 산재(작업장 인명사고) (2026-09-01 피드백)
                    or km.is_industrial_accident(title)
                    # 해양 사고 중 인명피해·수색구조 보도 (2026-09-01 피드백). 같은 날 오전의 반대
                    # 판단을 뒤집은 것이다 — km.is_marine_casualty 주석 참고.
                    or km.is_marine_casualty(title)
                    # 정기선 서비스 개편 (2026-09-01 피드백)
                    or is_liner_service(title)
                    # 기부·사회공헌 (2026-09-08 피드백)
                    or is_donation_noise(title)
                    # 증권가 코너·종목 시황 (2026-09-08 피드백)
                    or is_stock_title_noise(title)
                    # 솔루션 절감액 홍보 (2026-09-21 피드백)
                    or is_vendor_promo(title)
                    # 제목이 회사명뿐이라 내용이 없는 스텁 (2026-09-01 피드백)
                    or is_bare_entity_title(title)
                    # 자금 조달·부동산, 블로그 게시글 (2026-09-28 피드백)
                    or is_corp_finance_noise(title) or is_blog(title, link)):
        return None

    # 태깅: 고객사 > 아크메(자사) > 경쟁사 > 컴플라이언스 (우선순위대로 하나만)
    # 고객사 배지는 반드시 '제목'에서만 감지 → 카드 제목과 배지가 항상 일치.
    # (본문에만 등장하는 거래 상대에 잘못 태깅되던 문제 방지.)
    # 자사 언급은 경쟁사보다 앞선다: 여러 업체가 함께 소개된 기사에서 경쟁사 배지가
    # 붙어 '경쟁사 동향'으로 새던 문제(예: 해양진흥공사 AI 진단 기사) 방지.
    customer = detect_customer(title)
    own = bool(not customer and own)
    competitor = None if (customer or own) else detect_competitor(title)
    # 제목에 없어도 요약에 국내 경쟁사 참여가 드러나면 경쟁사 동정으로 태깅한다
    # (COMPETITOR_MENTION_NAMES 주석 참고)
    competitor_mention = False
    if not (customer or own or competitor):
        competitor = detect_competitor_mention(summary)
        competitor_mention = bool(competitor)
    compliance = bool(not customer and not own and not competitor
                      and is_compliance(title))

    # 콩스버그는 자율운항 기사만 (KONGSBERG_AUTONOMY_KEYWORDS 주석 참고)
    if not own and is_kongsberg_off_topic(title, full_text):
        return None
    # 지자체 협약 (is_local_gov_agreement 주석 참고) — 고객사가 당사자면 남긴다
    if not (own or customer) and is_local_gov_agreement(title):
        return None

    # 해사 전문지 기사·AI×조선/해양 융합 기사는 '해사 기사인가' 관문을 면제한다
    # (TRADE_PRESS_SOURCES, AI_CONVERGENCE_SIGNAL 주석 참고). 점수 경쟁은 그대로다.
    trade = source in TRADE_PRESS_SOURCES
    ai_marine = is_ai_marine(title, source, summary)

    # 키워드 필터 (일반 키워드 OR 고객사/자사/경쟁사/컴플라이언스)
    if (not matches_keywords(full_text) and not customer and not own
            and not competitor and not compliance and not trade and not ai_marine):
        return None

    # 해사 키워드가 하나도 없이 회사명 태그만으로 통과한 기사는 사업 맥락을 요구한다.
    # (후원·시상·지역행사처럼 회사명만 스친 기사 차단)
    #
    # 회사명 태그(고객사·경쟁사)로 들어온 기사에만 적용한다(2026-09-01). 예전에는 태그
    # 종류를 안 가려서, 규제·표준 태그로 들어온 기사까지 이 관문에 걸렸다. 실측:
    # 'UKHO to mark one year of S-100 trials at SMM 2026' 이 제외됐다 — 제목의 'ukho'·
    # 's-100' 으로 compliance 태그를 받았지만, 일반 해사어(shipping·vessel…)도 사업어
    # (contract·launch…)도 없어서 두 조건을 모두 못 넘겼다. S-100 은 전사 주력 상품에
    # 걸린 표준이라 '알아두면 좋은' 배경 지식으로 실려야 할 기사다.
    # 이 관문이 막으려던 건 '회사명만 스친 기사'이지 '주제어로 잡힌 기사'가 아니다.
    # 단, PERSONNEL_KEEP_CUSTOMERS 의 경영진 인사는 그 자체가 사업 맥락이다 —
    # 'ONE appoints … as new CEO' 에는 해사어도 사업어도 없다(2026-09-28 요청).
    keep_personnel = (customer in PERSONNEL_KEEP_CUSTOMERS
                      and any(p.search(km.strip_outlet(title)) for p in PERSONNEL_NOISE_PATTERNS))
    # 해사 전문지 기사는 매체가 맥락을 보장한다. 실측: 'SpecTec and HarborMax expand
    # AMOS across Asia-Pacific as 100+ vessels use HarborMax ICT'(Digital Ship)가
    # 구글(제목만) 경로에서 여기 걸렸다 — 'vessels' 복수형이 'vessel' 과 안 맞았다.
    if ((customer or competitor) and not matches_keywords(full_text)
            and not has_business_context(full_text) and not keep_personnel
            and not trade):
        return None

    # 제목이 해사 기사임을 전혀 보여주지 못하면 제외한다 (has_marine_title 주석 참고).
    # 회사 배지가 붙었거나 자사 기사면 이미 주제가 확인된 것이므로 게이트를 거치지 않는다.
    # 전문지·AI 융합 기사도 같다 — 융합 판정 자체가 제목의 산업어를 요구한다.
    if (not (customer or own or competitor or compliance or trade or ai_marine)
            and not has_marine_title(title)):
        return None

    # AI 융합 가점: 기존 AI 결합 보너스(relevance_score 안 +4, 제품 영역 한정)와 합쳐
    # '둘 중 큰 값'만 받게 한다 — 같은 'AI 기사'라는 이유로 두 번 벌지 않게.
    # 예전에는 제품 보너스를 받은 기사에 융합 가점을 아예 안 줘서, 융합 가점을 올리자
    # 오히려 역전이 났다(실측: '한화 방산3사 AX 전환' 11 → 10점 — 요약에 AI 제품어가
    # 있어 제품 보너스 +4 만 받고 융합 가점은 못 받았다).
    lower = km.strip_quotes(full_text).lower()
    product_ai = (km.any_keyword(lower, AI_SIGNAL_KEYWORDS)
                  and km.any_keyword(lower, AI_TARGET_KEYWORDS))
    ai_bonus = (AI_CONVERGENCE_BONUS - (4 if product_ai else 0)) if ai_marine else 0
    score = (relevance_score(full_text, customer, pub)
             + (5 if own else 0) + (3 if competitor else 0) + (2 if compliance else 0)
             + ai_bonus
             + (TRADE_PRESS_BONUS if trade else 0))
    return {
        "source": source,
        "lang": lang,
        "title": title,
        "summary": summary[:800],
        "link": link,
        "published": pub.isoformat() if pub else "",
        "customer": customer,        # 고객사명 또는 None
        "own": own,                  # 아크메 자체 언급 여부
        "competitor": competitor,    # 경쟁사명 또는 None
        "competitor_mention": competitor_mention,  # 제목이 아니라 요약 언급으로 태깅됐나
        "compliance": compliance,    # 컴플라이언스 여부
        "ai_marine": ai_marine,      # AI·AX × 조선/해양 융합 기사
        "trade_press": trade,        # 해사 전문지 기사
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
            # 검색어를 늘리면서 429(Too Many Requests)가 발생 → 짧은 간격 + 1회 재시도
            r = None
            for _ in range(2):
                # display=100 (API 최대). 30이면 검색어를 단독어로 넓힌 뒤 최신 30건이
                # 최근 2~3일치밖에 못 덮어 7일 창의 앞부분이 통째로 빠진다.
                # (실측 2026-08-10: "HMM" 단독 30건의 가장 오래된 기사가 8/7이었다.)
                r = requests.get("https://openapi.naver.com/v1/search/news.json",
                                 params={"query": q, "display": 100, "sort": "date"},
                                 headers=headers, timeout=15)
                if r.status_code != 429:
                    break
                time.sleep(1.5)
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

    # 규제기관·수로국 공식 발표 (2차 보도가 아니라 원문). official_sources.py 참고.
    articles += fetch_official()

    return dedup(articles)


def fetch_official() -> list[dict]:
    """공식 소스(UKHO 등) 수집 → 기사 dict. 컴플라이언스로 고정 태깅한다.

    태깅을 고정하는 이유: 수로국 공식 발표는 그 자체가 규제·표준 소식인데,
    is_compliance() 는 제목 키워드로 판정하므로 'Understanding ECDIS alerts' 같은
    제목이 안 걸린다. 소스가 이미 성격을 보장하므로 판정을 다시 하지 않는다.
    """
    out = []
    for item in official_sources.fetch_all_official():
        pub = item.get("published")
        if pub and pub < CUTOFF:
            continue                     # 수집 기간(7일) 밖은 버린다
        art = make_article(clean_html(item.get("title", "")),
                           clean_html(item.get("summary", "")),
                           item.get("link", ""), pub, "en",
                           official_sources.UKHO_NAME)
        if not art:
            continue
        # 고객사/경쟁사로 잘못 잡힌 게 아니라면 컴플라이언스로 고정
        if not art.get("customer") and not art.get("competitor"):
            art["compliance"] = True
        out.append(art)
    return out


# 본문 리드에서 태그를 보완할 때 볼 길이. 한국 기사는 리드에 주체가 나온다.
BODY_LEAD_CHARS = 300


def retag_from_body(articles: list[dict]) -> int:
    """본문 리드로 자사·경쟁사 태그를 보완한다. 태그가 하나도 없는 기사만 대상.

    왜 필요한가(2026-09-01 피드백):
      detect_customer/detect_competitor 는 '제목'만 본다. 기사 주체가 제목에 안 나오는
      기획 연재는 이 방식으로 못 잡는다. 실측 —
        제목  'AI로 북극해 유빙 정보 분석해 안전 항해 이끈다 [마린 유니콘]'
        본문  '[마린 유니콘] ⑪ 경쟁사1 … 해양 데이터 스타트업 경쟁사1(Competitor1)는 …'
      경쟁사1는 COMPETITORS 에 있는데 제목에 이름이 없어 태그가 안 붙었고, 그 결과
      '일반 기사'로 분류돼 기술 동향 1위 카드를 차지했다. 경쟁사 동향에 있어야 할 기사다.

    제목 판정을 본문 전체로 넓히지는 않는다 — 그건 예전에 오태깅을 낳아 되돌린 방식이다
    (detect_competitor 주석 참고: 'HD현대 영업이익 4조원' 기사가 본문에 스친
    'HD현대마린솔루션'으로 경쟁사가 됐다). 리드 앞 300자만 본다: 한국 기사는 리드에
    주체를 쓰므로 '이 기사가 누구 얘기인가'는 거기서 갈리고, 뒤쪽에 스치는 이름은 안 걸린다.

    본문은 add_bodies() 뒤에야 있으므로 make_article 이 아니라 여기서 한다.
    """
    changed = 0
    for art in articles:
        if art.get("customer") or art.get("own") or art.get("competitor"):
            continue
        lead = (art.get("body") or "")[:BODY_LEAD_CHARS]
        if not lead:
            continue
        if detect_own(lead):
            art["own"] = True
            art["relevance_score"] = art.get("relevance_score", 0) + 5
            changed += 1
            print(f"    · 자사 태그 보완: {art.get('title', '')[:46]}")
            continue
        name = detect_competitor(lead)
        # 콩스버그는 자율운항 이야기일 때만 경쟁사로 본다 (KONGSBERG 관문 주석 참고)
        if name == "Kongsberg" and not km.any_keyword(
                (art.get("title", "") + " " + lead).lower(), KONGSBERG_AUTONOMY_KEYWORDS):
            name = None
        if name:
            art["competitor"] = name
            art["relevance_score"] = art.get("relevance_score", 0) + 3
            changed += 1
            print(f"    · 경쟁사 태그 보완({name}): {art.get('title', '')[:38]}")
            continue

        # 고객사는 '태그 없이 점수만' 준다 (2026-09-21 피드백).
        #
        # 계기: 현대글로비스 소유·HMM오션서비스 관리 VLCC 가 이란에 해킹당해 FBI 가
        # 수사 중이라는 기사(n.news.naver.com/article/016/0002699577)가 뉴스레터에서
        # 빠졌다. 주체인 HMM오션서비스가 제목에 없어 고객사 가점(+6)을 통째로 못 받고
        # 5점에 머물렀다 — 그 주 실린 47건의 최저가 7점이었다.
        #
        # 태그(배지)는 왜 안 붙이나: 고객사 배지는 '제목에서만' 감지한다는 규칙이 있다
        # (detect_customer 주석). 카드에 '[HMM]' 배지를 달고 제목에는 HMM 이 없으면
        # 읽는 사람이 어긋남을 바로 알아챈다. 그 규칙은 그대로 둔다.
        #
        # 점수만 주면 두 가지가 같이 해결된다.
        #   · 전체 뉴스에서 뒤로 밀리지 않는다 (이 기사 5 → 9점).
        #   · 기술 동향 후보 자격이 남는다 — 5_analyze 의 후보는 '태그 없는 기사'라,
        #     태그를 붙이면 오히려 TOP 카드가 될 길이 막힌다.
        #
        # +4 인 이유: 제목 태그(+6)보다 낮게 둔다. 본문 리드에 이름이 나왔다고 해서
        # 그 회사가 기사의 주체라는 보장은 제목만큼 강하지 않다(거래 상대로 스칠 수 있다).
        name = detect_customer(lead)
        if name:
            art["customer_mention"] = name      # 배지가 아니라 기록용
            art["relevance_score"] = art.get("relevance_score", 0) + 4
            changed += 1
            print(f"    · 고객사 언급 가점({name}): {art.get('title', '')[:38]}")
    return changed


# 증권사 리포트를 풀어쓴 기사 — 본문으로만 드러난다 (2026-09-21 피드백).
#
# 제목 단계 필터(STOCK_TITLE_NOISE_KEYWORDS)는 '증권'·'장중' 같은 말을 제목에서 찾는다.
# 그런데 이 유형은 제목을 산업 기사처럼 쓴다 — 실측 제목에는 주식 신호어가 하나도 없고,
# 본문 중간에 "SK증권은 최근 보고서에서…", 끝에 "목표주가 97만원, 투자의견은 매수"가
# 붙는다. 제목만 보는 한 구조적으로 못 잡는다.
#
# 왜 '본문 전체'를 봐도 되는가: 아래 표현들은 증권사 리포트 인용 기사에만 쓰인다.
# 넓은 말('주가'·'증권'·'실적')은 절대 넣지 않는다 — 실측(2026-09-21, 47건)에서
# '주가'는 정상 기사 4건(삼성중공업 수주, 장금상선 제재 해명, 한국선급 진동 기술)에
# 스쳤고, '증권'은 '유가증권시장'으로도 걸린다. 아래 목록으로는 그 4건이 안 걸린다.
ANALYST_BODY_KEYWORDS = [
    "투자의견", "투자 의견", "매수의견", "매수 의견", "매도의견",
    "목표주가", "목표 주가", "목표가를", "목표가 제시",
    "투자의견은 매수", "비중확대 의견",
]
ANALYST_BODY_PATTERNS = [
    # 'SK증권 한승한 연구원', 'NH투자증권 애널리스트'
    re.compile(r"증권\s*[가-힣]{0,4}\s*(연구원|애널리스트)"),
    # 'SK증권은 최근 보고서에서', '키움증권은 리포트에서'
    re.compile(r"증권[은는이가]\s*[^.]{0,20}(보고서|리포트)"),
]


def is_analyst_body(body: str) -> bool:
    """본문이 증권사 리포트를 풀어쓴 기사인가."""
    if not body:
        return False
    low = body.lower()
    return (km.any_keyword(low, ANALYST_BODY_KEYWORDS)
            or any(p.search(body) for p in ANALYST_BODY_PATTERNS))


def drop_analyst_by_body(articles: list[dict]) -> list[dict]:
    """증권사 리포트 인용 기사를 본문 기준으로 제외한다.

    본문이 있어야 하므로 add_bodies() 뒤에서 호출한다(drop_stale_by_body 와 같은 자리).
    자사(아크메) 기사는 면제한다 — 다른 노이즈 필터와 같은 규칙이다.
    """
    kept = []
    for art in articles:
        if not art.get("own") and is_analyst_body(art.get("body") or ""):
            print(f"    · 증권 리포트 기사 제외: {art.get('title', '')[:46]}")
            continue
        kept.append(art)
    return kept


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


def _entity(art: dict) -> str | None:
    """같은 사건 판정용 주체 키. 렌더 단계와 기준을 공유하려고 keyword_match 에 둔다."""
    return km.entity_key(art)


# 상한 컷에서 소수 섹션이 통째로 사라지지 않도록 보장하는 최소 슬롯.
# 배경: 고객사 태그가 +6점이라 조선소 기사가 상위를 독식한다. 실측(2026-08-03)에서
# 60건 중 컴플라이언스는 4건뿐이었고 그나마 3건은 본문에 '선급'이 스친 오태깅이라,
# '글로벌 컴플라이언스' 섹션이 사실상 무의미했다. 점수 경쟁과 별도로 자리를 떼어둔다.
#
# 해외 기사 슬롯도 같은 이유로 필요하다. 점수 체계상 영문 기사는 구조적으로 불리하다 —
# 고객사 가점(+6)이 국내 해운사·조선소에 붙고, 최신성 가점도 국내 매체가 하루 먼저
# 받아간다. 실측(2026-08-10): 영문 기사 점수는 10/8/3, 국내는 최고 17이었다.
# 검색어를 늘려도 점수 경쟁에서 밀리면 결과는 같으므로 자리를 따로 확보한다.
#
# (기준, 최소 건수, 최저 점수). 최저 점수가 필요한 이유: 슬롯은 '자리를 비워두는' 장치라
# 채울 기사가 부족하면 관련성 없는 기사로도 채워진다. 실측(2026-08-10) 영문 슬롯을
# 점수 조건 없이 15건 잡았더니 'Smugglers Attempt to Ship Meth'(4점),
# 'War Risk Insurance Pays $11M'(4점) 같은 일반 해운 단신이 들어오고, 대신 9점짜리
# 국내 기사가 밀려났다. 자리는 떼어두되 최소 품질은 요구한다.
# 북극항로 기사 판별 (상한 컷에서 자리를 떼어두기 위한 기준)
ARCTIC_KEYWORDS = ["북극항로", "북극해", "극지 운항", "빙해",
                   "northern sea route", "arctic shipping", "arctic route"]


def is_arctic(art: dict) -> bool:
    return km.any_keyword((art.get("title", "") + " " + art.get("summary", "")).lower(),
                          ARCTIC_KEYWORDS)


MIN_SLOTS = [
    # 자사(아크메) 기사는 무조건 싣는다 — 슬롯을 가장 먼저 배정한다
    # (2026-09-01 피드백). 점수 경쟁에 맡기면 구조적으로 밀린다: 우리 회사는
    # 고객사 가점(+6)을 못 받고, 기사 자체도 세미나·기술설명회 형태가 많아
    # 대형 수주 기사와 같은 줄에 세우면 이길 수가 없다. 점수 하한도 두지 않는다.
    ("own", 5, 0),
    ("compliance", 8, 0),
    # 6 → 8 (2026-10-06 피드백). 경쟁사 검색어를 살리고
    # 요약 언급 태깅을 더해 후보가 늘어나므로 자리도 함께 늘린다.
    ("competitor", 8, 0),
    # AI·AX × 조선/해양 융합, 친환경·탈탄소 (2026-10-06 피드백). 점수 하한 5 —
    # 해외 기사 슬롯과 같은 이유로, 하한이 없으면 단신이 자리를 채운다.
    ("ai_marine", 8, 5),
    (is_green, 6, 5),
    (lambda a: a.get("lang") == "en", 12, 5),   # 해외 기사 최소 12건 (5점 이상만)
    # 북극항로 최소 4건 (5점 이상만). 검색어를 넣어도 점수 경쟁에서 밀리면 결과는
    # 같으므로 자리를 따로 확보한다 — 해외 기사 슬롯과 같은 이유다.
    # 북극항로 기사는 고객사 가점(+6)이 안 붙어 대체로 5~7점에 머무는데, 고객사·조선소
    # 기사는 15점을 넘는다. 점수 컷만으로는 구조적으로 못 올라온다.
    # 5점 하한을 둔 이유: 하한 없이 4건을 잡으면 'Svalbard Arctic Tourism'(4점),
    # 'Canada Arctic Cruises'(4점) 같은 관광 기사가 자리를 채운다(실측).
    (is_arctic, 4, 5),
]

# 주제별 상한 (기준, 최대 건수). 최소 슬롯의 반대쪽 짝이다.
#
# 배경(2026-08-25): 아이스네비게이션이 아크메 사업이라 북극 관련어를 제품 직결
# 키워드로 올렸더니(+2 → +5) 북극 기사가 4건에서 24건으로 뛰었다. 56건 중 43%다.
# 그중 7건은 같은 사건(팬스타 아크로호 출항)을 매체별로 쓴 것인데, 제목이 서로 많이
# 달라('닻 올렸다' / '3만5천t 배' / '45일 대장정') 중복 판정으로는 안 묶인다.
# 희소어 기준으로도 '북극항로'·'시범운항'은 너무 흔해 판정에서 빠진다.
#
# 중복 판정을 더 공격적으로 만드는 대신 상한으로 푼다 — 점수 순으로 잘리므로
# 같은 사건 기사는 자연히 줄고(7건 → 상위 3건), 서로 다른 사안이 우선 살아남는다.
# 잘못 묶어 중요한 기사를 잃을 위험도 없다. 최소 슬롯 4건과 짝이 되어 범위는 4~8건.
#
# 상한은 apply_cap 보다 먼저 적용한다. 그래야 잘려나간 자리가 다른 주제로 돌아간다.
#
# 8건 → 6건 (2026-09-01 피드백).
# 같은 피드백에 대한 주된 대응은 점수 쪽이다(동의어를 1종으로 세는 CORE_KEYWORD_GROUPS).
# 상한은 그 보조 장치라 한 단계만 조인다 — 최소 슬롯 4건과 짝이 되어 범위는 4~6건이다.
MAX_TOPIC_SLOTS = [
    (is_arctic, 6),
]


def apply_topic_caps(articles: list[dict]) -> list[dict]:
    """주제별 상한을 적용한다. 입력은 점수 내림차순이어야 한다(상위부터 남긴다)."""
    drop = set()
    for match, quota in MAX_TOPIC_SLOTS:
        kept = 0
        for i, art in enumerate(articles):
            if not match(art):
                continue
            kept += 1
            if kept > quota:
                drop.add(i)
    if drop:
        print(f"  [주제 상한] {len(drop)}건 제외 (북극항로 상한 "
              f"{MAX_TOPIC_SLOTS[0][1]}건)")
    return [a for i, a in enumerate(articles) if i not in drop]


def apply_cap(articles: list[dict]) -> list[dict]:
    """점수 상위 MAX_ARTICLES건을 남기되, 소수 태그에 최소 슬롯을 먼저 배정한다.
    입력은 점수 내림차순이어야 한다 (각 태그에서 상위 기사가 뽑히도록).
    슬롯 기준은 필드명 문자열 또는 기사 dict 를 받는 판정 함수 둘 다 허용한다."""
    # 상한 밖 주제를 먼저 떼어 둔다 (UNCAPPED 주석 참고). 상한은 나머지에만 적용된다.
    extra = [a for a in articles
             if any(a.get(tag) and a.get("relevance_score", 0) >= floor
                    for tag, floor in UNCAPPED)]
    if extra:
        print(f"  [상한 밖] AI 융합 {len(extra)}건은 상한과 별도로 싣습니다")
        extra_ids = {id(a) for a in extra}
        articles = [a for a in articles if id(a) not in extra_ids]
    if len(articles) <= MAX_ARTICLES:
        return sorted(articles + extra,
                      key=lambda x: x.get("relevance_score", 0), reverse=True)
    picked, taken = [], set()
    for tag, quota, floor in MIN_SLOTS:
        match = tag if callable(tag) else (lambda a, t=tag: a.get(t))
        got = 0
        for i, a in enumerate(articles):
            if got >= quota or len(picked) >= MAX_ARTICLES:
                break
            if i not in taken and match(a) and a.get("relevance_score", 0) >= floor:
                picked.append(a)
                taken.add(i)
                got += 1
    for i, a in enumerate(articles):
        if len(picked) >= MAX_ARTICLES:
            break
        if i not in taken:
            picked.append(a)
            taken.add(i)
    picked += extra
    picked.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return picked


# 보도량 가점 (같은 사건을 다룬 매체 수 → 가점).
#
# 2026-08-25 요청
# 지금까지 중복 제거는 몇 개 매체가 다뤘는지를 세어놓고 그냥 버렸다. 그런데 그 수가
# 곧 사건의 크기다 — 팬스타 아크로호 북극항로 첫 출항은 30개 넘는 매체가 썼고,
# 한 매체만 쓴 단신과 같은 취급을 받을 이유가 없다.
#
# 링크 중복·제목 완전일치는 세지 않는다. 그건 같은 기사가 여러 검색어에 걸린 것이지
# 다른 매체가 따로 취재한 게 아니다. '같은 사건, 다른 기사'만 보도량으로 친다.
COVERAGE_BONUS_TIERS = [
    (20, 8),    # 20개 매체 이상 — 그 주의 대형 사건
    (10, 6),
    (5, 4),
    (2, 2),     # 2개 매체만 돼도 단독 단신보다는 무겁다
]


def coverage_bonus(coverage: int) -> int:
    """같은 사건을 다룬 기사 수(대표 1건 포함)에 따른 가점."""
    for threshold, bonus in COVERAGE_BONUS_TIERS:
        if coverage >= threshold:
            return bonus
    return 0


def rescore_coverage(articles: list[dict]) -> int:
    """coverage 가 늘어난 기사의 보도량 가점을 다시 매긴다. 바뀐 건수를 돌려준다.

    옛 가점을 빼고 새 가점을 더하므로 여러 번 불러도 결과가 같다(멱등).
    쓰는 곳과 이유는 호출부(merge_same_event 뒤) 주석을 볼 것.
    """
    changed = 0
    for art in articles:
        new_bonus = coverage_bonus(art.get("coverage", 1))
        old_bonus = art.get("coverage_bonus", 0)
        if new_bonus != old_bonus:
            art["relevance_score"] = (art.get("relevance_score", 0)
                                      - old_bonus + new_bonus)
            art["coverage_bonus"] = new_bonus
            changed += 1
    return changed


def dedup(articles: list[dict]) -> list[dict]:
    """제목/링크 완전일치 + 문자 유사도 + 단어 단위 '같은 사건' 판정으로 중복 제거.
    관련성 점수가 높은 기사를 대표로 보존한다.

    중복은 버리되 '몇 건이었는지'는 coverage 필드에 남겨 가점으로 되돌린다."""
    # 점수 높은 순으로 먼저 정렬해야 중복 그룹에서 대표 기사가 살아남음
    articles.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    # 단어 단위 판정용: 코퍼스 전체의 문서빈도를 미리 구해 흔한 업계용어를 제외한다
    word_sets = [km.dup_tokens(a["title"]) for a in articles]
    # 요약이 제목 재탕인 기사(Google News)는 요약 비교에서 뺀다 — km.has_real_snippet 참고
    sum_sets = [km.summary_tokens(a.get("summary", "")) if km.has_real_snippet(a)
                else set() for a in articles]
    # df 는 제목의 모든 표기를 합쳐서 센다(비교는 word_sets 그대로) — km.df_tokens 참고
    freq = km.doc_freq([km.df_tokens(a) for a in articles])
    max_df = km.rare_cutoff(len(articles))   # 풀 크기에 비례한 희소 기준

    seen_titles: set[str] = set()
    seen_links: set[str] = set()
    kept_tokens: list[set] = []
    kept_words: list[tuple[set, str | None, set]] = []
    kept_sums: list[set] = []
    unique = []
    for art, words, sums in zip(articles, word_sets, sum_sets):
        key = normalize_title(art["title"])
        link = art.get("link", "")
        if key and key in seen_titles:
            continue
        if link and link in seen_links:
            continue
        # 중복이면 '몇 번째 대표 기사와 같은 사건인지'를 기록한다.
        # 그냥 버리지 않는 이유: 몇 개 매체가 같은 사건을 다뤘는가가 곧 사건의 크기다
        # (2026-08-25 요청). 아래에서 이 수를 가점으로 되돌린다.
        ent = _entity(art)
        names = km.entity_tokens(art)    # 회사명 토큰은 판정에서 뺀다
        tokens = title_tokens(art["title"])
        dup_idx = None
        for i, kt in enumerate(kept_tokens):
            if is_near_duplicate(tokens, [kt]):
                dup_idx = i
                break
        # 요약이 사실상 동일한 기사 (같은 보도자료를 받아쓴 경우.
        # 제목 표기가 갈려 제목 기반 판정을 빠져나가는 케이스를 잡는다)
        if dup_idx is None:
            for i, ks in enumerate(kept_sums):
                if km.near_identical(sums, ks):
                    dup_idx = i
                    break
        # 다른 매체가 같은 사안을 다르게 쓴 기사 (문자 유사도로는 안 걸리는 케이스)
        if dup_idx is None:
            for i, (kw, ke, kn) in enumerate(kept_words):
                if km.is_same_event(words, kw, freq, max_df=max_df,
                                    same_entity=(ent is not None and ent == ke),
                                    ignore=names | kn):
                    dup_idx = i
                    break
        if dup_idx is not None:
            rep = unique[dup_idx]
            rep["coverage"] = rep.get("coverage", 1) + 1
            continue

        seen_titles.add(key)
        if link:
            seen_links.add(link)
        kept_tokens.append(tokens)
        kept_words.append((words, ent, names))
        kept_sums.append(sums)
        art["coverage"] = 1
        unique.append(art)

    # 보도량 가점: 여러 매체가 같은 사건을 다뤘으면 그만큼 큰 사건이다.
    # 상한/정렬이 점수를 보므로 여기서 반영해야 한다(apply_topic_caps 앞).
    for art in unique:
        bonus = coverage_bonus(art.get("coverage", 1))
        if bonus:
            art["relevance_score"] = art.get("relevance_score", 0) + bonus
            art["coverage_bonus"] = bonus
    unique.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    # 주제별 상한을 먼저 적용한다 — 잘린 자리가 다른 주제로 돌아가야 하기 때문이다
    unique = apply_topic_caps(unique)
    # 관련성 상위 MAX_ARTICLES건만 보존 (unique는 점수 내림차순 상태)
    unique = apply_cap(unique)
    # 최종 검증 패스: 상한을 적용한 뒤의 목록을 자기 자신의 코퍼스로 다시 판정한다.
    # 상한 컷으로 코퍼스가 작아지면 희소 기준도 함께 좁아지므로, 위 단계에서
    # 흔한 단어로 취급돼 통과했던 중복이 여기서 걸린다. (결과물 무중복 보장)
    unique = drop_same_event(unique)
    # 최종 출력은 최신순
    unique.sort(key=lambda x: x["published"], reverse=True)
    return unique


def drop_same_event(articles: list[dict]) -> list[dict]:
    """주어진 목록 안에서 '같은 사건' 기사를 제거한다 (앞쪽 = 상위 점수 우선 보존)."""
    word_sets = [km.dup_tokens(a["title"]) for a in articles]
    sum_sets = [km.summary_tokens(a.get("summary_ko") or a.get("summary", ""))
                if km.has_real_snippet(a) else set() for a in articles]
    freq = km.doc_freq([km.df_tokens(a) for a in articles])
    max_df = km.rare_cutoff(len(articles))
    kept, kept_words, kept_sums = [], [], []
    for art, words, sums in zip(articles, word_sets, sum_sets):
        if any(km.near_identical(sums, ks) for ks in kept_sums):
            continue
        ent = _entity(art)
        names = km.entity_tokens(art)    # 회사명 토큰은 판정에서 뺀다
        if any(km.is_same_event(words, kw, freq, max_df=max_df,
                                same_entity=(ent is not None and ent == ke),
                                ignore=names | kn)
               for kw, ke, kn in kept_words):
            continue
        kept_words.append((words, ent, names))
        kept_sums.append(sums)
        kept.append(art)
    return kept


if __name__ == "__main__":
    print(f"\n[1단계] 뉴스 수집 시작 (최근 {DAYS_BACK}일)\n")
    news = fetch_all()

    # 중복 제거·상한 적용이 끝난 목록에만 본문을 붙인다(버려질 기사는 읽지 않는다).
    # RSS·네이버 스니펫은 문장 중간에서 끊겨, 분석 단계 모델이 끊긴 뒤를 지어내는
    # 원인이 됐다(2026-08-24 실측 → article_body.py 상단 주석 참고).
    print(f"\n[1-2단계] 기사 본문 수집 - {len(news)}건\n")
    ok, tried = add_bodies(news)
    # 2026-09-17 이전에는 여기서 '구글뉴스 N건은 원문 주소를 알 수 없어 대상 외' 를
    # 찍었다. 이제는 원문을 복원해 함께 긁으므로 제외 대상이 없다(SKIP_HOSTS 는 비어
    # 있다). 복원 성공/실패 집계는 add_bodies 가 직접 찍는다.
    skipped = len(news) - tried
    print(f"\n본문 {ok}/{tried}건 확보"
          + (f" (본문·이미지를 포기한 호스트 {skipped}건 제외)" if skipped else ""))

    # 피드 날짜를 못 믿는 경우가 있어(구글 뉴스가 옛 기사에 최근 pubDate 를 찍는다),
    # 본문을 확보한 기사는 본문에 찍힌 발행일과 대조해 옛 기사를 걸러낸다.
    before = len(news)
    news = drop_stale_by_body(news)
    if before != len(news):
        print(f"  본문 날짜 대조로 {before - len(news)}건 제외")

    # 증권사 리포트를 풀어쓴 기사 제외 (2026-09-21 피드백). 제목만으로는 산업 기사와
    # 구별되지 않아 본문을 봐야 한다 — drop_analyst_by_body 주석 참고.
    before = len(news)
    news = drop_analyst_by_body(news)
    if before != len(news):
        print(f"  증권 리포트 기사 {before - len(news)}건 제외")

    # 제목에 주체가 안 나오는 기사(기획 연재 등)의 자사·경쟁사 태그를 본문 리드로 보완.
    # 본문이 있어야 하므로 add_bodies 뒤에서 한다 (retag_from_body 주석 참고).
    retagged = retag_from_body(news)
    if retagged:
        print(f"  본문 리드로 태그 보완 {retagged}건")

    # 같은 사건을 다룬 기사 병합 (event_dedup.py 참고).
    # 본문을 '사건 한 줄'로 요약해 비교한다 — 제목·본문 유사도로는 구간이 겹쳐
    # "같은 사건"과 "같은 주제 다른 각도"를 못 가른다는 것을 실측으로 확인했다.
    # 본문이 있어야 하므로 add_bodies 뒤에서 한다.
    before = len(news)
    news = merge_same_event(news)
    if before != len(news):
        print(f"  사건 요약 대조로 {before - len(news)}건 병합")

    # 보도량 가점 재계산 (2026-09-08).
    #
    # 가점은 dedup() 안에서 한 번 매겨진다. 거기서 매겨야 하는 이유가 있다 —
    # 바로 뒤의 apply_topic_caps/apply_cap 이 점수를 보고 자르기 때문이다.
    # 문제는 그 시점의 coverage 가 '제목·본문 유사도로 합친 것까지'만 센 값이라는 것이다.
    # 표현이 다른 같은 사건은 그 단계를 빠져나가고, 바로 위 merge_same_event 가
    # 비로소 합친다. 그래서 합치고 나면 coverage 는 커지는데 가점은 옛 값에 묶인다.
    #
    # 실측(2026-09-08, HMM-발레 4.7조 장기운송계약): 50건이 수집돼 dedup 후 4건이
    # 남았고(매체마다 '잭팟'·'2056년까지'·'포트폴리오 다변화 결실(종합)' 로 달리 써서
    # 유사도 판정을 빠져나갔다), merge_same_event 가 1건으로 합쳐 coverage 5 → 54 가
    # 됐다. 그 주 최대 사건인데 가점은 5건 기준 +4 에 묶여 13점이었다 — 54건이면 +8 로
    # 17점이다.
    #
    # 여기서 다시 매겨도 수집 결과는 안 바뀐다. 상한·컷은 이미 끝났고, 이 점수를 쓰는
    # 곳은 이후의 편성(고객사 카드 선정·전체 뉴스 정렬)뿐이다. 사건이 실제로 얼마나
    # 컸는지가 그제서야 확정되므로, 편성은 그 값을 봐야 맞다.
    rescored = rescore_coverage(news)
    if rescored:
        print(f"  보도량 가점 재계산 {rescored}건")

    news.sort(key=lambda x: x["published"], reverse=True)

    customer_news = [a for a in news if a.get("customer")]
    general_news = [a for a in news if not a.get("customer")]

    out = Path("raw_news.json")
    out.write_text(json.dumps(news, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {len(news)}건 수집 완료 → {out}")
    print(f"  일반 해사 뉴스: {len(general_news)}건")
    print(f"  고객사 관련 뉴스: {len(customer_news)}건")
    if customer_news:
        from collections import Counter
        counts = Counter(a["customer"] for a in customer_news)
        for name, cnt in counts.most_common():
            print(f"    - {name}: {cnt}건")
