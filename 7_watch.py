"""
7_watch.py
발송 전 자가진단 — 뉴스레터가 나가기 전에 '이번 주 결과물'을 스스로 검사한다.

왜 필요한가:
  이 파이프라인의 문제는 매주 사람이 결과물을 읽고 찾아냈다. 소스 주석이 사실상
  그 피드백의 변경 이력이다. 그런데 주석은 다시 실행되지 않아서, 나중에 키워드를
  손보다가 예전에 고친 문제를 되살려도 아무도 모른다.
  실측(2026-08-31): event_dedup 이 Groq 429 로 통째로 실패했는데 로그에 한 줄만 남고
  파이프라인은 정상 종료했다. 같은 LPG 수주 기사가 3벌 실린 채 발송됐다.

3층 구조:
  1. 회귀 테스트  — watch_cases.json 의 과거 피드백을 매번 다시 통과시킨다
  2. 규칙 점검    — 결과물에서 '이미 지적받은 유형'을 기계적으로 찾는다
  3. LLM 검수     — 상위 카드를 모델에게 읽히고 근거를 대게 한다 (--llm)

LLM 지적은 버리지 않고 쌓는다(2026-09-01):
  지금까지 3층의 결과는 리포트에 '참고' 한 줄로 남고 끝이었다 — 배너에도 안 붙고
  다음 주면 사라졌다. 8/31 사고와 같은 구조다(감지는 되는데 전달이 안 된다).
  그래서 지적을 watch_candidates.json 에 적립해 회차마다 누적한다. 같은 기사가
  두 번 지적되면 '주의'로 올라와 사람 눈에 닿는다.

  다만 케이스 파일에 바로 넣지는 않는다. 케이스는 매주 도는 회귀 테스트라 오탐이
  하나 섞이면 그 오답에 키워드를 맞추게 된다 — 첫 판에서 LLM 이 40건 중 25건을
  지적했던 걸 생각하면 자동 승격은 위험하다. 승격은 사람이 --promote 로 한다.

설계 원칙:
  · 이 스크립트는 판정 규칙을 고치지 않는다. 찾아서 보고만 한다.
    키워드 목록은 판단의 근거가 주석으로 남아 있는 문서라, 에이전트가 조용히
    고쳐 쓰면 그 기록이 깨진다. 고칠지 말지는 사람이 정한다.
    자동으로 쓰는 파일은 대기 목록(watch_candidates.json)뿐이고, 그건 아무 효력이
    없다 — 회귀 테스트는 watch_cases.json 만 돌린다.
  · 기본은 경고만 하고 통과시킨다(exit 0). --block 을 줘야 발송을 막는다.

실행:
  python 7_watch.py                 # 규칙 + 회귀 (LLM 없음, 무료)
  python 7_watch.py --llm           # + LLM 검수 (지적을 대기 목록에 적립)
  python 7_watch.py --llm --block   # 심각 문제면 exit 1 (발송 중단용)

대기 목록 관리(검사는 돌지 않는다):
  python 7_watch.py --candidates    # 쌓인 지적 보기
  python 7_watch.py --promote 3     # 3번을 watch_cases.json 케이스로 승격
  python 7_watch.py --reject 3      # 3번을 반려 (다시 쌓이지 않는다)
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import keyword_match as km

load_dotenv()

RAW = Path("raw_news.json")
SUMMARIZED = Path("summarized_news.json")
ANALYSIS = Path("weekly_analysis.json")
CASES = Path("watch_cases.json")
CANDIDATES = Path("watch_candidates.json")
REPORT_JSON = Path("watch_report.json")

# 심각도. ERROR 만 --block 에서 발송을 막는다.
ERROR, WARN, INFO = "심각", "주의", "참고"


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []   # (심각도, 항목, 내용)

    def add(self, level: str, check: str, detail: str):
        self.items.append((level, check, detail))

    def has_error(self) -> bool:
        return any(lv == ERROR for lv, _, _ in self.items)

    def render(self) -> str:
        if not self.items:
            return "  문제 없음"
        order = {ERROR: 0, WARN: 1, INFO: 2}
        lines = []
        for level, check, detail in sorted(self.items, key=lambda x: order[x[0]]):
            lines.append(f"  [{level}] {check}\n        {detail}")
        return "\n".join(lines)


# ── 1층: 회귀 테스트 ────────────────────────────────────────────────────────

def run_regression(report: Report):
    """watch_cases.json 의 과거 피드백을 수집 단계에 다시 통과시킨다.

    "todo": true 인 케이스는 다르게 다룬다. 대기 목록에서 막 승격된 케이스라
    아직 거를 규칙이 없는 게 정상이기 때문이다. 그걸 '심각'으로 올리면 --block 이
    사람의 할 일 목록 때문에 발송을 막게 된다 — 회귀 실패는 '예전에 고친 게 되살아났다'
    는 뜻이어야 하고, 그 신호가 흐려지면 아무도 안 본다.
    """
    if not CASES.exists():
        report.add(WARN, "회귀 테스트", f"{CASES} 가 없어 건너뜁니다")
        return

    # 1_fetch_news 는 숫자로 시작해 import 문으로 못 불러온다
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch_news", "1_fetch_news.py")
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)

    cases = json.loads(CASES.read_text(encoding="utf-8")).get("cases", [])
    now = datetime.now(timezone.utc)
    failed, todo_failed, todo_done = [], [], []
    for case in cases:
        # source 는 선택 필드다(2026-10-06). 해사 전문지 경로(TRADE_PRESS_SOURCES)로
        # 들어와야 통과하는 기사가 있어서, 케이스가 수집 경로를 지정할 수 있게 한다.
        art = fetch.make_article(case["title"], case.get("summary", ""),
                                 "http://example.test", now, case.get("lang", "ko"),
                                 case.get("source", "회귀테스트"))
        actual = "drop" if art is None else "keep"
        # 본문 단계 필터 (2026-09-21). make_article 은 제목·요약만 본다. 증권사 리포트를
        # 풀어쓴 기사는 제목이 산업 기사와 똑같아 본문에서만 드러나므로(피드백), body 가 있는 케이스는 그 단계까지 태운다.
        if actual == "keep" and case.get("body") and fetch.is_analyst_body(case["body"]):
            actual = "drop"
        if actual != case["expect"]:
            (todo_failed if case.get("todo") else failed).append((case, actual))
        elif case.get("todo"):
            todo_done.append(case)

    if not failed and not todo_failed:
        print(f"  회귀 테스트 {len(cases)}건 전부 통과")
    for case, actual in failed:
        report.add(ERROR, f"회귀 실패 ({case.get('date','')})",
                   f"기대={case['expect']} 실제={actual} | {case['title'][:60]}\n"
                   f"        근거: {case.get('why', '')}")
    for case, actual in todo_failed:
        report.add(WARN, f"미해결 지적 ({case.get('date','')})",
                   f"승격된 지적인데 아직 거르지 못합니다 | {case['title'][:60]}\n"
                   f"        근거: {case.get('why', '')}\n"
                   f'        규칙을 손본 뒤 {CASES} 에서 "todo" 를 지우세요')
    for case in todo_done:
        report.add(INFO, "지적 해결됨",
                   f"{case['title'][:60]}\n"
                   f'        이제 통과합니다 — {CASES} 에서 "todo" 를 지우세요')


# ── 2층: 규칙 점검 ──────────────────────────────────────────────────────────

def check_pipeline_health(articles: list, report: Report):
    """단계가 조용히 실패했는지 — 결과물의 흔적으로 되짚는다."""
    if not articles:
        report.add(ERROR, "수집 결과", "raw_news.json 이 비어 있습니다")
        return

    # 사건 요약이 하나도 없으면 event_dedup 이 죽은 것이다 (2026-08-31 실패 유형)
    with_body = [a for a in articles if (a.get("body") or "").strip()]
    with_event = [a for a in articles if a.get("event_summary")]
    if with_body and not with_event:
        report.add(ERROR, "같은 사건 병합",
                   f"본문 있는 기사 {len(with_body)}건 중 사건 요약이 0건입니다. "
                   "event_dedup 이 실패했을 수 있습니다 (같은 사안이 여러 벌 실릴 위험). "
                   "실행 로그에서 GROQ_API_KEY·TPM 한도를 확인하세요.")
    elif with_body and len(with_event) < len(with_body) * 0.5:
        report.add(WARN, "같은 사건 병합",
                   f"본문 {len(with_body)}건 중 {len(with_event)}건만 요약됐습니다 "
                   "(절반 미만 — 중복이 남아 있을 수 있습니다)")

    # 번역 누락 (2026-09-28 실패 유형). Google 무료 번역이 IP 차단(429)으로 전멸했는데
    # 원문이 그대로 들어가 아무 흔적 없이 발송됐다 — 영→한 17건·한→영 33건 전부.
    # 국문 제목이 영어면 같은 사건 판정도 국문 기사와 대조를 못 해 중복이 실린다.
    # 2_summarize 가 Gemini 로 재번역하므로 여기 걸리면 그것까지 실패한 것이다.
    hangul = re.compile(r"[가-힣]")
    en_arts = [a for a in articles if a.get("lang", "en") == "en" and "title_ko" in a]
    ko_arts = [a for a in articles if a.get("lang") == "ko" and "title_en" in a]
    bad_ko = [a for a in en_arts if not hangul.search(a.get("title_ko") or "")]
    bad_en = [a for a in ko_arts if hangul.search(a.get("title_en") or "")]
    for label, bad, total in (("영→한", bad_ko, en_arts), ("한→영", bad_en, ko_arts)):
        if not bad:
            continue
        level = ERROR if len(bad) > max(2, len(total) * 0.3) else WARN
        report.add(level, "번역 누락",
                   f"{label} {len(bad)}/{len(total)}건이 원문 그대로입니다 "
                   f"(예: {(bad[0].get('title') or '')[:50]}). "
                   "실행 로그에서 Google 번역 429·Gemini 번역 실패를 확인하세요.")

    # 본문 확보율
    naver_or_rss =[a for a in articles if "news.google.com" not in (a.get("link") or "")]
    if naver_or_rss:
        rate = len(with_body) / len(naver_or_rss)
        if rate < 0.6:
            report.add(WARN, "본문 수집",
                       f"본문 확보율 {rate:.0%} ({len(with_body)}/{len(naver_or_rss)}) "
                       "— 요약 품질이 떨어질 수 있습니다")


def check_offtopic(articles: list, report: Report):
    """주제 밖 기사 — 제목에 해사 신호가 전혀 없는 기사 (대홍수 유형)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch_news", "1_fetch_news.py")
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)

    for art in articles:
        if art.get("customer") or art.get("own") or art.get("competitor"):
            continue
        title = art.get("title_ko") or art.get("title", "")
        # 번역 제목과 원문 제목을 모두 본다(2026-09-01). 번역이 해사 용어를 일반어로
        # 풀어 쓰면 원문에는 있던 신호가 사라진다. 실측: 'Singapore adds Brazil to green
        # shipping corridor network' → '싱가포르와 브라질의 디지털 운송 통로 협약'
        # ('shipping' 이 '운송'이 되어 해사 어휘가 0개가 됐다).
        if any(fetch.has_marine_title(t) for t in
               (title, art.get("title", ""), art.get("title_en", "")) if t):
            continue
        report.add(WARN, "주제 밖 의심",
                   f"{art.get('relevance_score', 0)}점 | {title[:64]}\n"
                   "        제목에 해사 어휘가 없습니다")


def check_duplicates(articles: list, report: Report):
    """같은 사건이 여러 벌 남아 있는지 (LPG 운반선 4척 유형).

    여기서는 is_same_event 를 그대로 쓰지 않는다(2026-09-01). 그 판정은 '수집 풀 전체'
    (수백 건)의 문서빈도를 전제로 하는데, 최종 목록은 57건뿐이라 흔한 업계어도 df<=3 이
    되어 희귀어로 오인된다. 실제로 그렇게 돌렸더니 '삼성중공업 컨테이너선 수주' 와
    '팬스타 북극항로 취항'이 같은 사건으로 잡혔다 — 검수 도구가 오탐을 내면 아무도
    안 보게 되므로, 여기서는 오탐이 거의 없는 두 신호만 쓴다.
      ㉠ 같은 주체 + 같은 규모 숫자 (같은 주에 5154억 수주가 두 건일 리 없다)
      ㉡ 사건 요약이 사실상 동일 (event_dedup 이 붙여 둔 문장)
    """
    kept = []
    for art in articles:
        title = art.get("title_ko") or art.get("title", "")
        nums = km.number_tokens(title) | km.number_tokens(art.get("title", ""))
        ent = km.entity_key(art)
        event = km.dup_tokens(art.get("event_summary") or "")
        for prev, pnums, pent, pevent in kept:
            same_number = bool(ent and ent == pent and (nums & pnums))
            same_event = bool(
                len(event) >= 4 and len(pevent) >= 4
                and len(event & pevent) / min(len(event), len(pevent)) >= 0.75)
            if not (same_number or same_event):
                continue
            why = "같은 주체·같은 금액" if same_number else "사건 요약이 동일"
            report.add(ERROR, "중복 게재",
                       f"{why}\n"
                       f"        A: {(prev.get('title_ko') or prev.get('title',''))[:62]}\n"
                       f"        B: {title[:62]}")
            break
        else:
            kept.append((art, nums, ent, event))


def check_topic_skew(analysis: dict, articles: list, report: Report):
    """한 주제가 상위 카드를 독식하는지 (북극항로 top2 유형)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("analyze", "5_analyze.py")
    an = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(an)

    tech = analysis.get("tech_news", [])
    arctic = [t for t in tech if an.is_arctic_article(t)]
    if len(tech) >= 2 and len(arctic) > 1:
        report.add(WARN, "주제 쏠림",
                   f"기술 동향 {len(tech)}건 중 북극 기사가 {len(arctic)}건입니다 "
                   "(상한은 1건이어야 합니다)")

    # 수집 단계 쏠림도 본다
    total_arctic = sum(1 for a in articles if an.is_arctic_article(a))
    if articles and total_arctic > len(articles) * 0.2:
        report.add(INFO, "주제 비중",
                   f"전체 {len(articles)}건 중 북극 기사 {total_arctic}건 "
                   f"({total_arctic / len(articles):.0%})")

    # 고객사 쏠림
    counts = Counter(a["customer"] for a in articles if a.get("customer"))
    for name, cnt in counts.items():
        if cnt > max(4, len(articles) * 0.15):
            report.add(INFO, "고객사 비중", f"{name} {cnt}건")


def check_own_coverage(articles: list, analysis: dict, report: Report):
    """자사(아크메) 기사가 수집됐는데 뉴스레터에서 빠지지 않았는지."""
    own = [a for a in articles if a.get("own")]
    if not own:
        report.add(INFO, "자사 기사", "이번 주 아크메 언급 기사가 없습니다")
        return
    shown = {(t.get("title_ko") or t.get("title", "")) for t in analysis.get("tech_news", [])}
    missing = [a for a in own
               if (a.get("title_ko") or a.get("title", "")) not in shown]
    print(f"  자사 기사 {len(own)}건 수집 (기술 동향 노출 {len(own) - len(missing)}건)")
    for art in missing:
        report.add(WARN, "자사 기사 미노출",
                   f"{art.get('relevance_score', 0)}점 | "
                   f"{(art.get('title_ko') or art.get('title',''))[:62]}\n"
                   "        '무조건 싣도록' 요청 대상입니다 (하단 전체 목록에는 남아 있습니다)")


def check_noise_patterns(articles: list, report: Report):
    """이미 지적받은 노이즈 유형이 다시 통과했는지 (제목 기준)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch_news", "1_fetch_news.py")
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)

    checks = [
        ("노사 소식", fetch.is_labor_noise),
        ("조직 신설 발표", fetch.is_org_launch),
        ("비조선 사업부문", fetch.is_nonmarine),
        ("인사 발령", fetch.is_personnel_noise),
        ("행사·교육·홍보", fetch.is_event_noise),
        ("자금 조달·부동산", fetch.is_corp_finance_noise),
        ("블로그", fetch.is_blog),
    ]
    for art in articles:
        if art.get("own"):        # 자사 기사는 면제 대상이다
            continue
        title = art.get("title_ko") or art.get("title", "")
        for label, fn in checks:
            if fn(title):
                report.add(WARN, f"노이즈 통과 ({label})", f"{title[:64]}")
                break


# ── 3층: LLM 검수 ───────────────────────────────────────────────────────────

# 프롬프트 설계 메모(2026-09-01):
#   첫 판에서 40건 중 25건을 지적했다. 원인은 '실체 있는 소식'의 기준을 모델이
#   '기술 상세가 들어 있는가'로 잡은 것이다. 그 기준으로는 '케이조선 석유화학제품
#   운반선 6척 수주'가 홍보로 걸린다 — 정작 고객사 동향 섹션이 실으려는 바로 그 내용인데.
#   그래서 ㉠ 무엇이 정상인지를 먼저 못박고 ㉡ 건수를 제한해 우선순위를 강제한다.
#   검수 도구는 재현율보다 정밀도가 중요하다. 오탐이 쌓이면 사람이 리포트를 안 본다.
LLM_PROMPT = """당신은 아크메 기술개발팀 주간 뉴스레터의 최종 검수자입니다.
아크메는 선박 항법(전자해도·WECDIS·통합브리지), 스마트십 플랫폼, 선박관리 ERP,
아이스네비게이션(빙해 항행 지원)을 만듭니다. 독자는 기술개발팀 실무자입니다.

이 뉴스레터가 정상적으로 싣는 내용입니다. 아래는 문제가 아니니 지적하지 마세요.
  · 고객사·조선소의 수주·계약·인도·건조·실적 소식 (독자가 시장을 보는 기본 정보다)
  · IMO·선급의 규제와 표준 동향, 탈탄소·연료 전환 논의
  · 경쟁사와 해외 업체의 제품 출시·투자·협력
  · 북극항로·빙해 운항 소식 (아크메 사업 영역이다)
  · 사고를 계기로 한 안전기준·규제·기술 기사 (사건 보도 자체와는 다르다)

아래에 해당하는 기사만, 확신하는 것만 지적하세요. 애매하면 적지 마세요.
  "무관"   — 해사 산업 뉴스가 아니다 (정치·연예·타 산업·일반 사회면)
  "중복"   — 목록 안의 다른 기사와 완전히 같은 사건이다 (reason 에 상대 번호를 적을 것)
  "홍보"   — 행사 개최 안내·수상·인사 발령·기업 이미지 홍보라 사안 자체가 없다
  "사내"   — 사업과 무관한 사내 소식 (복지·사옥·공시 목록·주가)

최대 5건까지만 고르세요. 심각한 것부터 적으세요. 없으면 빈 배열을 출력하세요.

형식(JSON만 출력):
{"flags": [{"id": 0, "kind": "무관", "reason": "한 줄 근거"}]}

기사 목록:
"""


FLAG_SCHEMA = {
    "type": "object",
    "properties": {
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["무관", "중복", "홍보", "사내"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "kind", "reason"],
            },
        }
    },
    "required": ["flags"],
}


def run_llm_review(articles: list, report: Report):
    """상위 기사를 모델에 읽히고 문제 기사를 지목하게 한다."""
    import gemini_client as gemini

    if not gemini.available():
        report.add(WARN, "LLM 검수", "GEMINI_API_KEY 가 없어 건너뜁니다")
        return

    # 전체를 넣어도 되지만(컨텍스트 100만 토큰) 상위 40건으로 자른다 —
    # 검수는 '실릴 만한 기사'를 보는 단계라 하위권까지 볼 이유가 없고,
    # 지적 건수를 5건으로 제한했기 때문에 후보가 넓으면 오히려 초점이 흐려진다.
    ranked = sorted(articles, key=lambda a: a.get("relevance_score", 0), reverse=True)[:40]
    lines = []
    for i, art in enumerate(ranked):
        title = art.get("title_ko") or art.get("title", "")
        summary = (art.get("summary_ko") or art.get("summary", ""))[:120]
        lines.append(f"[{i}] {title} | {summary}")

    try:
        data = gemini.ask_json(LLM_PROMPT + "\n".join(lines), FLAG_SCHEMA,
                               max_output_tokens=4096)
    except Exception as exc:
        report.add(WARN, "LLM 검수",
                   f"호출 실패({type(exc).__name__}: {exc}) — 규칙 점검 결과만 씁니다")
        return

    flags = data.get("flags")
    if flags is None:
        report.add(WARN, "LLM 검수", "응답을 해석하지 못했습니다")
        return
    if not flags:
        print("  LLM 검수: 지적 없음")
        return

    flagged = []
    for flag in flags:
        try:
            art = ranked[int(flag["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        title = art.get("title_ko") or art.get("title", "")
        kind, reason = flag.get("kind", "?"), flag.get("reason", "")
        report.add(INFO, f"LLM 지적 ({kind})",
                   f"{art.get('relevance_score', 0)}점 | {title[:60]}\n"
                   f"        {reason}")
        flagged.append({"art": art, "kind": kind, "reason": reason})

    record_candidates(flagged, report)


# ── 지적 적립: LLM 결과를 다음 회차로 넘긴다 ────────────────────────────────

CANDIDATES_README = [
    "LLM 검수(7_watch.py 3층)가 지적한 기사를 쌓아 두는 대기 목록이다. 스크립트가 자동으로 쓴다.",
    "여기 있는 항목은 아무 효력이 없다 — 회귀 테스트는 watch_cases.json 만 돌린다.",
    "사람이 확인하고 승격해야 케이스가 된다.",
    "",
    "  python 7_watch.py --candidates    대기 목록 보기",
    "  python 7_watch.py --promote 3     3번을 watch_cases.json 케이스로 승격",
    "  python 7_watch.py --reject 3      3번을 반려 (다음 회차에 다시 쌓이지 않는다)",
    "",
    "왜 자동 승격하지 않나: LLM 지적은 오탐이 섞인다(첫 판에서 40건 중 25건을 지적했다).",
    "케이스는 매주 도는 회귀 테스트라, 틀린 케이스가 들어가면 키워드를 그 오답에 맞춰",
    "고치게 된다. 되돌리기가 훨씬 비싸다.",
    "",
    "필드: seen(지적 횟수) status(대기|반려|승격) kind/reason(LLM 판정과 근거)",
]

# 제목만으로 판정할 수 있는 종류만 케이스가 될 수 있다. '중복'은 목록 안의 다른 기사와
# 대조해야 알 수 있어서 make_article(기사 한 건)로는 재현되지 않는다 — 리포트에만 남긴다.
RECORDABLE_KINDS = {"무관", "홍보", "사내"}


def norm_title(title: str) -> str:
    """대기 목록·케이스 대조용 제목 정규화 (1_fetch_news.normalize_title 와 같은 규칙)."""
    return re.sub(r"[^0-9a-z가-힣]+", "", (title or "").lower())


def load_candidates(report: Report | None):
    """대기 목록을 읽는다. 파일이 깨져 있으면 덮어쓰지 않고 포기한다."""
    if not CANDIDATES.exists():
        return {"_readme": CANDIDATES_README, "candidates": []}
    try:
        data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{CANDIDATES} 를 읽지 못했습니다({exc}) — 이번 지적은 적립하지 않습니다"
        if report:
            report.add(WARN, "지적 적립", msg)
        else:
            print(f"  [경고] {msg}")
        return None
    data.setdefault("candidates", [])
    return data


def save_candidates(data: dict) -> None:
    data["_readme"] = CANDIDATES_README        # 설명은 항상 최신 문구로 맞춘다
    CANDIDATES.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_candidates(flagged: list, report: Report) -> None:
    """LLM 지적을 대기 목록에 적립한다. 케이스로 만드는 것은 사람이 한다."""
    cases = []
    if CASES.exists():
        cases = json.loads(CASES.read_text(encoding="utf-8")).get("cases", [])
    known = {norm_title(c["title"]): c for c in cases}

    data = load_candidates(report)
    if data is None:
        return
    ledger = data["candidates"]
    index = {norm_title(c["title"]): c for c in ledger}

    today = f"{datetime.now():%Y-%m-%d}"
    added = 0
    for item in flagged:
        art, kind, reason = item["art"], item["kind"], item["reason"]
        title = art.get("title_ko") or art.get("title", "")
        key = norm_title(title)

        prior = known.get(key)
        if prior is not None:
            # 사람이 이미 결론을 낸 기사다. keep 으로 확정한 걸 LLM 이 지적했다면
            # 케이스가 아니라 프롬프트를 손볼 신호다 — 그래서 적립하지 않고 알린다.
            if prior.get("expect") == "keep":
                report.add(WARN, "LLM 오탐 의심",
                           f"{title[:60]}\n"
                           f"        사람이 keep 으로 확정한 케이스를 '{kind}'로 지적했습니다\n"
                           f"        케이스 근거: {prior.get('why', '')}")
            continue

        if art.get("own"):
            report.add(INFO, "LLM 지적 (자사 기사)",
                       f"{title[:60]}\n"
                       "        '무조건 싣도록' 한 대상이라 적립하지 않습니다")
            continue

        if kind not in RECORDABLE_KINDS:
            continue                      # '중복' — 제목 한 건으로는 재현되지 않는다

        entry = index.get(key)
        if entry is None:
            entry = {
                "title": title,
                "summary": (art.get("summary_ko") or art.get("summary") or "")[:200],
                "link": art.get("link", ""),
                "expect": "drop",
                "kind": kind,
                "reason": reason,
                "score": art.get("relevance_score", 0),
                "seen": 1,
                "first_seen": today,
                "last_seen": today,
                "status": "대기",
            }
            ledger.append(entry)
            index[key] = entry
            added += 1
        elif entry.get("status") == "대기":
            if entry.get("last_seen") != today:      # 한 회차에 두 번 세지 않는다
                entry["seen"] = entry.get("seen", 1) + 1
                entry["last_seen"] = today
            entry["kind"], entry["reason"] = kind, reason

    save_candidates(data)

    pending = [c for c in ledger if c.get("status") == "대기"]
    if added:
        print(f"  LLM 지적 {added}건 적립 → {CANDIDATES}")
    if not pending:
        return

    # 두 번 이상 지적된 건은 '주의'로 올린다. 배너에 실려 사람 눈에 닿는다.
    repeat = sorted((c for c in pending if c.get("seen", 1) >= 2),
                    key=lambda c: -c.get("seen", 1))
    for entry in repeat[:3]:
        report.add(WARN, f"반복 지적 ({entry.get('kind', '?')})",
                   f"{entry['seen']}회째 | {entry['title'][:56]}\n"
                   f"        {entry.get('reason', '')}\n"
                   f"        승격하려면: python 7_watch.py --promote "
                   f"{ledger.index(entry) + 1}")
    report.add(INFO, "승인 대기",
               f"{len(pending)}건 (반복 {len(repeat)}건) — python 7_watch.py --candidates")


def show_candidates() -> int:
    data = load_candidates(None)
    if data is None:
        return 1
    ledger = data["candidates"]
    if not ledger:
        print(f"\n대기 목록이 비어 있습니다 ({CANDIDATES})")
        return 0

    print(f"\n{'=' * 62}\nLLM 지적 대기 목록  ({CANDIDATES})\n{'=' * 62}")
    for i, entry in enumerate(ledger, 1):
        mark = {"대기": " ", "반려": "×", "승격": "→"}.get(entry.get("status"), "?")
        print(f"\n{mark} [{i}] {entry.get('kind', '?')} · {entry.get('seen', 1)}회 "
              f"· {entry.get('first_seen', '')}~{entry.get('last_seen', '')} "
              f"· {entry.get('status', '')}")
        print(f"     {entry.get('title', '')[:70]}")
        print(f"     {entry.get('reason', '')}")
    print(f"\n  승격: python 7_watch.py --promote <번호>   "
          f"반려: python 7_watch.py --reject <번호>\n")
    return 0


def render_case(entry: dict) -> str:
    """대기 항목을 watch_cases.json 에 넣을 블록으로 찍는다 (기존 들여쓰기와 동일)."""
    case = {
        "title": entry["title"],
        "summary": entry.get("summary", ""),
        "expect": entry.get("expect", "drop"),
        "why": f"LLM 검수 지적({entry.get('kind', '?')}, {entry.get('seen', 1)}회) "
               f"— {entry.get('reason', '')}",
        "date": entry.get("last_seen", f"{datetime.now():%Y-%m-%d}"),
        # 승격 직후엔 거를 규칙이 아직 없다. 회귀 테스트가 이걸 '심각'이 아니라
        # '미해결 지적'으로 보고하도록 표시해 둔다 — 규칙을 고치면 사람이 지운다.
        "todo": True,
    }
    body = json.dumps(case, ensure_ascii=False, indent=2)
    return "\n".join("    " + line for line in body.splitlines())


def promote(numbers: list[int]) -> int:
    """대기 항목을 watch_cases.json 케이스로 옮긴다.

    파일 전체를 json 으로 다시 찍지 않고 블록만 끼워 넣는다. 케이스 파일은 사람이
    묶음마다 빈 줄을 넣어 정리해 둔 문서라, 통째로 재직렬화하면 그 편집이 지워진다.
    """
    data = load_candidates(None)
    if data is None:
        return 1
    ledger = data["candidates"]

    text = CASES.read_text(encoding="utf-8")
    before = len(json.loads(text).get("cases", []))
    picked = []
    for n in numbers:
        if not 1 <= n <= len(ledger):
            print(f"  [오류] {n}번 항목이 없습니다 (1~{len(ledger)})")
            return 1
        entry = ledger[n - 1]
        if entry.get("status") != "대기":
            print(f"  [건너뜀] {n}번은 이미 '{entry.get('status')}' 상태입니다")
            continue
        picked.append((n, entry))

    if not picked:
        return 0

    for _, entry in picked:
        at = text.rfind("\n  ]")           # cases 배열의 닫는 괄호
        if at == -1:
            print(f"  [오류] {CASES} 구조를 알아보지 못했습니다 (수동으로 옮기세요)")
            return 1
        text = text[:at] + ",\n" + render_case(entry) + text[at:]

    try:                                   # 쓰기 전에 반드시 파싱해 본다
        after = len(json.loads(text).get("cases", []))
    except json.JSONDecodeError as exc:
        print(f"  [오류] 결과가 올바른 JSON 이 아닙니다({exc}) — 저장하지 않았습니다")
        return 1
    if after != before + len(picked):
        print(f"  [오류] 케이스 수가 맞지 않습니다({before}→{after}) — 저장하지 않았습니다")
        return 1

    CASES.write_text(text, encoding="utf-8")
    for _, entry in picked:
        entry["status"] = "승격"
    save_candidates(data)

    for n, entry in picked:
        print(f"  승격 [{n}] {entry['title'][:56]}")
    print(f"\n  {CASES} 에 {len(picked)}건 추가 (케이스 {before}→{after}건)")
    print('  아직 거르는 규칙이 없어 "todo" 로 표시했습니다. 1_fetch_news.py 를 손본 뒤')
    print("  회귀 테스트가 통과하면 그 표시를 지우세요.")
    return 0


def reject(numbers: list[int]) -> int:
    """지적을 반려한다. 같은 기사가 다시 지적돼도 대기 목록에 쌓이지 않는다."""
    data = load_candidates(None)
    if data is None:
        return 1
    ledger = data["candidates"]
    for n in numbers:
        if not 1 <= n <= len(ledger):
            print(f"  [오류] {n}번 항목이 없습니다 (1~{len(ledger)})")
            return 1
        ledger[n - 1]["status"] = "반려"
        print(f"  반려 [{n}] {ledger[n - 1]['title'][:56]}")
    save_candidates(data)
    return 0


def parse_numbers(flag: str) -> list[int]:
    """--promote 3 / --promote 3,5 / --promote 3 5 를 모두 받는다."""
    args = sys.argv[sys.argv.index(flag) + 1:]
    nums = []
    for arg in args:
        if arg.startswith("--"):
            break
        for part in arg.split(","):
            if part.strip().isdigit():
                nums.append(int(part))
    return nums


# ── 실행 ────────────────────────────────────────────────────────────────────

BANNER_PATH = "watch_banner.html"


def write_banner(report: "Report", counts: Counter, n_articles: int) -> None:
    """미리보기 메일 맨 위에 붙일 진단 배너를 만든다.

    왜 파일로 남기나(2026-09-01): 지금까지 자가진단 결과는 로그 파일에만 남았다.
    로그는 아무도 안 본다 — 8/31 사고 때도 "결과가 없어 원본을 그대로 씁니다"가
    로그에 찍혀 있었지만 그대로 발송됐다. 감지는 되는데 전달이 안 되는 구조였다.

    운영자 미리보기(07:45)와 팀 발송(09:00) 사이에 75분이 있으니, 그 메일 맨 위에
    붙여 사람 눈에 닿게 한다. 팀 메일에는 붙지 않는다 — 4_send_email 이 미리보기
    발송일 때만 읽는다(NEWSLETTER_DIAGNOSTICS 환경변수).

    파일은 매번 덮어쓴다(날짜를 안 붙인다). 지난주 배너가 남아 이번 주 메일에
    붙는 사고를 막으려면, 항상 '가장 최근 실행 결과' 하나만 있어야 한다.
    """
    if counts[ERROR]:
        bg, edge, head = "#FDECEA", "#C0392B", f"심각 {counts[ERROR]}건 — 확인이 필요합니다"
    elif counts[WARN]:
        bg, edge, head = "#FEF6E7", "#B7791F", f"주의 {counts[WARN]}건"
    else:
        bg, edge, head = "#EAF7EF", "#1A7F4B", "이상 없음"

    rows = []
    for level, check, detail in report.items:
        if level == INFO:          # 참고는 배너에 넣지 않는다 (로그·리포트에는 남는다)
            continue
        first = detail.strip().split("\n")[0].strip()
        rows.append(
            f'<tr><td style="padding:3px 8px 3px 0;white-space:nowrap;'
            f'vertical-align:top;font-weight:bold">[{level}]</td>'
            f'<td style="padding:3px 0;vertical-align:top">{check}<br>'
            f'<span style="color:#5D6B7C">{first}</span></td></tr>')

    body = ("".join(rows) or
            '<tr><td style="padding:3px 0">규칙 점검·회귀 테스트를 모두 통과했습니다.</td></tr>')

    html = (
        f'<div style="font-family:Malgun Gothic,sans-serif;font-size:13px;'
        f'background:{bg};border-left:4px solid {edge};padding:12px 16px;'
        f'margin:0 0 16px 0">'
        f'<div style="font-weight:bold;color:{edge};margin-bottom:6px">'
        f'발송 전 자가진단 — {head}</div>'
        f'<table style="border-collapse:collapse">{body}</table>'
        f'<div style="color:#7A8797;font-size:11px;margin-top:8px">'
        f'기사 {n_articles}건 검사 · {datetime.now():%m-%d %H:%M} · '
        f'이 안내는 미리보기에만 표시되며 팀 발송본에는 포함되지 않습니다</div></div>')

    Path(BANNER_PATH).write_text(html, encoding="utf-8")
    print(f"  미리보기 배너 저장 → {BANNER_PATH}")


def load(path: Path, label: str):
    if not path.exists():
        print(f"  [경고] {path} 가 없습니다 ({label} 점검을 건너뜁니다)")
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    # 대기 목록 관리는 검사와 섞지 않는다 — 사람이 손으로 부르는 명령이고,
    # 여기서 진단을 돌리면 watch_banner.html 을 엉뚱한 시점에 덮어쓴다.
    if "--candidates" in sys.argv:
        return show_candidates()
    for flag, fn in (("--promote", promote), ("--reject", reject)):
        if flag in sys.argv:
            nums = parse_numbers(flag)
            if not nums:
                print(f"  사용법: python 7_watch.py {flag} <번호> "
                      "(python 7_watch.py --candidates 로 번호 확인)")
                return 1
            return fn(nums)

    use_llm = "--llm" in sys.argv
    blocking = "--block" in sys.argv

    print(f"\n{'=' * 62}")
    print(f"[7단계] 발송 전 자가진단  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'=' * 62}\n")

    report = Report()

    print("· 회귀 테스트 (과거 피드백 재검증)")
    run_regression(report)

    # 번역까지 끝난 목록이 있으면 그쪽을 본다 (제목·요약이 한국어라 판정이 정확하다)
    articles = load(SUMMARIZED, "결과물") or load(RAW, "결과물") or []
    analysis = load(ANALYSIS, "카드 선정") or {}

    if articles:
        print(f"· 규칙 점검 ({len(articles)}건)")
        check_pipeline_health(articles, report)
        check_offtopic(articles, report)
        check_duplicates(articles, report)
        check_noise_patterns(articles, report)
        check_own_coverage(articles, analysis, report)
        if analysis:
            check_topic_skew(analysis, articles, report)

    if use_llm and articles:
        print("· LLM 검수")
        run_llm_review(articles, report)

    print(f"\n{'-' * 62}\n결과\n{'-' * 62}")
    print(report.render())

    counts = Counter(lv for lv, _, _ in report.items)
    print(f"\n  심각 {counts[ERROR]}건 / 주의 {counts[WARN]}건 / 참고 {counts[INFO]}건")

    out = Path(f"watch_report_{datetime.now():%Y%m%d}.md")
    out.write_text(
        f"# 발송 전 자가진단 {datetime.now():%Y-%m-%d %H:%M}\n\n"
        f"- 검사 기사: {len(articles)}건\n"
        f"- 심각 {counts[ERROR]} / 주의 {counts[WARN]} / 참고 {counts[INFO]}\n\n"
        + "\n".join(f"- **[{lv}] {chk}** — {dtl.strip()}"
                    for lv, chk, dtl in report.items) + "\n",
        encoding="utf-8")
    print(f"  리포트 저장 → {out}")

    # 기계가 읽을 사본. 8_send_watch_report.py 가 이걸로 운영자 메일을 만든다.
    # md 를 되파싱하는 것보다 낫다. 배너와 같은 이유로 날짜를 안 붙이고 한 벌만 둔다 —
    # 보내는 쪽이 generated 를 보고 '이번 실행 결과인지'를 판단한다.
    REPORT_JSON.write_text(json.dumps({
        "generated": datetime.now().isoformat(timespec="seconds"),
        "articles": len(articles),
        "counts": {ERROR: counts[ERROR], WARN: counts[WARN], INFO: counts[INFO]},
        "items": [{"level": lv, "check": chk, "detail": dtl.strip()}
                  for lv, chk, dtl in report.items],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    write_banner(report, counts, len(articles))

    if blocking and report.has_error():
        print("\n  [중단] '심각' 항목이 있어 발송을 막습니다 (--block)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
