"""
4_send_email.py
생성된 HTML 뉴스레터를 이메일로 발송

■ 방법 A) Outlook 데스크톱 앱 이용 (Windows, SMTP 설정 불필요)
  python 4_send_email.py --outlook          # Outlook에 초안 열기 (검토 후 직접 보내기)
  python 4_send_email.py --outlook --yes    # Outlook으로 즉시 발송
  (Outlook 데스크톱이 로그인돼 있어야 함. pip install pywin32 필요)

■ 방법 B) SMTP 직접 발송 (Gmail/사내 SMTP 등)
  python 4_send_email.py                    # DRY RUN (미리보기)
  python 4_send_email.py --yes              # 실제 발송
  .env 필요: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

■ 공통: 수신자는 .env 의 EMAIL_RECIPIENTS=a@x.com,b@y.com (쉼표 구분)
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

from profile_loader import BRAND

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASSWORD", "")

SENDER_NAME = BRAND.get("sender_name", BRAND["name_ko"])
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


def send_newsletter(html_path: Path, recipients: list[str], dry_run: bool = False):
    today = datetime.now().strftime("%Y년 %m월 %d일")
    subject = f"[{BRAND['short_ko']}] {BRAND.get('briefing_ko', '주간 브리핑')} {today}"
    html_content = html_path.read_text(encoding="utf-8")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL))
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    # 풀기능 웹버전 HTML 첨부 (열면 브라우저에서 2x2·토글 작동)
    web = find_latest_web_version()
    if web:
        part = MIMEBase("text", "html")
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
        mail.Subject = f"[{BRAND['short_ko']}] {BRAND.get('briefing_ko', '주간 브리핑')} {today}"
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


def run():
    html_path = find_latest_newsletter()
    if not html_path:
        print("발송할 뉴스레터 HTML이 없습니다. 3_build_html.py를 먼저 실행하세요.")
        return

    # 방법 A: Outlook 데스크톱 앱
    if "--outlook" in sys.argv:
        print(f"발송 대상 파일: {html_path.name}")
        print(f"수신자: {RECIPIENTS or '(비어있음 → Outlook 창에서 직접 입력)'}")
        send_via_outlook(html_path, RECIPIENTS, auto_send="--yes" in sys.argv)
        return

    # 방법 B: SMTP
    want_send = "--yes" in sys.argv or "-y" in sys.argv

    if not RECIPIENTS:
        print("수신자가 없습니다. .env에 EMAIL_RECIPIENTS=받는사람@메일.com 을 설정하세요.")
        want_send = False
    if want_send and not creds_ok():
        print("SMTP 계정이 아직 설정되지 않았습니다(.env의 SMTP_USER / SMTP_PASSWORD).")
        print("→ 실제 발송 대신 DRY RUN으로 표시합니다.\n")
        want_send = False

    send_newsletter(html_path, RECIPIENTS, dry_run=not want_send)


if __name__ == "__main__":
    run()
