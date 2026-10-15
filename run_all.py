"""
run_all.py
주간 뉴스 브리핑 파이프라인 통합 실행기
실행: python run_all.py            (수집 → 번역 → 분석 → HTML 생성)
      python run_all.py --send     (위 + 이메일 발송 단계까지)

각 단계를 순서대로 실행하고, 단계가 실패하거나 결과 파일이 생성되지 않으면
즉시 중단한다.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from profile_loader import PROFILE_PATH

# (스크립트, 기대 출력파일) — 출력파일이 새로 생성되어야 성공으로 간주
STEPS = [
    ("1_fetch_news.py", "raw_news.json"),
    ("2_summarize.py", "summarized_news.json"),
    ("5_analyze.py", "weekly_analysis.json"),
    ("3_build_html.py", None),  # weekly_newsletter_YYYYMMDD.html (날짜 가변)
    ("build_email.py", None),   # weekly_email_YYYYMMDD.html (이메일 호환 버전)
]


def check_env() -> bool:
    """필수 환경변수/설정을 점검한다."""
    ok = True
    groq = os.getenv("GROQ_API_KEY")
    if not groq or not groq.startswith("gsk_"):
        print("  [경고] GROQ_API_KEY가 없거나 형식이 이상합니다 → 5_analyze 단계 실패 예상")
        ok = False

    gemini = os.getenv("GEMINI_API_KEY", "")
    if gemini.strip().endswith("python") or " " in gemini.strip():
        print("  [경고] GEMINI_API_KEY 값에 공백/'python' 문자열이 섞여 있습니다. .env를 확인하세요.")

    if "--send" in sys.argv:
        smtp_user = os.getenv("SMTP_USER", "")
        if not smtp_user or "@" not in smtp_user or smtp_user.startswith("실제"):
            print("  [경고] SMTP_USER가 설정되지 않았습니다 → 발송 단계 실패 예상")
            ok = False
    return ok


def run_step(script: str, expected: str | None, start_ts: float) -> bool:
    """스크립트를 실행하고 성공 여부를 반환한다."""
    print(f"\n{'='*60}\n▶ 실행: {script}\n{'='*60}")
    if not Path(script).exists():
        print(f"  [오류] {script} 파일이 없습니다.")
        return False

    # stdin을 DEVNULL로 분리: 스케줄러 비대화형 실행 시 물려받은 콘솔이 종료신호를
    # 하위 프로세스로 흘려 0xC000013A로 죽던 문제 방지 (대화형 실행에도 무해)
    result = subprocess.run([sys.executable, script], stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print(f"  [오류] {script} 비정상 종료 (exit={result.returncode})")
        return False

    # 기대 출력파일이 이번 실행에서 새로 생성/갱신되었는지 확인
    if expected:
        p = Path(expected)
        if not p.exists() or p.stat().st_mtime < start_ts:
            print(f"  [오류] {script} 실행 후 {expected}가 갱신되지 않았습니다.")
            return False
    return True


def main():
    print(f"\n{'#'*60}")
    print(f"# 주간 뉴스 브리핑 파이프라인 (profile: {PROFILE_PATH.name})")
    print(f"# 시작: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'#'*60}")

    print("\n[환경 점검]")
    if not check_env():
        print("\n환경 설정에 문제가 있어 중단합니다. .env를 확인하세요.")
        return 1
    print("  환경 점검 통과")

    for script, expected in STEPS:
        start_ts = time.time()
        if not run_step(script, expected, start_ts):
            print(f"\n파이프라인 중단: {script} 단계 실패")
            return 1

    # 최신 HTML 확인
    htmls = sorted(Path(".").glob("weekly_newsletter_*.html"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if htmls:
        print(f"\n생성된 뉴스레터: {htmls[0].name}")

    if "--send" in sys.argv:
        start_ts = time.time()
        if not run_step("4_send_email.py", None, start_ts):
            print("\n발송 단계 실패")
            return 1

    if "--telegram" in sys.argv:
        start_ts = time.time()
        if not run_step("6_send_telegram.py", None, start_ts):
            print("\n텔레그램 전송 실패")
            return 1

    print(f"\n{'#'*60}")
    print(f"# 파이프라인 완료: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'#'*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
