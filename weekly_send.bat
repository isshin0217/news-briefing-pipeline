@echo off
REM ============================================================
REM  주간 브리핑 자동 생성 + Outlook 초안 열기 (Windows 작업 스케줄러용)
REM  * 자동 발송하지 않음. Outlook 초안을 열어 검토 후 직접 발송.
REM  * 각 python 호출의 stdin을 NUL로 돌려, 비대화형 실행 시
REM    콘솔 종료신호로 하위 프로세스가 죽는 문제(0xC000013A)를 막음
REM  * 앞 단계가 실패하면 다음 단계로 넘어가지 않고 중단
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not defined PY set "PY=python"

echo.>> weekly_run.log
echo ===== START %date% %time% ===== >> weekly_run.log

"%PY%" run_all.py < NUL >> weekly_run.log 2>&1
set RC=%errorlevel%
if not "%RC%"=="0" ( echo [중단] run_all.py 실패 exit=%RC% - 이후 단계 생략 >> weekly_run.log & goto :end )

"%PY%" 4_send_email.py --outlook < NUL >> weekly_run.log 2>&1
set RC=%errorlevel%
if not "%RC%"=="0" ( echo [경고] 4_send_email.py exit=%RC% >> weekly_run.log )

:end
echo ===== END   %date% %time% ===== >> weekly_run.log
