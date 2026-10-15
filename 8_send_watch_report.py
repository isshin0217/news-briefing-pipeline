"""
8_send_watch_report.py
자가진단 리포트를 운영자에게만 보낸다 — 매주, 판단할 거리만 모아서.

왜 별도 메일인가:
  7_watch 의 결과는 두 갈래로 나간다. 미리보기 메일 맨 위의 배너(watch_banner.html)는
  '이번 뉴스레터를 그대로 보내도 되나'를 75분 안에 판단하라는 것이고, 짧아야 한다.
  반면 LLM 지적을 케이스로 승격할지는 뉴스레터 발송과 시한이 다른 결정이라,
  근거·링크·명령어가 다 붙은 별도 메일이어야 한다. 배너에 다 넣으면 둘 다 안 읽힌다.

  실측(2026-09-01): LLM 지적은 리포트에 '참고' 한 줄로 남고 다음 주면 사라졌다.
  8/31 사고와 같은 구조다 — 감지는 되는데 사람에게 전달되지 않는다.

수신자:
  WATCH_REPORT_TO 환경변수뿐이다. EMAIL_RECIPIENTS 는 일부러 보지 않는다 —
  09:00 팀 발송 때 그 값은 팀 전체이고, 이 메일은 내부 진단이라 팀에 나가면 안 된다.
  값이 없으면 조용히 건너뛴다(파이프라인을 실패시키지 않는다).
  weekly_send.bat 이 NEWSLETTER_DIAGNOSTICS=1 인 실행(운영자 07:45)에서만 부른다.

실행:
  python 8_send_watch_report.py           # 화면에만 출력 (DRY RUN)
  python 8_send_watch_report.py --yes     # 실제 발송
"""

import html
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPORT_JSON = Path("watch_report.json")
CANDIDATES = Path("watch_candidates.json")
CASES = Path("watch_cases.json")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASSWORD", "")
SENDER_NAME = "아크메 뉴스레터 자가진단"

# 배너와 같은 기준(4_send_email.diagnostics_banner 주석 참고). 파이프라인이 중간에
# 죽어 7_watch 가 안 돌면 지난주 파일이 남는데, 그걸 '이번 주 결과'로 보내면 안 된다.
MAX_AGE_HOURS = 12

LEVEL_COLOR = {"심각": "#C0392B", "주의": "#B7791F", "참고": "#5D6B7C"}


def recipients() -> list[str]:
    return [e.strip() for e in os.getenv("WATCH_REPORT_TO", "").split(",") if e.strip()]


def load_report():
    if not REPORT_JSON.exists():
        print(f"  [건너뜀] {REPORT_JSON} 가 없습니다 (7_watch.py 가 돌지 않았습니다)")
        return None
    data = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    try:
        age = datetime.now() - datetime.fromisoformat(data.get("generated", ""))
    except ValueError:
        print(f"  [건너뜀] {REPORT_JSON} 의 생성 시각을 읽지 못했습니다")
        return None
    if age.total_seconds() > MAX_AGE_HOURS * 3600:
        print(f"  [건너뜀] 자가진단 결과가 {age.total_seconds() / 3600:.0f}시간 전 것입니다"
              " — 이번 실행에서 7_watch 가 돌지 않았을 수 있습니다")
        return None
    return data


def pending_candidates() -> list[tuple[int, dict]]:
    """(번호, 항목) 목록. 번호는 --promote 에 그대로 쓰는 대기 목록 순번이다."""
    if not CANDIDATES.exists():
        return []
    try:
        ledger = json.loads(CANDIDATES.read_text(encoding="utf-8")).get("candidates", [])
    except json.JSONDecodeError:
        return []
    return [(i, c) for i, c in enumerate(ledger, 1) if c.get("status") == "대기"]


def todo_cases() -> list[dict]:
    """승격했지만 아직 거르는 규칙을 안 만든 케이스."""
    if not CASES.exists():
        return []
    cases = json.loads(CASES.read_text(encoding="utf-8")).get("cases", [])
    return [c for c in cases if c.get("todo")]


def esc(text) -> str:
    return html.escape(str(text or ""))


def build_html(data: dict, pending: list, todos: list) -> str:
    counts = data.get("counts", {})
    generated = data.get("generated", "")[:16].replace("T", " ")

    parts = [
        '<div style="font-family:Malgun Gothic,sans-serif;font-size:14px;'
        'color:#25313F;max-width:780px">',
        f'<h2 style="font-size:17px;margin:0 0 4px 0">주간 자가진단 · {generated}</h2>',
        f'<div style="color:#5D6B7C;font-size:12px;margin-bottom:18px">'
        f'기사 {data.get("articles", 0)}건 검사 · '
        f'심각 {counts.get("심각", 0)} / 주의 {counts.get("주의", 0)} / '
        f'참고 {counts.get("참고", 0)} · 승인 대기 {len(pending)}건</div>',
    ]

    # ── 판단이 필요한 것을 맨 위에 둔다. 이 메일의 목적이다.
    parts.append('<h3 style="font-size:15px;margin:0 0 8px 0;padding-bottom:4px;'
                 'border-bottom:2px solid #25313F">승격 여부 판단 대기</h3>')
    if not pending:
        parts.append('<p style="color:#5D6B7C;margin:0 0 20px 0">'
                     '이번 주에 새로 판단할 지적이 없습니다.</p>')
    else:
        parts.append('<p style="color:#5D6B7C;font-size:12px;margin:0 0 10px 0">'
                     'LLM 검수가 지적한 기사입니다. 승격하면 다음 회차부터 회귀 테스트가 '
                     '이 기사를 매번 다시 검사합니다. 지금은 아무 효력이 없습니다.</p>')
        for num, entry in pending:
            seen = entry.get("seen", 1)
            repeat = (f' <span style="color:#B7791F;font-weight:bold">{seen}회 반복</span>'
                      if seen >= 2 else "")
            link = entry.get("link", "")
            title = esc(entry.get("title", ""))
            parts.append(
                f'<div style="border-left:3px solid #B7791F;background:#FEF6E7;'
                f'padding:10px 14px;margin:0 0 10px 0">'
                f'<div style="font-size:12px;color:#7A6224">[{num}] '
                f'{esc(entry.get("kind", "?"))} · {esc(entry.get("first_seen", ""))}'
                f' 최초{repeat}</div>'
                f'<div style="font-weight:bold;margin:3px 0">'
                + (f'<a href="{esc(link)}" style="color:#25313F">{title}</a>'
                   if link else title) +
                f'</div>'
                f'<div style="color:#5D6B7C;font-size:13px">'
                f'{esc(entry.get("reason", ""))}</div>'
                f'<div style="font-family:Consolas,monospace;font-size:12px;'
                f'color:#3A4A5C;margin-top:6px">'
                f'승격: python 7_watch.py --promote {num}'
                f' &nbsp;|&nbsp; 반려: python 7_watch.py --reject {num}</div>'
                f'</div>')

    # ── 승격했는데 아직 규칙이 없는 것 (사람의 밀린 할 일)
    if todos:
        parts.append('<h3 style="font-size:15px;margin:22px 0 8px 0;padding-bottom:4px;'
                     'border-bottom:2px solid #25313F">승격했지만 아직 안 걸러짐</h3>')
        for case in todos:
            parts.append(
                f'<div style="margin:0 0 8px 0">'
                f'<div style="font-weight:bold">{esc(case.get("title", ""))}</div>'
                f'<div style="color:#5D6B7C;font-size:12px">'
                f'{esc(case.get("why", ""))}</div></div>')
        parts.append('<div style="color:#5D6B7C;font-size:12px">'
                     '1_fetch_news.py 를 손봐 통과시킨 뒤 watch_cases.json 에서 '
                     '"todo" 를 지우면 보통 회귀 케이스가 됩니다.</div>')

    # ── 이번 주 진단 전체 (배너에는 첫 줄만 나가므로 여기에 전문을 싣는다)
    parts.append('<h3 style="font-size:15px;margin:22px 0 8px 0;padding-bottom:4px;'
                 'border-bottom:2px solid #25313F">이번 주 진단</h3>')
    items = data.get("items", [])
    if not items:
        parts.append('<p style="color:#1A7F4B;margin:0">문제 없음 — '
                     '규칙 점검과 회귀 테스트를 모두 통과했습니다.</p>')
    for item in items:
        level = item.get("level", "참고")
        color = LEVEL_COLOR.get(level, "#5D6B7C")
        detail = esc(item.get("detail", "")).replace("\n", "<br>")
        parts.append(
            f'<div style="margin:0 0 10px 0">'
            f'<span style="color:{color};font-weight:bold">[{esc(level)}]</span> '
            f'{esc(item.get("check", ""))}'
            f'<div style="color:#5D6B7C;font-size:13px;margin-left:4px">{detail}</div>'
            f'</div>')

    parts.append(
        '<div style="color:#7A8797;font-size:11px;margin-top:22px;'
        'border-top:1px solid #DCE3EA;padding-top:8px">'
        '서버의 뉴스레터 폴더에서 실행합니다 · '
        'python 7_watch.py --candidates (대기 목록 보기)<br>'
        '이 메일은 운영자에게만 갑니다. 팀 발송본에는 포함되지 않습니다.</div></div>')
    return "".join(parts)


def print_summary(data: dict, pending: list, todos: list, to: list[str]) -> None:
    counts = data.get("counts", {})
    print(f"  수신: {to or '(WATCH_REPORT_TO 미설정)'}")
    print(f"  심각 {counts.get('심각', 0)} / 주의 {counts.get('주의', 0)} /"
          f" 참고 {counts.get('참고', 0)} · 승인 대기 {len(pending)}건"
          f" · 미해결 승격 {len(todos)}건")
    for num, entry in pending:
        print(f"    [{num}] {entry.get('kind', '?')} {entry.get('seen', 1)}회 "
              f"| {entry.get('title', '')[:52]}")


def main() -> int:
    data = load_report()
    if data is None:
        return 0                      # 진단이 없다고 파이프라인을 실패시키지 않는다

    pending = pending_candidates()
    todos = todo_cases()
    to = recipients()

    counts = data.get("counts", {})
    today = datetime.now().strftime("%Y-%m-%d")
    head = []
    if pending:
        head.append(f"승인 대기 {len(pending)}건")
    if counts.get("심각"):
        head.append(f"심각 {counts['심각']}건")
    if counts.get("주의"):
        head.append(f"주의 {counts['주의']}건")
    subject = f"[뉴스레터 자가진단] {today} — " + (" / ".join(head) or "이상 없음")

    print(f"\n[8단계] 자가진단 리포트 — {subject}")
    print_summary(data, pending, todos, to)

    if "--yes" not in sys.argv:
        print("\n  [DRY RUN] 실제 발송 안 함. 보내려면: "
              "python 8_send_watch_report.py --yes")
        return 0

    if not to:
        print("  [건너뜀] WATCH_REPORT_TO 가 없어 보내지 않습니다 "
              "(weekly_send_me.bat 에서 지정합니다)")
        return 0
    if not SMTP_USER or not SMTP_PASS:
        print("  [건너뜀] SMTP 계정이 없어 보내지 않습니다 (.env)")
        return 0

    msg = MIMEText(build_html(data, pending, todos), "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, SMTP_USER))
    msg["To"] = ", ".join(to)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to, msg.as_string())
    except Exception as exc:
        # 이 메일이 실패해도 뉴스레터 발송은 계속돼야 한다.
        print(f"  [실패] 자가진단 리포트 발송 실패: {type(exc).__name__}: {exc}")
        return 0
    print(f"  발송 완료 → {', '.join(to)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
