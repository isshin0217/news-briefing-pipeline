"""
2_summarize.py
API 없이 키워드 분류 + Google 무료 번역으로 한국어 제목/요약 생성
(Google 이 막히면 Gemini 로 넘긴다 — GEMINI_FALLBACK 주석 참고)
실행: python 2_summarize.py
입력: raw_news.json
결과: summarized_news.json
"""

import json
import re
import time
from pathlib import Path
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

import gemini_client as gemini
import keyword_match as km

load_dotenv()

# Hiragana/Katakana + CJK Ext-A + CJK Unified + CJK Compat. Hangul(AC00-D7A3) excluded.
_CJK_RANGES = [(0x3040, 0x30ff), (0x3400, 0x4dbf), (0x4e00, 0x9fff), (0xf900, 0xfaff)]
CJK_PATTERN = re.compile('[' + ''.join(f'{chr(a)}-{chr(b)}' for a, b in _CJK_RANGES) + ']')

KEYWORD_CATEGORY = {
    # 이름에 AI 를 넣었다(2026-08-24 피드백). 분류 키워드에 원래 'ai'가 있어 AI 기사가
    # 이 칸에 들어오는데, 이름이 '자율운항/스마트십'이라 읽는 사람이 그렇게 읽지 않았다.
    "AI/자율운항/스마트십": ["autonomous", "smart ship", "mass", "ai", "digital",
                             "자율운항", "스마트십", "인공지능",
                             # AX(AI 전환) 흐름 (2026-10-06 피드백)
                             "ax", "ai 전환", "인공지능 전환", "llm", "agentic"],
    # '규정/컴플라이언스' 카테고리는 삭제했다(2026-08-24 피드백).
    # 규제 소식은 이미 상단 '🌐 글로벌 컴플라이언스(IMO·선급)' 카드 섹션이 따로 싣는다.
    # 하단 '전체 뉴스'에 같은 성격의 칸을 또 두니 같은 주제가 두 군데로 갈렸다.
    # 카드 섹션에 못 든 규제 기사는 아래 다른 칸(주로 신조/조선·기타)으로 분류된다.
    # "one"(ONE 선사)은 제거: 단어경계를 적용해도 영문 기사에서 수사 'one'이 흔해 오탐.
    # ONE 선사는 1_fetch_news 의 CUSTOMER_RULES 가 정확히 감지한다.
    # 조선소가 '하는 일'만 넣는다. 회사명은 COMPANY_NAMES 로 따로 뺐다(2026-09-01) —
    # 회사명이 주제를 결정하면 안 되기 때문이다. 실측: '무탄소선 시장 50% 노린다…
    # HD현대·한화오션·삼성重 친환경 기술 각축'이 회사명 2개(한화오션·hd현대)로
    # 탈탄소(친환경 1개)를 이겨 신조/조선으로 갔다.
    #
    # '조선' 단독은 뺐다. 한글은 부분 문자열로 판정하므로 '유조선'에 걸린다 —
    # 실측: '이란, 호르무즈 통항 위반 유조선 45척 블랙리스트'가 신조/조선이 됐다.
    # 걸려야 할 표현만 구체적으로 적는다.
    "신조/조선":         ["newbuilding", "vessel", "shipbuilding", "order", "delivery",
                         "신조", "조선소", "조선업", "조선사", "조선해양", "k-조선", "조선 빅3",
                         # 조선소가 하는 일
                         "수주", "건조", "창정비", "mro", "진수", "명명식", "도크", "야드",
                         "수리조선", "개조", "인도식", "기공식", "블록공장",
                         # 함정 (군함도 조선소가 짓는다)
                         "함정", "군함", "구축함", "호위함", "잠수함", "이지스함", "관공선",
                         "frigate", "destroyer", "naval vessel", "submarine"],
    # 'lng' 단독은 뺐다(2026-08-25 피드백). LNG 는 '연료'일 때만 탈탄소 소식이고, '화물'일 때는
    # 해운 소식이다. 실측: 'SK해운, 자산교환 통해 아시아 최대 LNG운반선 선사로 도약'과
    # '이란, 장금상선 선박 호르무즈 규정 위반 경고'가 'lng' 하나로 탈탄소 칸에 들어왔다.
    # 연료 표기만 남긴다.
    "탈탄소/친환경":     ["lng 추진", "lng연료", "lng 연료", "lng-fuelled", "lng fuel",
                         "이중연료", "dual fuel", "ammonia", "methanol", "암모니아", "메탄올",
                         "carbon", "emission", "green", "탈탄소", "친환경", "저탄소",
                         # 규제발 탈탄소 (2026-08-25 피드백: GHG 기사는 이 칸이 맞다)
                         "ghg", "온실가스", "net-zero", "net zero", "넷제로",
                         "decarbon", "탄소중립", "배출규제", "eu ets", "fueleu", "cii",
                         # 2026-09-07 피드백: '탄소 규제 강화에 AI 뜬다…DNV "2050년 해운
                         # 에너지 소비 최대 25% 절감"'이 AI 칸으로 갔다. 목록에 저탄소·
                         # 탈탄소·탄소중립은 있는데 규제를 가리키는 '탄소 규제'가 없어
                         # 탈탄소 점수가 0.0 이었다(AI 3.0: 제목 'ai' + 요약 '인공지능').
                         # 배출 관련 표현도 '배출규제' 하나뿐이라 같은 구멍이 있다.
                         "탄소 규제", "탄소규제", "탄소배출", "탄소 배출", "배출권", "배출량",
                         # 해운에서 '에너지 효율'은 EEXI·CII 로 규제되는 탈탄소 수단이다.
                         # 연료를 덜 쓰는 이야기는 어떤 수단을 쓰든 이 칸에 속한다.
                         "에너지 효율", "에너지효율", "에너지 절감", "연료 절감", "연료절감",
                         "eexi", "eedi",
                         # 2026-10-06 피드백("친환경, 탈탄소.. 빠지는 현상"): 선상탄소포집
                         # (OCCS) 기사가 이 칸이 아니라 컴플라이언스에만 실려 독자가 못 봤다.
                         "탄소포집", "occs", "carbon capture", "무탄소", "수소", "hydrogen",
                         "풍력보조", "wind-assisted"],
    # 북극항로를 여기에 둔다: 새 항로 개설·시범운항은 항로/물류 소식이다.
    # 이 항목이 없으면 '컨테이너선 첫 북극항로 시범운항'처럼 이번 주 최대 기사가
    # 어느 칸에도 안 맞아 '기타'로 떨어진다(실측 2026-08-24).
    # 'arctic'은 단어경계로 판정되므로 antarctic 에는 걸리지 않는다.
    "해운시황/물류":     ["freight", "rate", "container", "shipping", "trade", "port", "해운", "물류",
                         "북극항로", "북극해", "arctic", "northern sea route", "빙해",
                         # 선박 운항·항로·제재 (2026-09-01 피드백).
                         # 예전 목록에는 배가 '다니는' 이야기를 가리키는 말이 없어서,
                         # 그 기사가 요약에 스친 '유조선'의 '조선' 때문에 신조/조선으로 갔다.
                         "유조선", "탱커", "통항", "해협", "항행", "억류", "나포",
                         "제재", "블랙리스트", "용선", "운임", "선복", "기항", "체선",
                         "tanker", "strait", "sanction", "charter", "freight rate"],
}

# 회사명. 주제를 '거들' 뿐 결정하지는 않는다(가중치 WEAK_WEIGHT).
#
# 2026-09-01: 조선사명을 신조/조선 목록에 그냥 넣었더니 회사명 개수가 주제어를 눌렀다.
# 기사가 어느 칸에 속하는지는 '무엇에 관한 이야기인가'가 정하지 '누가 나오는가'가
# 정하지 않는다. 그래서 절반 이하 가중치로 따로 센다.
COMPANY_NAMES = {
    # '중공업'·'heavy industries'·'hhi' 도 여기 둔다 — 주제어가 아니라 회사명 조각이다.
    # 실측: 이것들이 주제어 목록에 있을 때 '삼성重도 피지컬 AI 도입 빨라진다'가
    # 요약의 '삼성중공업'에 걸려 AI 칸 대신 신조/조선으로 갔다.
    "신조/조선": ["한화오션", "삼성중공업", "hd현대", "현대중공업", "한국조선해양",
                  "케이조선", "hj중공업", "현대미포", "대선조선", "중공업",
                  "hanwha ocean", "samsung heavy", "hd hyundai", "heavy industries", "hhi"],
    "해운시황/물류": ["hmm", "팬오션", "대한해운", "sk해운", "장금상선", "현대글로비스",
                      "폴라리스쉬핑", "흥아해운", "ocean network express"],
}

TITLE_WEIGHT = 2.0    # 제목에 있는 주제어
SUMMARY_WEIGHT = 1.0  # 요약에만 있는 주제어
WEAK_WEIGHT = 0.4     # 회사명 (어디에 있든)

translator_ko = GoogleTranslator(source="en", target="ko")  # 영문 → 한국어
translator_en = GoogleTranslator(source="ko", target="en")  # 한국어 → 영문


def guess_category(title: str, summary: str) -> str:
    """주제어 점수가 가장 높은 카테고리로 분류한다. 동점이면 위 목록 순서가 이긴다.

    점수를 세 가지로 나눈다(2026-09-01). 그냥 개수를 세던 방식은 오분류가 잦았다.
      제목 2.0  — 기사의 주제는 제목이 말한다.
      요약 1.0  — 요약에만 스친 단어는 주제가 아닐 수 있다.
      회사명 0.4 — 누가 나오는가는 무엇에 관한 이야기인가를 정하지 않는다.

    실측(2026-09-01 피드백)으로 확인한 오분류 세 가지가 모두 이 셋으로 갈린다.
      '무탄소선 시장 50% 노린다…HD현대·한화오션·삼성重 친환경 기술 각축'
          회사명 2개(신조)가 주제어 1개(탈탄소)를 이겼다 → 회사명 가중치를 낮춰 해결
      '삼성重도 피지컬 AI 도입 빨라진다'
          제목은 AI인데 요약에 스친 '조선소'가 이겼다 → 제목 가중치를 올려 해결
      '이란, 호르무즈 통항 위반 유조선 45척 블랙리스트'
          '유조선'의 부분 문자열 '조선'에 걸렸다 → 목록에서 '조선' 단독을 빼서 해결

    앞선 이력(2026-08-24): 원래는 '첫 매칭 승리'였다. 목록 맨 앞 AI/자율운항 칸이
    'ai'·'자율운항' 같은 넓은 키워드를 갖고 있어 요약에 한 번만 스쳐도 그 칸으로 갔고,
    54건 중 29건(54%)이 그 칸에 몰렸다. 그래서 개수 비교로 바꿨고, 지금은 가중치로 바꾼다.
    """
    # 제목 꼬리의 언론사명 제거: "… - 조선일보/조선비즈"가 '조선'으로 오분류되던 문제 방지
    head = km.strip_outlet(title).lower()
    body = (summary or "").lower()
    both = head + " " + body

    best, best_score = "기타", 0.0
    for cat, keywords in KEYWORD_CATEGORY.items():
        # 영문 약어는 단어 단위로 판정 ('ai'가 hyundai/training에 걸리던 문제 방지)
        in_title = set(km.matched_keywords(head, keywords))
        in_body = set(km.matched_keywords(body, keywords)) - in_title
        score = TITLE_WEIGHT * len(in_title) + SUMMARY_WEIGHT * len(in_body)
        score += WEAK_WEIGHT * len(km.matched_keywords(both, COMPANY_NAMES.get(cat, [])))
        if score > best_score:        # 동점은 갱신하지 않는다 = 목록 순서가 우선순위
            best, best_score = cat, score
    return best


def _clean(text: str) -> str:
    """한자/가나 제거 후 남는 공백·괄호·구두점을 정리한다."""
    text = CJK_PATTERN.sub("", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)   # 빈 괄호 제거
    text = re.sub(r"\s{2,}", " ", text)            # 연속 공백 축소
    text = re.sub(r"\s+([,.)\]])", r"\1", text)    # 구두점 앞 공백 제거
    return text.strip()


# Google 무료 번역이 연달아 이만큼 실패하면 그 회차에는 더 부르지 않는다.
# 2026-09-28 실측: 서버 IP 가 TooManyRequests(429)로 막혀 요청 1건부터 전부 실패했다.
# 막힌 뒤에도 기사마다 두 번씩 재시도하며 시간만 쓰고, 결과는 전부 원문이었다.
GOOGLE_GIVE_UP_AFTER = 3
_google_fail_streak = 0


def translate(text: str, translator) -> str | None:
    """Google 무료 번역. 실패하면 None — 호출부가 Gemini 로 다시 번역한다.

    예전에는 실패 시 원문을 그대로 돌려줬다. 그래서 번역이 통째로 죽어도 아무도 몰랐다
    (2026-09-28: 영→한 17건·한→영 33건 전부 원문으로 발송). 게다가 국문 제목이
    영어로 남으면 같은 사건 판정(dedup_events, title_ko 기준)이 국문 기사와 대조를
    못 해 중복이 실린다 — 'CLdN orders two more ro-ro vessels from HD Hyundai'가
    고객사 카드의 'HD현대중공업, 3362억원 규모 로로선 2척 수주'와 나란히 실렸다.
    """
    global _google_fail_streak
    if not text or not text.strip():
        return ""
    if _google_fail_streak >= GOOGLE_GIVE_UP_AFTER:
        return None
    for attempt in range(2):  # 한자가 섞이면 1회 재시도
        try:
            result = translator.translate(text[:4500]) or ""
        except Exception:
            time.sleep(0.6)
            continue
        _google_fail_streak = 0
        # 한자가 없으면 바로 사용, 있으면 재시도(마지막엔 제거)
        if not CJK_PATTERN.search(result):
            return _clean(result)
        if attempt == 0:
            time.sleep(0.6)
            continue
        return _clean(result)
    _google_fail_streak += 1
    if _google_fail_streak == GOOGLE_GIVE_UP_AFTER:
        print(f"  [경고] Google 번역 {GOOGLE_GIVE_UP_AFTER}건 연속 실패 — "
              "이번 회차는 Gemini 로 번역합니다")
    return None


# GEMINI_FALLBACK: Google 이 못 한 번역을 모아 Gemini 로 한 번에 처리한다.
# 배치로 묶는 이유 — 기사마다 부르면 50건이 50회 호출이다. 10건씩이면 5회로 끝난다.
GEMINI_BATCH = 10
_TRANSLATE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"items": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "INTEGER"},
                       "title": {"type": "STRING"},
                       "summary": {"type": "STRING"}},
        "required": ["id", "title", "summary"]}}},
    "required": ["items"],
}
_LANG_NAME = {"ko": "한국어", "en": "영어"}


def gemini_translate(jobs: list[dict], target: str) -> dict[int, tuple[str, str]]:
    """jobs=[{id, title, summary}] 를 target 언어로. 반환 {id: (title, summary)}.
    실패한 배치는 빠진다 — 호출부가 원문을 유지하고 경고를 남긴다."""
    out = {}
    if not jobs or not gemini.available():
        return out
    lang = _LANG_NAME[target]
    for i in range(0, len(jobs), GEMINI_BATCH):
        batch = jobs[i:i + GEMINI_BATCH]
        prompt = (
            f"다음 해운·조선 뉴스의 제목과 요약을 {lang}로 번역하세요.\n"
            "- 회사명·선종·기관명은 업계에서 통용되는 표기를 쓰세요 "
            "(예: ro-ro → 로로선, HD Hyundai Heavy Industries → HD현대중공업).\n"
            "- 제목 끝의 ' - 매체명' 은 그대로 두세요.\n"
            "- 내용을 더하거나 줄이지 말고, id 는 입력 그대로 돌려주세요.\n\n"
            + json.dumps(batch, ensure_ascii=False))
        try:
            res = gemini.ask_json(prompt, _TRANSLATE_SCHEMA, model=gemini.FAST_MODEL)
        except Exception as e:
            print(f"  [경고] Gemini 번역 실패 ({i + 1}~{i + len(batch)}번째): {e}")
            continue
        for item in res.get("items", []):
            if item.get("title", "").strip():
                out[item["id"]] = (_clean(item["title"]), _clean(item.get("summary", "")))
    return out


def drop_translated_noise(summarized: list) -> list:
    """번역된 한국어 제목으로 수집 단계 제외 규칙을 한 번 더 적용한다.

    2026-10-19 피드백: 영문 기사
    'Union Leader Launches Crane Sit-in at Hyundai Heavy Industries' Ulsan Yard'가
    전체 뉴스에 실렸다. 수집 단계(1_fetch_news.make_article)는 원제목만 보는데 영문
    노사 목록에 'sit-in'·'union leader'가 없어 통과했고(고객사 +6, 20점), 번역된 제목
    '…노조 위원장 크레인 고공 농성 돌입'은 '노조'로 걸리지만 번역은 수집 다음 단계다.

    영문 표현을 목록에 하나씩 더하는 것만으로는 매번 한발 늦는다. 제외 목록은 한국어
    쪽이 훨씬 촘촘하니, 번역이 끝난 여기서 한국어 제목으로 한 번 더 본다. 이 파일이
    summarized_news.json 을 쓰므로 분석·카드·전체 뉴스·메일 모두 이 결과를 따른다.
    판정 함수는 5_analyze.is_collect_noise 와 같은 것을 쓴다(자사 기사 면제도 같다).
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fetch_news", str(Path(__file__).parent / "1_fetch_news.py"))
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)
    checks = (fetch.is_event_noise, fetch.is_vendor_promo, fetch.is_donation_noise,
              fetch.is_labor_noise, fetch.is_stock_title_noise, fetch.is_medical_noise)

    kept = []
    for art in summarized:
        title_ko = art.get("title_ko") or ""
        # 한국어 원문 기사는 수집 단계에서 이미 같은 제목으로 판정받았다.
        if art.get("own") or art.get("lang") != "en" or not title_ko:
            kept.append(art)
            continue
        if any(f(title_ko) for f in checks):
            print(f"  · 번역 제목으로 제외: {title_ko[:50]}")
            continue
        kept.append(art)
    if len(kept) != len(summarized):
        print(f"  번역 후 재판정 {len(summarized) - len(kept)}건 제외")
    return kept


def run():
    raw_path = Path("raw_news.json")
    if not raw_path.exists():
        print("raw_news.json이 없습니다. 먼저 1_fetch_news.py를 실행하세요.")
        return False

    articles = json.loads(raw_path.read_text(encoding="utf-8"))
    print(f"\n[2단계] 분류 + 번역 시작 - 총 {len(articles)}건\n")

    summarized = []
    pending = {"ko": [], "en": []}   # Google 번역이 실패한 summarized 인덱스 (목표 언어별)
    for i, art in enumerate(articles, 1):
        # 연재 코너명·말머리를 '번역 전에' 뗀다 (2026-09-01 피드백).
        # 붙은 채로 넘기면 번역·분석 단계가 코너명을 고유명사로 읽는다 —
        # '[마린 유니콘]'이 한 줄 결론에서 회사 이름이 됐다(km.strip_corner 주석 참고).
        # 원문은 title_raw 에 남겨 둔다(링크 대조·디버깅용).
        title = km.strip_corner(art["title"])
        category = guess_category(title, art["summary"])
        lang = art.get("lang", "en")

        # 모든 기사에 한/영 4개 필드를 채운다 (부족한 언어만 번역)
        # 번역이 실패하면(None) 일단 원문을 넣고 pending 에 올려 Gemini 로 다시 한다.
        if lang == "en":
            print(f"  [{i}/{len(articles)}] EN→KO 번역: {title[:50]}...")
            title_en, summary_en = title, art["summary"][:800]
            title_ko = translate(title, translator_ko)
            summary_ko = translate(art["summary"][:1000], translator_ko)
            if title_ko is None or summary_ko is None:
                pending["ko"].append(len(summarized))
            title_ko = title_ko or title
            summary_ko = summary_ko or art["summary"][:1000]
        else:
            print(f"  [{i}/{len(articles)}] KO→EN 번역: {title[:50]}...")
            title_ko, summary_ko = title, art["summary"][:500]
            title_en = translate(title, translator_en)
            summary_en = translate(art["summary"][:1000], translator_en)
            if title_en is None or summary_en is None:
                pending["en"].append(len(summarized))
            title_en = title_en or title
            summary_en = summary_en or art["summary"][:1000]
        if _google_fail_streak < GOOGLE_GIVE_UP_AFTER:
            time.sleep(0.5)  # 무료 번역 rate limit 방지

        summarized.append({
            **art,
            "title": title,              # 코너명을 뗀 제목으로 바꾼다
            "title_raw": art["title"],   # 원문 제목 (대조·디버깅용)
            "title_ko": title_ko,
            "title_en": title_en,
            "summary_ko": summary_ko,
            "summary_en": summary_en,
            "category": category,
            "importance": 4 if art.get("customer") else 2,
            "keywords": [],
            "error": None,
        })

    # Google 이 못 한 번역을 Gemini 로 (GEMINI_FALLBACK 주석 참고)
    for target, idxs in pending.items():
        if not idxs:
            continue
        print(f"\n  Google 번역 실패 {len(idxs)}건 → Gemini 로 {_LANG_NAME[target]} 번역")
        src_sum = "summary_en" if target == "ko" else "summary_ko"
        jobs = [{"id": n, "title": summarized[n]["title"],
                 "summary": summarized[n][src_sum][:1000]} for n in idxs]
        done = gemini_translate(jobs, target)
        for n, (t, s) in done.items():
            summarized[n][f"title_{target}"] = t
            summarized[n][f"summary_{target}"] = s or summarized[n][f"summary_{target}"]
        left = [n for n in idxs if n not in done]
        print(f"  Gemini 번역 {len(done)}건 완료"
              + (f", {len(left)}건은 원문 유지" if left else ""))
        for n in left:
            summarized[n]["translate_failed"] = target

    summarized = drop_translated_noise(summarized)

    out = Path("summarized_news.json")
    out.write_text(json.dumps(summarized, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n완료 → {out}")

    from collections import Counter
    cats = Counter(a["category"] for a in summarized)
    print("\n카테고리별 건수:")
    for cat, cnt in cats.most_common():
        print(f"  {cat}: {cnt}건")
    return True


if __name__ == "__main__":
    run()
