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

import issue_digest as idg

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


def analysis_row(label: str, bg: str, text: str) -> str:
    if not text:
        return ""
    return (f'<div style="font-size:12px;line-height:1.5;margin-top:6px;">'
            f'{badge(label, bg)} <span style="color:#4a5568;">{esc(text)}</span></div>')


def build_tech(tech_list, bullets=None) -> str:
    """TOP 카드: [TOP n] → [제목] → [불릿 요약] → [의의 및 시사점] (2026-09-07 피드백).
    불릿이 없으면 예전 통문장 요약으로 되돌아간다."""
    if not tech_list:
        return empty_row("이번 주 주요 기술/경쟁사 뉴스가 없습니다.")
    bullets = bullets or {}
    cards = []
    for it in tech_list:
        cat = it.get("category", "")
        # 아크메가 직접 언급된 기사는 전용 배지로 구분 (3_build_html 의 own_badge 와 같은 색).
        # 2026-09-07: 웹 빌더에만 있고 메일 빌더에 없어서, 팀이 받는 메일에는 배지가 빠졌다.
        own_badge = f' {badge("아크메", "#0a2342")}' if it.get("own") else ""
        content = (f'<div style="font-size:11px;color:#3b82f6;font-weight:bold;">'
                   f'TOP {it.get("rank","")} · {esc(cat)}{own_badge}</div>')
        ko = bullets.get(it.get("link"), {}).get("ko", [])
        # 불릿이 없을 때 기사 요약(summary_ko)으로 되돌아가지 않는다 (2026-10-06 피드백). summary_ko 는 네이버·RSS
        # 스니펫이라 본문 한가운데서 잘린 문장이다('거제조선소의 제3도크는 길이 640m…').
        # 불릿이 없으면 제목 + 의의 및 시사점만 싣는다.
        body = ('<ul style="margin:6px 0 0;padding-left:17px;">' + "".join(
            f'<li style="font-size:13px;color:#4a5568;line-height:1.7;">{esc(b)}</li>'
            for b in ko) + "</ul>") if ko else ""
        content += title_link(it) + meta_line(it) + body
        # 기회/위험/시사점 3분할 → '의의 및 시사점' 한 항목 (구형 키도 받아준다)
        content += analysis_row("의의 및 시사점", "#3b82f6",
                                it.get("insight_ko") or it.get("action_ko")
                                or it.get("opportunity_ko", ""))
        cards.append(card_inner("#3b82f6", content))
    return grid2(cards)


def build_thumb_cards(items, empty_msg) -> str:
    """경쟁사 동향: 썸네일 + 태그 + 제목 카드 2열 (2026-09-07 피드백).

    메일에서 외부 이미지는 수신자가 '이미지 표시'를 누르기 전까지 막힌다. 그래서
    width 를 명시해 차단 상태에서도 칸이 무너지지 않게 한다.

    2026-09-17 변경: 이미지가 없는 기사는 자리를 통째로 비웠었는데("빈 사각형보다
    낫다"), 2열 그리드에서 옆칸만 사진이 있으면 높이가 어긋나 오히려 빠뜨린 것처럼
    보인다는 지적을 받았다(2026-09-14 발송분). 같은 규격의 회색 상자로 바꾼다.
    """
    if not items:
        return empty_row(empty_msg)
    cards = []
    for a in items:
        image = a.get("image") or ""
        thumb = _thumb_img(image, icon="🏁")
        tag = esc(a.get("competitor", ""))
        pub = esc((a.get("published", "") or "")[:10])
        cards.append(
            f'<a href="{esc(a.get("link", "#"))}" style="display:block;'
            f'text-decoration:none;color:#1e3a5f;font-family:{FONT};">{thumb}'
            f'<span style="display:inline-block;font-size:11px;font-weight:bold;'
            f'color:#4a5568;background:#edf2f7;border-radius:4px;padding:2px 8px;'
            f'margin-bottom:6px;">{tag}</span>'
            f'<div style="font-size:14px;font-weight:bold;color:#1e3a5f;'
            f'line-height:1.45;">{esc(a.get("title_ko") or a.get("title", ""))}</div>'
            f'<div style="font-size:11px;color:#718096;margin-top:5px;">'
            f'{esc(a.get("source", ""))} · {pub}</div></a>'
        )
    return grid2(cards, vgap=20)


# 메일 썸네일 규격. 웹은 CSS aspect-ratio 로 16:9 를 맞추지만 메일에는 그 속성이
# 없다(Outlook 은 object-fit 도 무시한다). 그래서 width/height 를 픽셀로 못 박아
# 카드마다 사진 높이가 들쭉날쭉하던 것을 없앤다 — 2026-09-07 피드백.
# object-fit 은 Gmail·Apple Mail 에서만 먹어 거기서는 잘라서 채우고, Outlook 은
# 같은 상자에 맞춰 늘린다. 어느 쪽이든 상자 크기는 모든 카드가 같다.
THUMB_W, THUMB_H = 360, 202          # 16:9


def _thumb_img(image: str, icon: str = "📰") -> str:
    if not image:
        return _thumb_placeholder(icon)
    return (f'<img src="{esc(image)}" width="{THUMB_W}" height="{THUMB_H}" alt="" '
            f'style="width:100%;max-width:{THUMB_W}px;height:{THUMB_H}px;'
            f'object-fit:cover;display:block;border:0;border-radius:6px;'
            f'margin-bottom:9px;">')


def _thumb_placeholder(icon: str = "📰") -> str:
    """사진을 못 구한 카드의 자리맞춤 상자. 웹 빌더의 .thumb-none 과 같은 역할.

    table 로 만든다 — Outlook 은 div 의 height 를 자주 무시하는데, 그러면 상자가
    납작해져 옆칸과 다시 어긋난다. height 속성과 스타일을 둘 다 준 이유도 같다.
    """
    return (f'<table role="presentation" width="{THUMB_W}" height="{THUMB_H}" '
            f'cellpadding="0" cellspacing="0" border="0" '
            f'style="width:100%;max-width:{THUMB_W}px;height:{THUMB_H}px;'
            f'background:#eef2f7;border-radius:6px;margin-bottom:9px;">'
            f'<tr><td align="center" valign="middle" height="{THUMB_H}" '
            f'style="height:{THUMB_H}px;font-size:30px;color:#c2ccd9;'
            f'font-family:{FONT};line-height:1;">{icon}</td></tr></table>')


def build_issue_thumb_cards(issues, by_link, empty_msg, icon: str = "🏢") -> str:
    """고객사 동향: 사진 + 태그 + 한 줄 요약 카드 2열 (2026-09-07 피드백).

    확장(펼치기)은 넣지 않는다 — 메일에서 동작하지 않아서, 첨부 HTML 몫이다.
    기사 제목과 불릿은 첨부에서 '자세히 보기'를 누르면 나온다.
    """
    if not issues:
        return empty_row(empty_msg)
    cards = []
    for issue in issues:
        arts = [by_link[l] for l in issue.get("links", []) if l in by_link]
        if not arts:
            continue
        image = next((a["image"] for a in arts if a.get("image")), "")
        thumb = _thumb_img(image, icon=icon)
        subject = esc(issue.get("subject_ko", ""))
        tag = (f'<span style="display:inline-block;font-size:11px;font-weight:bold;'
               f'color:#4a5568;background:#edf2f7;border-radius:4px;padding:2px 8px;'
               f'margin-bottom:6px;">{subject}</span>' if subject else "")
        count = f' ({len(arts)}건)' if len(arts) > 1 else ""
        cards.append(
            f'<a href="{esc(arts[0].get("link", "#"))}" style="display:block;'
            f'text-decoration:none;color:#1e3a5f;font-family:{FONT};">{thumb}{tag}'
            f'<div style="font-size:14px;font-weight:bold;color:#1e3a5f;'
            f'line-height:1.45;">{esc(issue.get("title_ko", ""))}{count}</div></a>'
        )
    return grid2(cards, vgap=20)


def build_simple(items, border_color, badge_bg, badge_text_fn, empty_msg,
                 thumb_icon: str = "") -> str:
    """배지 + 제목 + 요약 카드. 이슈 생성이 실패했을 때 쓰는 폴백이다.

    thumb_icon 을 주면 카드 맨 위에 사진을 넣는다 (2026-09-21 피드백). 그 회차에 사진이 빠진 원인은 이슈 생성 실패였지만
    (issue_digest 에서 고쳤다), 폴백 카드에 사진이 아예 없던 것도 함께 고친다 —
    웹 빌더의 build_customer_news_html 과 같은 조치다.
    """
    if not items:
        return empty_row(empty_msg)
    cards = []
    for it in items:
        content = (_thumb_img(it.get("image") or "", icon=thumb_icon)
                   if thumb_icon else "")
        content += f'<div>{badge(badge_text_fn(it), badge_bg)}</div>'
        # 요약 스니펫은 싣지 않는다 (build_tech 의 2026-10-06 주석과 같은 이유)
        content += title_link(it) + meta_line(it)
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


# ── 이슈 (2026-09-07 피드백) ────────────────────────────────────────────
# 메일 본문은 '한 줄 요약'만 싣는다. 기사 제목·출처·불릿을 펼쳐 실었더니
# "본문에서 다 펼쳐져 있으니까 너무 길어"라는 피드백이 왔다(48KB → 화면 열 몇 개).
# 접었다 펴는 건 메일에서 안 되므로(Outlook·Gmail 이 JS 와 <details> 를 걷어낸다)
# 상세는 첨부 HTML 몫이고, 메일은 훑어보는 목록으로 둔다.


def build_issue_lines(issues, by_link, wrap: bool = True) -> str:
    """전체 뉴스: 이슈 한 줄씩만 (2026-09-07 피드백).

    카드 섹션과 달리 전체 뉴스는 이슈가 25개 넘게 쌓인다. 거기에 기사 제목과 불릿까지
    펼치면 메일이 화면 열 몇 개 분량이 된다. 메일 본문은 한 줄 목록으로 훑게 하고,
    기사 제목·불릿은 접었다 펼 수 있는 첨부 HTML 몫으로 둔다.

    2026-10-06: 한 줄씩을 <a style="display:block"> 로 만들었더니 Outlook 데스크톱
    (Word 엔진)이 display:block 을 무시해 <a> 가 인라인으로 남았다. 그래서 줄바꿈 없이
    한 문단으로 이어 붙고 왼쪽 회색 선·간격도 사라졌다(웹메일에서는 멀쩡해 보였다).
    줄마다 table 행으로 만들고 <a> 는 글자에만 건다.
    """
    rows = ""
    for issue in issues:
        arts = [by_link[l] for l in issue.get("links", []) if l in by_link]
        if not arts:
            continue
        subject = esc(issue.get("subject_ko", ""))
        head = f'<b>[{subject}]</b> ' if subject else ""
        count = (f' <span style="color:#718096;">({len(arts)}건)</span>'
                 if len(arts) > 1 else "")
        rows += (
            f'<tr><td style="padding:0 0 4px 0;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td style="border-left:2px solid #cbd5e0;padding:4px 0 4px 11px;'
            f'font-family:{FONT};font-size:13px;line-height:1.55;color:#2d3748;">'
            f'<a href="{esc(arts[0].get("link", "#"))}" '
            f'style="text-decoration:none;color:#2d3748;">'
            f'{head}{esc(issue.get("title_ko", ""))}{count}</a>'
            f'</td></tr></table></td></tr>'
        )
    if rows:
        rows = (f'<table role="presentation" width="100%" cellpadding="0" '
                f'cellspacing="0">{rows}</table>')
    if not rows or not wrap:
        # wrap=False 는 2열 셀 안에 넣을 때. <tr> 로 감싸면 안 된다.
        return rows
    return (f'<tr><td style="padding:0 28px 8px;font-family:{FONT};">{rows}</td></tr>')


def build_all_news_issues(all_news, issues_by_group, by_link) -> str:
    """전체 뉴스 카테고리를 2열로 (2026-09-07 피드백).

    카테고리마다 전폭 한 줄씩 쓰면 세로로 길어진다. 한 줄 요약이라 폭이 남아
    두 칸으로 접는다 — 웹의 .cat-grid 와 같은 배치다.
    """
    cells = []
    for cat, arts in all_news:
        cat_issues = issues_by_group.get(cat, [])
        body = (build_issue_lines(cat_issues, by_link, wrap=False) if cat_issues
                else "".join(_article_block(a) for a in arts))
        cells.append(
            f'<div style="font-family:{FONT};">'
            f'<div style="font-size:14px;font-weight:bold;color:#0a2342;'
            f'border-bottom:2px solid #3b82f6;padding-bottom:5px;margin-bottom:9px;">'
            f'{esc(bw.CATEGORY_ICON.get(cat, "📰"))} {esc(cat)}</div>{body}</div>'
        )
    return grid2(cells, vgap=18)




def build_email(analysis, articles) -> str:
    today = datetime.now()
    period = esc(analysis.get("period", ""))
    concl = esc(analysis.get("one_line_conclusion_ko") or analysis.get("one_line_conclusion", ""))

    # 실릴 기사 집합은 웹 빌더와 같은 함수에서 받는다 (두 빌더가 갈라지지 않도록)
    lists = bw.section_lists(analysis, articles)
    tech_list = lists["tech"]
    customer_list = lists["customer"]
    competitor_list = lists["competitor"]
    compliance_list = lists["compliance"]
    total = analysis.get("total_news", len(articles))

    issues_by_group, bullets = idg.index(analysis)
    by_link = {a.get("link"): a for a in articles if a.get("link")}
    # tech_news 는 weekly_analysis 에서 오지만 불릿은 summarized_news 의 link 로 찾는다.
    # 같은 link 를 쓰므로 그대로 맞는다(merge_metadata 가 링크를 우리 데이터에서 채운다).

    parts = []
    # 헤더
    parts.append(
        f'<tr><td style="background:#0a2342;padding:24px 28px 18px;font-family:{FONT};color:#ffffff;">'
        f'<div style="font-size:20px;font-weight:bold;letter-spacing:1px;">⚓ Acme Marine</div>'
        f'<div style="font-size:14px;color:#dbe7f5;margin-top:6px;">아크메 기술개발팀 주간 해사 브리핑</div>'
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
        f'📎 <b>첨부된 HTML 파일</b>을 열면 기사별 상세 요약 펼쳐보기·주제 바로가기·한/영 전환까지 전체 기능으로 보실 수 있습니다.'
        f'</td></tr>'
    )

    # 섹션들
    parts.append(section_header("🔬 기술 동향"))
    parts.append(build_tech(tech_list, bullets))

    parts.append(section_header("🏢 고객사 동향"))
    customer_issues = issues_by_group.get(bw.CUSTOMER_GROUP, [])
    parts.append(build_issue_thumb_cards(customer_issues, by_link,
                                         "이번 주 주요 고객사 관련 뉴스가 없습니다.")
                 if customer_issues
                 else build_simple(customer_list, "#f59e0b", "#f59e0b",
                                   lambda it: it.get("customer", ""),
                                   "이번 주 주요 고객사 관련 뉴스가 없습니다.",
                                   thumb_icon="🏢"))

    parts.append(section_header("🌐 글로벌 컴플라이언스 (IMO·선급)"))
    # 메일 본문은 한 줄 요약만 (2026-09-07 피드백: 전체 뉴스와 같은 이유로 길어진다)
    compliance_issues = issues_by_group.get(bw.COMPLIANCE_GROUP, [])
    # 이슈 생성이 실패해도 한 줄 목록 모양은 유지한다 (2026-10-06 피드백: 폴백 카드가
    # '🌐 글로벌 규제' 배지 + 요약 스니펫으로 나가 "기사 본문이 끼어들었다"는 지적).
    # 기사 하나를 1건짜리 이슈로 보고 같은 함수로 그린다.
    if not compliance_issues:
        compliance_issues = [{"links": [a.get("link")], "subject_ko": idg.guess_subject(a),
                              "title_ko": a.get("title_ko") or a.get("title", "")}
                             for a in compliance_list if a.get("link")]
        by_link = {**by_link, **{a.get("link"): a for a in compliance_list if a.get("link")}}
    parts.append(build_issue_lines(compliance_issues, by_link) if compliance_issues
                 else empty_row("이번 주 주요 규제·컴플라이언스 뉴스가 없습니다."))

    parts.append(section_header("🏁 경쟁사 동향"))
    competitor_issues = issues_by_group.get(bw.COMPETITOR_GROUP, [])
    parts.append(build_issue_thumb_cards(competitor_issues, by_link,
                                         "이번 주 주요 경쟁사 뉴스가 없습니다.",
                                         icon="🏁")
                 if competitor_issues
                 else build_thumb_cards(competitor_list,
                                        "이번 주 주요 경쟁사 뉴스가 없습니다."))

    parts.append(section_header("📰 이번 주 전체 뉴스"))
    parts.append(build_all_news_issues(lists["all_news"], issues_by_group, by_link))

    # 푸터
    parts.append(
        f'<tr><td style="background:#f7fafc;border-top:1px solid #e2e8f0;padding:18px 28px;'
        f'font-family:{FONT};font-size:11px;color:#a0aec0;line-height:1.7;text-align:center;">'
        f'아크메 기술개발팀 &nbsp;|&nbsp; 공개 RSS·네이버/구글 뉴스 기반 자동 수집·AI 분석<br>'
        f'문의: newsletter@example.com</td></tr>'
    )

    inner = "".join(parts)
    return (
        f'<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>아크메 주간 해사 브리핑</title></head>'
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
    bw.archive_build(fname)          # 빌드마다 시각별 사본 (3_build_html.archive_build 참고)
    if size_kb > 100:
        print("  주의: 100KB 초과 → Gmail에서 '메시지 생략'될 수 있습니다.")


if __name__ == "__main__":
    run()
