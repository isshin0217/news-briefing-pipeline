"""
build_email.py
Outlook 데스크톱(Word 렌더링 엔진)에서도 안 깨지는 이메일 전용 HTML 생성.
- table 기반 단일 컬럼 레이아웃, 인라인 스타일, JS/토글/그리드 없음 (한국어 고정)
- 웹 버전(3_build_html.py, weekly_newsletter_*.html)은 그대로 두고 별도 파일 생성

실행: python build_email.py
입력: weekly_analysis.json + summarized_news.json
결과: weekly_email_YYYYMMDD.html
"""

import json
import html
import importlib.util
from datetime import datetime
from pathlib import Path

# 웹 빌더의 선정 로직/상수 재사용 (파일명이 숫자로 시작해 importlib 사용)
_spec = importlib.util.spec_from_file_location("bw", str(Path(__file__).parent / "3_build_html.py"))
bw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bw)

FONT = "'Malgun Gothic','Apple SD Gothic Neo',Arial,sans-serif"


def esc(s) -> str:
    # 원문에 이미 있는 &nbsp; &amp; &#39; 등 엔티티를 먼저 풀고 다시 escape (이중 escape 방지)
    return html.escape(html.unescape(str(s or "")))


def score_sort(items):
    return sorted(items, key=lambda x: x.get("relevance_score", 0), reverse=True)


def section_header(icon_title: str) -> str:
    return (
        f'<tr><td style="padding:22px 28px 6px;font-family:{FONT};">'
        f'<div style="font-size:16px;font-weight:bold;color:#0a2342;'
        f'border-bottom:2px solid #3b82f6;padding-bottom:6px;">{esc(icon_title)}</div>'
        f'</td></tr>'
    )


def card_inner(border_color: str, content: str) -> str:
    """테두리 있는 카드 1개 (그리드 셀 안에 들어감)."""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid #e2e8f0;border-left:4px solid {border_color};">'
        f'<tr><td style="padding:12px 14px;font-family:{FONT};">{content}</td></tr></table>'
    )


def grid2(cards, vgap: int = 12) -> str:
    """카드 리스트를 2열 table로 배치 (Outlook 호환)."""
    if not cards:
        return ""
    rows = ""
    for i in range(0, len(cards), 2):
        left = cards[i]
        right = cards[i + 1] if i + 1 < len(cards) else "&nbsp;"
        rows += (
            "<tr>"
            f'<td width="50%" valign="top" style="padding:0 6px {vgap}px 0;">{left}</td>'
            f'<td width="50%" valign="top" style="padding:0 0 {vgap}px 6px;">{right}</td>'
            "</tr>"
        )
    return (
        f'<tr><td style="padding:0 28px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
        f'</td></tr>'
    )


def empty_row(msg: str) -> str:
    return (f'<tr><td style="padding:4px 28px 12px;font-family:{FONT};color:#94a3b8;'
            f'font-size:13px;">{esc(msg)}</td></tr>')


def badge(text: str, bg: str) -> str:
    return (f'<span style="display:inline-block;font-size:11px;font-weight:bold;'
            f'background:{bg};color:#ffffff;padding:2px 8px;border-radius:10px;">{esc(text)}</span>')


def title_link(item) -> str:
    t = esc(item.get("title_ko") or item.get("title", ""))
    link = esc(item.get("link", "#"))
    return (f'<div style="font-size:14px;font-weight:bold;line-height:1.4;margin:6px 0 4px;">'
            f'<a href="{link}" style="color:#1e3a5f;text-decoration:none;">'
            f'<span style="color:#1e3a5f;text-decoration:none;">{t}</span></a></div>')


def meta_line(item) -> str:
    return f'<div style="font-size:11px;color:#718096;">{esc(item.get("source",""))}</div>'


def summary_line(item, limit=220) -> str:
    s = esc((item.get("summary_ko") or item.get("summary", ""))[:limit])
    if not s:
        return ""
    return f'<div style="font-size:12px;color:#4a5568;line-height:1.65;margin-top:6px;">{s}</div>'


def analysis_row(label: str, bg: str, text: str) -> str:
    if not text:
        return ""
    return (f'<div style="font-size:12px;line-height:1.5;margin-top:6px;">'
            f'{badge(label, bg)} <span style="color:#4a5568;">{esc(text)}</span></div>')


def build_tech(tech_list) -> str:
    if not tech_list:
        return empty_row("이번 주 주요 기술/경쟁사 뉴스가 없습니다.")
    cards = []
    for it in tech_list:
        cat = it.get("category", "")
        content = (f'<div style="font-size:11px;color:#3b82f6;font-weight:bold;">'
                   f'TOP {it.get("rank","")} · {esc(cat)}</div>')
        content += title_link(it) + meta_line(it) + summary_line(it)
        content += analysis_row("기회", "#10b981", it.get("opportunity_ko") or it.get("opportunity", ""))
        content += analysis_row("위험", "#ef4444", it.get("risk_ko") or it.get("risk", ""))
        content += analysis_row("액션", "#3b82f6", it.get("action_ko") or it.get("action", ""))
        cards.append(card_inner("#3b82f6", content))
    return grid2(cards)


def build_simple(items, border_color, badge_bg, badge_text_fn, empty_msg) -> str:
    if not items:
        return empty_row(empty_msg)
    cards = []
    for it in items:
        content = f'<div>{badge(badge_text_fn(it), badge_bg)}</div>'
        content += title_link(it) + meta_line(it) + summary_line(it, 180)
        cards.append(card_inner(border_color, content))
    return grid2(cards)


def _article_block(a) -> str:
    link = esc(a.get("link", "#"))
    title = esc(a.get("title_ko") or a.get("title", ""))
    pub = esc(a.get("published", "")[:10])
    return (f'<div style="border-left:3px solid #3b82f6;padding:5px 0 5px 12px;font-family:{FONT};">'
            f'<div style="font-size:13px;font-weight:bold;line-height:1.4;">'
            f'<a href="{link}" style="color:#1e3a5f;text-decoration:none;">'
            f'<span style="color:#1e3a5f;text-decoration:none;">{title}</span></a></div>'
            f'<div style="font-size:11px;color:#718096;margin-top:2px;">{esc(a.get("source",""))} · {pub}</div>'
            f'</div>')


def build_all_news(articles) -> str:
    groups = {}
    for a in articles:
        groups.setdefault(a.get("category", "기타"), []).append(a)
    html_out = ""
    for cat in bw.CATEGORY_ORDER:
        arts = groups.get(cat)
        if not arts:
            continue
        icon = bw.CATEGORY_ICON.get(cat, "📰")
        html_out += section_header(f"{icon} {cat}")
        html_out += grid2([_article_block(a) for a in arts], vgap=10)
    return html_out


def build_email(analysis, articles) -> str:
    today = datetime.now()
    period = esc(analysis.get("period", ""))
    concl = esc(analysis.get("one_line_conclusion_ko") or analysis.get("one_line_conclusion", ""))

    tech_list = analysis.get("tech_news", [])
    customer_list = bw.select_customer_news(articles)
    competitor_list = bw.dedup_events(score_sort([a for a in articles if a.get("competitor")]))[:8]
    compliance_list = bw.dedup_events(score_sort([a for a in articles if a.get("compliance")]))[:4]
    total = analysis.get("total_news", len(articles))

    parts = []
    # 헤더
    parts.append(
        f'<tr><td style="background:#0a2342;padding:24px 28px 18px;font-family:{FONT};color:#ffffff;">'
        f'<div style="font-size:20px;font-weight:bold;letter-spacing:1px;">{esc(bw.LOGO_TEXT)}</div>'
        f'<div style="font-size:14px;color:#dbe7f5;margin-top:6px;">{esc(bw.COMPANY_NAME_KO)} {esc(bw.BRIEFING_KO)}</div>'
        f'<div style="font-size:12px;color:#93c5fd;margin-top:4px;">📅 {esc(today.strftime("%Y년 %m월 %d일"))} 발행 &nbsp;|&nbsp; 수집 기간: {period}</div>'
        f'</td></tr>'
    )
    # 한 줄 결론
    if concl:
        parts.append(
            f'<tr><td style="background:#0a2342;border-top:1px solid #1a4a7a;padding:14px 28px;font-family:{FONT};color:#ffffff;">'
            f'<div style="font-size:11px;color:#93c5fd;font-weight:bold;letter-spacing:1px;">이번 주 한 줄 결론</div>'
            f'<div style="font-size:14px;line-height:1.6;margin-top:5px;">{concl}</div>'
            f'</td></tr>'
        )
    # 요약 바
    parts.append(
        f'<tr><td style="background:#e8f0fe;padding:10px 28px;font-family:{FONT};font-size:12px;color:#2d5a9e;">'
        f'수집 뉴스 <b>{total}</b>건 &nbsp;|&nbsp; 기술 <b>{len(tech_list)}</b> &nbsp;|&nbsp; '
        f'고객사 <b>{len(customer_list)}</b> &nbsp;|&nbsp; 경쟁사 <b>{len(competitor_list)}</b> &nbsp;|&nbsp; '
        f'컴플라이언스 <b>{len(compliance_list)}</b>'
        f'</td></tr>'
    )
    # 브라우저 전체보기 안내
    parts.append(
        f'<tr><td style="background:#fff7ed;border-top:1px solid #fed7aa;padding:9px 28px;'
        f'font-family:{FONT};font-size:12px;color:#9a3412;">'
        f'📎 <b>첨부된 HTML 파일</b>을 열면 주제 바로가기·한/영 전환까지 전체 기능으로 보실 수 있습니다.'
        f'</td></tr>'
    )

    # 섹션들
    parts.append(section_header("🔬 기술 동향"))
    parts.append(build_tech(tech_list))

    parts.append(section_header("🏢 고객사 동향"))
    parts.append(build_simple(customer_list, "#f59e0b", "#f59e0b",
                              lambda it: it.get("customer", ""),
                              "이번 주 주요 고객사 관련 뉴스가 없습니다."))

    parts.append(section_header("🌐 글로벌 컴플라이언스"))
    parts.append(build_simple(compliance_list, "#0ea5e9", "#0ea5e9",
                              lambda it: "🌐 글로벌 규제", "이번 주 주요 규제·컴플라이언스 뉴스가 없습니다."))

    parts.append(section_header("🏁 경쟁사 동향"))
    parts.append(build_simple(competitor_list, "#8b5cf6", "#8b5cf6",
                              lambda it: it.get("competitor",""), "이번 주 주요 경쟁사 뉴스가 없습니다."))

    parts.append(section_header("📰 이번 주 전체 뉴스"))
    parts.append(build_all_news(articles))

    # 푸터
    parts.append(
        f'<tr><td style="background:#f7fafc;border-top:1px solid #e2e8f0;padding:18px 28px;'
        f'font-family:{FONT};font-size:11px;color:#a0aec0;line-height:1.7;text-align:center;">'
        f'{esc(bw.COMPANY_NAME_KO)} &nbsp;|&nbsp; 공개 RSS·네이버/구글 뉴스 기반 자동 수집·AI 분석<br>'
        f'문의: {esc(bw.CONTACT)}</td></tr>'
    )

    inner = "".join(parts)
    return (
        f'<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>{esc(bw.COMPANY_NAME_KO)} {esc(bw.BRIEFING_KO)}</title></head>'
        f'<body style="margin:0;padding:0;background:#f0f4f8;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f0f4f8;"><tr><td align="center" style="padding:16px 8px;">'
        f'<table role="presentation" width="820" cellpadding="0" cellspacing="0" '
        f'style="width:820px;max-width:820px;background:#ffffff;">'
        f'{inner}'
        f'</table></td></tr></table></body></html>'
    )


def run():
    ap, npth = Path("weekly_analysis.json"), Path("summarized_news.json")
    if not ap.exists() or not npth.exists():
        print("weekly_analysis.json / summarized_news.json 이 필요합니다. 앞 단계를 먼저 실행하세요.")
        return
    analysis = json.loads(ap.read_text(encoding="utf-8"))
    articles = json.loads(npth.read_text(encoding="utf-8"))
    html_out = build_email(analysis, articles)
    fname = f"weekly_email_{datetime.now():%Y%m%d}.html"
    Path(fname).write_text(html_out, encoding="utf-8")
    size_kb = len(html_out.encode("utf-8")) / 1024
    print(f"[이메일용 HTML 생성 완료] → {fname}  ({size_kb:.0f} KB)")
    if size_kb > 100:
        print("  주의: 100KB 초과 → Gmail에서 '메시지 생략'될 수 있습니다.")


if __name__ == "__main__":
    run()
