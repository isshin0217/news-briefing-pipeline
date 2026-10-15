"""
3_build_html.py
summarized_news.json + weekly_analysis.json → 주간 브리핑 HTML 생성
실행: python 3_build_html.py
결과: weekly_newsletter_YYYYMMDD.html
"""

import json
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import keyword_match as km
import issue_digest as idg

COMPANY_NAME_KO = "아크메 기술개발팀"
COMPANY_NAME_EN = "Acme Marine Technology Development Division"
LOGO_TEXT = "⚓ Acme Marine"

CATEGORY_KO_EN = {
    "AI/자율운항/스마트십": "AI / Autonomous / Smart Ship",
    "신조/조선":         "Newbuilding / Shipyard",
    "탈탄소/친환경":     "Decarbonization",
    "해운시황/물류":     "Shipping Market",
    "기타":              "Others",
}

CATEGORY_ICON = {
    "AI/자율운항/스마트십": "🤖",
    "신조/조선":         "🏗️",
    "탈탄소/친환경":     "🌿",
    "해운시황/물류":     "📦",
    "기타":              "📰",
}

# '규정/컴플라이언스' 칸은 삭제했다(2026-08-24 피드백) — 상단 '🌐 글로벌 컴플라이언스'
# 카드 섹션과 성격이 겹쳐 같은 주제가 두 군데로 갈렸다. 2_summarize.py 의
# KEYWORD_CATEGORY 에서도 함께 뺐다.
CATEGORY_ORDER = ["AI/자율운항/스마트십", "신조/조선", "탈탄소/친환경",
                  "해운시황/물류", "기타"]
# 전체 뉴스 카테고리별 앵커 id (네비 버튼 연결용)
CATEGORY_SLUG = {
    "AI/자율운항/스마트십": "cat-auto",
    "신조/조선":         "cat-ship",
    "탈탄소/친환경":     "cat-green",
    "해운시황/물류":     "cat-market",
    "기타":              "cat-etc",
}

# 카테고리 이름이 바뀌거나 없어져도 기사가 조용히 사라지지 않게 한다.
# select_all_news() 는 CATEGORY_ORDER 만 훑으므로, 목록에 없는 이름을 단 기사는
# 그대로 두면 어느 칸에도 안 나온다. 지난주 summarized_news.json 을 다시 빌드하거나
# 앞으로 카테고리를 또 손볼 때 기사가 누락되는 사고를 막는다.
LEGACY_CATEGORY_MAP = {
    "자율운항/스마트십": "AI/자율운항/스마트십",
    "규정/컴플라이언스": "기타",
}


def normalize_category(name: str) -> str:
    """기사의 카테고리를 현재 목록에 있는 이름으로 맞춘다."""
    name = name or "기타"
    name = LEGACY_CATEGORY_MAP.get(name, name)
    return name if name in CATEGORY_ORDER else "기타"

STATUS_COLOR = {"주목": "#3b82f6", "기회": "#10b981", "위험": "#ef4444", "모니터링": "#f59e0b"}
STATUS_EN = {"주목": "Watch", "기회": "Opportunity", "위험": "Risk", "모니터링": "Monitor"}

# 고객사명 한국어 → 영어 표기
CUSTOMER_EN = {
    "HMM": "HMM",
    "HMM오션서비스": "HMM Ocean Service",
    "에이치라인해운": "H-Line Shipping",
    "현대LNG해운": "Hyundai LNG Shipping",
    "ONE": "ONE",
    "예시해운1": "ExampleShipping1 Shipping",
    "예시해운2": "Example Shipping2",
    "예시해양기술": "Kukje Ocean Technology",
    "ExampleLine3": "ExampleLine3",
    "ExampleLine4": "ExampleLine4",
    "팬오션": "Pan Ocean",
    "대한해운": "Korea Line",
    "SK해운": "SK Shipping",
    "장금상선": "Sinokor",
    "현대글로비스": "Hyundai Glovis",
    "폴라리스쉬핑": "Polaris Shipping",
    "KSS해운": "KSS Line",
    "KLCSM": "KLCSM",
    "시도상선": "Sido Shipping",
    "HD현대중공업": "HD Hyundai Heavy",
    "HD한국조선해양": "HD KSOE",
    "한화오션": "Hanwha Ocean",
    "삼성중공업": "Samsung Heavy",
    "HJ중공업": "HJ Shipbuilding",
    "케이조선": "K Shipbuilding",
}

# 고객사 구분 태그: 해운사 vs 조선소 (고객사 카드에 소배지로 표기)
SHIPYARD_CUSTOMERS = {"HD현대중공업", "HD한국조선해양", "한화오션", "삼성중공업",
                      "HJ중공업", "케이조선"}


def customer_type_badge(name: str) -> tuple[str, str]:
    """고객사명 → (한글 구분 배지, 영문 구분 배지)."""
    if name in SHIPYARD_CUSTOMERS:
        return ("조선소", "Shipyard")
    return ("해운사", "Carrier")

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body { font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', Arial, sans-serif;
       background: #f0f4f8; color: #1a202c; }

/* 주제 바로가기 네비게이션 (스크롤 시 상단 고정) */
.topic-nav { position: sticky; top: 0; z-index: 30;
             display: flex; flex-wrap: wrap; gap: 6px;
             padding: 10px 40px; background: #0a2342;
             border-bottom: 1px solid #1a4a7a; }
.topic-nav a { font-size: 12px; font-weight: 600; text-decoration: none;
               color: rgba(255,255,255,0.78); white-space: nowrap;
               padding: 5px 12px; border-radius: 14px;
               background: rgba(255,255,255,0.10); transition: background .15s; }
.topic-nav a:hover { background: rgba(255,255,255,0.28); color: #fff; }
.topic-nav .nav-sep { width: 1px; align-self: stretch; margin: 2px 4px;
                      background: rgba(255,255,255,0.25); }
.section { scroll-margin-top: 54px; }  /* 고정 네비에 가리지 않도록 */

.wrapper { max-width: 1080px; margin: 24px auto; background: #fff;
           border-radius: 10px;
           box-shadow: 0 4px 24px rgba(10,35,66,0.10); }

.header { background: linear-gradient(135deg, #0a2342 0%, #1a4a7a 100%);
          color: #fff; padding: 32px 40px 24px; border-radius: 10px 10px 0 0; }
.header .logo { font-size: 22px; font-weight: 700; letter-spacing: 1px; }
.header h1 { font-size: 15px; font-weight: 400; margin-top: 6px; opacity: 0.8; }
.header .period { font-size: 13px; opacity: 0.6; margin-top: 4px; }
.lang-toggle { display: flex; margin-top: 16px; width: fit-content;
               border-radius: 6px; overflow: hidden;
               border: 1px solid rgba(255,255,255,0.3); }
.lang-btn { padding: 6px 18px; font-size: 13px; font-weight: 600; cursor: pointer;
            border: none; background: transparent; color: rgba(255,255,255,0.6); }
.lang-btn.active { background: rgba(255,255,255,0.2); color: #fff; }

.conclusion { background: #0a2342; color: #fff; padding: 16px 40px;
              font-size: 14px; line-height: 1.6; border-bottom: 3px solid #3b82f6; }
.conclusion .label { font-size: 11px; color: #93c5fd; font-weight: 700;
                      letter-spacing: 1px; margin-bottom: 6px; }

.summary-bar { background: #e8f0fe; padding: 12px 40px;
               font-size: 13px; color: #2d5a9e;
               border-bottom: 1px solid #c6d8f5; }

.section { padding: 24px 40px 0; }
.section-header { font-size: 16px; font-weight: 700; color: #0a2342;
                   border-bottom: 2px solid #3b82f6; padding-bottom: 8px;
                   margin-bottom: 16px; }

/* 기술 뉴스 카드 */
.tech-card { border: 1px solid #e2e8f0; border-radius: 8px;
              padding: 16px; margin-bottom: 16px;
              border-left: 4px solid #3b82f6; }
.tech-card .rank { font-size: 11px; font-weight: 700; color: #3b82f6;
                    letter-spacing: 1px; margin-bottom: 6px; }
.tech-card .cat-badge { display: inline-block; font-size: 11px; padding: 2px 8px;
                         border-radius: 10px; background: #eff6ff; color: #3b82f6;
                         margin-left: 6px; font-weight: 600; }
.tech-card .title { font-size: 14px; font-weight: 700; color: #1e3a5f;
                     line-height: 1.4; margin-bottom: 8px; }
.tech-card .title a { color: inherit; text-decoration: none; }
.tech-card .title a:hover { text-decoration: underline; }
.tech-card .meta { font-size: 11px; color: #718096; margin-bottom: 10px; }
.tech-card .summary { font-size: 13px; color: #4a5568; line-height: 1.65; margin-bottom: 12px; }
.tech-card .analysis-row { display: flex; gap: 8px; margin-bottom: 6px; align-items: flex-start; }
.tech-card .analysis-label { font-size: 11px; font-weight: 700; color: #fff;
                               padding: 2px 8px; border-radius: 3px; white-space: nowrap; flex-shrink: 0; }
.tech-card .analysis-text { font-size: 12px; color: #4a5568; line-height: 1.5; }
.tech-card .a-bullets { margin: 0 0 12px; padding-left: 17px; }
.tech-card .a-bullets li { font-size: 13px; color: #4a5568; line-height: 1.7; }

/* 고객사 카드 - 심플하게 */
.customer-card { border: 1px solid #e2e8f0; border-radius: 8px;
                  padding: 14px 16px; margin-bottom: 12px;
                  border-left: 4px solid #f59e0b; background: #fffbeb; }
.customer-card .customer-name { display: inline-block; font-size: 11px; font-weight: 700;
                                  background: #f59e0b; color: #fff; padding: 2px 8px;
                                  border-radius: 10px; margin-bottom: 6px; }
.customer-card .customer-type { display: inline-block; font-size: 10px; font-weight: 700;
                                  background: #fde68a; color: #92400e; padding: 2px 7px;
                                  border-radius: 10px; margin-bottom: 6px; margin-left: 4px; }
.customer-card .title { font-size: 13px; font-weight: 700; color: #1e3a5f; line-height: 1.4; margin-bottom: 6px; }
.customer-card .title a { color: inherit; text-decoration: none; }
.customer-card .title a:hover { text-decoration: underline; }
.customer-card .meta { font-size: 11px; color: #718096; margin-bottom: 6px; }
.customer-card .summary { font-size: 12px; color: #4a5568; line-height: 1.6; margin-bottom: 8px; }
/* 폴백 카드의 사진 (이슈 생성 실패 시 쓰는 카드 — build_customer_news_html 주석) */
.customer-card .card-thumb { margin: 0 0 10px; aspect-ratio: 16 / 9; }

/* 경쟁사 카드 (보라) / 컴플라이언스 카드 (하늘) */
.customer-card.competitor { border-left-color: #8b5cf6; background: #f5f3ff; }
.customer-card.competitor .customer-name { background: #8b5cf6; }
.customer-card.compliance { border-left-color: #0ea5e9; background: #f0f9ff; }
.customer-card.compliance .customer-name { background: #0ea5e9; }
.customer-card .relevance { font-size: 12px; color: #92400e; background: #fef3c7;
                              padding: 6px 10px; border-radius: 4px; }
.customer-card .relevance.lang-ko::before { content: "💬 관련성: "; font-weight: 700; }
.customer-card .relevance.lang-en::before { content: "💬 Relevance: "; font-weight: 700; }

/* 기술 레이더 */
.radar-grid { display: grid; grid-template-columns: 1fr; gap: 8px; margin-bottom: 16px; }
.radar-item { border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; }
.radar-item .r-status { font-size: 11px; font-weight: 700; padding: 2px 8px;
                          border-radius: 10px; color: #fff; display: inline-block; margin-bottom: 6px; }
.radar-item .r-area { font-size: 13px; font-weight: 700; color: #1e3a5f; margin-bottom: 4px; }
.radar-item .r-desc { font-size: 12px; color: #718096; line-height: 1.4; }

/* 액션 */
.action-group { margin-bottom: 16px; }
.action-group .ag-title { font-size: 12px; font-weight: 700; color: #fff;
                            background: #1a4a7a; display: inline-block;
                            padding: 3px 10px; border-radius: 3px; margin-bottom: 8px; }
.action-item { font-size: 13px; color: #4a5568; line-height: 1.6;
                padding: 4px 0 4px 16px; border-left: 2px solid #e2e8f0; margin-bottom: 4px; }

/* 기술/고객사 카드 2열 그리드 */
.card-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.card-grid-2 .tech-card, .card-grid-2 .customer-card { margin-bottom: 0; }
@media (max-width: 640px) { .card-grid-2 { grid-template-columns: 1fr; } }

/* 전체 뉴스 */
.article-grid { display: grid; grid-template-columns: 1fr; gap: 0 32px; }
@media (min-width: 760px) { .article-grid { grid-template-columns: 1fr 1fr; } }
.article { border-left: 3px solid #3b82f6; padding: 10px 0 10px 14px; margin-bottom: 14px; }
.article .a-title { font-size: 13px; font-weight: 700; color: #1e3a5f; line-height: 1.4; }
.article .a-title a { color: inherit; text-decoration: none; }
.article .a-title a:hover { text-decoration: underline; }
.article .a-meta { font-size: 11px; color: #718096; margin: 3px 0 5px; }
.article .a-summary { font-size: 12px; color: #4a5568; line-height: 1.6; }

/* 이슈 접이식 목록 (2026-09-07) */
.issue { border-bottom: 1px solid #edf2f7; }
.issue:last-child { border-bottom: none; }
.issue-head { display: flex; align-items: baseline; gap: 7px; cursor: pointer;
               padding: 10px 4px; user-select: none; }
.issue-head:hover { background: #f7fafc; }
.issue-caret { color: #a0aec0; font-size: 10px; flex-shrink: 0; width: 11px;
                transition: transform .15s; }
.issue.open .issue-caret { transform: rotate(180deg); color: #3b82f6; }
.issue-subject { font-size: 12px; font-weight: 700; color: #0a2342; flex-shrink: 0; }
.issue-title { font-size: 13px; color: #2d3748; line-height: 1.45; }
.issue.open .issue-title { font-weight: 700; color: #1e3a5f; }
.issue-count { font-size: 10px; color: #718096; background: #edf2f7;
                border-radius: 8px; padding: 1px 7px; flex-shrink: 0; }
.issue-body { display: none; padding: 0 4px 12px 22px; }
.issue.open .issue-body { display: block; }
.a-card { display: block; text-decoration: none; color: inherit;
           border-left: 3px solid #cbd5e0; padding: 8px 0 8px 12px; margin-top: 8px; }
.a-card:hover { border-left-color: #3b82f6; background: #f7fafc; }
.a-card .a-title { font-size: 13px; font-weight: 700; color: #1e3a5f; line-height: 1.4; }
.a-card:hover .a-title { text-decoration: underline; }
.a-card .a-meta { font-size: 11px; color: #718096; margin: 3px 0 5px; }
.a-card .a-summary { font-size: 12px; color: #4a5568; line-height: 1.6; }
.a-bullets { margin: 4px 0 0; padding-left: 16px; }
.a-bullets li { font-size: 12px; color: #4a5568; line-height: 1.65; margin-bottom: 2px; }

/* 전체 뉴스 카테고리 2열 배치 (2026-09-07 피드백) */
.cat-grid { display: grid; grid-template-columns: 1fr; gap: 0 34px;
             padding: 24px 40px 8px; }
@media (min-width: 760px) { .cat-grid { grid-template-columns: 1fr 1fr; } }
.cat-cell { scroll-margin-top: 54px; margin-bottom: 26px; }
/* 칸 안에서는 기사 목록을 1열로 — 2열 안의 2열은 너무 좁다 */
.cat-cell .article-grid { grid-template-columns: 1fr; }

/* 썸네일 카드 그리드 — 경쟁사 동향 (2026-09-07) */
.thumb-grid { display: grid; grid-template-columns: 1fr; gap: 22px 24px; }
@media (min-width: 760px) { .thumb-grid { grid-template-columns: 1fr 1fr; } }
.thumb-card { display: block; text-decoration: none; color: inherit; }
/* .thumb 규격은 카드 종류와 무관하게 하나로 둔다. 예전에는 '.thumb-card .thumb' 로
   썼는데, 고객사 카드(.issue-card)에는 그 선택자가 안 걸려 width:100% 가 빠졌다.
   그래서 사진이 원본 크기로 나와 카드마다 크기가 제각각이었다(2026-09-07 실측:
   삼성중공업 카드만 사진이 작게 떴다). */
.thumb { width: 100%; aspect-ratio: 16 / 10; object-fit: cover;
          border-radius: 8px; display: block; background: #e2e8f0; }
.thumb-none { display: flex; align-items: center; justify-content: center;
               font-size: 30px; color: #a0aec0;
               background: linear-gradient(135deg, #edf2f7, #cbd5e0); }
.thumb-card .thumb-tag { display: inline-block; margin: 11px 0 7px; font-size: 11px;
                          font-weight: 600; color: #4a5568; background: #edf2f7;
                          border-radius: 4px; padding: 3px 9px; }
.thumb-card .thumb-title { font-size: 14px; font-weight: 700; color: #1e3a5f;
                            line-height: 1.45; }
.thumb-card:hover .thumb-title { text-decoration: underline; }
.thumb-card .thumb-meta { font-size: 11px; color: #718096; margin-top: 6px; }

/* 사진 + 한 줄 요약 + 확장 카드 — 고객사 동향 (2026-09-07) */
/* 카드끼리 키를 맞춘다: 제목 길이가 달라도 '자세히 보기' 줄이 같은 높이에 온다 */
.issue-card { border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden;
               background: #fff; display: flex; flex-direction: column; }
.issue-card > a { display: block; }
.issue-card .thumb { border-radius: 0; aspect-ratio: 16 / 9; }
.issue-card .thumb-none { aspect-ratio: 16 / 9; }
.issue-card .issue-expand { margin-top: auto; }
.issue-card .issue-card-head { padding: 12px 14px 0; }
.issue-card .thumb-tag { margin: 0 0 7px; }
.issue-card .issue-title { font-size: 14px; font-weight: 700; color: #1e3a5f;
                            line-height: 1.45; }
.issue-card .issue-expand { display: flex; align-items: center; gap: 5px;
                             padding: 9px 14px 12px; font-size: 11px; font-weight: 600;
                             color: #3b82f6; cursor: pointer; user-select: none; }
.issue-card .issue-expand:hover { text-decoration: underline; }
.issue-card .issue-body { border-top: 1px solid #edf2f7; margin: 0 14px;
                           padding: 0 0 12px; }
.issue-card .a-card { margin-top: 10px; }

hr.div { border: none; border-top: 1px solid #e2e8f0; margin: 20px 40px; }

.footer { background: #f7fafc; border-top: 1px solid #e2e8f0;
          padding: 20px 40px; text-align: center;
          font-size: 11px; color: #a0aec0; line-height: 1.8;
          border-radius: 0 0 10px 10px; }

body.en .lang-ko { display: none; }
/* 2x2 그리드 레이아웃 */
.grid-2x2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0; align-items: stretch; }
.grid-cell { padding: 24px 24px 20px; }
.grid-cell:nth-child(1) { border-right: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; }
.grid-cell:nth-child(2) { border-bottom: 1px solid #e2e8f0; }
.grid-cell:nth-child(3) { border-right: 1px solid #e2e8f0; }
.grid-cell .section-header { font-size: 14px; }

/* 이미지/링크가 가로로 넘치지 않도록 */
img { max-width: 100%; height: auto; }
a { word-break: break-word; overflow-wrap: anywhere; }

/* ── 태블릿 이하: 2x2 → 1열, 가로 여백 축소 ─────────────────── */
@media (max-width: 640px) {
  .grid-2x2 { grid-template-columns: 1fr; }
  .grid-cell { padding: 20px 18px 16px; }
  .grid-cell:nth-child(1), .grid-cell:nth-child(2), .grid-cell:nth-child(3) {
    border-right: none; border-bottom: 1px solid #e2e8f0;
  }
  .header      { padding: 24px 18px 20px; }
  .conclusion  { padding: 14px 18px; }
  .summary-bar { padding: 10px 18px; }
  .topic-nav   { padding: 8px 18px; }
  .section     { padding: 20px 18px 0; }
  .cat-grid    { padding: 20px 18px 0; }
  hr.div       { margin: 16px 18px; }
  .footer      { padding: 18px 18px; }
  .summary-bar span { display: block; line-height: 1.9; }
}

/* ── 모바일: 폰트/헤더 추가 축소 ───────────────────────────── */
@media (max-width: 400px) {
  .header .logo { font-size: 19px; }
  .header h1    { font-size: 13px; }
  .section-header { font-size: 14px; }
  .tech-card, .customer-card { padding: 12px 14px; }
}

body.ko .lang-en { display: none; }
"""

JS = """
function setLang(lang) {
  document.body.className = lang;
  document.querySelectorAll('.lang-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.lang === lang);
  });
  localStorage.setItem('nl-lang', lang);
}
document.addEventListener('DOMContentLoaded', function() {
  setLang(localStorage.getItem('nl-lang') || 'ko');
});

// 이슈 펼치기/접기. 카드 안의 링크는 이슈를 다시 접지 않도록 클릭을 가로채지 않는다
// (머리줄에만 핸들러가 붙어 있어 카드 클릭은 그대로 기사로 간다).
function toggleIssue(head) {
  head.parentNode.classList.toggle('open');
}
"""


def build_tech_news_html(tech_list, bullets=None):
    """TOP 카드. 2026-09-07 피드백으로 요약을 통문장에서 개조식 불릿으로 바꿨다.
       [TOP n] → [제목] → [불릿 요약] → [의의 및 시사점]
    불릿이 없으면(이슈 생성 실패) 예전 통문장 요약으로 되돌아간다."""
    if not tech_list:
        return '<p style="color:#94a3b8;font-size:13px;padding:12px 0">이번 주 주요 기술/경쟁사 뉴스가 없습니다.</p>'
    bullets = bullets or {}
    html = ""
    for item in tech_list:
        cat = normalize_category(item.get("category"))
        cat_en = CATEGORY_KO_EN.get(cat, cat)
        title_ko = item.get("title_ko") or item.get("title", "")
        title_en = item.get("title_en") or item.get("title", "")
        summary_ko = item.get("summary_ko") or item.get("summary", "")
        summary_en = item.get("summary_en") or item.get("summary", "")
        # 아크메가 직접 언급된 기사는 전용 배지로 구분
        own_badge = ('<span class="cat-badge" style="background:#0a2342;color:#fff">'
                     '아크메</span>') if item.get("own") else ""
        # 기회/위험/시사점 3분할 → '의의 및 시사점' 한 항목 (구형 키도 받아준다)
        insight_ko = (item.get("insight_ko") or item.get("action_ko")
                      or item.get("opportunity_ko") or "")
        insight_en = (item.get("insight_en") or item.get("action_en")
                      or item.get("opportunity_en") or "")
        b = bullets.get(item.get("link"), {})
        if b.get("ko"):
            body = (_bullet_list(b["ko"], "lang-ko")
                    + _bullet_list(b.get("en") or b["ko"], "lang-en"))
        else:
            body = (f'<div class="summary lang-ko">{summary_ko}</div>'
                    f'<div class="summary lang-en">{summary_en}</div>')
        html += f"""
    <div class="tech-card">
      <div class="rank">TOP {item.get('rank','')} <span class="cat-badge lang-ko">{cat}</span><span class="cat-badge lang-en">{cat_en}</span>{own_badge}</div>
      <div class="title lang-ko"><a href="{item.get('link','#')}" target="_blank">{title_ko}</a></div>
      <div class="title lang-en"><a href="{item.get('link','#')}" target="_blank">{title_en}</a></div>
      <div class="meta">{item.get('source','')}</div>
      {body}
      <div class="analysis-row">
        <span class="analysis-label lang-ko" style="background:#3b82f6">의의 및 시사점</span>
        <span class="analysis-label lang-en" style="background:#3b82f6">Takeaway</span>
        <span class="analysis-text lang-ko">{insight_ko}</span>
        <span class="analysis-text lang-en">{insight_en or insight_ko}</span>
      </div>
    </div>"""
    return html


def build_customer_news_html(customer_list):
    """이슈 생성이 실패했을 때 쓰는 고객사 카드 (build_issue_cards_html 의 대체).

    사진을 여기에도 넣는다 (2026-09-21 피드백). 그 주에 사진이 빠진 진짜 원인은 이슈 생성 실패였고 그건 issue_digest
    쪽에서 고쳤지만, 폴백 카드에 사진이 아예 없던 것도 같이 고친다 — 다음에 또
    이슈 생성이 실패하더라도 독자가 보는 화면은 사진 있는 카드로 남는다.
    """
    if not customer_list:
        return '<p style="color:#94a3b8;font-size:13px;padding:12px 0">이번 주 주요 고객사 관련 뉴스가 없습니다.</p>'
    html = ""
    for item in customer_list:
        image = item.get("image") or ""
        thumb = (f'<a href="{_esc(item.get("link", "#"))}" target="_blank">'
                 f'<img class="thumb card-thumb" src="{_esc(image)}" alt="" loading="lazy"></a>'
                 if image else '<div class="thumb card-thumb thumb-none">🏢</div>')
        title_ko = item.get("title_ko") or item.get("title", "")
        title_en = item.get("title_en") or item.get("title", "")
        summary_ko = item.get("summary_ko") or item.get("summary", "")
        summary_en = item.get("summary_en") or item.get("summary", "")
        cust_ko = item.get("customer", "")
        cust_en = CUSTOMER_EN.get(cust_ko, cust_ko)
        type_ko, type_en = customer_type_badge(cust_ko)
        html += f"""
    <div class="customer-card">
      {thumb}
      <span class="customer-name lang-ko">{cust_ko}</span>
      <span class="customer-name lang-en">{cust_en}</span>
      <span class="customer-type lang-ko">{type_ko}</span>
      <span class="customer-type lang-en">{type_en}</span>
      <div class="title lang-ko"><a href="{item.get('link','#')}" target="_blank">{title_ko}</a></div>
      <div class="title lang-en"><a href="{item.get('link','#')}" target="_blank">{title_en}</a></div>
      <div class="meta">{item.get('source','')}</div>
      <div class="summary lang-ko">{summary_ko}</div>
      <div class="summary lang-en">{summary_en}</div>
    </div>"""
    return html


def _theme_card(item, extra_class, badge_ko, badge_en):
    """고객사 카드 스타일을 재사용하는 심플 카드(경쟁사/컴플라이언스용)."""
    title_ko = item.get("title_ko") or item.get("title", "")
    title_en = item.get("title_en") or item.get("title", "")
    summary_ko = item.get("summary_ko") or item.get("summary", "")
    summary_en = item.get("summary_en") or item.get("summary", "")
    return f"""
    <div class="customer-card {extra_class}">
      <span class="customer-name lang-ko">{badge_ko}</span>
      <span class="customer-name lang-en">{badge_en}</span>
      <div class="title lang-ko"><a href="{item.get('link','#')}" target="_blank">{title_ko}</a></div>
      <div class="title lang-en"><a href="{item.get('link','#')}" target="_blank">{title_en}</a></div>
      <div class="meta">{item.get('source','')}</div>
      <div class="summary lang-ko">{summary_ko}</div>
      <div class="summary lang-en">{summary_en}</div>
    </div>"""


def build_competitor_news_html(items):
    """경쟁사 동향: 썸네일 + 태그 + 제목 카드 (2026-09-07 피드백).

    이미지는 article_body.add_bodies 가 og:image 로 채운다. 본문 크롤이 실패한
    기사는 이미지도 없으므로, 없을 때 자리표시자를 넣어 그리드가 무너지지 않게 한다
    — 절반쯤은 이미지가 없는 것이 정상 상태다.
    """
    if not items:
        return '<p style="color:#94a3b8;font-size:13px;padding:12px 0">이번 주 주요 경쟁사 뉴스가 없습니다.</p>'
    cards = ""
    for a in items:
        image = a.get("image") or ""
        thumb = (f'<img class="thumb" src="{_esc(image)}" alt="" loading="lazy">'
                 if image else '<div class="thumb thumb-none">🏁</div>')
        tag = a.get("competitor", "")
        pub = (a.get("published", "") or "")[:10]
        cards += f"""
      <a class="thumb-card" href="{_esc(a.get('link', '#'))}" target="_blank">
        {thumb}
        <span class="thumb-tag">{_esc(tag)}</span>
        <div class="thumb-title lang-ko">{_esc(a.get('title_ko') or a.get('title', ''))}</div>
        <div class="thumb-title lang-en">{_esc(a.get('title_en') or a.get('title', ''))}</div>
        <div class="thumb-meta">{_esc(a.get('source', ''))} · {_esc(pub)}</div>
      </a>"""
    return f'<div class="thumb-grid">{cards}</div>'


def build_compliance_news_html(items):
    if not items:
        return '<p style="color:#94a3b8;font-size:13px;padding:12px 0">이번 주 주요 규제·컴플라이언스 뉴스가 없습니다.</p>'
    return "".join(
        _theme_card(a, "compliance", "🌐 글로벌 규제", "🌐 Compliance")
        for a in items
    )


def build_radar_html(radar_list):
    html = '<div class="radar-grid">'
    for item in radar_list:
        status = item.get("status", "모니터링")
        color = STATUS_COLOR.get(status, "#94a3b8")
        status_en = STATUS_EN.get(status, status)
        area_ko = item.get("area_ko") or item.get("area", "")
        area_en = item.get("area_en") or item.get("area_ko") or item.get("area", "")
        desc_ko = item.get("description_ko") or item.get("description", "")
        desc_en = item.get("description_en") or item.get("description_ko") or item.get("description", "")
        html += f"""
    <div class="radar-item">
      <span class="r-status lang-ko" style="background:{color}">{status}</span>
      <span class="r-status lang-en" style="background:{color}">{status_en}</span>
      <div class="r-area lang-ko">{area_ko}</div>
      <div class="r-area lang-en">{area_en}</div>
      <div class="r-desc lang-ko">{desc_ko}</div>
      <div class="r-desc lang-en">{desc_en}</div>
    </div>"""
    html += "</div>"
    return html


def build_actions_html(actions):
    # immediate(오늘 할 일)와 decision_needed(의사결정 필요)는 제외
    labels = {
        "this_week": ("🟡 이번 주 검토", "This Week"),
        "roadmap":   ("🟢 로드맵 반영",   "Roadmap"),
    }
    html = ""
    for key, (label_ko, label_en) in labels.items():
        items = actions.get(key, [])
        if not items:
            continue
        items_html = ""
        for it in items:
            if isinstance(it, dict):
                it_ko = it.get("ko") or it.get("en", "")
                it_en = it.get("en") or it.get("ko", "")
            else:
                it_ko = it_en = it  # 구버전 호환(문자열)
            items_html += (
                f'<div class="action-item lang-ko">{it_ko}</div>'
                f'<div class="action-item lang-en">{it_en}</div>'
            )
        html += f"""
    <div class="action-group">
      <div class="ag-title lang-ko">{label_ko}</div>
      <div class="ag-title lang-en">{label_en}</div>
      {items_html}
    </div>"""
    return html


# 전체 뉴스 카테고리당 최대 노출 건수 (관련성 점수 상위 순, 나머지는 컷)
MAX_PER_CATEGORY = 8


def select_all_news(articles, exclude_links=(), shown_articles=()) -> list[tuple[str, list]]:
    """'이번 주 전체 뉴스' 섹션에 실을 기사를 카테고리별로 고른다.

    반환: [(카테고리명, [기사, ...]), ...] — CATEGORY_ORDER 순, 빈 카테고리는 없음.

    선정만 떼어낸 이유(2026-08-24): 웹 빌더는 여기서 음수 점수 제외·근접중복 제거·
    카테고리당 상한을 적용하는데, 메일 빌더는 같은 이름의 함수를 따로 갖고 있으면서
    그 세 가지를 하나도 하지 않았다. 그래서 같은 분석본으로 만들어도 웹 38건 /
    메일 56건으로 갈렸다(메일에만 18건 더 실림, 정렬 순서도 달랐다).
    두 빌더는 렌더링 방식이 달라야 하지만(웹=CSS 그리드, 메일=table 레이아웃)
    '무엇을 싣는가'는 하나여야 한다. 지금은 section_lists() 가 그 하나다 —
    사본을 다시 만들지 말 것.
    """
    skip = {l for l in exclude_links if l}
    # 음수 점수(순수 노이즈: 인사·인물·PR 등)는 제외
    pool = [a for a in articles
            if a.get("link") not in skip and a.get("relevance_score", 0) >= 0]

    # 중복 제거는 '카테고리로 나누기 전에' 한 번에 한다(2026-08-24).
    # 예전에는 카테고리별로 따로 돌려서, 같은 사건 기사가 서로 다른 칸에 들어가면
    # 끝내 만나지 못했다. 실측: 코친조선소 블록공장 JV 철회 기사가 국문판은
    # '신조/조선', 영문판은 '기타'에 각각 실렸다. dedup_events 는 title_ko 를 보므로
    # 같은 목록에만 있었다면 잡혔을 쌍이다(영문 기사도 번역된 국문 제목을 갖고 있다).
    #
    # 위쪽 카드(기술·고객사·경쟁사·컴플라이언스)에 실린 기사도 중복 판정의 기준으로
    # 함께 넣는다(2026-09-08). 예전에는 skip 으로 '빼고' 나서 dedup 을 돌려서,
    # 카드 기사와 전체 뉴스 후보가 서로 대조된 적이 없었다. 같은 사건을 다른 매체가
    # 쓴 기사가 둘 있으면 하나는 카드에, 하나는 전체 뉴스에 나란히 실릴 수 있었다.
    #
    # 평소에는 수집 단계의 사건 병합(event_dedup.merge_same_event)이 같은 사건을
    # 한 건으로 합쳐 두므로 이 경우가 잘 안 생긴다. 문제는 그 단계가 LLM 호출이라
    # 통째로 실패할 수 있다는 것이다 — 실측(2026-08-31): Groq 429 로 전멸해 같은 LPG
    # 운반선 수주 기사가 3벌 실렸다(7_watch.py 가 감시하는 바로 그 유형이다).
    # 그때 마지막으로 막아 주는 그물이 여기다.
    #
    # 기준으로 넣기만 하고 결과에서는 도로 뺀다 — 카드에 이미 실렸으므로.
    seed = [a for a in shown_articles if a and a.get("link")]
    ranked = sorted(pool, key=lambda x: x.get("relevance_score", 0), reverse=True)
    pool = [a for a in dedup_events(seed + ranked, corpus=articles)
            if a.get("link") not in skip]

    # 2026-10-06 에 AI 융합 기사를 AI 칸으로 모으고 상한을 풀었다가 2026-10-12 에
    # 되돌렸다(사용자 결정 "예전처럼 되돌리기" — "AI/자율운항/스마트십 이 카테고리에
    # 왜이렇게 많아갑자기?"). 모든 칸이 같은 상한을 쓴다.
    groups = defaultdict(list)
    for art in pool:
        groups[normalize_category(art.get("category"))].append(art)

    out = []
    for cat in CATEGORY_ORDER:
        # pool 이 이미 점수 내림차순이라 그룹 안에서도 순서가 유지된다
        arts = groups.get(cat, [])[:MAX_PER_CATEGORY]
        if arts:
            out.append((cat, arts))
    return out


def flat_section_html(cat, arts):
    """카테고리 한 칸을 평면 목록으로. 이슈 생성이 실패했을 때 쓰는 기존 렌더다."""
    icon = CATEGORY_ICON.get(cat, "📰")
    cat_en = CATEGORY_KO_EN.get(cat, cat)
    items_html = ""
    for art in arts:
        pub = art.get("published", "")[:10]
        items_html += f"""
        <div class="article">
          <div class="a-title lang-ko"><a href="{art.get('link','#')}" target="_blank">{art.get('title_ko') or art.get('title','')}</a></div>
          <div class="a-title lang-en"><a href="{art.get('link','#')}" target="_blank">{art.get('title_en') or art.get('title','')}</a></div>
          <div class="a-meta">{art.get('source','')} · {pub}</div>
          <div class="a-summary lang-ko">{art.get('summary_ko','')[:200]}</div>
          <div class="a-summary lang-en">{(art.get('summary_en') or art.get('summary','') )[:200]}</div>
        </div>"""
    return f"""
      <div class="cat-cell" id="{CATEGORY_SLUG.get(cat, 'cat-etc')}">
        <div class="section-header lang-ko">{icon} {cat}</div>
        <div class="section-header lang-en">{icon} {cat_en}</div>
        <div class="article-grid">
        {items_html}
        </div>
      </div>"""


# ── 이슈 접이식 목록 (2026-09-07 피드백) ────────────────────────────────
# 카테고리 밑에 이슈 한 줄을 세우고 기사는 접어 둔다. 펼치면 기사 카드가 나오고,
# 카드는 제목만이 아니라 카드 전체가 기사 링크다.
#
# 접기/펼치기는 이 첨부 HTML 에서만 동작한다. 메일 본문(build_email.py)은
# Outlook·Gmail 이 JS 와 <details> 를 걷어내므로 같은 내용을 펼친 채로 싣는다.

def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _bullet_list(items, lang_class) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{_esc(b)}</li>" for b in items)
    return f'<ul class="a-bullets {lang_class}">{lis}</ul>'


def build_issue_card_html(art, bullets) -> str:
    """펼쳤을 때 나오는 기사 카드 1개. 카드 전체가 기사 링크다."""
    pub = (art.get("published", "") or "")[:10]
    ko = bullets.get("ko", []) if bullets else []
    en = bullets.get("en", []) or ko if bullets else []
    # 불릿이 없으면(스니펫뿐이라 확실한 사실이 없던 기사) 기존 요약을 줄여서 대신 쓴다.
    fallback_ko = "" if ko else (art.get("summary_ko", "") or "")[:140]
    fallback_en = "" if ko else (art.get("summary_en") or art.get("summary", ""))[:140]
    return f"""
        <a class="a-card" href="{_esc(art.get('link', '#'))}" target="_blank">
          <div class="a-title lang-ko">{_esc(art.get('title_ko') or art.get('title', ''))}</div>
          <div class="a-title lang-en">{_esc(art.get('title_en') or art.get('title', ''))}</div>
          <div class="a-meta">{_esc(art.get('source', ''))} · {_esc(pub)}</div>
          {_bullet_list(ko, 'lang-ko')}{_bullet_list(en, 'lang-en')}
          {f'<div class="a-summary lang-ko">{_esc(fallback_ko)}</div>' if fallback_ko else ''}
          {f'<div class="a-summary lang-en">{_esc(fallback_en)}</div>' if fallback_en else ''}
        </a>"""


def build_issue_html(issues, bullets, by_link) -> str:
    """이슈 목록 한 덩어리. 각 이슈는 머리줄 + 접힌 기사 카드들."""
    html = ""
    for n, issue in enumerate(issues):
        arts = [by_link[l] for l in issue.get("links", []) if l in by_link]
        if not arts:
            continue
        subject_ko = issue.get("subject_ko", "")
        subject_en = issue.get("subject_en") or subject_ko
        subj_html = ""
        if subject_ko:
            subj_html = (f'<span class="issue-subject lang-ko">[{_esc(subject_ko)}]</span>'
                         f'<span class="issue-subject lang-en">[{_esc(subject_en)}]</span>')
        count = (f'<span class="issue-count">{len(arts)}건</span>'
                 if len(arts) > 1 else "")
        cards = "".join(build_issue_card_html(a, bullets.get(a.get("link"), {}))
                        for a in arts)
        html += f"""
      <div class="issue">
        <div class="issue-head" onclick="toggleIssue(this)">
          <span class="issue-caret">&#9660;</span>
          {subj_html}
          <span class="issue-title lang-ko">{_esc(issue.get('title_ko', ''))}</span>
          <span class="issue-title lang-en">{_esc(issue.get('title_en') or issue.get('title_ko', ''))}</span>
          {count}
        </div>
        <div class="issue-body">{cards}</div>
      </div>"""
    return html


def _issue_image(arts) -> str:
    """이슈 대표 이미지 = 묶인 기사 중 처음으로 이미지가 있는 것."""
    for a in arts:
        if a.get("image"):
            return a["image"]
    return ""


def build_issue_cards_html(issues, bullets, by_link, icon="🏢") -> str:
    """이슈를 '사진 + 한 줄 요약 + 확장' 카드 2열로 (2026-09-07 피드백).

    접이식 목록(build_issue_html)과 같은 데이터를 쓰지만 생김새가 다르다.
    목록은 이슈가 10개 넘게 쌓이는 전체 뉴스용이고, 이쪽은 6건 안팎이라
    사진을 넣어도 화면이 늘어지지 않는 섹션용이다.
    """
    cards = ""
    for issue in issues:
        arts = [by_link[l] for l in issue.get("links", []) if l in by_link]
        if not arts:
            continue
        image = _issue_image(arts)
        href = _esc(arts[0].get("link", "#"))
        thumb = (f'<a href="{href}" target="_blank"><img class="thumb" '
                 f'src="{_esc(image)}" alt="" loading="lazy"></a>'
                 if image else f'<div class="thumb thumb-none">{icon}</div>')
        subject = issue.get("subject_ko", "")
        tag = (f'<span class="thumb-tag lang-ko">{_esc(subject)}</span>'
               f'<span class="thumb-tag lang-en">{_esc(issue.get("subject_en") or subject)}</span>'
               if subject else "")
        count = f" ({len(arts)}건)" if len(arts) > 1 else ""
        inner = "".join(build_issue_card_html(a, bullets.get(a.get("link"), {}))
                        for a in arts)
        cards += f"""
      <div class="issue issue-card">
        {thumb}
        <div class="issue-card-head">
          {tag}
          <div class="issue-title lang-ko">{_esc(issue.get('title_ko', ''))}{count}</div>
          <div class="issue-title lang-en">{_esc(issue.get('title_en') or issue.get('title_ko', ''))}{count}</div>
        </div>
        <div class="issue-expand" onclick="toggleIssue(this)">
          <span class="issue-caret">&#9660;</span>
          <span class="lang-ko">자세히 보기</span><span class="lang-en">Details</span>
        </div>
        <div class="issue-body">{inner}</div>
      </div>"""
    return f'<div class="thumb-grid">{cards}</div>'


def build_issue_section_html(cat, issues, bullets, by_link) -> str:
    """전체 뉴스 한 카테고리를 이슈 목록으로. 바깥의 .cat-grid 가 2열로 배치한다."""
    icon = CATEGORY_ICON.get(cat, "📰")
    cat_en = CATEGORY_KO_EN.get(cat, cat)
    return f"""
      <div class="cat-cell" id="{CATEGORY_SLUG.get(cat, 'cat-etc')}">
        <div class="section-header lang-ko">{icon} {cat}</div>
        <div class="section-header lang-en">{icon} {cat_en}</div>
        {build_issue_html(issues, bullets, by_link)}
      </div>"""


def _title_bigrams(text):
    core = re.split(r"[…]|\.\.\.| - ", text or "")[0].lower()
    core = re.sub(r"[^0-9a-z가-힣]", "", core)
    return {core[i:i + 2] for i in range(len(core) - 1)}


def _content_bigrams(text, limit=160):
    """요약(본문) 앞부분의 문자 bigram 집합. 다매체 동일 사안 판별용."""
    core = re.sub(r"[^0-9a-z가-힣]", "", (text or "").lower())[:limit]
    return {core[i:i + 2] for i in range(len(core) - 1)}


# 인사 마커. PERSONNEL_KEEP_CUSTOMERS 의 고객사는 이 마커로 거르지 않는다.
_PERSONNEL_MARKERS = ["[인사]", "인사발령", "임원인사", "회장]", "취임", "승진"]
# 1_fetch_news.PERSONNEL_KEEP_CUSTOMERS 와 같아야 한다(숫자로 시작하는 파일이라
# import 하지 않고 적는다).
PERSONNEL_KEEP_CUSTOMERS = {"ONE"}

# 고객사 동향에서 제외할 단순 사내소식/인물 프로필 마커
_CUSTOMER_SKIP = _PERSONNEL_MARKERS + [
                  "[부고]", "부고", "[동정]",
                  "동정", "신년사", "who is", "[wo is",
                  # 조직·거점 신설 발표 (2026-08-25 피드백).
                  # 수집 단계에서 -5점 감점하지만, 고객사 카드는 업체별로 하나씩 뽑는
                  # 구조라 그 업체의 유일한 기사면 점수와 무관하게 실린다. 실측:
                  # '삼성중공업-KAIST 차세대 선박연구센터 출범'이 감점 뒤에도 카드에 남았다.
                  "센터 출범", "센터 가동", "센터 구축", "센터 개소", "센터 설립",
                  "혁신허브", "혁신 허브", "허브 구축", "거점 구축", "혁신 거점",
                  # 기부·인재육성 (2026-09-21 피드백).
                  # 수집 단계에서도 막지만(DONATION_NOISE_KEYWORDS), 지난주 데이터를
                  # 다시 빌드할 때를 위해 여기서도 본다 — 위 두 항목과 같은 이유다.
                  "발전기금", "발전 기금", "기탁", "장학금", "사회공헌",
                  "인재 육성", "인재육성", "인재 양성", "인재양성",
                  # 노사·상생 홍보 (2026-09-28 피드백: 삼성중공업 임협 타결, 케이조선 협력사
                  # 대금 조기 지급이 고객사 카드에 실렸다). 수집 단계(LABOR_NOISE_KEYWORDS ·
                  # DONATION_NOISE_KEYWORDS)에서 막지만, 위 항목들과 같은 이유로 여기서도 본다.
                  "임협", "임금협상", "임단협", "기본급",
                  "조기 지급", "조기지급", "대금 조기"]
_CUSTOMER_SKIP_NON_PERSONNEL = [m for m in _CUSTOMER_SKIP if m not in _PERSONNEL_MARKERS]

# 위 목록은 제목만 본다. 그런데 같은 '연구센터 신설' 기사가 제목에는 그 말을 안 쓰고
# 본문에서만 밝히는 경우가 있다 — 2026-09-07 피드백으로 지목된
# '삼성중공업, 미래선박 R&D 투트랙…AI부터 차세대 연료까지'가 그랬다. 제목에는
# '센터'가 한 번도 안 나오고, 요약 첫 문장이 "지난 8월 문을 연 'SHI-KAIST 차세대
# 선박연구센터'는…" 이다. 그래서 요약까지 보는 목록을 따로 둔다.
#
# 제목 목록과 분리한 이유: 요약은 스치듯 언급되는 말이 많아 넓은 키워드를 쓰면
# 멀쩡한 기사가 떨어진다. 여기에는 '연구 조직 신설' 외에는 쓰이지 않는 말만 넣는다.
_CUSTOMER_SKIP_IN_SUMMARY = ["연구센터", "협력센터", "연구소 설립", "연구원 설립",
                             "r&d센터", "r&d 센터"]


# 선급 발표 기사는 고객사 동향이 아니다 (2026-09-21 피드백).
#   "KR-한화오션, 18K LNG 벙커링선 공동개발 — 이것도 빼는게 좋을것 같아"
#   "HD한국조선해양, 100MW급 부유식 발전설비 미국선급(ABS) 기본 인증 획득
#    — 이것도 별로 안중요한 얘긴데"
#   "전체적으로 엘엔지 벙커링선 얘기가 너무 중복되는것같아"
#
# 셋은 한 덩어리다. 가스텍 2026 기간에 선급(KR·ABS·DNV·NK)이 조선소와 맺은 개념승인·
# 기본인증·공동개발 MOU 보도자료가 쏟아졌고, 회사명이 제목에 있으니 전부 고객사 태그
# (+6점)를 받아 고객사 카드를 채웠다. 실측(2026-09-21 회차) — 카드 6장 중 3장이 이것:
#   KR-한화오션, 18K LNG 벙커링선 공동개발                         22점
#   한국선급-HD현대중공업, 3D 모델 기반 승인체계 공동개발            -
#   HD한국조선해양, 100MW급 부유식 발전설비 ABS 기본 인증 획득       -
# 여기에 컴플라이언스 카드의 '한화파워 LNG 벙커링선 미국 선급 개념승인'까지 겹쳐
# 한 회차에 같은 얘기가 네 번 나왔다.
#
# 제외가 아니라 '자리 옮김'이다. 선급 소식은 글로벌 컴플라이언스 섹션과 전체 뉴스에
# 그대로 남는다 — 고객사 동향은 '고객사가 무엇을 했는가'를 싣는 자리이고, 선급이 주는
# 인증·개념승인(AiP)은 선급이 주체인 정례 보도자료다.
#
# 수집 단계에서 빼지 않는 이유: 컴플라이언스 섹션에는 실려야 하는 기사다.
CLASS_SOCIETY_NAMES = ["한국선급", "미국선급", "노르웨이선급", "일본선급", "영국선급",
                       "프랑스선급", "중국선급", "이탈리아선급", "독일선급",
                       "kr-", "-kr", " abs", "abs)", "(abs", "dnv", "로이드",
                       "lloyd", "뷰로베리타스", "bureau veritas", " bv)", "class nk",
                       "classnk", " nk)", "ccs", "rina", "선급"]

# 선급이 내놓는 행위. 이 말이 선급 이름과 함께 있으면 선급 보도자료로 본다.
CLASS_NOTICE_ACTIONS = ["인증", "승인", "공동개발", "공동 개발", "협약", "mou",
                        "업무협약", "양해각서", "획득", "취득", "발급"]

# 선급 이름이 제목에 없어도 선급 발표인 것이 분명한 말 (약칭·영문 표기 포함).
# '인증 획득' 단독은 넣지 않는다 — '형식승인 획득'처럼 제품 인증 기사가 걸린다.
CLASS_NOTICE_ALONE = ["기본 인증", "기본인증", "개념승인", "개념 승인",
                      "개념설계 인증", "기본설계 인증", "aip 획득", "aip 취득",
                      "approval in principle", "aip for", "basic design approval"]


def is_class_notice(title: str) -> bool:
    """선급의 인증·개념승인·공동개발 발표 기사인가 (제목 기준).

    제목만 본다. 본문 판정은 오제외를 낳는다 — 조선·해운 기사는 본문 어딘가에서
    선급 이름을 스치듯 언급하는 일이 아주 흔하다(실측: '케이조선, 가스텍서 친환경
    기술 보폭' 본문에 '일본선급협회(NK)'가 나오지만 이 기사의 주체는 케이조선이다).
    """
    t = km.strip_quotes(km.strip_outlet(title or "")).lower()
    if any(m in t for m in CLASS_NOTICE_ALONE):
        return True
    return (any(n in t for n in CLASS_SOCIETY_NAMES)
            and any(a in t for a in CLASS_NOTICE_ACTIONS))


# 행사·전시회 쏠림 상한 (2026-09-21 피드백).
#
# 실측(2026-09-21 회차, 가스텍 2026 기간):
#   고객사  케이조선, 가스텍서 친환경 기술 보폭            24점
#   고객사  가스텍 달군 K-LNG 기술…한화오션은 선박 공략     20점
#   기술    HD현대, '가스텍'에서 미래 조선 기술 공개        ← 이 카드가 빠졌으면 했다
#   여기에 선급 인증 보도자료(대부분 가스텍 발표분)까지 겹쳐 회차 전체가 한 행사였다.
#
# 행사 기사를 빼지는 않는다 — 위 두 고객사 카드는 남기라는 판단이었다(2026-09-21).
# 문제는 '한 행사가 회차를 덮는 것'이므로 건수로 막는다.
#
# 행사 이름은 제목에서만 찾는다. 본문·요약까지 보면 그 주에 열린 행사를 스치듯 언급한
# 기사가 전부 같은 행사로 묶인다 — 전시회 기간에는 업계 기사 절반이 그렇다.
SHOW_KEYWORDS = {
    "가스텍": ["가스텍", "gastech"],
    "SMM": ["smm 2026", "smm 2028", "smm 함부르크", "smm hamburg"],
    "노르쉬핑": ["nor-shipping", "노르쉬핑"],
    "포시도니아": ["posidonia", "포시도니아"],
    "마린텍": ["marintec", "마린텍"],
    "코마린": ["kormarine", "코마린", "마린위크"],
    "시트레이드": ["seatrade", "시트레이드"],
    "CES": ["ces 2026", "ces 2027"],
}

# 한 행사가 카드 섹션에서 차지할 수 있는 최대 건수.
# 2건인 이유: 큰 전시회는 그 주의 실제 뉴스이기도 해서 0으로 두면 업계 흐름이 빠진다.
# 반대로 3건이면 2열 그리드 한 줄 반이 같은 행사라 '다 그 얘기'로 읽힌다.
MAX_CARDS_PER_SHOW = 2


def show_name(title: str) -> str | None:
    """제목에 들어 있는 행사·전시회 이름. 없으면 None."""
    t = km.strip_quotes(km.strip_outlet(title or "")).lower()
    for name, words in SHOW_KEYWORDS.items():
        if any(w in t for w in words):
            return name
    return None


def select_customer_news(articles, per_customer=1, total=12, exclude=()):
    """고객사 동향 선정: 관련성 순 + 근접중복 제거 + 고객사별 최대 per_customer건
    (특정 고객사 편중 방지, 매각/인수 등 중요 소식 누락 방지, 사내소식 제외).

    per_customer=1 인 이유: 같은 회사의 같은 사안을 여러 매체가 다르게 쓴 기사는
    제목·요약 유사도로 걸러지지 않는다(실측: 중복쌍 제목 0.00~0.38 / 요약 0.03~0.41 로
    정상쌍 0.12/0.11 과 겹쳐 임계값 분리 불가). 회사당 1건으로 제한해 확실히 막는다.
    회사별 2건 이상 노출이 필요해지면 이 값을 올리면 된다."""
    def is_noise(a):
        # 따옴표 제거 후 판정 — km.strip_quotes 주석 참고
        # (실측: "삼성중공업-KAIST '차세대 선박연구센터' 출범" 이 '센터 출범'에 안 걸렸다)
        t = km.strip_quotes(a.get("title_ko") or a.get("title", "")).lower()
        # 조선·해운이 아닌 사업부문(토건·건설·반도체) 기사는 고객사 동향이 아니다.
        # 수집 단계에서도 막지만, 지난주 데이터를 다시 빌드할 때를 위해 여기서도 본다.
        if km.any_keyword(km.strip_outlet(t), km.NONMARINE_KEYWORDS):
            return True
        # 산재(작업장 인명사고)는 고객사 '동향'이 아니다 (2026-09-01 피드백).
        # 수집 단계에서도 막지만, 지난주 데이터를 다시 빌드할 때를 위해 여기서도 본다.
        if km.is_industrial_accident(a.get("title_ko") or a.get("title", "")):
            return True
        # 인사 기사라도 남기는 고객사(ONE 등)는 인사 마커를 건너뛴다 — 수집 단계의
        # PERSONNEL_KEEP_CUSTOMERS 와 같은 목록이다(2026-09-28 요청).
        skip = (_CUSTOMER_SKIP_NON_PERSONNEL
                if a.get("customer") in PERSONNEL_KEEP_CUSTOMERS else _CUSTOMER_SKIP)
        if any(m in t for m in skip):
            return True
        # 선급 인증·개념승인·공동개발 보도자료는 컴플라이언스 자리다 (2026-09-21 피드백)
        if is_class_notice(a.get("title_ko") or a.get("title", "")):
            return True
        # 제목이 감추고 본문이 밝히는 '연구 조직 신설' (_CUSTOMER_SKIP_IN_SUMMARY 주석)
        summary = km.strip_quotes(a.get("summary_ko") or a.get("summary", "")).lower()
        return any(m in summary for m in _CUSTOMER_SKIP_IN_SUMMARY)

    cust = dedup_events(sorted(
        [a for a in articles if a.get("customer") and not is_noise(a)
         and a.get("link") not in exclude],
        key=lambda x: x.get("relevance_score", 0), reverse=True), corpus=articles)
    picked, shows = [], {}
    for a in cust:
        if sum(1 for x in picked if x["customer"] == a["customer"]) >= per_customer:
            continue
        # 같은 행사 기사는 MAX_CARDS_PER_SHOW 건까지만. 넘치면 그 업체의 다음 기사가
        # 자리를 받는다(이 루프가 계속 도므로 자동으로 채워진다) — SHOW_KEYWORDS 주석 참고.
        show = show_name(a.get("title_ko") or a.get("title", ""))
        if show and shows.get(show, 0) >= MAX_CARDS_PER_SHOW:
            continue
        if show:
            shows[show] = shows.get(show, 0) + 1
        picked.append(a)
        if len(picked) >= total:
            break
    return picked


def select_competitor_news(articles, per_competitor=1, total=8):
    """경쟁사 동향 선정: 고객사 섹션과 같은 방식으로 업체당 최대 per_competitor건.

    제한이 없으면 한 업체가 섹션을 독식한다. 실측(2026-08-03): 4건 전부
    HD현대마린솔루션 실적 기사였다(모회사 HD현대 실적 기사까지 경쟁사로 태깅됨).
    dedup_events 는 '같은 사건'만 걸러서 서로 다른 사안이면 통과하므로 여기서 막는다."""
    comp = dedup_events(sorted(
        [a for a in articles if a.get("competitor")],
        key=lambda x: x.get("relevance_score", 0), reverse=True), corpus=articles)
    picked = []
    for a in comp:
        if sum(1 for x in picked if x["competitor"] == a["competitor"]) < per_competitor:
            picked.append(a)
        if len(picked) >= total:
            break
    return picked


def even_count(items):
    """항목 수를 짝수로 맞춘다 (홀수면 마지막 1건 제외).
    카드 섹션이 2열 그리드(card-grid-2)라 홀수면 마지막 줄에 빈칸이 생긴다.

    단 1건뿐일 때는 그대로 둔다: 0건으로 만들면 실제 있는 뉴스가 통째로 사라지고
    '이번 주 뉴스가 없습니다' 안내가 대신 나가 사실과 어긋난다."""
    if len(items) <= 1:
        return items
    return items[:len(items) - 1] if len(items) % 2 else items


def dedup_events(items, overlap_threshold=0.4, summary_threshold=0.6, corpus=None):
    """섹션 안의 근접 중복 제거 - 같은 사건은 한 번만.
    정렬이 상위(관련성 높음)부터이므로 먼저 나온 기사가 유지된다.

    판정을 둘 다 쓴다(2026-08-24):
      · 문자 bigram 유사도 — 원래부터 있던 방식. 표현이 비슷한 제목/요약을 잡는다.
      · 단어 단위 '같은 사건' 판정(km.is_same_event) — 수집 단계가 쓰는 방식.

    수집 단계는 진작 단어 방식으로 옮겼는데 렌더 단계만 bigram 에 남아 있었다. 그 결과
    수집 단계가 걸러낸 중복 유형이 여기서는 그대로 통과했다. 실측: '코친조선소 블록공장
    JV 철회' 기사 2건이 서로 다른 법인명(HD한국조선해양/HD현대중공업)으로 쓰여 뉴스레터
    '신조/조선' 목록에 나란히 실렸다. bigram 으로는 회사명이 유사도를 지배해 안 걸린다
    (keyword_match.py 상단 주석 참고).
    """
    # 문서빈도는 '그 주 수집분 전체'에서 센다(corpus). 섹션 목록만으로 세면 안 된다 —
    # 좁은 목록에서는 흔한 말도 df<=2 라 '희귀 고유명사'로 오인된다.
    # 실측(2026-08-24): 38건 풀에서 '한화오션'(df 2)과 'shipping'(df 2)이 희귀어로 잡혀
    # 서로 무관한 기사 2쌍이 중복 처리됐다.
    #   '한화오션, 日 이이노해운 가스운반선 인도' x '한화오션, 돛 단 LNG선'
    #   'China's Newnew Shipping to Murmansk' x 'ESL Shipping NIS2 cybersecurity'
    # 그 주 전체(50~60건)에서 세면 두 단어 모두 df 4 이상이라 희귀어에서 빠진다.
    # df 는 제목의 모든 표기(국문·영문·원문)를 합쳐서 센다 — km.df_tokens 주석 참고.
    ref = corpus if corpus is not None else items
    freq = km.doc_freq([km.df_tokens(a) for a in ref])
    max_df = km.rare_cutoff(len(ref))

    kept, kept_bg, kept_sbg, kept_words = [], [], [], []
    for a in items:
        bg = _title_bigrams(a.get("title_ko") or a.get("title", ""))
        sbg = _content_bigrams(a.get("summary_ko") or a.get("summary", ""))
        title_dup = len(bg) >= 5 and any(
            len(bg & k) / min(len(bg), len(k)) >= overlap_threshold
            for k in kept_bg if len(k) >= 5)
        summary_dup = len(sbg) >= 20 and any(
            len(sbg & k) / min(len(sbg), len(k)) >= summary_threshold
            for k in kept_sbg if len(k) >= 20)

        words = km.dup_tokens(a.get("title_ko") or a.get("title", ""))
        ent = km.entity_key(a)
        names = km.entity_tokens(a)      # 회사명 토큰은 판정에서 뺀다
        event_dup = any(
            km.is_same_event(words, kw, freq, max_df=max_df,
                             same_entity=(ent is not None and ent == ke),
                             ignore=names | kn)
            for kw, ke, kn in kept_words)

        if title_dup or summary_dup or event_dup:
            continue
        kept_bg.append(bg)
        kept_sbg.append(sbg)
        kept_words.append((words, ent, names))
        kept.append(a)
    return kept


def with_source(items, articles):
    """weekly_analysis 의 항목을 같은 link 의 원본 기사(summarized_news)로 바꾼다.

    2026-09-07 로 article_fields 가 body 를 담게 됐지만, 그 전에 만들어진
    weekly_analysis.json 으로 다시 빌드하는 일이 있어 여기서 한 번 더 맞춘다.
    원본에는 image·relevance_score 등 분석본이 안 담는 필드도 있다.
    """
    by_link = {a.get("link"): a for a in articles if a.get("link")}
    return [by_link.get(i.get("link"), i) for i in items]


# 이슈 그룹 이름. 5_analyze(이슈 생성) · 웹 · 메일이 같은 문자열을 써야 짝이 맞는다.
# 전체 뉴스는 카테고리명(CATEGORY_ORDER)을 그대로 그룹명으로 쓴다 — 이름이 겹치지 않는다.
CUSTOMER_GROUP = "고객사 동향"
COMPLIANCE_GROUP = "글로벌 컴플라이언스"
COMPETITOR_GROUP = "경쟁사 동향"
TECH_GROUP = "기술 동향"


def section_lists(analysis, articles) -> dict:
    """세 곳이 같은 기사 집합을 보게 하는 단일 진입점.

    웹 빌더와 메일 빌더가 각자 같은 코드를 갖고 있었고, 여기에 5_analyze 의 이슈
    생성까지 세 번째 사본이 생길 참이었다. 셋이 어긋나면 '이슈에는 있는데 목록에는
    없는 기사'가 나온다 — 예전에 웹 38건 / 메일 56건으로 갈렸던 것과 같은 종류의 사고다.
    """
    tech = even_count(analysis.get("tech_news", []))
    customer = even_count(select_customer_news(articles))
    competitor = even_count(select_competitor_news(articles))
    compliance = even_count(dedup_events(sorted(
        [a for a in articles if a.get("compliance")],
        key=lambda x: x.get("relevance_score", 0), reverse=True), corpus=articles)[:4])
    shown_items = tech + customer + competitor + compliance
    shown = {i.get("link") for i in shown_items}
    return {
        "tech": tech,
        "customer": customer,
        "competitor": competitor,
        "compliance": compliance,
        "shown_links": shown,
        # 링크만이 아니라 기사 자체도 넘긴다 — 전체 뉴스가 카드와 같은 사건을
        # 싣지 않도록 대조하는 데 쓴다(select_all_news 주석 참고).
        "all_news": select_all_news(articles, shown, shown_items),
    }


def issue_groups(analysis, articles) -> list[tuple[str, list]]:
    """이슈·불릿을 만들 대상.

    기술 동향은 이슈로 접지 않는다 — 카드마다 '의의 및 시사점'이 붙는 자리라
    한 줄로 접으면 그 분석이 갈 곳이 없다. 그래도 목록에 넣는 이유는 **불릿**이다:
    2026-09-07 피드백으로 카드 요약이 통문장에서 개조식 3줄로 바뀌었고, 그 3줄을
    만드는 곳이 여기뿐이다. 이 그룹의 issues 는 어느 빌더도 읽지 않는다.

    경쟁사 동향도 들어간다. 썸네일 카드에 원문 제목을 그대로 넣었더니
    "왜 요약이 아니라 제목이냐"는 피드백이 왔다(2026-09-07) — 다른 섹션은 전부
    이슈 한 줄 요약이 들어가는데 거기만 제목이었다. 이제 같은 이슈를 쓴다.
    """
    lists = section_lists(analysis, articles)
    return [
        # tech_news 만 weekly_analysis 에서 온다 — 본문이 딸려 오도록 원본으로 맞춘다
        (TECH_GROUP, with_source(lists["tech"], articles)),
        (CUSTOMER_GROUP, lists["customer"]),
        (COMPLIANCE_GROUP, lists["compliance"]),
        (COMPETITOR_GROUP, lists["competitor"]),
    ] + list(lists["all_news"])


def build_html(analysis, articles):
    today = datetime.now()
    today_ko = today.strftime("%Y년 %m월 %d일")
    today_en = today.strftime("%B %d, %Y")
    period = analysis.get("period", "")
    total = analysis.get("total_news", len(articles))

    # 카드·목록에 실릴 기사 집합. 메일 빌더·5_analyze 도 같은 함수를 쓴다.
    lists = section_lists(analysis, articles)
    tech_list = lists["tech"]
    customer_list = lists["customer"]
    competitor_list = lists["competitor"]
    compliance_list = lists["compliance"]
    shown_links = lists["shown_links"]

    # 이슈 접이식 (2026-09-07). 5_analyze 가 만들어 두지 못했으면 평면 목록으로 간다.
    issues_by_group, bullets = idg.index(analysis)
    by_link = {a.get("link"): a for a in articles if a.get("link")}

    def issue_or_cards(group, cards_html):
        """이슈가 있으면 접이식으로, 없으면(생성 실패) 예전 카드 그리드로."""
        issues = issues_by_group.get(group, [])
        if issues:
            return f'<div class="issue-list">{build_issue_html(issues, bullets, by_link)}</div>'
        return f'<div class="card-grid-2">{cards_html}</div>'

    # 고객사 동향: 사진 + 한 줄 요약 + 확장 카드 2열 (2026-09-07 피드백)
    customer_issues = issues_by_group.get(CUSTOMER_GROUP, [])
    customer_html = (build_issue_cards_html(customer_issues, bullets, by_link)
                     if customer_issues
                     else f'<div class="card-grid-2">{build_customer_news_html(customer_list)}</div>')
    compliance_html = issue_or_cards(COMPLIANCE_GROUP,
                                     build_compliance_news_html(compliance_list))
    # 경쟁사도 고객사와 같은 카드다 — 다른 건 자리표시자 아이콘뿐
    competitor_issues = issues_by_group.get(COMPETITOR_GROUP, [])
    competitor_html = (build_issue_cards_html(competitor_issues, bullets, by_link,
                                              icon="🏁")
                       if competitor_issues
                       else build_competitor_news_html(competitor_list))

    all_news_html = ""
    for cat, arts in lists["all_news"]:
        cat_issues = issues_by_group.get(cat, [])
        all_news_html += (build_issue_section_html(cat, cat_issues, bullets, by_link)
                          if cat_issues else flat_section_html(cat, arts))

    # 전체 뉴스 하위 카테고리 네비 버튼 (실제로 실리는 카테고리만 — 예전에는 '남은
    # 기사'로 판정해, 음수 점수·중복으로 걸러져 섹션이 안 생긴 칸까지 버튼이 나갔다)
    present_cats = [c for c, _ in lists["all_news"]]
    cat_nav = "".join(
        f'<a href="#{CATEGORY_SLUG[c]}">'
        f'<span class="lang-ko">{CATEGORY_ICON.get(c, "📰")} {c}</span>'
        f'<span class="lang-en">{CATEGORY_ICON.get(c, "📰")} {CATEGORY_KO_EN.get(c, c)}</span></a>'
        for c in present_cats
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>아크메 주간 해사 브리핑</title>
  <style>{CSS}</style>
</head>
<body class="ko">
<div class="wrapper">

  <div class="header">
    <div class="logo">{LOGO_TEXT}</div>
    <h1 class="lang-ko">{COMPANY_NAME_KO} 주간 해사 브리핑</h1>
    <h1 class="lang-en">{COMPANY_NAME_EN} Weekly Maritime Briefing</h1>
    <div class="period lang-ko">📅 {today_ko} 발행 &nbsp;|&nbsp; 수집 기간: {period}</div>
    <div class="period lang-en">📅 {today_en} &nbsp;|&nbsp; Period: {period}</div>
    <div class="lang-toggle">
      <button class="lang-btn active" data-lang="ko" onclick="setLang('ko')">한국어</button>
      <button class="lang-btn" data-lang="en" onclick="setLang('en')">English</button>
    </div>
  </div>

  <div class="conclusion">
    <div class="label lang-ko">이번 주 한 줄 결론</div>
    <div class="label lang-en">THIS WEEK'S KEY TAKEAWAY</div>
    <div class="lang-ko">{analysis.get('one_line_conclusion_ko') or analysis.get('one_line_conclusion','')}</div>
    <div class="lang-en">{analysis.get('one_line_conclusion_en') or analysis.get('one_line_conclusion_ko') or analysis.get('one_line_conclusion','')}</div>
  </div>

  <div class="summary-bar">
    <span class="lang-ko">수집 뉴스: <strong>{total}건</strong> &nbsp;|&nbsp; 기술동향: <strong>{len(tech_list)}건</strong> &nbsp;|&nbsp; 고객사: <strong>{len(customer_list)}건</strong></span>
    <span class="lang-en">Total: <strong>{total}</strong> &nbsp;|&nbsp; Tech: <strong>{len(tech_list)}</strong> &nbsp;|&nbsp; Customers: <strong>{len(customer_list)}</strong></span>
  </div>

  <nav class="topic-nav">
    <a href="#sec-tech"><span class="lang-ko">🔬 기술 동향</span><span class="lang-en">🔬 Tech</span></a>
    <a href="#sec-customer"><span class="lang-ko">🏢 고객사</span><span class="lang-en">🏢 Customers</span></a>
    <a href="#sec-compliance"><span class="lang-ko">🌐 컴플라이언스</span><span class="lang-en">🌐 Compliance</span></a>
    <a href="#sec-competitor"><span class="lang-ko">🏁 경쟁사</span><span class="lang-en">🏁 Competitors</span></a>
    <span class="nav-sep"></span>
    {cat_nav}
  </nav>

  <div class="section" id="sec-tech">
    <div class="section-header lang-ko">🔬 기술 동향</div>
    <div class="section-header lang-en">🔬 Tech & Industry Trends</div>
    <div class="card-grid-2">{build_tech_news_html(tech_list, bullets)}</div>
  </div>
  <hr class="div">

  <div class="section" id="sec-customer">
    <div class="section-header lang-ko">🏢 고객사 동향 (해운사 · Big3 조선소)</div>
    <div class="section-header lang-en">🏢 Customer News (Carriers &amp; Big3 Shipyards)</div>
    {customer_html}
  </div>
  <hr class="div">

  <div class="section" id="sec-compliance">
    <div class="section-header lang-ko">🌐 글로벌 컴플라이언스 (IMO·선급)</div>
    <div class="section-header lang-en">🌐 Global Compliance (IMO & Class)</div>
    {compliance_html}
  </div>
  <hr class="div">

  <div class="section" id="sec-competitor">
    <div class="section-header lang-ko">🏁 경쟁사 동향</div>
    <div class="section-header lang-en">🏁 Competitor News</div>
    {competitor_html}
  </div>
  <hr class="div">

  <div class="section" id="sec-all">
    <div class="section-header lang-ko">📰 이번 주 전체 뉴스</div>
    <div class="section-header lang-en">📰 All News This Week</div>
  </div>
  <div class="cat-grid">{all_news_html}</div>

  <div class="footer">
    <span class="lang-ko">{COMPANY_NAME_KO} &nbsp;|&nbsp; 본 브리핑은 공개 RSS 피드 기반 자동 수집·AI 분석입니다.<br>문의: newsletter@example.com</span>
    <span class="lang-en">{COMPANY_NAME_EN} &nbsp;|&nbsp; Auto-collected from public RSS feeds and analyzed by AI.<br>Contact: newsletter@example.com</span>
  </div>
</div>
<script>{JS}</script>
</body>
</html>"""


def run():
    analysis_path = Path("weekly_analysis.json")
    news_path = Path("summarized_news.json")

    if not analysis_path.exists():
        print("weekly_analysis.json이 없습니다. 5_analyze.py를 먼저 실행하세요.")
        return
    if not news_path.exists():
        print("summarized_news.json이 없습니다.")
        return

    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    articles = json.loads(news_path.read_text(encoding="utf-8"))

    html = build_html(analysis, articles)
    fname = f"weekly_newsletter_{datetime.now().strftime('%Y%m%d')}.html"
    Path(fname).write_text(html, encoding="utf-8")
    print(f"\n[HTML 생성 완료] → {fname}")
    archive_build(fname)


def archive_build(fname: str) -> None:
    """방금 만든 파일의 사본을 builds/YYYYMMDD/이름_HHMMSS.html 로 남긴다.

    배경(2026-10-12): 발송 직전 지적을 받아 몇 분 간격으로 여러 번 다시 빌드했는데,
    매번 같은 이름(weekly_email_YYYYMMDD.html)을 덮어써서 "8:57분에 보낸 버전으로
    보내"라는 요청에 응할 수 없었다. 모델 요약은 다시 돌리면 문장이 달라져 재현도 안 된다.
    사본을 못 남겨도 빌드는 성공으로 둔다 — 보관은 부가 기능이다.
    """
    try:
        now = datetime.now()
        src = Path(fname)
        dest = Path("builds") / now.strftime("%Y%m%d") / f"{src.stem}_{now:%H%M%S}{src.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        print(f"  사본 보관 → {dest}")
    except Exception as e:
        print(f"  [주의] 사본 보관 실패 ({type(e).__name__}: {e})")


if __name__ == "__main__":
    run()
