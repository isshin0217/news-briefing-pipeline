"""
5_analyze.py
summarized_news.json → Gemini API로 아크메 관점 주간 분석 브리핑 생성
실행: python 5_analyze.py
결과: weekly_analysis.json

설계 메모:
- LLM은 뉴스 본문을 지어내지 않도록 [id]만 참조한다.
- 링크/출처/제목/요약 등 사실 필드는 우리 데이터(summarized_news.json)에서
  id로 병합한다. (LLM 환각 링크 방지)
- 출력 스키마는 3_build_html.py가 기대하는 구조와 정확히 일치한다:
  week_summary, one_line_conclusion, tech_news[], customer_news[],
  tech_radar[], actions{immediate,this_week,roadmap,decision_needed}
"""

import json
import os
import re
import importlib.util
import requests
import time
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

import gemini_client as gemini
import issue_digest as idg
import publish_history as ph

load_dotenv()

# 고객사 동향 선정 로직을 웹 빌더와 공유 (파일명이 숫자로 시작해 importlib 사용)
_bw_spec = importlib.util.spec_from_file_location("bw", str(Path(__file__).parent / "3_build_html.py"))
bw = importlib.util.module_from_spec(_bw_spec)
_bw_spec.loader.exec_module(bw)

# 수집 단계 노이즈 필터도 같이 쓴다 (2026-09-21). 기술 동향의 빈 자리를 채울 때
# (fill_tech_slots) 후보가 '이미 수집된 기사'라 make_article 의 제외 규칙을 안 거친다.
# 지난주 데이터를 다시 빌드하거나, 규칙을 새로 만든 직후 이번 주 데이터를 다시 돌릴 때
# 걸러졌어야 할 기사가 대체 카드로 올라온다 — 실측(2026-09-21): 행사 상한으로 뺀
# 자리에 'IIT Madras students win Njord Challenge'(그날 규칙을 만든 학생 대회 기사)가
# 들어왔다. 3_build_html 이 고객사 카드에서 같은 이유로 수집 규칙을 다시 보는 것과 같다.
_fn_spec = importlib.util.spec_from_file_location(
    "fetch_news", str(Path(__file__).parent / "1_fetch_news.py"))
fetch = importlib.util.module_from_spec(_fn_spec)
_fn_spec.loader.exec_module(fetch)


def is_collect_noise(art: dict) -> bool:
    """수집 단계였다면 제외됐을 기사인가 (자사 기사는 수집 단계와 같이 면제)."""
    if art.get("own"):
        return False
    title = art.get("title_ko") or art.get("title", "")
    raw = art.get("title", "") or title
    return any(f(t) for t in {title, raw} for f in (
        fetch.is_event_noise, fetch.is_vendor_promo, fetch.is_donation_noise,
        fetch.is_labor_noise, fetch.is_stock_title_noise)) \
        or fetch.is_analyst_body(art.get("body") or "")

# 키 확인은 gemini_client.available() 이 한다.
# --- 입력 크기 ------------------------------------------------------------
# 2026-09-01: Groq → Gemini 전환으로 토큰 예산 machinery 를 걷어냈다.
#
# 예전에는 무료 티어 분당 8,000토큰 안에 (프롬프트 + 출력)을 우겨 넣어야 해서,
# 후보를 30건으로 자르고 요약을 110자로 깎고 그마저 못 맞으면 단계적으로 더 줄이는
# INPUT_TIERS 사다리를 뒀다. 실제로는 매주 최하단(12건/60자)까지 내려가 돌던 시기도
# 있었다 — 60자면 스니펫이 첫 문장도 못 채워, 모델이 보는 건 사실상 제목뿐이었다.
#
# Gemini 는 컨텍스트가 100만 토큰이라 그 사다리가 통째로 필요 없다. 후보를 다 넣고
# 요약도 온전히 넣는다. 이건 배관 정리가 아니라 품질 개선이다 — 모델이 고를 수 있는
# 폭이 넓어지고, 잘린 문장 대신 온전한 요약을 읽는다.
MAX_NEWS_TO_LLM = 60     # 사실상 상한 없음 (한 주 수집이 55~60건)
MAX_TECH_NEWS = 4        # 기술/경쟁사 카드 최대 개수 (2열 그리드 → 짝수)
SUMMARY_LIMIT = 400      # 일반 기사 요약 길이 (110 → 400)
OWN_SUMMARY_LIMIT = 600  # 자사 기사는 더 넉넉히 (320 → 600)
MAX_OUT_TOKENS = 8192    # 출력 상한. Gemini 는 미사용분이 과금되지 않는다.

# tech_news 후보에서 제외할 '산업·외교·행사 일반' 클러스터.
# 'AI·자율운항·조선' 단어로 키워드 점수는 높지만 아크메 제품(선박 데이터·항법·선박관리)과
# 직접 관련이 약한 뉴스(한미 조선동맹/마스가, 회장 동정, 엑스포, 인력양성 등). tech 선별에만 적용.
TECH_EXCLUDE_KEYWORDS = [
    "마스가", "masga", "조선동맹", "조선 동맹", "대미 협력", "대미협력",
    "한미 조선", "한미조선", "조선협력", "워싱턴", "the ceo", "회장 동정",
    "엑스포", "박람회", "융합인재", "총장", "파업",
    # 인물 중심 기사. 실측: '정기선, AI로 해양 산업 재편' 기사가 tech 로 뽑히자
    # LLM이 정기선(사람)을 회사처럼 취급해 "정기선이 상용화하면"이라고 썼다.
    "정기선", "오션 트랜스포메이션",
    # 부동산·시설 투자 (기술 동향이 아님). 실측: '판교 R&D센터 2단계 신축 타당성
    # 검토' 기사가 tech 1순위로 올라가, LLM이 없는 내용("자율운항 개발 가속")을 지어냈다.
    "신축", "증축", "타당성 검토", "r&d센터", "연구소 설립", "사옥", "부지",
    # 대학·학술 연구 성과 (아크메 제품군과 무관한 기초연구).
    # 실측: '국립부경대 연구팀, 풍속 정보 융합한 AI 모델로 장기 해양 파고 예측' 기사가
    # tech 로 뽑히자, LLM이 아크메가 파고 예측 모델을 개발 중인 것처럼 지어냈다
    # ("예측 모델이 상용화 지연될 경우 … 데이터 서비스 경쟁력이 약화될 위험").
    "연구팀", "논문", "학술지", "국립대", "대학교 연구", "학회 발표",
    # 기상·해양 예보 (아크메 사업 영역이 아님 — 아래 [사업 영역이 아닌 것] 참조)
    "파고 예측", "파고예측", "기상 예측", "해양 예보", "기상 예보", "해수면 온도",
    # 비해사 기업의 사업 확장·체질전환 기사 (2026-10-06 피드백). 실측: '마음AI, 완전자율운항선박까지 영역 확장…피지컬AI 체질전환
    # 가속'이 TOP2. 본문은 매출·영업손실·사업구조 재편 설명이라 기업 IR·홍보물에 가깝다.
    # 해당 기업은 우리 경쟁사·고객사가 아니고, 전체 뉴스에는 그대로 남는다.
    "체질전환", "체질 전환", "사업 구조 재편", "사업구조 재편", "사업 재편",
    "영역 확장", "영역을 확장", "사업 다각화", "신사업 진출",
]
GROQ_RETRIES = 3         # JSON 파싱 실패 시 재시도 횟수
GROQ_RATE_RETRIES = 4    # 429(분당 토큰 한도) 재시도 횟수 — 마지막 시도는 그대로 올린다

COMPANY_CONTEXT = """
[회사 개요] (예시 설정 — 실제 사용 시 자기 회사 정보로 바꿔 쓰세요)
- 회사명: 아크메 (Acme Marine) — 가상의 해사 소프트웨어 회사
- 사업 한 줄 요약: 선박의 안전 운항과 최적 운용, 각종 규제 대응을 위한 솔루션 제공

[사업 영역 및 제품]

1. 스마트십 & 자율운항 플랫폼
- Smart Ship Platform: 선주 주도 선박 빅데이터 플랫폼
- Autonomous Ship Platform: 자율운항 모드 구성, 테스트/실증선 적용
- FleetHub Edge(DAQ): 선박 IoT 데이터 수집 (항해/화물/영상/엔진/알람/보안)

2. 사이버보안
- IACS UR E26(선박 전체) & UR E27(선내 시스템) 대응

3. FleetHub Service (구독형 선대관리)
- ENC/Digital Publications: 전자해도·항해 출판물 공급
- FleetHub Easy Chart: ENC 및 출판물 관리 (PSC/ISM/TMSA 대응)
- FleetHub Master & ERP: 선박·해운 ERP (승선원/정비/운항/GHG규제/회계/조달)
- FleetHub Monitoring: 실시간 선박 모니터링 (CII/OPEX/환경규제 대응)

4. 항법 장비 (Navigation)
- ECDIS / WECDIS / RADAR / IBS(통합브리지시스템)

5. 빙해 항행 지원(아이스네비게이션)

[사업 영역이 '아닌' 것 — 아크메가 만들지 않는 것]
아래는 아크메의 제품·개발 과제가 아닙니다. "아크메의 OO가 지연되면",
"아크메의 OO 경쟁력이 약화됨"처럼 우리가 하고 있다고 전제하고 쓰면 안 됩니다.
- 기상·해양 예보 모델: 파고·풍속·해류·해수면 온도 예측 모델을 만들지 않음
- 선박 설계·건조·엔진/추진기 제조 (조선소·엔진사 영역)
- 위성통신 서비스, 항만 운영 시스템, 화물 운송 주선
- 연료(암모니아·메탄올) 개발, 탄소포집 설비
- 선박 금융·보험·용선 중개
이런 주제의 기사는 '업계 흐름' 또는 '고객사 환경 변화'로만 다루세요.

[모니터링 대상 기업 — 예시 설정]
- 해운사(1그룹, 고객사 동향으로 다룸): HMM, HMM오션서비스, 에이치라인해운, 현대LNG해운,
  ONE(Ocean Network Express), 예시해운1, 예시해운2, 예시해양기술, ExampleLine3, ExampleLine4
- 관련 해운사(2그룹): 팬오션, 대한해운, SK해운, 장금상선, 현대글로비스,
  폴라리스쉬핑, KSS해운, KLCSM, 시도상선
- 조선소: HD현대중공업, HD한국조선해양, 한화오션, 삼성중공업, HJ중공업, 케이조선
- 선급: KR, DNV, ABS, LR

[경쟁사 — 모니터링 대상]
- 스마트십/자율운항/상황인지: 경쟁사1~5, HD현대 아비커스(Avikus), ABS Nautical Systems,
  Kongsberg, MEGURI 2040, NAVTOR, HD현대마린솔루션
- 해운 ERP/선박관리 SW: 경쟁사6, 경쟁사7, SpecTec(AMOS), BASSnet, ShipNet,
  Hanseaticsoft, Helm CONNECT, MESPAS, Veson, MeSoft
- 항법/ENC: 경쟁사8, 경쟁사9

[기술개발팀 핵심 관심 영역 — 우선순위 순]
1순위. 플랫폼팀 업무: 스마트십 플랫폼(선주 빅데이터), 자율운항 플랫폼(MASS),
       FleetHub Edge(DAQ) 등 선박 데이터 수집·플랫폼
2순위. 솔루션팀 업무: 항법(ECDIS/WECDIS/RADAR/IBS), ENC/전자출판물,
       FleetHub 선박관리·해운 ERP(승선원/정비/운항/회계/조달), 실시간 모니터링(CII/OPEX)
3순위. AI 접목 모니터링: AI/머신러닝이 스마트십·선박관리 솔루션·해운 ERP에 적용되는 사례
4순위. 규제 대응(솔루션팀 연계): S-100 ECDIS 전환(2029), CII/FuelEU/EU ETS 탈탄소,
       IMO MASS Code, TMSA/Rightship 등 선박평가·베팅
- 참고: 사이버보안(IACS UR E26/E27)은 기술 동향 우선순위가 낮음.
        규제 마감·대형 이슈가 아니면 기술 동향 카드로 무리하게 넣지 말 것.
"""

ANALYSIS_PROMPT = """당신은 아크메 기술개발팀 전용 AI 브리핑 에이전트입니다.

[매우 중요한 작성 규칙]
- 반드시 순수한 한국어로만 작성하세요. 한자(漢字), 중국어, 일본어 문자는 절대 사용하지 마세요.
- 영문 고유명사(제품명, 회사명, 규정명: ECDIS, HMM, IACS UR E26 등)는 그대로 써도 됩니다.
- 뉴스의 제목/링크/출처를 새로 만들지 마세요. 각 뉴스는 아래 [id] 번호로만 지칭하세요.
- 분석(기회/위험/액션/관련성)은 반드시 아크메의 제품·사업·경쟁 구도와 연결해서 구체적으로 쓰세요.

[분석 작성 규칙 — 반드시 지킬 것]
0. **모든 한국어(_ko) 문장은 개조식 명사형으로 끝내세요.** 브리핑 문서 문체입니다.
   허용: "~임", "~함", "~됨", "~있음", "~없음", "~필요", "~가능", "~우려", "~전망"
   금지: "~다", "~이다", "~한다", "~합니다", "~습니다", "~였다", "~것이다"
   (좋은 예: "데이터 표준화와 AI 플랫폼 확대는 아크메의 스마트십·자율운항 플랫폼
    경쟁력을 강화할 핵심 신호임")
   (나쁜 예: "…핵심 신호다", "…대응할 수 있다", "…로드맵을 검토한다")
   영어(_en) 필드는 평범한 영어 문장으로 쓰세요(이 규칙은 한국어에만 적용).
1. **기사에 실제로 쓰인 내용만 근거로 쓰세요.** 기사에 없는 사실을 추론하거나 덧붙이지 마세요.
   (나쁜 예: '연구소 신축 검토' 기사를 두고 "자율운항 개발에 속도를 내고 있는 만큼"이라고 쓰는 것)
   기사 내용만으로 아크메와 연결이 안 되면, 그 기사는 애초에 선정하지 마세요.
2. **조선소와 해운사는 고객사입니다. 절대 '경쟁사'라고 쓰지 마세요.**
   HD현대중공업·HD한국조선해양·삼성중공업·한화오션·HJ중공업·케이조선 = 고객사(조선소)
   HMM·에이치라인해운·현대LNG해운·ONE 등 = 고객사(해운사)
   경쟁사는 위 [경쟁사 — 모니터링 대상] 목록에 있는 회사뿐입니다(Competitor1, Competitor3,
   아비커스, SpecTec, BASSnet, NAVTOR 등). 고객사 소식에는 경쟁 구도가 아니라
   '아크메가 놓치면 손해인 지점'이나 '고객사 환경이 어떻게 바뀌는지'를 쓰세요.
3. **카드마다 다른 내용을 쓰세요.** 단어만 바꿔 같은 문장을 반복하면 안 됩니다.
   각 카드는 해당 기사에만 해당하는 고유한 사실·수치·회사명을 담아야 합니다.
4. **다른 회사의 자산을 아크메가 쓸 수 있는 것처럼 쓰지 마세요.**
   타사의 기술·설비·연구소·플랫폼·데이터·파트너십은 그 회사 것입니다.
   "직접 활용 가능", "연계해 활용", "우리 테스트 환경에 적용" 같은 표현은 금지입니다.
   (나쁜 예: "HD현대의 디지털 트윈 실증은 아크메 Autonomous Ship Platform의 테스트 환경
    구축에 직접 활용 가능하다" → HD현대 내부 설비이며 아크메가 쓸 수 있다는 근거가 없음)
   타사 소식은 '시장이 어디로 가는지 알려주는 신호'로만 다루세요.
5. **제3자 간 제휴·계약 기사를 아크메의 근거로 삼지 마세요.**
   A사와 B사가 손잡은 소식은 아크메와 직접 관계가 없습니다.
   (나쁜 예: "라쿠텐과 Smart Ship Hub의 통합 사례는 아크메가 보안 모듈을 추가할 근거가 된다"
    → 남들끼리 한 계약이 우리 의사결정의 근거가 될 수 없음)
   이런 기사는 '경쟁·시장 구도가 이렇게 바뀌고 있다'는 관점으로만 쓰거나, 그렇게도 쓸 말이
   없으면 **선정하지 마세요**.
6. **억지로 연결하지 마세요.** 기사를 읽고 아크메가 실제로 할 수 있는 일이 떠오르지 않으면
   그 기사는 tech_news에서 빼세요. 카드 수를 채우는 것보다 억지 해석을 피하는 것이 중요합니다.
7. **사람 이름을 회사처럼 쓰지 마세요.** 정기선·최성안 등은 개인(경영진)입니다.
   (나쁜 예: "정기선이 자체 AI 솔루션을 상용화하면" → 회사는 HD현대입니다)
   개인 발언·동정 중심 기사라면 tech_news에 넣지 마세요.
   정부기관(해양수산부, 한국해양진흥공사 등)도 경쟁사가 아닙니다. 정책·지원사업 주체로 쓰세요.
8. **기한·일정 표현과 업무 지시문을 쓰지 마세요.**
   이 브리핑을 읽는 사람은 담당자가 아니라 업계 흐름을 파악하려는 구성원입니다.
   금지: "이번 주에", "다음 주까지", "2주 내", "8월 초까지", "~를 작성한다", "~에게 전달한다",
        "회의를 잡아", "협약을 체결하고", "보고서를 입수하고"
   (나쁜 예: "다음 주까지 API 스키마 초안을 작성해 HMM·ONE에 제안서로 전달한다")
   대신 무엇을 지켜봐야 하는지, 이 흐름이 아크메에 무엇을 의미하는지 서술하세요.
   (좋은 예: "고객사가 데이터 연동 표준을 외부에 맞추기 시작하면 FleetHub Edge의 수집 항목
    정의가 종속될 수 있어, 표준 논의 참여 여부가 갈림길임")
9. **아래 표현은 금지합니다** (내용이 없는 상투어):
   "기회가 될 수 있습니다", "경쟁력을 강화할 수 있습니다", "경쟁력을 약화시킬 수 있습니다",
   "협력 관계를 강화할 수 있습니다", "더욱 집중해야 합니다", "기술 개발을 가속화해야 합니다",
   "주목할 필요가 있습니다", "대응이 필요합니다"
   대신 무엇을·어디에·어떻게 하는지 구체적으로 쓰세요.
   (좋은 예: "ONE이 ShipDC 연계를 확대하면 FleetHub Edge의 데이터 수집 항목을 ShipDC 표준에
    맞춰 매핑해 둘 필요가 있음")
10. **과장하지 마세요. 기사에 나온 사실의 크기만큼만 쓰세요.**
   기사 한두 건을 근거로 시장 전체의 '급증·폭발'을 단정하면 안 됩니다.
   정책 지원 사업 발표나 세미나 발표는 '수요가 급증했다'는 근거가 되지 못합니다.
   금지: "급증", "폭발적", "급성장", "획기적", "혁신적", "대폭", "압도적", "절실", "시급",
        "필수적", "최대 수혜", "선점 필수", "재편", "판도를 바꿈"
   사실 그대로 담담하게 쓰세요. 확실하지 않으면 "~하는 단계임", "~논의가 이어짐",
   "~가능성 있음"처럼 정도를 낮춰 표현하세요.
   (나쁜 예: "AI 기반 선박 데이터 플랫폼 수요가 급증함")
   (좋은 예: "해진공이 중소선사 AI 도입 진단 사업을 시작함. 데이터 표준화 논의도 이어지는 단계임")
11. **[여기서 잘림] 표시가 있으면 그 뒤를 추측해 채우지 마세요.**
   뉴스 요약이 문장 중간에서 끊긴 것이므로, 끊긴 자리 뒤의 내용은 '알 수 없음'입니다.
   특히 동사가 잘렸을 때 마음대로 단정하면 안 됩니다.
   (나쁜 예: 기사가 "아크메는 선박 관리 디지털전환(DX)·AI전환(AX) …[여기서 잘림]"인데
    "아크메가 DX·AX 서비스를 제공함"이라고 쓰는 것 → 제공한다는 말은 기사에 없음.
    행사에서 발표·소개한 것을 사업 실적으로 바꿔 쓴 것임)
   (좋은 예: "아크메가 선박관리 DX·AX를 주제로 발표에 참여함")
12. **아크메가 하지 않는 일을 하고 있다고 전제하지 마세요.**
   위 [사업 영역이 '아닌' 것] 목록을 반드시 확인하세요. 그 목록에 있는 주제는 우리 제품이
   아니므로, 우리의 개발 일정·상용화·경쟁력이 걸린 것처럼 쓰면 사실이 아닙니다.
   (나쁜 예: 대학 연구팀의 파고 예측 AI 기사를 두고 "예측 모델이 상용화 지연될 경우
    고객사가 외부 파고 예측 솔루션을 선택해 아크메의 데이터 서비스 경쟁력이 약화될 위험"
    → 아크메는 파고 예측 모델을 만들지 않음. 없는 개발 과제를 지어낸 것임)
   기사 주제가 우리 제품군([사업 영역 및 제품])에 없으면 tech_news에서 빼세요.
13. **회사가 무엇을 '했다'고 쓸 때는 기사에 있는 동사를 그대로 쓰세요.**
   발표·소개·시연을 제공·공급·수주·구축·상용화로 바꿔 쓰면 없는 실적을 만드는 것입니다.
   기사에 근거가 없으면 아래 표현을 쓰지 마세요:
   "제공함", "공급함", "수주함", "구축함", "상용화함", "참여 가능함", "확보 기회가 있음"
   (나쁜 예: "아크메는 자율운항 모드 확장 및 실증 선박 확보 기회가 있음"
    → 실증선을 확보할 수 있다는 근거가 기사에 없음)

[아크메 회사 정보]
{context}

[이번 주 뉴스 목록]
{news_list}

위 뉴스를 분석하여, 아래 JSON 형식으로만 응답하세요. 마크다운/설명 없이 JSON만 출력하세요.
_ko 필드는 한국어로, _en 필드는 자연스러운 영어(English)로 작성하세요. 같은 내용의 번역이어야 합니다.

{{
  "tech_news": [
    {{
      "id": 3,
      "insight_ko": "이 소식이 아크메에 어떤 의미인지와 지켜볼 지점을 2문장 이내로. 타사 자산을 우리가 쓸 수 있다는 식으로 쓰지 말고, 시장 신호 또는 우리가 실제로 취할 수 있는 이득만 쓸 것. 기한·일정 표현과 업무 지시문은 금지",
      "insight_en": "What this means for Acme Marine and what to watch, in two sentences or less. Never imply access to another company's assets; no deadlines, no task orders"
    }}
  ],
  "customer_news": [
    {{ "id": 1 }}
  ]
}}

[선정 규칙]
- tech_news: [고객사] 태그가 없는 기사 중 **최대 {max_tech}건**. 카드가 2열이라 2건 또는 {max_tech}건이 됩니다.

  **2건이 정답인 주가 대부분입니다. {max_tech}건은 정말 좋은 기사가 그만큼 있을 때만입니다.**
  빈자리를 남기는 것이 약한 기사를 채우는 것보다 낫습니다. 카드 수는 채워야 할 할당량이 아닙니다.

  넣을지 말지는 이 질문으로 판단하세요 —
  **"이 기사를 읽고 아크메가 무엇을 다르게 해야 하는지 한 문장으로 쓸 수 있는가?"**
  그게 안 되면 넣지 마세요. '주시할 필요가 있음'·'흐름을 파악하는 계기'·'동향을 모니터링'처럼
  두루뭉술하게밖에 못 쓰겠다면, 그건 애초에 카드감이 아니라는 뜻입니다.

  특히 아래는 단어가 겹쳐도 넣지 마세요(홍보물이라 실체가 없습니다):
  · 해외 벤더끼리의 제품 통합·제휴 발표 (A사가 B사 기술을 자사 플랫폼에 넣었다는 보도자료)
  · 스타트업의 지원사업 선정·투자 유치 (팁스·창업지원·시드투자). 제품이 나온 게 아닙니다.
  · 행사 개최·참가 안내, 수상, 기념
  · 해사 기업이 아닌 회사(AI·IT 기업 등)가 조선·해운으로 '영역을 넓힌다'는 기업 소개·IR성 기사
    (국책과제 수주·매출 구조 변화를 홍보하는 기사. 예: 'OO AI, 자율운항선박까지 영역 확장')
  · 채용·공개채용·인력 모집 소식
  이런 기사밖에 없으면 2건만 고르거나, 정말 없으면 그마저도 줄이세요.
  · 우선순위: 플랫폼팀(스마트십·자율운항 플랫폼·선박데이터) > 솔루션팀(항법·ENC·선박관리 ERP·모니터링) > AI가 스마트십/선박관리에 접목되는 사례.
  · [중요·오분류 주의] 아크메 '플랫폼'은 "선박에 탑재되어 항해·엔진·화물·영상 등 운항 데이터를 수집·모니터링·운용"하는 선상 데이터 플랫폼입니다.
    "플랫폼"이라는 단어가 있어도, 조선소의 설계·건조·전주기(PLM/라이프사이클/디지털트윈) 관리나 기업 디지털전환용 플랫폼
    (예: 지멘스 Xcelerator·Teamcenter류 설계·생산 플랫폼)은 사업 영역이 전혀 다르므로 tech_news에서 제외하세요. 단어만 겹칠 뿐 관련 없음.
  · [중요·오분류 주의] '마스가', '조선동맹', '한미 조선 협력', 정기선/회장 동정 등 산업·외교·기업 일반 뉴스는
    'AI·자율운항' 단어가 있어도 아크메 제품(선박 데이터 플랫폼·항법·선박관리)과 직접 관련이 약하면 tech_news에서 제외하세요.
    자율운항 실증·시험, AI가 선박관리/스마트십/항법에 실제 적용된 사례, ENC/ECDIS/선박데이터 등 제품 직결 기사를 우선하세요.
  · 사이버보안 단독 기사는 규제 마감·대형 이슈가 아니면 tech_news에 넣지 마세요(우선순위 낮음).
  · **[아크메 직접 언급] 태그가 붙은 기사는 우리 회사 소식이므로 반드시 tech_news에 포함하세요(최우선).**
    이 기사의 insight에는 **기사 내용에 나온 아크메가 한 일을 그대로 먼저 적으세요.**
    (예: "아크메는 선박관리 디지털전환(DX)·AI전환(AX) 서비스와 기술 동향을 발표함")
    시장 해석은 그 뒤에 덧붙이세요. 우리 소식이 있는데 일반적인 시장 전망만 쓰면 안 됩니다.
    [아크메 직접 언급] 기사가 여러 건이면 **가장 대표적인 1건만** 고르고, 남은 카드는
    다른 주제로 채우세요. 같은 사안을 두 카드로 싣지 마세요.
  · **북극항로·빙해 항행·아이스네비게이션 기사는 발견되면 반드시 tech_news에 한 자리 넣으세요.**
    **순서는 맨 앞일 필요가 없습니다.** 다른 카드와 같은 기준(아크메에 대한 중요도)으로 정하세요.
    전망·인터뷰·통계 기사라면 실제 사건이나 제품 직결 기사 뒤에 두세요.
    아크메는 아이스네비게이션(빙해 항행 지원) 사업을 하고 있어, 북극항로가 열리는 것은
    해도(ENC)·항법·빙해 운항 지원 수요와 직결되는 우리 사업 소식입니다. 독자가 반드시 봐야 할 내용입니다.
    한국 컨테이너선의 첫 북극항로 시범운항(팬스타 아크로호)처럼 항로 개설·실증·상업화 소식이 여기 해당합니다.
    해외 일반 AI 동향 기사(예: 특정국 해운업계의 AI 투자 관망)보다 이쪽이 우선입니다.
    **북극 기사가 여러 건이면 '실제로 일어난 사건'(선박 출항·실증 운항·기항 계약)을 먼저 고르고,
    전망·과제 분석 기사('상용화까지 첩첩산중' 류)는 그 다음입니다.** 독자가 알아야 할 것은 먼저 사건입니다.
  · AI/머신러닝이 스마트십·선박관리 솔루션·해운 ERP에 적용된 기사는 발견되면 반드시 우선 포함하세요.
  · 같은 사건을 다룬 기사가 여러 건이면 하나만 대표로 선정하세요(중복 금지).
- customer_news: [고객사] 태그가 있는 기사만 id로 나열. 사옥 이전·인사 같은 단순 사내소식과 주가·목표주가·실적 등 투자자용 소식은 제외하되, 고객사의 매각·인수·합병·신조 발주 등 사업에 중요한 소식은 반드시 포함하세요.
- _en 필드는 절대 비워두지 말고 반드시 영어로 채우세요.
- 뉴스가 부족한 항목은 빈 배열로 두어도 됩니다."""


# 북극항로·빙해 항행 기사 판별. 아크메 아이스네비게이션 사업 직결이라
# tech_news 후보에서 자사 기사 바로 다음 자리를 준다(2026-08-25).
ARCTIC_MARKERS = ["북극항로", "북극해", "북극 항", "빙해", "내빙", "쇄빙",
                  "arctic", "northern sea route", "ice navigation", "polar code"]

# 북극 기사를 기술 동향에 연달아 실을 수 있는 최대 회차 (2026-09-21 피드백).
#
# 이 값만큼 연속으로 실렸으면 다음 회차는 쉬어 간다. 아래 '반드시 한 자리' 보장이
# 그대로 있으면 매주 1번 카드가 북극이 된다 — 실측으로 6주 연속이었다
# (publish_history.py 상단 주석에 회차별 제목이 있다).
#
# 3 인 이유: 아크메 아이스네비게이션 사업 직결이라 자주 보이는 것 자체는 맞다.
# 2 로 두면 격주가 되어 '쉬는 주'가 너무 잦고, 4 이상이면 피로가 그대로 남는다.
# 3주 싣고 1주 쉬는 셈이다. 쉬는 주에도 기사가 사라지지는 않는다 — 전체 뉴스
# (해운시황/물류)에 그대로 실린다. 빠지는 것은 '기술 동향 카드 한 자리'뿐이다.
ARCTIC_STREAK_LIMIT = 3


def is_arctic_article(art: dict) -> bool:
    text = ((art.get("title_ko") or "") + " " + (art.get("title") or "") + " "
            + (art.get("summary_ko") or "")).lower()
    return any(m in text for m in ARCTIC_MARKERS)


def candidate_articles(articles: list) -> list:
    """tech_news 후보가 될 수 있는 기사를 우선순위 순으로 반환한다."""
    # 경쟁사/컴플라이언스 태깅 기사는 전용 섹션에서 다루므로 tech 후보에서 제외
    def is_general(a):
        return not a.get("customer") and not a.get("competitor") and not a.get("compliance")

    # tech_news 후보로는 '일반(비태깅)' 기사만 LLM에 제시한다.
    # 고객사/경쟁사/컴플라이언스 기사는 각 전용 섹션에서 결정론적으로 선정하므로 제외.
    # (약한 모델이 눈에 띄는 조선사·고객사 기사를 tech로 잘못 고르는 문제 원천 차단)
    def is_tech_noise(a):
        t = ((a.get("title_ko") or a.get("title", "")) + " " +
             (a.get("summary_ko") or a.get("summary", ""))).lower()
        return any(k in t for k in TECH_EXCLUDE_KEYWORDS)

    # 자사(아크메) 기사는 tech 노이즈 필터를 면제한다 — 수집 단계와 같은 이유다
    # (2026-09-01 피드백). 우리 회사 소식은 세미나·설명회·전시 형태로
    # 보도되는 일이 많아, 남의 회사 기준으로 만든 제외 목록에 그대로 걸린다.
    cand = [a for a in articles
            if is_general(a) and (a.get("own") or not is_tech_noise(a))]

    def by_score(xs):
        """점수 내림차순. 동점이면 보도량(coverage)이 많은 쪽이 앞선다.

        동점이 잦은데(점수는 정수라 18점 동점이 흔하다) 그때 순서가 수집 순서라는
        우연으로 갈렸다. 보도량은 이미 '사건의 크기'로 쓰는 값이니(coverage_bonus)
        같은 점수라면 더 많은 매체가 다룬 쪽이 그 사건의 대표판이다.

        실측(2026-09-01 피드백):
          팬스타, 국내 최초 컨테이너선 북극항로 취항   18점 · 14개 매체
          북극항로 시범 운항 아크로호, 북극해 진입 임박  18점 · 85개 매체  ← 이쪽이 대표판
        둘은 같은 사건이지만 사건 문장이 달라('취항했다' / '운항한다') 병합에서 빠졌다.
        """
        return sorted(xs, key=lambda a: (a.get("relevance_score", 0),
                                         a.get("coverage", 1)), reverse=True)

    # 아크메가 직접 언급된 기사를 맨 앞에 둔다 (LLM이 반드시 보고 고르도록)
    own = [a for a in cand if a.get("own")]
    rest = [a for a in cand if not a.get("own")]
    # 북극항로·빙해 항행은 아이스네비게이션 사업 직결이라 자사 기사 다음 자리다
    # (2026-08-25). 실측: 이 정렬이 없을 때 28점짜리 북극 기사가 후보 목록 7번째에
    # 묻혀 LLM 이 9점짜리 해외 AI 동향 기사를 상단 카드로 골랐다.
    # 앞자리를 주는 북극 기사는 '가장 점수 높은 1건'만이다 (2026-09-01 피드백).
    #
    # 예전에는 북극 기사를 전부 앞으로 당겼다. 그러면 후보 목록 1~6번이 죄다 북극이라
    # 모델이 3장짜리 기술 동향 카드 중 2장을 북극으로 채운다. 실측(2026-08-31):
    # tech_news 1위 '팬스타 아크로호 북극해 진입', 2위 'AI로 북극해 유빙 정보 분석'.
    # 우선순위의 목적은 '북극 기사가 반드시 한 자리를 차지하는 것'이지(아래 보장 로직)
    # '섹션을 차지하는 것'이 아니다. 1건만 당기고 나머지는 점수 순 제자리에 둔다.
    arctic_all = by_score([a for a in rest if is_arctic_article(a)])
    arctic, arctic_rest = arctic_all[:1], arctic_all[1:]
    rest = [a for a in rest if not is_arctic_article(a)] + arctic_rest
    general = [a for a in rest if a.get("category", "기타") != "기타"]
    others = [a for a in rest if a.get("category", "기타") == "기타"]
    # 그룹 안에서는 점수 내림차순. 예전에는 수집 순서(최신순)를 그대로 써서
    # 고득점 기사가 목록 중간에 흩어졌다.
    return by_score(own) + by_score(arctic) + by_score(general) + by_score(others)


def build_news_list(cands: list,
                    max_items: int = MAX_NEWS_TO_LLM,
                    summary_limit: int = 110,
                    own_summary_limit: int = 320) -> tuple[str, dict]:
    """후보 기사 목록을 LLM용 텍스트와 id→기사 매핑으로 만든다.

    max_items/summary_limit는 토큰 예산에 맞춰 fit_prompt()가 조절한다.
    """
    filtered = cands[:max_items]

    id_map = {}
    lines = []
    for idx, art in enumerate(filtered):
        id_map[idx] = art
        customer_tag = f"[고객사: {art['customer']}] " if art.get("customer") else ""
        # 아이스네비게이션 사업 직결 표시. 자사 기사 태그와 같은 방식으로,
        # LLM 이 목록에서 이 기사를 놓치지 않게 한다(2026-08-25).
        if is_arctic_article(art):
            customer_tag = "[북극항로·아이스네비게이션 최우선] " + customer_tag
        if art.get("own"):
            customer_tag = "[아크메 직접 언급] " + customer_tag
        title = art.get("title_ko") or art.get("title", "")
        # 요약 길이는 이제 토큰 예산이 아니라 '읽기 좋은 분량'으로 정한다(2026-09-01).
        # 프롬프트 + max_completion_tokens 합이 한도를 넘으면 413 Payload Too Large.
        # 단 아크메 언급 기사는 넉넉히 넘긴다: 110자 컷이 "아크메는 선박 관리 디지털"
        # 에서 잘려 우리가 무엇을 발표했는지(DX·AX) 정보가 LLM에 아예 전달되지 않았다.
        # 자사 기사는 한 주에 2~3건뿐이라 토큰 증가폭도 작다.
        limit = own_summary_limit if art.get("own") else summary_limit
        # 본문(article_body.py)이 있으면 스니펫 대신 쓴다. 스니펫은 160자쯤에서
        # 문장 중간에 끊기지만 본문은 평균 1,900자라, 같은 260자를 잘라도 '끊긴
        # 문장'이 아니라 '온전한 도입부'가 들어간다.
        # 영문 기사의 본문은 영문 그대로다(번역하지 않는다 — article_body.py 주석 참고).
        full = (art.get("body") or art.get("summary_ko") or art.get("summary", ""))
        summary = full[:limit]
        # 원문이 잘려 있으면 명시한다. 뉴스 피드 스니펫은 문장 중간에서 끊기는데,
        # 하필 동사 자리에서 끊기면 모델이 뒷말을 지어낸다. 실측: 자사 기사가
        # "아크메는 선박 관리 디지털전환(DX)·AI전환(AX)..." 에서 끊겼는데
        # 모델이 "서비스를 제공함"이라고 없는 사실을 단정했다(실제로는 발표·소개).
        if len(full) > limit or full.rstrip().endswith(("...", "…")):
            summary = summary.rstrip().rstrip(".…") + " …[여기서 잘림]"
        lines.append(f"[id={idx}] {customer_tag}{title}")
        lines.append(f"    출처: {art.get('source', '')} | 카테고리: {art.get('category', '기타')}")
        lines.append(f"    내용: {summary}")
        lines.append("")
    return "\n".join(lines), id_map


SCREEN_PROMPT = """당신은 아크메 기술개발팀의 뉴스 선별 담당자입니다.

아크메는 선박용 소프트웨어·장비 회사입니다. 주요 사업:
- 스마트십/자율운항 플랫폼: 선박 운항 데이터(항해·엔진·화물·영상) 수집·모니터링, KASS/MASS
- 항법 장비: ECDIS, WECDIS, RADAR, IBS, ENC/전자출판물
- 선박관리·해운 ERP: 승선원·정비·운항·회계·조달, CII/OPEX 등 규제 모니터링
- 선박 사이버보안(IACS UR E26/E27)

아래 뉴스 중 위 사업과 관련성이 높은 것을 최대 {take}건 고르세요.

[고르는 기준]
- 자율운항 실증·시험, AI가 선박관리/스마트십/항법에 실제 적용된 사례, ENC/ECDIS/선박
  데이터 관련 기사를 우선하세요.
- [아크메 직접 언급] 태그가 붙은 기사는 무조건 포함하세요.
- 조선소의 설계·건조·전주기(PLM/디지털트윈) 관리나 기업 디지털전환용 플랫폼은
  사업 영역이 다릅니다. '플랫폼'이라는 단어만 겹칠 뿐이므로 고르지 마세요.
- 산업·외교·행사 일반 뉴스(조선동맹, 회장 동정, 엑스포, 인력양성)와 주가·실적 등
  투자자용 소식은 고르지 마세요.
- 사이버보안 단독 기사는 규제 마감·대형 이슈일 때만 고르세요.
- 관련성 있는 기사가 {take}건보다 적으면 적게 골라도 됩니다.

[뉴스 목록]
{news_list}

관련성 높은 순서대로, 아래 JSON 형식으로만 출력하세요. 설명·마크다운 없이:
{{"picks": [{{"id": 0}}, {{"id": 3}}, {{"id": 7}}]}}

id는 반드시 위 [id=N]의 N 하나만 숫자로 넣으세요. 여러 id를 한 값에 붙여 쓰지 마세요."""

# SCREEN_PROMPT 와 관련 상수는 더 이상 쓰지 않는다(2026-09-01, Gemini 전환).
# 선별 단계 자체가 사라졌다 — build_analysis_prompt 위 주석 참고.
# 프롬프트 문자열은 남겨 둔다: 나중에 후보가 수백 건으로 늘어 다시 필요해질 수 있다.

# --- 한 줄 결론 전용 호출 ---------------------------------------------------
# 결론을 분석 호출에서 분리한 이유(실측 2026-08-24):
# 한 줄 결론이 "IMO가 MASS 코드를 적용하고, Evergreen이 AI 항해 최적화 기술을 시험함"
# 으로 나갔는데, 앞 절의 근거는 tech_news 에 뽑히지도 않은 기고문 한 건이었다.
# 그 기고문의 스니펫은 "IMO는 지난달 1일부터 자율운항선박(MASS) 코드 적용을…" 에서
# 동사째로 잘려 있었고(적용했다/앞두고 있다 구분 불가), 심지어 '지난달' 일이라
# 이번 주 흐름도 아니었다. 후보 30건을 통째로 보여주면 모델은 선정과 무관하게
# 눈에 띄는 문장을 집어 온다.
# → 결론은 '뉴스레터에 실제로 실린 기사'만 담은 별도 호출로 쓴다. 근거를 프롬프트
#    수준에서 막는 게 아니라, 볼 수 있는 기사 자체를 실린 것으로 제한하는 방식이다.
# 부수 효과: 이 호출은 기사가 10여 건뿐이라 요약을 넉넉히 넣을 수 있고, 분석 호출도
# 결론 규칙·스키마가 빠진 만큼 요약 길이를 늘릴 자리를 얻었다.
# 8192 로 잡는다(2026-09-01). 2048 은 첫 시도가 MAX_TOKENS 로 실패했다 —
# gemini-3.6-flash 는 사고 토큰을 먼저 쓰는데 그게 maxOutputTokens 에 함께 계산된다.
# 미사용분은 과금되지 않으므로 넉넉히 잡는 쪽이 맞다.
CONCLUSION_OUT_TOKENS = 8192
CONCLUSION_MAX_ITEMS = 14        # tech + 고객사 + 경쟁사 섹션 합계 상한
CONCLUSION_LIMIT = 800           # 결론 근거 기사의 요약 길이 (사다리 없이 고정)

CONCLUSION_PROMPT = """당신은 아크메 기술개발팀 주간 해사 브리핑의 편집자입니다.

아래는 이번 주 뉴스레터에 **실제로 실린 기사 전부**입니다.
이 목록을 읽고 '이번 주 업계에 무슨 일이 있었는지'를 요약하세요.

[반드시 지킬 것]
1. **아래 목록에 있는 기사만 근거로 쓰세요.** 목록에 없는 사건·규제·회사는 알고 있어도
   쓰지 마세요. 읽는 사람은 이 결론 바로 아래에서 목록의 기사 카드를 보게 됩니다.
   결론에 나온 이야기의 근거 기사가 카드에 없으면 읽는 사람이 확인할 방법이 없습니다.
2. **이번 주에 새로 일어난 일만 쓰세요.** 기사가 배경 설명으로 끌어온 지난 사실은
   이번 주 흐름이 아닙니다. 본문에 "지난달", "지난해", "올해 초부터", "이미 시행",
   "앞두고 있다" 같은 시점 표현이 붙은 사실은 결론에 쓰지 마세요. 사실이어도 안 됩니다.
   (나쁜 예: 기고문이 "IMO는 지난달 1일부터 MASS 코드 적용을 시작했고…"로 운을 뗐다고
    해서 "IMO가 MASS 코드 적용을 시작함"을 이번 주 결론으로 쓰는 것 — 지난달 일입니다)
   기고문·칼럼·세미나 기사에서 '이번 주에 일어난 일'은 그 글이 실렸다는 것, 즉
   **누가 무엇을 주장·제안·논의했는지**입니다. 글쓴이가 배경으로 든 규제·통계가 아닙니다.
   (좋은 예: "부산상의 부회장이 센텀2지구를 해양 실증 국가프로젝트로 격상하자고 제안함")
3. **[여기서 잘림] 표시 뒤를 추측해 채우지 마세요.** 문장이 동사 자리에서 끊겼으면
   그 동작이 일어났는지 알 수 없다는 뜻입니다. 확실한 부분만 쓰거나 다른 기사를 쓰세요.
   (나쁜 예: "…코드 적용을 …[여기서 잘림]" → "IMO가 MASS 코드를 적용함")
4. **누가 무엇을 했는지 넣으세요.** 기관·회사명과 사업·기술 이름이 반드시 들어가야 합니다.
   (나쁜 예: "AI 전환 및 데이터 표준화 움직임이 활발히 진행됨" → 주체가 없음)
5. **아크메와 자사 제품명(FleetHub, ECDIS 등)은 언급하지 마세요.** 우리에게 무슨
   의미인지도 쓰지 마세요. 업계에 무슨 일이 있었는지만 담담하게 쓰세요.
6. **한국어 문장은 개조식 명사형으로 끝내세요.**
   허용: "~함", "~임", "~됨", "~있음", "~전망"  /  금지: "~했다", "~이다", "~한다", "~합니다"
7. **과장하지 마세요.** 금지어: "급증", "폭발적", "획기적", "혁신적", "재편", "분수령",
   "핵심 촉진제", "판도를 바꿈". 기사에 나온 사실의 크기만큼만 쓰세요.
8. 한자(漢字)·중국어·일본어 문자를 쓰지 마세요. 영문 고유명사(HMM, IMO, ECDIS)는 그대로 씁니다.

[이번 주 뉴스레터에 실린 기사]
{news_list}

아래 JSON 형식으로만 응답하세요. 마크다운·설명 없이 JSON만 출력하세요.
{{
  "week_summary": "이번 주 업계에 무슨 일이 있었는지 2-3문장. 전망·평가가 아니라 사실 중심 (한국어, 개조식)",
  "one_line_conclusion_ko": "이번 주 흐름 한 줄. 위 목록에서 가장 중요한 소식 1-2건을 골라 '누가 무엇을 했는지'로 쓸 것. 북극항로·빙해 항행 소식이 목록에 있으면 그것을 먼저 넣을 것(아크메 아이스네비게이션 사업 직결). 이때 '중요성이 부각됨'처럼 뭉뚱그리지 말고 무슨 일이 있었는지 구체적으로 쓸 것 — 선박명·출항지·항로가 있으면 그대로 넣을 것. (좋은 예: '팬스타 아크로호가 부산신항에서 북극항로 첫 시범운항에 나섰고, 한화오션이 내빙 VLGC를 인도함' / 나쁜 예: '북극항로 중요성 부각') (한국어, 개조식 명사형으로 끝낼 것)",
  "one_line_conclusion_en": "Same one-line industry trend in natural English"
}}"""


# 선별(screen) 단계는 제거했다(2026-09-01, Groq → Gemini 전환).
#
# 그 단계는 '후보를 한 번의 분석 호출에 다 넣으면 8,000토큰 한도를 넘는다'는 이유
# 하나로 존재했다. 후보를 청크로 쪼개 가벼운 호출로 먼저 추리고, 청크 사이마다
# 62초씩 쉬었다. 손실도 컸다 — 실측(2026-08-25)에서 '19건 → 6건 선별'하며 북극 기사를
# 전부 떨어뜨려, 나중에 코드로 한 자리를 보장해야 했다(merge_metadata 의 북극 보장 로직).
#
# Gemini 는 컨텍스트가 100만 토큰이라 후보 전부(55~60건)가 한 번에 들어간다.
# 두 단계 선별에서 잃던 기사가 없어지고, 62초 대기도 사라진다.


def build_analysis_prompt(cands: list) -> tuple[str, dict]:
    """분석 호출용 프롬프트와 id_map 을 만든다. 후보를 자르지 않는다."""
    news_list, id_map = build_news_list(cands, MAX_NEWS_TO_LLM,
                                        SUMMARY_LIMIT, OWN_SUMMARY_LIMIT)
    prompt = ANALYSIS_PROMPT.format(
        context=COMPANY_CONTEXT,
        news_list=news_list,
        max_tech=MAX_TECH_NEWS,
    )
    print(f"  분석 입력: 기사 {len(id_map)}건 / 요약 {SUMMARY_LIMIT}자 "
          f"(프롬프트 {len(prompt):,}자)")
    return prompt, id_map


def conclusion_articles(analysis: dict, articles: list) -> list:
    """한 줄 결론의 근거가 될 기사 = 이번 주 뉴스레터에 실제로 실리는 기사.

    tech_news 는 LLM 이 고른 카드, 고객사·경쟁사 동향은 3_build_html 과 같은
    결정론적 선정 로직을 그대로 쓴다. 셋 다 결론 바로 아래에 카드로 나가므로,
    결론에 인용된 소식은 반드시 독자가 카드에서 확인할 수 있다.

    tech_news 는 원본 기사로 맞춰서 넘긴다(2026-09-07). 예전에는 merge_metadata 가
    만든 항목에 body 가 없어서 TOP 카드만 400자 스니펫으로 결론에 들어갔다.
    고객사·경쟁사는 원본이라 본문 800자가 통째로 들어가므로, 정작 1·2위로 뽑은
    기사가 가장 빈약한 근거가 되는 셈이었다. 지금은 article_fields 가 body 를
    담지만, 옛 분석본으로 다시 돌릴 때를 위해 bw.with_source 로 한 번 더 맞춘다.
    """
    tech = bw.with_source(analysis.get("tech_news", []), articles)
    picked, seen = [], set()
    for group in (tech,
                  bw.select_customer_news(articles),
                  bw.select_competitor_news(articles)):
        for art in group:
            link = art.get("link")
            if link and link in seen:
                continue
            if link:
                seen.add(link)
            picked.append(art)
    return picked[:CONCLUSION_MAX_ITEMS]


def write_conclusion(selected: list) -> dict:
    """선정된 기사만 보여주고 week_summary / 한 줄 결론을 받아온다.

    실패는 호출자가 처리한다(대체 문장으로 넘어간다).
    """
    if not selected:
        raise ValueError("결론 근거로 쓸 기사가 없음")

    # 요약을 줄일 이유가 없어졌다(2026-09-01). 예전에는 토큰 예산에 맞춰 요약 길이를
    # 단계적으로 깎았는데(CONCLUSION_TIERS), 결론은 실린 기사 몇 건만 보는 호출이라
    # Gemini 컨텍스트에서는 전문을 넣어도 남는다. 근거가 온전해야 결론이 구체적이다.
    news_list, _ = build_news_list(selected, len(selected),
                                   CONCLUSION_LIMIT, CONCLUSION_LIMIT)
    prompt = CONCLUSION_PROMPT.format(news_list=news_list)
    print(f"  한 줄 결론: 실린 기사 {len(selected)}건 / 요약 {CONCLUSION_LIMIT}자")

    data = clean_analysis_text(ask(prompt, CONCLUSION_SCHEMA,
                                   max_out=CONCLUSION_OUT_TOKENS))
    if not (data.get("one_line_conclusion_ko") or "").strip():
        raise ValueError("한 줄 결론이 비어 있음")
    return data


def fallback_conclusion(selected: list) -> dict:
    """결론 호출이 실패했을 때 쓰는 대체 문장.

    없는 사실을 지어내지 않도록 실린 기사 제목을 그대로 인용하기만 한다.
    문장이 매끄럽지는 않지만, 근거 없는 한 줄이 나가는 것보다 낫다.
    """
    def pick(field, alt):
        out = []
        for art in selected[:2]:
            t = (art.get(field) or art.get(alt, "")).strip()
            t = t.rsplit(" - ", 1)[0] if " - " in t else t   # 구글뉴스 언론사 꼬리 제거
            if len(t) > 60:
                t = t[:60].rstrip() + "…"
            if t:
                out.append(t)
        return out

    ko, en = pick("title_ko", "title"), pick("title_en", "title")
    return {
        "week_summary": " / ".join(ko),
        "one_line_conclusion_ko": ("이번 주 주요 소식: " + " / ".join(ko)) if ko else "",
        "one_line_conclusion_en": ("This week: " + " / ".join(en)) if en else "",
    }


# --- 응답 스키마 ----------------------------------------------------------
# 스키마를 API 에 넘기면 모델이 이 형태를 벗어난 응답을 낼 수 없다. Groq 시절에는
# "JSON만 출력하세요"라고 부탁한 뒤 extract_json() 으로 긁어내고, 실패하면 재시도했다.

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "tech_news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "insight_ko": {"type": "string"},
                    "insight_en": {"type": "string"},
                },
                "required": ["id", "insight_ko", "insight_en"],
            },
        },
        "customer_news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        "week_summary": {"type": "string"},
    },
    "required": ["tech_news", "week_summary"],
}

CONCLUSION_SCHEMA = {
    "type": "object",
    "properties": {
        "week_summary": {"type": "string"},
        "one_line_conclusion_ko": {"type": "string"},
        "one_line_conclusion_en": {"type": "string"},
    },
    "required": ["week_summary", "one_line_conclusion_ko", "one_line_conclusion_en"],
}


def ask(prompt: str, schema: dict, *, max_out: int = MAX_OUT_TOKENS) -> dict:
    """Gemini 에 물어 스키마를 만족하는 dict 를 받는다.

    재시도·백오프는 gemini_client 가 맡는다. 여기서 볼 실패는 503(모델 혼잡)뿐이고,
    그건 일시적이라 재시도로 통과한다 — Groq 의 분당 토큰 한도처럼 예산을 계산해
    가며 피해 다녀야 하는 벽이 아니다.

    extract_json() 도 없앴다. 스키마를 API 에 넘기면 응답이 그 형태를 벗어날 수
    없으므로, 앞뒤에 붙은 말을 걷어내거나 파싱 실패를 재시도할 일이 없다.
    """
    return gemini.ask_json(prompt, schema, model=gemini.SMART_MODEL,
                           max_output_tokens=max_out, temperature=0.3)


def strip_cjk(text: str) -> str:
    """한자/일본어 문자를 제거하고 공백을 정리한다. (한글·영문·숫자·기호는 보존)"""
    if not text:
        return ""
    # CJK 통합 한자 + 확장 + 일본어 가나만 제거 (한글 자모/기호는 건드리지 않음)
    text = re.sub(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def clean_analysis_text(obj):
    """분석 결과 전체를 순회하며 한자를 제거한다."""
    if isinstance(obj, str):
        return strip_cjk(obj)
    if isinstance(obj, list):
        return [clean_analysis_text(x) for x in obj]
    if isinstance(obj, dict):
        return {k: clean_analysis_text(v) for k, v in obj.items()}
    return obj


def merge_metadata(analysis: dict, id_map: dict, pool: list | None = None) -> dict:
    """LLM이 참조한 id를 실제 기사 메타데이터로 병합한다. (환각 링크 방지)"""

    def article_fields(art: dict) -> dict:
        return {
            "title_ko": art.get("title_ko") or art.get("title", ""),
            "title_en": art.get("title_en") or art.get("title", ""),
            "summary_ko": (art.get("summary_ko") or "")[:400],
            "summary_en": (art.get("summary_en") or art.get("summary") or "")[:400],
            "source": art.get("source", ""),
            "link": art.get("link", "#"),
            "category": art.get("category", "기타"),
            "customer": art.get("customer"),
            "own": art.get("own", False),   # 아크메 배지 렌더링용
            "published": art.get("published", ""),
            # 본문(2026-09-07). 없을 때 세 군데가 연달아 400자 스니펫으로 떨어졌다:
            # 한 줄 결론, TOP 카드 불릿, 그리고 두 곳에 쓰이는 요약 근거.
            # 실측: '북극항로·필수선대 키운다… 정부, 공급망 안전판 구축' 기사의 요약이
            # 예산안이 아니라 스니펫에 걸린 IMO MASS Code 배경 문단으로 나왔다.
            # 요약을 만드는 쪽은 이 항목만 받으므로, 근거가 여기 함께 있어야 한다.
            "image": art.get("image", ""),
            "body": art.get("body", ""),
        }

    # tech_news 병합 (분석 필드는 한/영 양쪽 보존)
    tech_out = []
    for item in analysis.get("tech_news", []):
        art = id_map.get(int(item.get("id", -1))) if str(item.get("id", "")).lstrip("-").isdigit() else None
        if not art:
            continue
        # 가드: 고객사/경쟁사/컴플라이언스 태그 기사는 전용 섹션 소관 → tech에서 제외
        # (LLM이 규칙을 어기고 태깅된 기사를 tech로 골라도 코드에서 차단)
        if art.get("customer") or art.get("competitor") or art.get("compliance"):
            continue
        tech_out.append({
            "rank": len(tech_out) + 1,
            **article_fields(art),
            # 기회/위험/시사점 3분할을 '의의 및 시사점' 한 항목으로 통합.
            # 구형 응답(opportunity/action)도 받아 넘긴다 — 모델이 옛 키로 답해도 카드가 비지 않도록.
            "insight_ko": (item.get("insight_ko") or item.get("insight")
                           or item.get("opportunity_ko") or item.get("action_ko", "")),
            "insight_en": (item.get("insight_en")
                           or item.get("opportunity_en") or item.get("action_en", "")),
        })
    # 북극항로 기사는 반드시 한 자리를 준다 (2026-08-25).
    #
    # 프롬프트에 [최우선]으로 명시하고 후보 목록 1~3위에 올려도 모델이 계속 뺐다.
    # 앞쪽에 있는 '산업·외교·정책 일반 뉴스는 제외' 규칙에 '[정책리뷰] 한국 컨테이너선
    # 부산 첫 출항, 북극항로 시험대' 같은 제목이 걸리는 것으로 보인다.
    # 아이스네비게이션은 아크메 사업이고 독자가 반드시 봐야 할 내용이므로,
    # 모델 판단에 맡기지 않고 코드에서 보장한다.
    #
    # 찾는 범위는 id_map(LLM 에 넘어간 목록)이 아니라 pool(전체 기사)이다.
    # 후보가 많으면 screen_candidates 가 먼저 추려내는데, 실측(2026-08-25)에서
    # '19건 → 6건 선별'하며 북극 기사를 전부 떨어뜨렸다. id_map 만 보면 못 찾는다.
    # 북극 기사는 최대 1건 (2026-09-01 피드백). 후보 정렬로 앞자리를 1건만 줘도
    # 모델이 목록 뒤쪽에서 북극 기사를 더 골라올 수 있어, 결과에서 한 번 더 막는다.
    # 아래 '반드시 한 자리' 보장과 짝이 되어 기술 동향의 북극 기사 수는 정확히 1건이 된다.
    seen_arctic = False
    capped = []
    arctic_insight = None
    for item in tech_out:
        if is_arctic_article(item):
            # 모델이 쓴 시사점은 챙겨 둔다. 아래에서 대표판으로 갈아끼울 때 재사용한다.
            arctic_insight = arctic_insight or (item.get("insight_ko"), item.get("insight_en"))
            if seen_arctic:
                print(f"  [북극 상한] 기술 동향에서 제외: {item.get('title_ko', '')[:44]}")
                continue
            seen_arctic = True
        capped.append(item)
    tech_out = capped

    # 북극 자리는 '가장 많은 매체가 다룬 판'으로 못박는다 (2026-09-01 피드백).
    #
    # 모델은 본문이 긴 기사를 고른다 — 시사점을 쓸 근거가 많기 때문이다. 그런데 같은
    # 사건이라도 본문이 긴 쪽이 대표판이라는 보장은 없다. 실측:
    #   팬스타, 국내 최초 컨테이너선 북극항로 취항      18점 · 14개 매체 · 본문 2,500자
    #   북극항로 시범 운항 아크로호, 북극해 진입 임박   18점 · 85개 매체 · 본문 없음
    # 85개 매체가 실은 쪽이 그 주 독자가 실제로 본 헤드라인인데, 본문이 없다는 이유로
    # 모델은 매번 앞의 것을 골랐다(사건 문장이 달라 병합에서도 빠졌다).
    #
    # 어차피 북극은 '반드시 한 자리'를 코드가 보장하는 주제다. 그 자리를 누가 차지할지도
    # 모델 판단이 아니라 보도량으로 정한다. 모델이 써 둔 시사점은 같은 사건이므로 그대로 쓴다.
    arctic_pool = [a for a in (pool if pool is not None else id_map.values())
                   if is_arctic_article(a)
                   and not (a.get("customer") or a.get("competitor")
                            or a.get("compliance"))]

    def fill_tech_slots(items: list, skip=None) -> list:
        """기술 동향의 빈 자리를 다음 후보로 채운다 (MAX_TECH_NEWS 까지).

        후보 목록(candidate_articles)을 그대로 쓴다. 점수순 정렬만 직접 하면 tech 노이즈
        필터(TECH_EXCLUDE_KEYWORDS)를 건너뛰게 된다 — 실측(2026-09-21): 점수만으로
        고르면 '[…울산 산업 전환의 시간' 좌담회]'가 올라왔다.

        skip(기사)->bool 로 이번에 뺀 주제를 되돌아오지 않게 막는다.
        """
        shown = {t.get("link") for t in items}
        spare = [a for a in candidate_articles(pool if pool is not None
                                               else list(id_map.values()))
                 if a.get("link") not in shown and not (skip and skip(a))
                 # 수집 규칙을 다시 본다 — is_collect_noise 주석 참고
                 and not is_collect_noise(a)]
        # 여기서는 점수순으로 다시 세운다 (2026-09-21).
        #
        # candidate_articles 의 순서는 'LLM 에게 무엇을 먼저 보여줄까'를 정한 것이라
        # 카테고리가 '기타'인 기사를 맨 뒤로 보낸다(분류가 약한 기사가 모이는 칸이라서).
        # 그런데 대체 카드는 사람이 아니라 코드가 고르는 자리이고, 여기서는 그 규칙이
        # 손해다 — 실측(2026-09-21): 행사 상한으로 자리가 비었을 때 19점짜리
        # '선박 사이버공격 대응 법제화'(기타)가 10점대 기사들에 밀려 안 뽑혔다.
        # 그날은 사람이 손으로 바꿔 넣었다. 점수를 그대로 쓰면 코드가 같은 선택을 한다.
        #
        # LLM 에게 보여주는 순서(candidate_articles)는 건드리지 않는다 — 그건 모델의
        # 선택에 영향을 주는 별개의 축이다.
        spare.sort(key=lambda a: (a.get("relevance_score", 0), a.get("coverage", 1)),
                   reverse=True)
        for art in spare[:max(0, MAX_TECH_NEWS - len(items))]:
            items.append({
                "rank": len(items) + 1,
                **article_fields(art),
                # 모델이 쓴 시사점이 없는 자리다. 북극 보장 경로와 같은 방식으로
                # 사건 요약(없으면 기사 요약)을 넣는다.
                "insight_ko": (art.get("event_summary")
                               or (art.get("summary_ko") or art.get("summary", ""))[:160]),
                "insight_en": (art.get("summary_en") or "")[:200],
            })
            print(f"      · 대체 게재: {(art.get('title_ko') or art.get('title', ''))[:44]}")
        return items

    # 북극 쉬어가기 (2026-09-21 피드백). 연속 게재가 상한에 닿으면 이번 회차는 보장을
    # 건너뛰고, 모델이 스스로 고른 북극 기사도 뺀다 — ARCTIC_STREAK_LIMIT 주석 참고.
    #
    # 뺀 자리는 다음 후보로 채운다. 이 회차의 기술 동향이 2장뿐이었던 적이 있어
    # (2026-09-21 실측), 그냥 빼기만 하면 섹션이 1장으로 줄어든다.
    streak = ph.arctic_streak()
    if streak >= ARCTIC_STREAK_LIMIT and arctic_pool:
        kept = [t for t in tech_out if not is_arctic_article(t)]
        print(f"  [북극 쉬어가기] 최근 {streak}회 연속 게재 → 이번 회차 기술 동향에서 제외"
              f" (전체 뉴스에는 그대로 실립니다)")
        for t in tech_out:
            if is_arctic_article(t):
                print(f"      · 제외: {(t.get('title_ko') or t.get('title', ''))[:44]}")
        tech_out = kept
        arctic_pool = []          # 아래 '반드시 한 자리' 보장도 건너뛴다

        tech_out = fill_tech_slots(tech_out, skip=is_arctic_article)

    if arctic_pool:
        # 이 자리에서는 보도량이 '먼저'다. 점수를 앞세우면 1점 차이로 대표판이 밀린다 —
        # 실측: 19점·9개 매체짜리 업계 총론 기사가 18점·85개 매체인 실제 사건 기사를 이겼다.
        # 어느 판을 실을지는 '그 주에 업계가 무엇을 뉴스로 봤는가'로 정하는 게 맞다.
        top = max(arctic_pool, key=lambda a: (a.get("coverage", 1),
                                              a.get("relevance_score", 0)))
        chosen = top.get("title_ko") or top.get("title", "")
        current = next((t.get("title_ko") or t.get("title", "")
                        for t in tech_out if is_arctic_article(t)), None)
        if current != chosen:
            if current:
                print(f"  [북극 대표판] {current[:34]} → {chosen[:34]} "
                      f"(보도 {top.get('coverage', 1)}개 매체)")
            # 대표판은 원래 북극 카드가 있던 자리에 넣는다. 북극 카드가 없었으면 맨 뒤에
            # 붙이되, 상한에 잘리지 않게 마지막 칸 안쪽에 둔다(아래 '맨 앞 고정 해제' 참고).
            pos = next((i for i, t in enumerate(tech_out) if is_arctic_article(t)), None)
            tech_out = [t for t in tech_out if not is_arctic_article(t)]
            if pos is None:
                pos = min(len(tech_out), MAX_TECH_NEWS - 1)
            summary = (arctic_insight[0] if arctic_insight else None) or (
                top.get("event_summary")
                or (top.get("summary_ko") or top.get("summary", ""))[:160])
            tech_out.insert(pos, {
                "rank": 0,
                **article_fields(top),
                "insight_ko": summary,
                "insight_en": ((arctic_insight[1] if arctic_insight else None)
                               or top.get("summary_en", "")[:200]),
            })
            if not current:
                print(f"  [보장] 북극항로 기사를 기술 동향에 넣음: {top.get('title', '')[:44]}")

    # 북극 기사 '맨 앞 고정'은 해제했다 (2026-10-06 피드백). 2026-09-01 에 "1번 항목으로" 요청을 받아 여기서 북극 카드를 0번으로
    # 정렬했는데, 보장과 겹쳐 최근 8회 중 7회 TOP1 이 북극이었다.
    # 한 자리 보장(위)은 그대로 두고, 순서는 모델이 정한 중요도를 따른다.

    # 행사·전시회 쏠림 상한 (2026-09-21 피드백).
    #
    # 고객사·경쟁사·컴플라이언스 카드는 3_build_html 이 결정론적으로 고르므로 여기서
    # 미리 세어 둘 수 있다. 그 쪽 상한은 select_customer_news 안에 있고(업체별 1건이라
    # 다음 기사가 자동으로 자리를 받는다), 여기서는 남은 몫만큼만 기술 동향에 허용한다.
    #
    # 실측(2026-09-21): 고객사 카드에 가스텍 기사가 2건(케이조선·한화오션) 있는 상태에서
    # 기술 동향 TOP3 가 'HD현대, 가스텍에서 미래 조선 기술 공개' 였다. 그 회차는 손으로
    # 뺐고, 이 상한이 같은 판단을 자동으로 한다.
    show_counts = None
    try:
        others = bw.section_lists({}, pool if pool is not None else list(id_map.values()))
        show_counts = {}
        for item in others["customer"] + others["competitor"] + others["compliance"]:
            name = bw.show_name(item.get("title_ko") or item.get("title", ""))
            if name:
                show_counts[name] = show_counts.get(name, 0) + 1
    except Exception as e:
        # 집계가 실패해도 뉴스레터는 나간다 — 상한은 품질 장치이지 필수 단계가 아니다.
        print(f"  [주의] 행사 쏠림 집계 실패 ({type(e).__name__}: {e}) — 상한을 건너뜁니다")

    if show_counts is not None:
        kept = []
        for item in tech_out:
            name = bw.show_name(item.get("title_ko") or item.get("title", ""))
            if name and show_counts.get(name, 0) >= bw.MAX_CARDS_PER_SHOW:
                print(f"  [행사 상한] '{name}' 기사가 다른 카드에 이미 "
                      f"{show_counts[name]}건 → 기술 동향에서 제외: "
                      f"{(item.get('title_ko') or item.get('title', ''))[:40]}")
                continue
            if name:
                show_counts[name] = show_counts.get(name, 0) + 1
            kept.append(item)
        if len(kept) < len(tech_out):
            tech_out = fill_tech_slots(
                kept,
                # 뺀 행사 기사가 대체 자리로 되돌아오지 않게 막는다.
                # 쉬어가는 중이면 북극 기사도 함께 막는다.
                skip=lambda a: (
                    (bw.show_name(a.get("title_ko") or a.get("title", "")) or "") in
                    {s for s, n in show_counts.items() if n >= bw.MAX_CARDS_PER_SHOW}
                    or (streak >= ARCTIC_STREAK_LIMIT and is_arctic_article(a))))

    tech_out = tech_out[:MAX_TECH_NEWS]
    # 북극 카드는 TOP1 에 두지 않는다 (2026-10-06 피드백, 같은 날 두 번째:
    # "그리고 또 북극해가 1등이야?"). 오전에 '맨 앞 고정'을 풀었지만(위 주석) 모델이
    # 여전히 북극을 1번으로 골랐다 — 후보 목록에서 북극 기사가 자사 다음 둘째 자리이고
    # '[북극항로·아이스네비게이션 최우선]' 꼬리표가 붙어 있어서다. 한 자리 보장은
    # 유지하고 순서만 코드로 정한다: 북극 카드가 1번이면 마지막 자리로 보낸다.
    if len(tech_out) > 1 and is_arctic_article(tech_out[0]):
        tech_out = tech_out[1:] + tech_out[:1]
        print(f"  [북극 순서] TOP1 → TOP{len(tech_out)}: "
              f"{(tech_out[-1].get('title_ko') or tech_out[-1].get('title', ''))[:40]}")
    analysis["tech_news"] = tech_out
    for i, item in enumerate(analysis["tech_news"], 1):
        item["rank"] = i

    # 이번 회차에 북극 기사가 실렸는지 기록한다. 다음 회차의 '쉬어가기' 판정이 이걸 본다
    # (publish_history.py 참고). 같은 날짜로 다시 돌리면 덮어쓰므로 재실행해도 안전하다.
    ph.record(datetime.now().strftime("%Y-%m-%d"),
              [t.get("title_ko") or t.get("title", "") for t in analysis["tech_news"]],
              any(is_arctic_article(t) for t in analysis["tech_news"]))

    # customer_news 병합
    cust_out = []
    seen = set()
    for item in analysis.get("customer_news", []):
        raw_id = item.get("id", -1)
        if not str(raw_id).lstrip("-").isdigit():
            continue
        art = id_map.get(int(raw_id))
        if not art or int(raw_id) in seen:
            continue
        seen.add(int(raw_id))
        cust_out.append(article_fields(art))
    analysis["customer_news"] = cust_out

    return analysis


def run():
    src = Path("summarized_news.json")
    if not src.exists():
        print("summarized_news.json이 없습니다. 먼저 2_summarize.py를 실행하세요.")
        return False

    if not gemini.available():
        print(".env 파일에 GEMINI_API_KEY가 없습니다.")
        return False

    articles = json.loads(src.read_text(encoding="utf-8"))
    print(f"\n[5단계] Gemini 주간 분석 시작 - 총 {len(articles)}건 뉴스 분석 중...\n")

    analysis = None
    cands = candidate_articles(articles)[:MAX_NEWS_TO_LLM]

    # 후보를 전부 한 번에 넣는다. 선별·예산 맞추기 단계는 사라졌다(2026-09-01).
    prompt, id_map = build_analysis_prompt(cands)

    for attempt in range(1, GROQ_RETRIES + 1):
        try:
            analysis = ask(prompt, ANALYSIS_SCHEMA)
            break
        except Exception as e:
            print(f"  분석 호출 실패 (시도 {attempt}/{GROQ_RETRIES}): "
                  f"{type(e).__name__}: {e}")
            if attempt < GROQ_RETRIES:
                time.sleep(3)

    if analysis is None:
        # 주 모델이 하루 한도(429)에 걸리면 가벼운 모델로 한 번 더 간다. 한도는 모델별로
        # 따로 잡힌다. 실측(2026-10-12 08:29): 재빌드가 이 단계에서 3회 모두 429 로 멈춰
        # 팀 발송 직전에 뉴스레터를 다시 만들 수 없었다. 같은 시각 FAST_MODEL 은 정상이었다.
        # 시사점 문장 품질은 조금 떨어질 수 있지만, 발송이 멈추는 것보다 낫다.
        try:
            print(f"  주 모델 실패 → {gemini.FAST_MODEL} 로 분석합니다")
            analysis = gemini.ask_json(prompt, ANALYSIS_SCHEMA, model=gemini.FAST_MODEL,
                                       max_output_tokens=MAX_OUT_TOKENS, temperature=0.3)
        except Exception as e:
            print(f"  대체 모델도 실패: {type(e).__name__}: {e}")
    if analysis is None:
        print("분석 실패: 모델 응답을 받지 못했습니다.")
        return False

    # 한자 제거 → 실제 메타데이터 병합
    analysis = clean_analysis_text(analysis)
    analysis = merge_metadata(analysis, id_map, pool=articles)

    # customer_news는 LLM 대신 결정론적으로 선정(HTML/이메일과 동일 로직) → 텔레그램용.
    analysis["customer_news"] = [{
        "title_ko": a.get("title_ko") or a.get("title", ""),
        "title_en": a.get("title_en") or a.get("title", ""),
        "link": a.get("link", "#"),
        "source": a.get("source", ""),
        "customer": a.get("customer"),
    } for a in bw.select_customer_news(articles)]

    # 한 줄 결론은 '뉴스레터에 실린 기사'만 보여주는 별도 호출로 쓴다.
    # 분석 호출은 후보 30건을 보므로, 거기서 결론까지 쓰게 하면 카드에 없는 기사를
    # 근거로 삼는다(2026-08-24 실측: 기고문 배경 문장이 한 줄 결론이 됨).
    selected = conclusion_articles(analysis, articles)
    for attempt in range(1, GROQ_RETRIES + 1):
        try:
            analysis.update(write_conclusion(selected))
            break
        except Exception as e:
            print(f"  한 줄 결론 생성 실패 (시도 {attempt}/{GROQ_RETRIES}): "
                  f"{type(e).__name__}: {e}")
            if attempt < GROQ_RETRIES:
                time.sleep(3)
    else:
        print("  → 결론 호출이 모두 실패해 기사 제목을 그대로 인용합니다.")
        analysis.update(fallback_conclusion(selected))

    # 이슈 묶기 + 기사별 개조식 불릿 (2026-09-07 피드백).
    # 실패해도 뉴스레터는 나간다 — 두 빌더가 이슈가 없으면 기존 평면 목록으로 되돌아간다.
    # 그래서 여기서만 예외를 삼키고, 무엇이 실패했는지는 로그에 남긴다.
    try:
        analysis.update(clean_analysis_text(
            idg.build(bw.issue_groups(analysis, articles),
                      detail_groups={bw.TECH_GROUP})))
    except Exception as e:
        print(f"  이슈·불릿 생성 실패 ({type(e).__name__}: {e})")
        print("  → 이번 회차는 기존 목록 형태로 나갑니다.")
        analysis.pop("issues", None)
        analysis.pop("bullets", None)

    today = datetime.now()
    week_start = (today - timedelta(days=7)).strftime("%Y.%m.%d")
    week_end = today.strftime("%Y.%m.%d")
    analysis["period"] = f"{week_start} ~ {week_end}"
    analysis["generated_at"] = today.isoformat()
    analysis["total_news"] = len(articles)

    out = Path("weekly_analysis.json")
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"분석 완료 → {out}")
    concl = analysis.get("one_line_conclusion_ko") or analysis.get("one_line_conclusion", "")
    print(f"\n한 줄 결론: {concl}")
    # 한 줄 결론은 업계 흐름만 담아야 한다(결론 프롬프트 규칙 5). 모델이 규칙을 어기고
    # 자사와 엮으면 눈에 띄도록 알린다. 자동으로 고치지는 않는다(문장이 깨질 수 있음).
    if any(w in concl for w in ("아크메", "FleetHub", "자사")):
        print("  [경고] 한 줄 결론에 자사 언급이 섞였습니다. 업계 흐름만 담기도록 확인하세요.")
    print(f"  기술/경쟁사 뉴스: {len(analysis.get('tech_news', []))}건")
    print(f"  고객사 뉴스: {len(analysis.get('customer_news', []))}건")
    return True


if __name__ == "__main__":
    run()
