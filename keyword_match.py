"""
keyword_match.py
키워드 매칭 유틸 — 짧은 영문 약어의 부분 문자열 오탐을 막는다.

배경:
  기존에는 모든 키워드를 `kw in text` 로 판정해서 짧은 영문 약어가 엉뚱한 단어에 걸렸다.
    "ai"    → hyund(ai), tr(ai)ning, s(ai)d      (안전 셧다운 기사가 AI 뉴스로 분류됨)
    "imo"   → 무관한 단어 일부
    "one"   → ph(one), z(one), milest(one)
    "order" → b(order), rec(order)
  영문 키워드에만 단어 경계를 적용해 해결한다.

한국어에는 경계를 적용하지 않는다:
  한국어는 조사가 붙어 쓰이므로("선박이", "해운은", "HD현대중공업") 경계를 걸면
  오히려 정상 매칭을 놓친다. 한글이 섞인 키워드는 기존 부분 문자열 판정을 유지한다.

주의: 입력 text 는 호출부에서 소문자로 변환해 넘긴다 (기존 동작과 동일).
"""

import re
from functools import lru_cache

_HANGUL = re.compile(r"[가-힣]")


@lru_cache(maxsize=4096)
def _pattern(keyword: str):
    """영문 전용 키워드는 단어경계 정규식, 한글 포함 키워드는 None(부분 문자열 판정)."""
    if _HANGUL.search(keyword):
        return None
    kw = keyword.strip()          # "amos ", "one ", " ai" 처럼 공백으로 경계를 흉내낸 값 정리
    if not kw:
        return None
    # \b 대신 명시적 lookaround: 하이픈 포함 키워드("h-line", "s-100")도 올바르게 처리
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")


def has_keyword(text: str, keyword: str) -> bool:
    """text 안에 keyword 가 있는지 판정 (영문은 단어 단위, 한글은 부분 문자열)."""
    pat = _pattern(keyword)
    if pat is None:
        return keyword in text
    return pat.search(text) is not None


def any_keyword(text: str, keywords) -> bool:
    return any(has_keyword(text, kw) for kw in keywords)


def count_keywords(text: str, keywords) -> int:
    """매칭된 키워드 개수 (점수 가중용)."""
    return sum(1 for kw in keywords if has_keyword(text, kw))


def count_kinds(text: str, keywords, groups=()) -> int:
    """매칭된 키워드 '종류' 수. groups 에 든 동의어 묶음은 몇 개가 맞든 1종으로 센다.

    왜 필요한가(2026-09-01 피드백):
      점수는 '몇 종류의 제품 주제를 건드렸나'로 매기는데(1_fetch_news.relevance_score),
      count_keywords 는 같은 주제의 동의어를 각각 1종으로 센다. 북극 계열은 핵심어에
      '북극항로·북극해 항로·쇄빙·내빙·ice class·polar code…' 11개가 들어 있어,
      북극 기사는 한 주제만 다뤄도 손쉽게 3종 상한을 채워 +15를 받았다.
      실측(2026-08-31): '팬스타 아크로호 북극해 진입' 이 북극항로+내빙+쇄빙 3종으로
      15점을 벌어 28점 1위, '첫발 뗐지만 상용화까지는' 이 같은 방식으로 26점 2위였다.
      WECDIS+S-100+ENC 처럼 실제로 세 주제를 건드린 기사와 같은 대우를 받은 셈이다.
    동의어 묶음을 1종으로 세면 주제 우선순위(핵심어 +5)는 그대로 두고 중복 계산만 막는다.
    """
    grouped = {kw for g in groups for kw in g}
    kinds = sum(1 for kw in keywords
                if kw not in grouped and has_keyword(text, kw))
    kinds += sum(1 for g in groups if any_keyword(text, g))
    return kinds


def matched_keywords(text: str, keywords) -> list:
    """매칭된 키워드 목록 (디버깅·검증용)."""
    return [kw for kw in keywords if has_keyword(text, kw)]


def first_position(text: str, keywords) -> int:
    """키워드 중 가장 앞선 등장 위치. 하나도 없으면 -1.
    (고객사 감지에서 '제목 맨 앞에 나온 회사'를 고르는 데 사용)"""
    best = -1
    for kw in keywords:
        pat = _pattern(kw)
        if pat is None:
            pos = text.find(kw)
        else:
            mt = pat.search(text)
            pos = mt.start() if mt else -1
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
    return best


# 조선·해운과 무관한 사업부문. 국내 중공업사는 조선 외에 건설·토목·플랜트·전자
# 부문을 함께 갖고 있어, 회사명만 보고 태깅하면 그쪽 기사가 고객사 동향에 실린다
# (2026-08-25 피드백). 수집 단계(1_fetch_news)와 카드 선정(3_build_html)이 같은
# 기준을 쓰도록 여기에 둔다. 제목으로만 판정한다 — 본문에 '반도체'가 스치는
# 정상 해사 기사를 막지 않기 위해서다.
NONMARINE_KEYWORDS = [
    "토건", "토목", "건설부문", "건설 부문", "건설사업", "건설 사업",
    "고속도로", "국도", "아파트", "주택", "재건축", "재개발",
    "반도체", "디스플레이", "시공능력", "도로 공사", "교량",
    # '터널' 단독은 뺐다(2026-09-01). 한글은 부분 문자열 판정이라 '터널 스러스터'
    # (tunnel thruster — 선박 추진 장치)에 걸린다. 실측: 'Kongsberg Maritime,
    # 림 드라이브 터널 스러스터 범위 확장'이 비조선 사업부문으로 잡혔다.
    "터널 공사", "터널 굴착", "지하터널", "터널 시공",
    # 항공·육상 물류 (2026-09-01 피드백). 종합물류사는 해운 외에 항공·육상 부문을 함께 갖고 있어,
    # 회사명만 보고 태깅하면 그쪽 기사가 고객사 동향에 실린다 — 삼성중공업 토건과 같은 유형.
    # '물류'·'운송' 단독은 넣지 않는다 — 해상 물류 기사가 통째로 걸린다.
    "항공포워딩", "항공 포워딩", "항공화물", "항공 화물", "항공운송", "항공 운송",
    "항공사업", "육상운송", "택배", "철도 물류",
    # 전력 설비 (2026-10-06 피드백). 조선사가 짓는 해상 전력 설비라 '해상'·고객사명으로 20점을
    # 받아 고객사 동향에 올랐다. 해상풍력 '설치선'(선박)은 걸리지 않게 설비어만 넣는다.
    "변전소", "변전설비",
    # 우주 (2026-10-12 피드백: '누리호 발사 뒷받친 HD현대重...로켓 발사대 어떻게 만들었나')
    "누리호", "발사대", "발사체", "로켓",
    # 수산·어업 정책 (2026-10-12 피드백: '남재헌 해양수산부 차관, "어선 대폭 줄이고…
    # 잡는 방식 확 바꾼다"' 빼라). '어선'은 해양 사고·선종 어휘라 KEYWORDS 에 남겨 두고,
    # 어업 정책을 가리키는 말만 넣는다.
    "어업", "어획", "감척", "수산자원", "잡는 방식", "양식장", "어민",
]

# ── 산업재해(작업장 인명사고) 판정 ──────────────────────────────────────────
#
# 2026-09-01 피드백.
# 실측(2026-09-01): HD현대중공업 작업장 사망 사고가 국문·영문 3벌 수집됐고 그중 한 건이
# 고객사 동향 카드에 실렸다. 주체가 같고(HD현대) 공유 숫자가 없어 중복 규칙으로도
# 안 걸리는 유형이라, 사안 자체를 수집에서 막는 게 맞다.
#
# 핵심은 '해양 사고'와 가르는 것이다. 선박 침몰·충돌·좌초로 인한 인명 피해는 해사
# 뉴스라 실려야 한다. 그래서 인명 피해어 하나만으로는 절대 걸지 않고,
# '작업장 맥락'이 함께 있을 때만 산재로 본다.
#   걸림   'HD현대중공업, 보일러실서 근로자 1명 사망'     (사망 + 보일러실·근로자)
#   안 걸림 '여객선 침몰 3명 사망'                        (작업장 맥락 없음)
#   안 걸림 '선원 2명 실종'                               (작업장 맥락 없음)
# '중대재해'·'산업재해'·'산재'는 그 말 자체가 작업장 사고라 단독으로 잡는다.
_CASUALTY_TERMS = [
    "숨져", "숨진", "숨졌", "사망", "부상", "다쳐", "다친", "매몰", "추락",
    "질식", "끼임", "협착", "감전", "화상", "중상", "참변",
    # 영문은 단어 단위 판정이라 활용형을 다 적어야 한다(2026-09-01).
    # 실측: 'Another Worker Dies at HD Hyundai Heavy Industries'가 'dies' 가
    # 목록에 없어 통과했다 — 'died' 만 있었다.
    "fatal", "fatality", "fatalities", "death", "deaths", "die", "dies", "died",
    "kill", "kills", "killed", "injure", "injured", "injury", "injuries", "dead",
]
_WORKPLACE_TERMS = [
    "작업장", "보일러실", "근로자", "노동자", "협력업체", "하청", "사내하청",
    "공장", "야드", "도크", "조선소", "사업장", "작업 중", "작업중",
    "크레인", "비계", "용접공", "안전사고",
    "workplace", "worker", "workers", "shipyard", "plant", "subcontractor",
]
# 단독으로 산재를 뜻하는 말
_ACCIDENT_ALONE = [
    "중대재해", "중대 재해", "산업재해", "산재", "노동재해",
    "고용노동부 조사", "작업중지", "작업 중지",
    "industrial accident", "workplace accident", "occupational",
]


def is_industrial_accident(title: str) -> bool:
    """작업장 인명사고(산재) 기사인가 — 제목 기준.

    해양 사고(침몰·충돌·좌초)와 구분한다: 인명 피해어 + 작업장 맥락이 함께 있어야 한다.
    """
    low = strip_quotes(strip_outlet(title or "")).lower()
    if any_keyword(low, _ACCIDENT_ALONE):
        return True
    return any_keyword(low, _CASUALTY_TERMS) and any_keyword(low, _WORKPLACE_TERMS)


# ── 해양 사고(인명피해·수색구조) 판정 ────────────────────────────────────────
#
# 2026-09-01 피드백.
# 같은 날 오전에는 반대로 판단했었다 — 산재를 빼면서 '선박 침몰·좌초로 인한 인명 피해는
# 해사 뉴스라 실려야 한다'고 보고 선종·사고 어휘를 수집 키워드에 넣었다(1_fetch_news.py
# KEYWORDS 주석). 그 판단을 뒤집는다. 독자는 기술개발팀이고, 사건사고 보도에는
# 제품·기술·시장 정보가 없다는 게 실무자 판단이다.
#
# 범위는 '인명피해·수색구조 사고만'이다. 사고를 계기로 한 규제·기술 기사는 남긴다.
#   걸림   '여객선 침몰로 승객 3명 사망…해경 수색 중'   (침몰 + 사망)
#   걸림   '화물선 좌초, 선원 8명 구조'                  (좌초 + 명 구조)
#   안 걸림 'IMO 극지 운항 안전기준 폴라코드 개정 논의'   (사고어 없음)
#   안 걸림 '선박 충돌 방지 AI 시스템 개발'              (충돌만, 인명·구조 없음)
#   안 걸림 '유조선 좌초, 기름 유출 방제 작업'            (인명·구조 없음 — 환경 사고)
#
# 사고어 하나만으로는 절대 걸지 않는다. '충돌 방지'·'좌초 위험 예측' 처럼 사고어는
# 항법 제품 기사에 그대로 등장한다 — 아크메가 파는 게 그걸 막는 물건이라 그렇다.
_MARINE_ACCIDENT_TERMS = [
    "침몰", "좌초", "전복", "충돌", "해난", "조난", "표류", "뒤집",
    "capsize", "capsized", "capsizing", "aground", "grounding", "grounded",
    "sinking", "sank", "sunk", "collision", "collided", "adrift",
]
# 인명피해·수색구조 맥락. '구조'는 '선박의 구조(構造)'와 겹쳐서 단독으로 쓰지 않고
# 활용형·수량 표현으로만 잡는다 — 실제로 회귀 케이스의 요약문에 '선박의 구조와 장비'가
# 들어 있다(폴라코드 설명). 제목만 보더라도 '선체 구조 개선' 같은 기사를 죽이면 안 된다.
_SEA_CASUALTY_TERMS = [
    "사망", "숨져", "숨진", "숨졌", "실종", "부상", "익사", "참변", "인명피해",
    "수색", "구조작업", "구조 작업", "인명구조", "구조대", "구조됐", "구조된",
    "구조돼", "구조해", "구조했", "명 구조", "명이 구조", "전원 구조", "긴급구조",
    "dead", "death", "deaths", "died", "dies", "missing", "drowned", "drowning",
    "rescue", "rescued", "rescuing", "survivors", "search and rescue",
]


def is_marine_casualty(title: str) -> bool:
    """해양 사고(인명피해·수색구조) 보도인가 — 제목 기준.

    사고어 + 인명피해/수색구조어가 함께 있을 때만 참이다. is_industrial_accident 와
    같은 구조인데, 저쪽은 '작업장 맥락'을 짝으로 요구하고 여기는 '사람이 다쳤거나
    찾고 있다'를 요구한다.
    """
    low = strip_quotes(strip_outlet(title or "")).lower()
    return (any_keyword(low, _MARINE_ACCIDENT_TERMS)
            and any_keyword(low, _SEA_CASUALTY_TERMS))


_QUOTES = re.compile(r"[‘’“”'\"`]")


def strip_quotes(text: str) -> str:
    """따옴표를 지운다. 여러 단어로 된 한글 키워드 판정 전에 쓴다.

    한국 기사 제목은 고유명사를 따옴표로 감싸는 일이 잦은데, 한글 키워드는 단어경계를
    못 써서 부분 문자열로 판정하므로 따옴표 하나가 매칭을 통째로 깨뜨린다.
    실측(2026-08-25): "삼성중공업-KAIST '차세대 선박연구센터' 출범" 이 감점 키워드
    '센터 출범' 에 안 걸렸다 — 실제 문자열은 "센터' 출범" 이기 때문이다.
    따옴표를 지우면 걸린다. ('한국해양대·KOMERI 'AX 혁신허브' 구축' 도 같은 경우)
    """
    return _QUOTES.sub("", text or "")


# 제목 앞뒤에 붙는 연재 코너명·말머리. "[마린 유니콘]", "[MASGA 인사이드]", "[단독]" 등.
_CORNER_HEAD = re.compile(r"^(?:\s*[\[\(【][^\]\)】]{1,22}[\]\)】]\s*)+")
_CORNER_TAIL = re.compile(r"(?:\s*[\[\(【][^\]\)】]{1,22}[\]\)】]\s*)+$")
# 코너명을 뗀 뒤 남는 회차 표시 (①②③ / 1) / (2) / <3> )
_SERIES_HEAD = re.compile(r"^\s*(?:[①-⑮㉠-㉻]|\(?\d{1,2}\)|<\d{1,2}>)\s*")


def strip_corner(title: str, min_left: int = 6) -> str:
    """제목 앞뒤의 연재 코너명·말머리를 뗀다 (2026-09-01 피드백).

    왜 필요한가: 코너명이 붙은 채로 번역·분석 단계에 넘어가면 모델이 그걸 고유명사로
    읽는다. 실측(2026-09-01) 한 줄 결론이 이렇게 나왔다 —
        기사 제목  'AI로 북극해 유빙 정보 분석해 안전 항해 이끈다 [마린 유니콘]'
        한 줄 결론 '마린 유니콘이 AI 기반 북극해 유빙 정보 분석 플랫폼을 구축하고…'
    '마린 유니콘'은 신문 연재 코너명인데 회사 이름이 돼 버렸다.

    앞뒤만 뗀다. 제목 가운데 있는 괄호는 내용의 일부다
    ('무탄소선 시장 50% 노린다...HD현대·한화오션·삼성重 친환경 기술 각축').
    떼고 나서 너무 짧아지면(min_left 미만) 원본을 그대로 쓴다 — 괄호가 곧 제목인 기사도
    있기 때문이다.
    """
    if not title:
        return ""
    out = _CORNER_TAIL.sub("", _CORNER_HEAD.sub("", title)).strip()
    out = _SERIES_HEAD.sub("", out).strip()
    # 코너명을 떼면서 남은 구두점 정리
    out = re.sub(r"^[\s·,\-–—:]+", "", out).strip()
    return out if len(out) >= min_left else title.strip()


def strip_outlet(title: str) -> str:
    """Google News 제목 꼬리의 언론사명을 제거한다.
    "제목 - 조선일보" 처럼 매체명이 붙어 '조선'·'중앙' 등이 오탐되던 문제 방지."""
    return title.rsplit(" - ", 1)[0] if " - " in title else title


# ─────────────────────────────────────────────────────────────────────────────
# 같은 사건 판정 (다른 매체가 같은 사안을 다르게 쓴 기사 제거)
#
# 문자 bigram 유사도는 실측에서 실패했다: 중복쌍 0.00~0.38 / 정상쌍 0.12 로 구간이
# 겹쳐 임계값을 못 잡는다. 회사명이 bigram을 지배해 정작 사건을 특정하는 단어
# ('무인수상정', 'AIDC용', '브랜드평판')의 신호가 묻히기 때문이다.
#
# 그래서 단어 단위로 바꾸고, 코퍼스에서 드물게 등장하는 토큰만 비교한다.
# 추가로 '같은 회사 기사인지'를 게이트로 쓴다 — 실측상 오탐은 대부분
# 서로 다른 회사 기사끼리 흔한 업계용어('글로벌','목표','확대')를 공유한 경우였다.
# ─────────────────────────────────────────────────────────────────────────────

# 조사/어미 — 토큰 끝에서 떼어내 표기를 통일한다 (긴 것부터 검사)
_PARTICLES = ("으로서", "으로", "에서", "까지", "부터", "에게", "이라며", "라며",
              "이라고", "라고", "은", "는", "이", "가", "을", "를", "에", "의",
              "도", "와", "과", "로", "만", "및", "등")

_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣 ]")

# 제목 속 '규모 숫자' (5,154억 / 515.4 Billion / 1740억). 구분기호를 지운 숫자만 남긴다.
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")


def number_tokens(title: str, min_digits: int = 3) -> set:
    """제목에 든 규모 숫자를 표기와 무관한 '#숫자' 토큰으로 만든다.

    왜 필요한가(2026-09-01 피드백):
      같은 수주 건이 세 벌 실렸는데, 세 제목이 공유하는 토큰이 '4척' 하나뿐이라
      is_same_event 를 통과했다. 계약금액은 셋 다 있었지만 표기가 갈렸다.
        HD한국조선해양, LPG운반선 4척 5154억 수주            → '5154억'
        HD현대중공업, 5154억원 규모...LPG 운반선 4척 건조 계약 → '5154억원'
        HD Korea Shipbuilding Wins 515.4 Billion Won Order   → '515', '4'
      금액·척수 같은 숫자는 사건을 아주 강하게 특정하는데(같은 주에 5154억 수주가
      두 건일 리 없다), 단위와 구분기호 때문에 문자 토큰으로는 안 겹친다.
      숫자만 뽑아 정규화하면 셋 다 '#5154' 로 만나 국문·영문 기사까지 함께 묶인다.

    연도(19xx·20xx)는 뺀다 — 사건이 아니라 시점이라 서로 다른 기사에 흔히 겹친다.
    (df 기준으로도 대개 걸러지지만, 드물게 나온 연도가 강한 신호로 오인되는 걸 막는다.)
    """
    out = set()
    for match in _NUMBER.finditer(strip_outlet(title or "")):
        digits = re.sub(r"\D", "", match.group(0)).lstrip("0")
        if len(digits) < min_digits:
            continue
        if len(digits) == 4 and 1900 <= int(digits) <= 2099:
            continue
        out.add("#" + digits)
    return out


def dup_tokens(title: str) -> set:
    """중복 판정용 단어 집합. 언론사명·기호 제거 후 조사를 떼고 2자 이상만 남긴다.
    규모 숫자는 표기가 갈려도 만나도록 '#숫자' 로 정규화해 함께 넣는다(number_tokens)."""
    text = _NON_WORD.sub(" ", strip_outlet(title or ""))
    out = number_tokens(title)
    for word in text.split():
        word = word.lower()
        for particle in _PARTICLES:
            if len(word) > len(particle) + 1 and word.endswith(particle):
                word = word[: -len(particle)]
                break
        if len(word) >= 2:
            out.add(word)
    return out


def doc_freq(token_sets) -> dict:
    """토큰별 문서빈도. 흔한 업계용어를 판정에서 제외하는 데 쓴다."""
    freq = {}
    for tokens in token_sets:
        for token in tokens:
            freq[token] = freq.get(token, 0) + 1
    return freq


def df_tokens(article: dict) -> set:
    """문서빈도 계산용 토큰 — 제목의 모든 표기(국문·영문·원문)를 합쳐서 센다.

    비교용 토큰은 dup_tokens(title_ko) 하나만 쓰지만, df 는 여기서 센다(2026-08-24).
    국문 제목만으로 세면 영문 단어의 빈도를 구조적으로 못 잡는다. 실측 54건 중
    국문 제목이 50건이라 6자 이상 영문 단어는 전부 df<=2 가 되어 '희귀 고유명사'로
    취급됐다 — commercial·management·services·digital·launch 등 35개가 그랬다.
    그 결과 무관한 두 기사가 'shipping' 하나를 공유한 것만으로 같은 사건이 됐다.
      'China's Newnew Shipping to Murmansk' x 'ESL Shipping ties fleet to NIS2'
    세 표기를 합쳐 세면 shipping 은 df 2 → 6 으로 올라 희소 기준(max_df 3)에서 빠진다.
    한글 토큰의 df 는 그대로다 — 기사당 한 집합으로 세므로 중복 가산이 없다
    (실측: '한화오션' df 3, '코친조선소' df 2 — 합치기 전후 동일).
    """
    joined = " ".join(v for v in (article.get("title_ko"), article.get("title_en"),
                                  article.get("title")) if v)
    return dup_tokens(joined)


def rare_cutoff(corpus_size: int, ratio: float = 0.06, floor: int = 3) -> int:
    """'희소 토큰' 기준 문서빈도. 코퍼스 크기에 비례해야 한다.

    절대값(예: 6)으로 두면 수집 풀이 커질 때 기준이 과도하게 엄격해져 판정이 무력화된다.
    (실측: 100건 기준으로 맞춘 6을 수백 건 풀에 그대로 쓰자 중복 15쌍이 통과했다.)"""
    return max(floor, round(corpus_size * ratio))


def summary_tokens(summary: str, limit: int = 220) -> set:
    """요약 앞부분의 단어 집합 (같은 보도자료를 받아쓴 기사 판별용)."""
    return dup_tokens((summary or "")[:limit])


def has_real_snippet(article: dict, min_new_tokens: int = 8) -> bool:
    """요약이 '진짜 기사 스니펫'인가, 아니면 제목을 재탕한 것인가.

    Google News RSS 는 요약을 주지 않는다 — description 이 '제목  매체명' 이다
    (실측 2026-08-24: 33~101자, 제목에 없는 토큰이 1~4개뿐).
    그런데 near_identical 은 "요약이 거의 같으면 같은 보도자료를 받아쓴 기사"라는
    전제로 만들어졌다. 요약이 사실상 제목이면 그 전제가 깨져서, 제목만 비슷한
    서로 다른 사건까지 같은 기사로 묶인다.

    실측(2026-08-24) 북극항로 국문 검색어 3개 → 후보 211건:
        near_identical 로 206건 제거, 최종 1건.
        '아라온호 해빙 관측' · '팬스타 아크로 시범운항 출항' · '울산항 4척 유치'
        · '서방, 러시아 협력 우려' 처럼 명백히 다른 사건이 한 건으로 뭉갰다.
    이게 "북극항로 기사가 거의 없다"의 실제 원인이었다.

    제목에 없는 요약 토큰 수로 가른다. 실측 분포가 깨끗하게 갈린다:
        Google News   1~4개   (제목 재탕)
        네이버         16~28개 (진짜 스니펫)
        기타 해외 RSS   12~26개 (진짜 스니펫)
    """
    summary = article.get("summary_ko") or article.get("summary") or ""
    title = article.get("title_ko") or article.get("title") or ""
    return len(summary_tokens(summary) - dup_tokens(title)) >= min_new_tokens


def near_identical(tokens_a: set, tokens_b: set, threshold: float = 0.7) -> bool:
    """두 단어 집합이 거의 같은지. 요약이 사실상 동일한 기사를 잡는다.

    제목 표기가 갈려도(예: '해양진흥공사' vs '해진공') 같은 보도자료를 받아쓴 기사는
    요약이 거의 일치한다. 실측에서 이 두 기사의 요약 공유 토큰이 28개였고 제목은 1개였다."""
    if len(tokens_a) < 8 or len(tokens_b) < 8:   # 너무 짧으면 판정 불가
        return False
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b)) >= threshold


# 같은 사건 판정에서 하나로 볼 계열사. 매체마다 같은 사안에 다른 법인명을 쓴다.
# 실측(2026-08-24): 인도 코친조선소 블록공장 JV 철회를 두고 한 매체는
# 'HD한국조선해양, 印 코친조선소 블록공장 JV 추진 중단', 다른 매체는
# '[단독] HD현대중공업, 인도 코친조선소 합작 블록 사업 계획 철회'로 썼다.
# 주체가 갈리면 is_same_event 가 엄격한 경로(희소 토큰 3개 이상)를 타는데, 두 제목이
# 공유한 토큰은 '코친조선소' 하나뿐이라 중복이 그대로 통과했다.
#
# 주의: 경쟁사 태그(HD현대마린솔루션)는 넣지 않는다 — 같은 그룹이지만 사업이 다르고
# 전용 섹션에서 따로 다룬다. 현대글로비스도 넣지 않는다(현대차그룹, 별개 회사다).
CORPORATE_GROUPS = {
    "HD현대중공업": "HD현대",
    "HD한국조선해양": "HD현대",
    "HMM오션서비스": "HMM",      # 모회사/선박관리 자회사 — 매체가 섞어 쓴다
}


def entity_name(article: dict) -> str | None:
    """기사에 붙은 회사명 원문 (계열사 묶기 전). 판정에서 회사명 토큰을 빼는 데 쓴다.

    entity_key 는 계열사를 묶은 '주체 키'라 이 용도로 쓸 수 없다. 실측(2026-08-24):
    customer='HD현대중공업' 이면 entity_key 는 'HD현대' 를 돌려주므로, 제목에 실제로
    들어 있는 토큰 'hd현대중공업' 이 걸러지지 않았다.
    """
    return (article.get("customer") or article.get("competitor")
            or ("아크메" if article.get("own") else None))


def entity_tokens(*articles) -> set:
    """주어진 기사들의 회사명에서 나온 토큰 집합 (같은 사건 판정에서 제외할 토큰).

    회사명은 판별 토큰이 될 수 없다(2026-08-24). 같은 회사 기사끼리는 회사명 공유가
    당연해서 정보량이 0인데, 한글 5자 이상 + df<=2 조건을 만족해 '희귀 고유명사'
    단독 판정을 통과해 버린다. 실측: 고객사 섹션에서 서로 무관한 두 기사가
    'hd현대중공업' 하나만 겹쳐 병합됐다.
      'HD현대중공업, 인도 코친조선소 블록공장 합작법인 폐쇄'
        x '현대차 이어 HD현대중공업도…산업계 성과급 파업 확산'
    같은 사건 여부는 회사명이 아니라 사건을 특정하는 단어로 판정해야 한다.
    (코친조선소 JV 기사쌍은 '코친조선소'·'블록공장' 을 공유하므로 그대로 걸린다.)
    """
    out = set()
    for art in articles:
        name = entity_name(art) if isinstance(art, dict) else art
        if name:
            out |= dup_tokens(name)
    return out


def entity_key(article: dict) -> str | None:
    """같은 사건 판정용 주체 키 (고객사·경쟁사 태그 또는 자사 언급).

    own 을 빼먹으면 아크메 언급 기사끼리 '다른 주체'로 취급돼 중복이 통과한다.
    계열사는 CORPORATE_GROUPS 로 묶어 같은 주체로 본다.
    수집 단계(1_fetch_news)와 렌더 단계(3_build_html)가 같은 기준을 쓰도록 여기에 둔다.
    """
    name = entity_name(article)
    return CORPORATE_GROUPS.get(name, name)


# _rare_proper_noun 에서 제외할 단어 — 사건을 특정하지 못하는 일반어·주제어.
#
# 왜 df 만으로는 부족한가(2026-08-24): 희소 기준은 '이 주에 몇 건 나왔나'만 보므로
# '고유명사라서 드문 단어'와 '일반어인데 마침 2건에만 나온 단어'를 구별하지 못한다.
# 실측 54건에서 6자 이상 영문 토큰 159개가 df<=2 였고 그중 company·global·market·
# project·services·management 같은 일반어가 다수였다. 이 자격은 '한 단어만 겹쳐도
# 같은 사건'이라는 강한 판정이라, 무관한 두 기사가 'shipping' 하나로 묶인 사고가 났다.
#
# 주제어(arctic·autonomous 등)도 같이 넣는다 — 같은 주제의 서로 다른 사건을 다룬
# 기사가 흔하고(북극항로 기사 여러 건이 이번 주에 처음 들어왔다), 주제어 하나로는
# 사건이 특정되지 않는다. 목록에 없어도 희소 토큰 3개 경로는 그대로 살아 있다.
_GENERIC_TOKENS = frozenset({
    # 일반 업계·경영 용어
    "company", "companies", "corporation", "business", "industry", "industrial",
    "industries", "global", "market", "markets", "project", "projects",
    "system", "systems", "service", "services", "solution", "solutions",
    "management", "contract", "contracts", "growth", "capital", "investment",
    "operation", "operations", "development", "production", "technology",
    "engineering", "manufacturing", "cooperation", "agreement", "partnership",
    "national", "international", "commercial", "strategy", "platform",
    "research", "conference", "exhibition", "announced", "announces",
    "launch", "launched", "launches", "prepares", "expands", "expansion",
    "competitiveness", "innovation", "transformation", "framework",
    # 해사 일반어 — 업계 기사면 거의 다 들어간다
    "shipping", "maritime", "vessel", "vessels", "shipyard", "shipyards",
    "shipbuilding", "shipowner", "shipowners", "offshore", "container",
    "containers", "tanker", "tankers", "carrier", "carriers", "propulsion",
    "voyage", "fleets", "portside", "harbour",
    # 주제어 — 같은 주제의 다른 사건이 흔하다
    "arctic", "autonomous", "autonomy", "northern", "digital", "digitalization",
    "cybersecurity", "cyberattacks", "decarbonisation", "decarbonization",
    "methanol", "ammonia", "hydrogen", "regulatory", "compliance",
    "electronic", "certification", "verification", "optimization",
    "optimisation", "connectivity", "sustainability",
})


def _rare_proper_noun(token: str, freq: dict) -> bool:
    """'이 토큰 하나만 겹쳐도 같은 사건'이라고 볼 만큼 특정적인 고유명사인가.

    글자 수 기준을 한글과 영문에 다르게 둔다(2026-08-24).
      한글 5자: '코친조선소'처럼 한 글자가 형태소라 5자면 이미 충분히 특정적이다.
        기준이 6자였을 때, 같은 사건을 다룬 두 기사
        ('HD한국조선해양, 印 코친조선소 블록공장 JV 추진 중단' /
         '[단독] HD현대중공업, 인도 코친조선소 합작 블록 사업 계획 철회')가
        공유 토큰 '코친조선소' 하나뿐이라 한 글자 차이로 중복을 놓쳤다.
      영문 6자: 5자 영단어는 일상어가 많다. 실제로 5자로 낮춰보니 'fleet' 하나로
        'Transpetro Prepares New Fleet'과 'ESL Shipping ties fleet connectivity'가
        같은 사건으로 묶였다. 영문은 기존 6자를 유지한다.

    df<=2 조건이 함께 걸려 있어 '삼성중공업'처럼 흔한 5자 회사명은 걸리지 않는다
    (그런 이름은 한 주에 2건을 훨씬 넘게 등장한다).
    """
    if token in _GENERIC_TOKENS:      # 일반어·주제어는 df 가 낮아도 사건을 특정 못한다
        return False
    if freq.get(token, 0) > 2:
        return False
    return len(token) >= (5 if _HANGUL.search(token) else 6)


def is_same_event(tokens_a: set, tokens_b: set, freq: dict,
                  same_entity: bool, max_df: int = 6,
                  ignore: frozenset = frozenset()) -> bool:
    """두 기사가 같은 사건을 다룬 것인지 판정.

    same_entity=True (같은 고객사/경쟁사 태그): 희소 토큰 2개 공유로 충분.
    same_entity=False: 오탐이 잦으므로 3개 이상 + 그중 4자 이상 특징어가 있어야 한다
        ('글로벌·경쟁력·강화'처럼 짧고 일반적인 단어만 겹치는 경우를 배제).

    ignore: 판정에서 뺄 토큰 (회사명 — entity_tokens 주석 참고)."""
    # 같은 주체 기사가 같은 규모 숫자를 공유하면 그것만으로 같은 사건으로 본다.
    # 희소(df) 기준을 적용하지 않는 게 핵심이다(2026-09-01) — 큰 사건일수록 여러 매체가
    # 같은 금액을 쓰므로 df 가 올라가는데, 그 높은 df 는 '흔한 단어'라는 뜻이 아니라
    # '중복이 많다'는 뜻이다. df 로 걸러내면 정작 잡아야 할 대형 중복부터 빠져나간다.
    # 실측: 'HD한국조선해양, LPG운반선 4척 5154억 수주' 와 영문판 'HD Korea Shipbuilding
    # Wins 515.4 Billion Won Order for 4 LPG Carriers' 는 공유 토큰이 '#5154' 하나뿐이라
    # (국문/영문이라 단어가 안 겹친다) df 기준을 통과 못 해 중복이 그대로 실렸다.
    shared_numbers = [t for t in (tokens_a & tokens_b)
                      if t.startswith("#") and t not in ignore]
    if same_entity and shared_numbers:
        return True
    shared = [t for t in (tokens_a & tokens_b)
              if freq.get(t, 0) <= max_df and t not in ignore]
    # 아주 희귀한 긴 고유명사를 공유하면 그것만으로 같은 주체·같은 사안으로 본다.
    # (실측: '제이제이앤컴퍼니스' 기사 2건이 제목상 공유 토큰이 적어 통과했다.
    #  한 주에 2건 이하로만 등장하는 고유명사는 신호가 충분히 강하다.)
    if any(_rare_proper_noun(t, freq) for t in shared):
        return True
    if same_entity:
        # 같은 주체 기사끼리는 판별력 있는 4자 이상 희귀어 하나만 겹쳐도 같은 사안으로 본다.
        # (실측: 해진공 중소선사 AI 사업 기사 3건이 '해양진흥공사'/'해진공'/기관명 없음으로
        #  표기가 갈려 제목 공유 토큰이 '중소선사' 하나뿐이었다.)
        if any(len(t) >= 4 and freq.get(t, 0) <= 2 for t in shared):
            return True
        return len(shared) >= 2
    return len(shared) >= 3 and any(len(t) >= 4 for t in shared)
