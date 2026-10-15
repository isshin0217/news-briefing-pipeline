"""
5_analyze.py
summarized_news.json → Groq API로 우리 회사(profile.json) 관점 주간 분석 브리핑 생성
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

load_dotenv()

from profile_loader import BRAND, KEYWORDS as KW, company_context, analysis_rules

# 고객사 동향 선정 로직을 웹 빌더와 공유 (파일명이 숫자로 시작해 importlib 사용)
_bw_spec = importlib.util.spec_from_file_location("bw", str(Path(__file__).parent / "3_build_html.py"))
bw = importlib.util.module_from_spec(_bw_spec)
_bw_spec.loader.exec_module(bw)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

MAX_NEWS_TO_LLM = 30     # LLM에 전달할 최대 기사 수
MAX_TECH_NEWS = 5        # 기술/경쟁사 카드 최대 개수

# tech_news 후보에서 제외할 '산업·행사 일반' 키워드 (profile.json keywords.tech_exclude)
TECH_EXCLUDE_KEYWORDS = KW.get("tech_exclude", [])
GROQ_RETRIES = 3         # JSON 파싱 실패 시 재시도 횟수

ANALYSIS_PROMPT = """당신은 {team} 전용 AI 브리핑 에이전트입니다.

[매우 중요한 작성 규칙]
- 반드시 순수한 한국어로만 작성하세요. 한자(漢字), 중국어, 일본어 문자는 절대 사용하지 마세요.
- 영문 고유명사(제품명, 회사명, 규정명: ECDIS, IMO, IACS 등)는 그대로 써도 됩니다.
- 뉴스의 제목/링크/출처를 새로 만들지 마세요. 각 뉴스는 아래 [id] 번호로만 지칭하세요.
- 분석(기회/위험/액션/관련성)은 반드시 {company}의 제품·사업·경쟁 구도와 연결해서 구체적으로 쓰세요.
  ("협력 관계를 강화할 수 있습니다" 같은 뻔한 문장 금지)

[{company} 회사 정보]
{context}

[이번 주 뉴스 목록]
{news_list}

위 뉴스를 분석하여, 아래 JSON 형식으로만 응답하세요. 마크다운/설명 없이 JSON만 출력하세요.
_ko 필드는 한국어로, _en 필드는 자연스러운 영어(English)로 작성하세요. 같은 내용의 번역이어야 합니다.

{{
  "week_summary": "이번 주 업계 핵심 흐름을 2-3문장으로 요약",
  "one_line_conclusion_ko": "{company} 관점에서 이번 주 가장 중요한 한 줄 결론 (한국어)",
  "one_line_conclusion_en": "Same key takeaway in English",
  "tech_news": [
    {{
      "id": 3,
      "opportunity_ko": "{company}에게 기회가 되는 이유 (제품·사업과 연결)",
      "opportunity_en": "Why this is an opportunity for {company_en} (in English)",
      "risk_ko": "주의할 위험 또는 경쟁사 움직임",
      "risk_en": "Risk or competitor movement to watch (in English)",
      "action_ko": "기술팀/사업팀이 바로 할 수 있는 후속 액션",
      "action_en": "Concrete follow-up action for the tech/biz team (in English)"
    }}
  ],
  "customer_news": [
    {{ "id": 1 }}
  ]
}}

[선정 규칙]
- tech_news: [고객사] 태그가 없는 기사 중, {company} 제품/사업/경쟁과 관련성이 높은 순으로 최대 {max_tech}건. 관련성이 약하면 개수를 줄이세요(억지로 채우지 말 것).
{rules}
  · 같은 사건을 다룬 기사가 여러 건이면 하나만 대표로 선정하세요(중복 금지).
- customer_news: [고객사] 태그가 있는 기사만 id로 나열. 사옥 이전·인사 같은 단순 사내소식과 주가·목표주가·실적 등 투자자용 소식은 제외하되, 고객사의 매각·인수·합병·신조 발주 등 사업에 중요한 소식은 반드시 포함하세요.
- _en 필드는 절대 비워두지 말고 반드시 영어로 채우세요.
- 뉴스가 부족한 항목은 빈 배열로 두어도 됩니다."""


def build_news_list(articles: list) -> tuple[str, dict]:
    """LLM용 뉴스 목록 텍스트와 id→기사 매핑을 반환한다."""
    # 경쟁사/컴플라이언스 태깅 기사는 전용 섹션에서 다루므로 tech 후보에서 제외
    def is_general(a):
        return not a.get("customer") and not a.get("competitor") and not a.get("compliance")

    # tech_news 후보로는 '일반(비태깅)' 기사만 LLM에 제시한다.
    # 고객사/경쟁사/컴플라이언스 기사는 각 전용 섹션에서 결정론적으로 선정하므로 제외.
    # (약한 모델이 눈에 띄는 고객사 기사를 tech로 잘못 고르는 문제 원천 차단)
    def is_tech_noise(a):
        t = ((a.get("title_ko") or a.get("title", "")) + " " +
             (a.get("summary_ko") or a.get("summary", ""))).lower()
        return any(k in t for k in TECH_EXCLUDE_KEYWORDS)

    general = [a for a in articles if is_general(a) and a.get("category", "기타") != "기타" and not is_tech_noise(a)]
    others = [a for a in articles if is_general(a) and a.get("category", "기타") == "기타" and not is_tech_noise(a)]
    filtered = (general + others)[:MAX_NEWS_TO_LLM]

    id_map = {}
    lines = []
    for idx, art in enumerate(filtered):
        id_map[idx] = art
        customer_tag = f"[고객사: {art['customer']}] " if art.get("customer") else ""
        title = art.get("title_ko") or art.get("title", "")
        summary = (art.get("summary_ko") or art.get("summary", ""))[:150]
        lines.append(f"[id={idx}] {customer_tag}{title}")
        lines.append(f"    출처: {art.get('source', '')} | 카테고리: {art.get('category', '기타')}")
        lines.append(f"    내용: {summary}")
        lines.append("")
    return "\n".join(lines), id_map


def call_groq(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},  # JSON 강제
    }
    resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=90)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def extract_json(raw: str) -> dict:
    """응답에서 JSON 객체만 견고하게 추출한다."""
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 첫 '{' ~ 마지막 '}' 사이만 다시 시도
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start:end + 1])
        raise


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


def merge_metadata(analysis: dict, id_map: dict) -> dict:
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
            "published": art.get("published", ""),
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
            "opportunity_ko": item.get("opportunity_ko") or item.get("opportunity", ""),
            "opportunity_en": item.get("opportunity_en", ""),
            "risk_ko": item.get("risk_ko") or item.get("risk", ""),
            "risk_en": item.get("risk_en", ""),
            "action_ko": item.get("action_ko") or item.get("action", ""),
            "action_en": item.get("action_en", ""),
        })
    analysis["tech_news"] = tech_out[:MAX_TECH_NEWS]

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

    if not GROQ_API_KEY:
        print(".env 파일에 GROQ_API_KEY가 없습니다.")
        return False

    articles = json.loads(src.read_text(encoding="utf-8"))
    print(f"\n[5단계] Groq 주간 분석 시작 - 총 {len(articles)}건 뉴스 분석 중...\n")

    news_list, id_map = build_news_list(articles)
    prompt = ANALYSIS_PROMPT.format(
        context=company_context(),
        rules=analysis_rules(),
        team=BRAND["name_ko"],
        company=BRAND["short_ko"],
        company_en=BRAND["name_en"],
        news_list=news_list,
        max_tech=MAX_TECH_NEWS,
    )

    analysis = None
    for attempt in range(1, GROQ_RETRIES + 1):
        try:
            raw = call_groq(prompt)
            analysis = extract_json(raw)
            break
        except json.JSONDecodeError as e:
            print(f"  JSON 파싱 실패 (시도 {attempt}/{GROQ_RETRIES}): {e}")
            time.sleep(2)
        except requests.RequestException as e:
            print(f"  Groq API 오류 (시도 {attempt}/{GROQ_RETRIES}): {e}")
            time.sleep(3)

    if analysis is None:
        print("분석 실패: Groq 응답을 파싱하지 못했습니다.")
        return False

    # 한자 제거 → 실제 메타데이터 병합
    analysis = clean_analysis_text(analysis)
    analysis = merge_metadata(analysis, id_map)

    # customer_news는 LLM 대신 결정론적으로 선정(HTML/이메일과 동일 로직) → 텔레그램용.
    analysis["customer_news"] = [{
        "title_ko": a.get("title_ko") or a.get("title", ""),
        "title_en": a.get("title_en") or a.get("title", ""),
        "link": a.get("link", "#"),
        "source": a.get("source", ""),
        "customer": a.get("customer"),
    } for a in bw.select_customer_news(articles)]

    today = datetime.now()
    week_start = (today - timedelta(days=7)).strftime("%Y.%m.%d")
    week_end = today.strftime("%Y.%m.%d")
    analysis["period"] = f"{week_start} ~ {week_end}"
    analysis["generated_at"] = today.isoformat()
    analysis["total_news"] = len(articles)

    out = Path("weekly_analysis.json")
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"분석 완료 → {out}")
    print(f"\n한 줄 결론: {analysis.get('one_line_conclusion_ko') or analysis.get('one_line_conclusion', '')}")
    print(f"  기술/경쟁사 뉴스: {len(analysis.get('tech_news', []))}건")
    print(f"  고객사 뉴스: {len(analysis.get('customer_news', []))}건")
    return True


if __name__ == "__main__":
    run()
