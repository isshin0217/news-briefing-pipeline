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

from profile_loader import BRAND, customer_en, customer_group

COMPANY_NAME_KO = BRAND["name_ko"]
COMPANY_NAME_EN = BRAND["name_en"]
LOGO_TEXT = BRAND["logo"]
BRIEFING_KO = BRAND.get("briefing_ko", "주간 브리핑")
BRIEFING_EN = BRAND.get("briefing_en", "Weekly Briefing")
CONTACT = BRAND.get("contact", "")

CATEGORY_KO_EN = {
    "자율운항/스마트십": "Autonomous / Smart Ship",
    "사이버보안/규정":   "Cyber / Regulation",
    "신조/조선":         "Newbuilding / Shipyard",
    "탈탄소/친환경":     "Decarbonization",
    "해운시황/물류":     "Shipping Market",
    "기타":              "Others",
}

CATEGORY_ICON = {
    "자율운항/스마트십": "🤖",
    "사이버보안/규정":   "🔒",
    "신조/조선":         "🏗️",
    "탈탄소/친환경":     "🌿",
    "해운시황/물류":     "📦",
    "기타":              "📰",
}

# 사이버보안/규정은 우선순위가 낮아 맨 아래(기타 바로 위)로 배치
CATEGORY_ORDER = ["자율운항/스마트십", "신조/조선", "탈탄소/친환경",
                  "해운시황/물류", "사이버보안/규정", "기타"]

# 전체 뉴스 카테고리별 앵커 id (네비 버튼 연결용)
CATEGORY_SLUG = {
    "자율운항/스마트십": "cat-auto",
    "사이버보안/규정":   "cat-cyber",
    "신조/조선":         "cat-ship",
    "탈탄소/친환경":     "cat-green",
    "해운시황/물류":     "cat-market",
    "기타":              "cat-etc",
}

STATUS_COLOR = {"주목": "#3b82f6", "기회": "#10b981", "위험": "#ef4444", "모니터링": "#f59e0b"}
STATUS_EN = {"주목": "Watch", "기회": "Opportunity", "위험": "Risk", "모니터링": "Monitor"}

# 고객사 영문 표기 / 구분 배지 (profile.json 의 customers, customer_groups)
CUSTOMER_EN = {}  # 하위호환용 (customer_en() 사용)


def customer_type_badge(name: str) -> tuple[str, str]:
    """고객사명 → (한글 구분 배지, 영문 구분 배지)."""
    return customer_group(name)

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
"""


def build_tech_news_html(tech_list):
    if not tech_list:
        return '<p style="color:#94a3b8;font-size:13px;padding:12px 0">이번 주 주요 기술/경쟁사 뉴스가 없습니다.</p>'
    html = ""
    for item in tech_list:
        cat = item.get("category", "")
        cat_en = CATEGORY_KO_EN.get(cat, cat)
        title_ko = item.get("title_ko") or item.get("title", "")
        title_en = item.get("title_en") or item.get("title", "")
        summary_ko = item.get("summary_ko") or item.get("summary", "")
        summary_en = item.get("summary_en") or item.get("summary", "")
        html += f"""
    <div class="tech-card">
      <div class="rank">TOP {item.get('rank','')} <span class="cat-badge lang-ko">{cat}</span><span class="cat-badge lang-en">{cat_en}</span></div>
      <div class="title lang-ko"><a href="{item.get('link','#')}" target="_blank">{title_ko}</a></div>
      <div class="title lang-en"><a href="{item.get('link','#')}" target="_blank">{title_en}</a></div>
      <div class="meta">{item.get('source','')}</div>
      <div class="summary lang-ko">{summary_ko}</div>
      <div class="summary lang-en">{summary_en}</div>
      <div class="analysis-row">
        <span class="analysis-label lang-ko" style="background:#10b981">기회</span>
        <span class="analysis-label lang-en" style="background:#10b981">Opportunity</span>
        <span class="analysis-text lang-ko">{item.get('opportunity_ko') or item.get('opportunity','')}</span>
        <span class="analysis-text lang-en">{item.get('opportunity_en') or item.get('opportunity_ko') or item.get('opportunity','')}</span>
      </div>
      <div class="analysis-row">
        <span class="analysis-label lang-ko" style="background:#ef4444">위험</span>
        <span class="analysis-label lang-en" style="background:#ef4444">Risk</span>
        <span class="analysis-text lang-ko">{item.get('risk_ko') or item.get('risk','')}</span>
        <span class="analysis-text lang-en">{item.get('risk_en') or item.get('risk_ko') or item.get('risk','')}</span>
      </div>
      <div class="analysis-row">
        <span class="analysis-label lang-ko" style="background:#3b82f6">액션</span>
        <span class="analysis-label lang-en" style="background:#3b82f6">Action</span>
        <span class="analysis-text lang-ko">{item.get('action_ko') or item.get('action','')}</span>
        <span class="analysis-text lang-en">{item.get('action_en') or item.get('action_ko') or item.get('action','')}</span>
      </div>
    </div>"""
    return html


def build_customer_news_html(customer_list):
    if not customer_list:
        return '<p style="color:#94a3b8;font-size:13px;padding:12px 0">이번 주 주요 고객사 관련 뉴스가 없습니다.</p>'
    html = ""
    for item in customer_list:
        title_ko = item.get("title_ko") or item.get("title", "")
        title_en = item.get("title_en") or item.get("title", "")
        summary_ko = item.get("summary_ko") or item.get("summary", "")
        summary_en = item.get("summary_en") or item.get("summary", "")
        cust_ko = item.get("customer", "")
        cust_en = customer_en(cust_ko)
        type_ko, type_en = customer_type_badge(cust_ko)
        type_html = (f'<span class="customer-type lang-ko">{type_ko}</span>\n'
                     f'      <span class="customer-type lang-en">{type_en}</span>') if type_ko else ""
        html += f"""
    <div class="customer-card">
      <span class="customer-name lang-ko">{cust_ko}</span>
      <span class="customer-name lang-en">{cust_en}</span>
      {type_html}
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
    if not items:
        return '<p style="color:#94a3b8;font-size:13px;padding:12px 0">이번 주 주요 경쟁사 뉴스가 없습니다.</p>'
    return "".join(
        _theme_card(a, "competitor", a.get("competitor", ""), a.get("competitor", ""))
        for a in items
    )


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


def build_all_news_html(articles):
    groups = defaultdict(list)
    for art in articles:
        groups[art.get("category", "기타")].append(art)

    html = ""
    for cat in CATEGORY_ORDER:
        arts = groups.get(cat)
        if not arts:
            continue
        # 음수 점수(순수 노이즈: 인사·인물·PR 등)는 제외
        arts = [a for a in arts if a.get("relevance_score", 0) >= 0]
        if not arts:
            continue
        # 관련성 점수 상위 N건만 노출 (근접중복 제거 후 상한 적용)
        arts = dedup_events(sorted(
            arts, key=lambda x: x.get("relevance_score", 0), reverse=True))[:MAX_PER_CATEGORY]
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
        html += f"""
      <div class="section" id="{CATEGORY_SLUG.get(cat, 'cat-etc')}">
        <div class="section-header lang-ko">{icon} {cat}</div>
        <div class="section-header lang-en">{icon} {cat_en}</div>
        <div class="article-grid">
        {items_html}
        </div>
      </div>
      <hr class="div">"""
    return html


def _title_bigrams(text):
    core = re.split(r"[…]|\.\.\.| - ", text or "")[0].lower()
    core = re.sub(r"[^0-9a-z가-힣]", "", core)
    return {core[i:i + 2] for i in range(len(core) - 1)}


def _content_bigrams(text, limit=160):
    """요약(본문) 앞부분의 문자 bigram 집합. 다매체 동일 사안 판별용."""
    core = re.sub(r"[^0-9a-z가-힣]", "", (text or "").lower())[:limit]
    return {core[i:i + 2] for i in range(len(core) - 1)}


# 고객사 동향에서 제외할 단순 사내소식/인물 프로필 마커
_CUSTOMER_SKIP = ["[인사]", "인사발령", "임원인사", "[부고]", "부고", "[동정]",
                  "동정", "신년사", "who is", "[wo is", "회장]", "취임", "승진"]


def select_customer_news(articles, per_customer=3, total=12):
    """고객사 동향 선정: 관련성 순 + 근접중복 제거 + 고객사별 최대 per_customer건
    (특정 고객사 편중 방지, 매각/인수 등 중요 소식 누락 방지, 사내소식 제외)."""
    def is_noise(a):
        t = (a.get("title_ko") or a.get("title", "")).lower()
        return any(m in t for m in _CUSTOMER_SKIP)

    cust = dedup_events(sorted(
        [a for a in articles if a.get("customer") and not is_noise(a)],
        key=lambda x: x.get("relevance_score", 0), reverse=True))
    picked = []
    for a in cust:
        if sum(1 for x in picked if x["customer"] == a["customer"]) < per_customer:
            picked.append(a)
        if len(picked) >= total:
            break
    return picked


def dedup_events(items, overlap_threshold=0.4, summary_threshold=0.6):
    """좁은 섹션(컴플라이언스/경쟁사/고객사)용 근접 중복 제거 - 같은 사건은 한 번만.
    제목 표현이 달라도 요약(본문)이 거의 같으면(다른 매체가 같은 사안을 보도) 중복으로 간주.
    정렬이 상위(관련성 높음)부터이므로 먼저 나온 기사가 유지된다."""
    kept, kept_bg, kept_sbg = [], [], []
    for a in items:
        bg = _title_bigrams(a.get("title_ko") or a.get("title", ""))
        sbg = _content_bigrams(a.get("summary_ko") or a.get("summary", ""))
        title_dup = len(bg) >= 5 and any(
            len(bg & k) / min(len(bg), len(k)) >= overlap_threshold
            for k in kept_bg if len(k) >= 5)
        summary_dup = len(sbg) >= 20 and any(
            len(sbg & k) / min(len(sbg), len(k)) >= summary_threshold
            for k in kept_sbg if len(k) >= 20)
        if title_dup or summary_dup:
            continue
        kept_bg.append(bg)
        kept_sbg.append(sbg)
        kept.append(a)
    return kept


def build_html(analysis, articles):
    today = datetime.now()
    today_ko = today.strftime("%Y년 %m월 %d일")
    today_en = today.strftime("%B %d, %Y")
    period = analysis.get("period", "")
    total = analysis.get("total_news", len(articles))

    tech_list = analysis.get("tech_news", [])
    # 고객사 동향: LLM 대신 결정론적 선정 (관련성 + 고객사별 다양성 보장)
    customer_list = select_customer_news(articles)

    # 경쟁사/컴플라이언스: 관련성 상위 정렬 → 섹션 내 근접중복 제거 → 상한 적용
    competitor_list = dedup_events(sorted(
        [a for a in articles if a.get("competitor")],
        key=lambda x: x.get("relevance_score", 0), reverse=True))[:8]
    compliance_list = dedup_events(sorted(
        [a for a in articles if a.get("compliance")],
        key=lambda x: x.get("relevance_score", 0), reverse=True))[:4]

    # 전체 뉴스 하위 카테고리 네비 버튼 (기사가 있는 카테고리만)
    present_cats = [c for c in CATEGORY_ORDER
                    if any(a.get("category", "기타") == c for a in articles)]
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
  <title>{COMPANY_NAME_KO} {BRIEFING_KO}</title>
  <style>{CSS}</style>
</head>
<body class="ko">
<div class="wrapper">

  <div class="header">
    <div class="logo">{LOGO_TEXT}</div>
    <h1 class="lang-ko">{COMPANY_NAME_KO} {BRIEFING_KO}</h1>
    <h1 class="lang-en">{COMPANY_NAME_EN} {BRIEFING_EN}</h1>
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
    <div class="card-grid-2">{build_tech_news_html(tech_list)}</div>
  </div>
  <hr class="div">

  <div class="section" id="sec-customer">
    <div class="section-header lang-ko">🏢 고객사 동향</div>
    <div class="section-header lang-en">🏢 Customer News</div>
    <div class="card-grid-2">{build_customer_news_html(customer_list)}</div>
  </div>
  <hr class="div">

  <div class="section" id="sec-compliance">
    <div class="section-header lang-ko">🌐 글로벌 컴플라이언스 (IMO·선급)</div>
    <div class="section-header lang-en">🌐 Global Compliance (IMO & Class)</div>
    <div class="card-grid-2">{build_compliance_news_html(compliance_list)}</div>
  </div>
  <hr class="div">

  <div class="section" id="sec-competitor">
    <div class="section-header lang-ko">🏁 경쟁사 동향</div>
    <div class="section-header lang-en">🏁 Competitor News</div>
    <div class="card-grid-2">{build_competitor_news_html(competitor_list)}</div>
  </div>
  <hr class="div">

  <div class="section" id="sec-all">
    <div class="section-header lang-ko">📰 이번 주 전체 뉴스</div>
    <div class="section-header lang-en">📰 All News This Week</div>
  </div>
  {build_all_news_html(articles)}

  <div class="footer">
    <span class="lang-ko">{COMPANY_NAME_KO} &nbsp;|&nbsp; 본 브리핑은 공개 RSS 피드 기반 자동 수집·AI 분석입니다.<br>문의: {CONTACT}</span>
    <span class="lang-en">{COMPANY_NAME_EN} &nbsp;|&nbsp; Auto-collected from public RSS feeds and analyzed by AI.<br>Contact: {CONTACT}</span>
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


if __name__ == "__main__":
    run()
