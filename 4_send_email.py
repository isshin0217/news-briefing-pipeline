"""
4_send_email.py
생성된 HTML 뉴스레터를 이메일로 발송

■ 방법 A) Outlook 데스크톱 앱 이용 (회사 이메일 추천, SMTP 설정 불필요)
  python 4_send_email.py --outlook          # Outlook에 초안 열기 (검토 후 직접 보내기)
  python 4_send_email.py --outlook --yes    # Outlook으로 즉시 발송
  (Outlook 데스크톱이 회사 계정으로 로그인돼 있어야 함. pip install pywin32 필요)

■ 방법 B) SMTP 직접 발송 (Gmail/사내 SMTP 등)
  python 4_send_email.py                    # DRY RUN (미리보기)
  python 4_send_email.py --yes              # 실제 발송
  .env 필요: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

■ 공통: 수신자는 .env 의 EMAIL_RECIPIENTS=a@x.com,b@y.com (쉼표 구분)

■ --require-today : 오늘 날짜로 만든 뉴스레터가 아니면 발송하지 않고 종료(exit 3)
  빌드 없이 '이미 만들어 둔 파일만 보내는' 실행에서 쓴다. 앞선 빌드가 실패했을 때
  지난주 파일이 최신으로 잡혀 그대로 나가는 것을 막는다.
"""

import os
import sys
import smtplib
import glob
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASSWORD", "")

SENDER_NAME = "아크메 기술개발부"
SENDER_EMAIL = SMTP_USER

# 수신자: .env의 EMAIL_RECIPIENTS (쉼표 구분)
RECIPIENTS = [e.strip() for e in os.getenv("EMAIL_RECIPIENTS", "").split(",") if e.strip()]


def creds_ok() -> bool:
    """SMTP 계정이 실제 값으로 채워졌는지 확인 (플레이스홀더 감지)."""
    if not SMTP_USER or "@" not in SMTP_USER or SMTP_USER.startswith("실제"):
        return False
    if not SMTP_PASS or SMTP_PASS.startswith("실제"):
        return False
    return True


def find_latest_newsletter() -> Path | None:
    # 이메일 전용(Outlook 안전) 버전을 우선 사용, 없으면 웹 버전
    for pattern in ("weekly_email_*.html", "weekly_newsletter_*.html", "newsletter_*.html"):
        files = glob.glob(pattern)
        if files:
            return Path(max(files, key=lambda f: Path(f).stat().st_mtime))
    return None


def find_latest_web_version() -> Path | None:
    """풀기능 웹버전(첨부용) 탐색."""
    files = glob.glob("weekly_newsletter_*.html")
    return Path(max(files, key=lambda f: Path(f).stat().st_mtime)) if files else None


BANNER_PATH = Path("watch_banner.html")


def diagnostics_banner() -> str:
    """미리보기 발송일 때만 자가진단 배너를 돌려준다 (7_watch 가 만든다).

    2026-09-01: 자가진단 결과가 로그 파일에만 남아 아무도 안 보던 문제를 막는다.
    운영자 미리보기(07:45)와 팀 발송(09:00) 사이 75분 안에 눈에 닿게 하는 게 목적이라
    팀 메일에는 붙이지 않는다 — weekly_send_me.bat 만 NEWSLETTER_DIAGNOSTICS 를 켠다.

    배너는 오늘 만든 것만 쓴다. 파이프라인이 중간에 죽어 7_watch 가 안 돌면 지난주
    배너가 남아 있을 수 있는데, 그걸 '오늘은 이상 없음'으로 읽으면 안 된다.
    """
    if os.getenv("NEWSLETTER_DIAGNOSTICS", "") != "1":
        return ""
    if not BANNER_PATH.exists():
        return ""
    age = datetime.now() - datetime.fromtimestamp(BANNER_PATH.stat().st_mtime)
    if age.total_seconds() > 12 * 3600:
        print("  [경고] 자가진단 배너가 12시간 이상 지난 것이라 붙이지 않습니다"
              " — 7_watch 가 이번 실행에서 돌지 않았을 수 있습니다.")
        return ""
    return BANNER_PATH.read_text(encoding="utf-8")


def send_newsletter(html_path: Path, recipients: list[str], dry_run: bool = False):
    today = datetime.now().strftime("%Y년 %m월 %d일")
    subject = f"[아크메] 주간 해사 뉴스레터 {today}"
    html_content = html_path.read_text(encoding="utf-8")

    # 진단 배너는 저장된 파일이 아니라 '보내는 본문'에만 넣는다.
    # weekly_email_*.html 은 미리보기·팀 발송이 같은 파일을 쓰므로, 파일에 써넣으면
    # 팀에게도 내부 진단이 나간다.
    banner = diagnostics_banner()
    if banner:
        marker = "<body"
        idx = html_content.find(marker)
        if idx != -1:                       # <body ...> 여는 태그 바로 뒤에 넣는다
            end = html_content.find(">", idx)
            html_content = (html_content[:end + 1] + banner
                            + html_content[end + 1:])
        else:
            html_content = banner + html_content
        print("  미리보기 자가진단 배너를 본문 맨 위에 붙였습니다.")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL))
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    # 풀기능 웹버전 HTML 첨부 (열면 브라우저에서 2x2·토글 작동)
    #
    # Content-Type은 반드시 application/octet-stream 이어야 한다.
    # text/html 로 두면 Outlook이 이 파트를 "두 번째 본문"으로 흡수해버려
    # 첨부 아이콘이 아예 안 보인다(2026-08-20 TEST 발송에서 확인).
    # Content-Disposition: attachment 만으로는 부족하다.
    web = find_latest_web_version()
    if web:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(web.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=web.name)
        msg.attach(part)

    if dry_run:
        print("\n[DRY RUN] 실제 발송 안 함 (미리보기)")
        print(f"  제목  : {subject}")
        print(f"  발신  : {SENDER_NAME} <{SENDER_EMAIL or '(미설정)'}>")
        print(f"  수신  : {recipients or '(미설정)'}")
        print(f"  파일  : {html_path.name}")
        web = find_latest_web_version()
        print(f"  첨부  : {web.name if web else '(없음)'}")
        print(f"  SMTP  : {SMTP_HOST}:{SMTP_PORT}")
        print("\n실제로 보내려면:  python 4_send_email.py --yes")
        return

    print("\n[4단계] 이메일 발송 중...")
    print(f"  SMTP: {SMTP_HOST}:{SMTP_PORT}  수신자: {', '.join(recipients)}")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
        print(f"\n발송 완료! 수신자 {len(recipients)}명")
    except Exception as e:
        print(f"\n발송 실패: {type(e).__name__}: {e}")
        raise


def send_via_outlook(html_path: Path, recipients: list[str], auto_send: bool = False):
    """이미 로그인된 Outlook 데스크톱 앱으로 발송 (SMTP 불필요, 회사 계정 그대로)."""
    try:
        import win32com.client
    except ImportError:
        print("pywin32가 필요합니다. 설치:  pip install pywin32")
        return

    today = datetime.now().strftime("%Y년 %m월 %d일")
    html_content = html_path.read_text(encoding="utf-8")
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        # 발신 계정 지정: 미지정이면 Outbox에 걸린 채 전송이 안 되는 문제가 있어(계정 바인딩 누락),
        # SMTP_USER와 일치하는 계정(없으면 첫 번째 계정)을 명시적으로 붙인다.
        send_acct = None
        try:
            for acct in outlook.Session.Accounts:
                if SMTP_USER and (acct.SmtpAddress or "").lower() == SMTP_USER.lower():
                    send_acct = acct
                    break
            if send_acct is None and outlook.Session.Accounts.Count > 0:
                send_acct = outlook.Session.Accounts.Item(1)
        except Exception:
            send_acct = None

        mail = outlook.CreateItem(0)  # 0 = olMailItem
        mail.Subject = f"[아크메] 주간 해사 뉴스레터 {today}"
        if recipients:
            mail.To = "; ".join(recipients)
        mail.HTMLBody = html_content
        if send_acct is not None:
            mail.SendUsingAccount = send_acct
            print(f"  발신 계정 지정: {send_acct.SmtpAddress}")

        # 풀기능 웹버전(2x2·네비·한영 토글) HTML을 첨부 → 브라우저에서 열람 가능
        web = find_latest_web_version()
        if web:
            mail.Attachments.Add(str(web.resolve()))
            print(f"  웹버전 첨부: {web.name} (열면 브라우저에서 2x2·토글 작동)")
        if auto_send:
            mail.Send()
            print(f"\nOutlook으로 발송 완료! 수신자 {len(recipients)}명")
        else:
            mail.Display()  # 초안 창 열기 (검토 후 직접 [보내기])
            print("\nOutlook에 초안을 열었습니다. 내용 확인 후 [보내기]를 누르세요.")
            print("바로 보내려면:  python 4_send_email.py --outlook --yes")
    except Exception as e:
        print(f"\nOutlook 발송 실패: {type(e).__name__}: {e}")
        print("Outlook 데스크톱 앱이 설치·실행되어 있는지 확인하세요.")


def require_today(*paths) -> None:
    """주어진 파일들이 모두 '오늘' 만든 것인지 확인. 아니면 종료(exit 3).

    빌드를 하지 않고 기존 파일만 보내는 실행(팀 발송)에서 쓴다. 08:00 빌드가 실패하면
    find_latest_newsletter() 가 지난주 파일을 최신으로 집어 그대로 팀에 나가버린다.
    파일명(weekly_email_YYYYMMDD.html)과 수정시각을 둘 다 본다 — 파일명만 보면
    같은 이름으로 덮어쓰다 실패한 경우를 놓치고, 시각만 보면 재발송 때 오판한다.
    """
    today = datetime.now().date()
    for path in paths:
        if path is None:
            continue
        stamped = today.strftime("%Y%m%d") in path.name
        touched = datetime.fromtimestamp(path.stat().st_mtime).date() == today
        if not (stamped and touched):
            print(f"[중단] 오늘 만든 뉴스레터가 아닙니다: {path.name} "
                  f"(수정 {datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M})")
            print("  앞선 빌드 단계가 실패했을 수 있습니다. weekly_run.log 를 확인하세요.")
            print("  지난 뉴스레터가 잘못 나가지 않도록 발송하지 않고 종료합니다.")
            sys.exit(3)


def run():
    html_path = find_latest_newsletter()
    if not html_path:
        print("발송할 뉴스레터 HTML이 없습니다. 3_build_html.py를 먼저 실행하세요.")
        return

    # 본문 HTML 과 첨부 웹버전 둘 다 오늘 것이어야 한다
    if "--require-today" in sys.argv:
        require_today(html_path, find_latest_web_version())

    # 방법 A: Outlook 데스크톱 앱
    if "--outlook" in sys.argv:
        print(f"발송 대상 파일: {html_path.name}")
        print(f"수신자: {RECIPIENTS or '(비어있음 → Outlook 창에서 직접 입력)'}")
        send_via_outlook(html_path, RECIPIENTS, auto_send="--yes" in sys.argv)
        return

    # 방법 B: SMTP
    want_send = "--yes" in sys.argv or "-y" in sys.argv

    # --yes를 명시했는데 보낼 수 없는 상태면 DRY RUN으로 조용히 넘어가지 않고 실패시킨다.
    # 무인 스케줄 실행에서는 아무도 출력을 보지 않으므로, 종료코드 0으로 끝나면
    # weekly_send.bat이 "발송 완료"로 기록해 사실상 발송 누락을 은폐하게 된다.
    if want_send:
        if not RECIPIENTS:
            print("[오류] 수신자가 없습니다. .env에 EMAIL_RECIPIENTS=받는사람@메일.com 을 설정하세요.")
            sys.exit(2)
        if not creds_ok():
            print("[오류] SMTP 계정이 설정되지 않았습니다(.env의 SMTP_USER / SMTP_PASSWORD).")
            sys.exit(2)

    if not RECIPIENTS:
        print("수신자가 없습니다. .env에 EMAIL_RECIPIENTS=받는사람@메일.com 을 설정하세요.")

    send_newsletter(html_path, RECIPIENTS, dry_run=not want_send)


if __name__ == "__main__":
    run()
