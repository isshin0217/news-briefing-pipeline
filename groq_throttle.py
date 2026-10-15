"""
groq_throttle.py
Groq 무료 티어의 분당 토큰 한도(TPM)를 429 를 맞기 '전에' 피해 가는 공용 페이서.

왜 필요한가 (2026-09-01 피드백):
  실측한 계정 한도는 이렇다.
      x-ratelimit-limit-requests   1,000 / 일   ← 우리는 10회 안팎만 쓴다. 여유 넘침
      x-ratelimit-limit-tokens     8,000 / 분   ← 이게 유일한 병목
      x-ratelimit-reset-tokens     38.8초       ← 버킷이 매분 다시 찬다
  즉 넘을 수 없는 벽이 아니라 '속도 제한'이고, 기다리면 반드시 통과한다.

  그런데 지금까지는 최대 속도로 쏘고 429 를 맞은 뒤에야 물러났다. 결과가 재시도 횟수에
  달려 있어서, 재시도가 모자라면 단계가 통째로 죽는다.
    2026-08-31  event_dedup 이 429 로 전멸 → 같은 LPG 수주 기사 3벌 게재
    2026-09-01  전체 실행에서 429 8회 (결과는 나왔지만 운에 기댄 구조)

  응답 헤더가 잔여 토큰과 리필 시각을 알려준다. 그걸 보고 미리 쉬면 429 자체가 안 난다.

쓰는 법:
    import groq_throttle as gt
    gt.pace(need)                 # 호출 직전 — 예산이 모자라면 리필까지 대기
    resp = requests.post(...)
    gt.remember(resp)             # 호출 직후 — 헤더의 잔여량 기록
    if resp.status_code == 429:
        time.sleep(gt.retry_wait(resp))

상태는 프로세스 안에서만 공유된다. 파이프라인 단계가 서로 다른 프로세스라 단계 간에는
이어지지 않지만, 429 를 만드는 건 대부분 한 단계 안의 연속 호출이라 이걸로 충분하다.
"""

import re
import time

# 한 번 호출에 쓰는 토큰의 기본 추정치 (프롬프트 + 출력).
# event_dedup 한 배치가 1,900 토큰, 5_analyze 카드 생성이 그보다 조금 크다.
DEFAULT_NEED = 2500
MAX_WAIT = 65.0          # 한 번에 이 이상은 안 기다린다 (버킷이 분당 리필이므로 충분)

_RESET_RE = re.compile(r"(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?")
_state = {"remaining": None, "reset": 0.0, "at": 0.0}


def parse_reset(value: str) -> float:
    """'38.835s' / '2m30s' / '1m' → 초."""
    match = _RESET_RE.fullmatch((value or "").strip())
    if not match or not any(match.groups()):
        return 0.0
    return float(match.group(1) or 0) * 60 + float(match.group(2) or 0)


def remember(response) -> None:
    """응답 헤더의 잔여 토큰·리필 시각을 기록한다. 헤더가 없으면 조용히 넘어간다."""
    try:
        remaining = response.headers.get("x-ratelimit-remaining-tokens")
        if remaining is None:
            return
        _state["remaining"] = int(remaining)
        _state["reset"] = parse_reset(
            response.headers.get("x-ratelimit-reset-tokens", ""))
        _state["at"] = time.monotonic()
    except (AttributeError, TypeError, ValueError):
        pass


def pace(need: int = DEFAULT_NEED, quiet: bool = False) -> float:
    """다음 호출에 need 만큼 쓸 수 있을 때까지 기다린다. 기다린 초를 돌려준다.

    직전 응답 이후 흐른 시간만큼은 이미 리필됐다고 보고 대기시간에서 뺀다.
    """
    remaining = _state["remaining"]
    if remaining is None or remaining >= need:
        return 0.0
    elapsed = time.monotonic() - _state["at"]
    wait = min(max(_state["reset"] - elapsed + 1.0, 0.0), MAX_WAIT)
    if wait <= 0:
        _state["remaining"] = None
        return 0.0
    if not quiet:
        print(f"    (TPM 여유 {remaining}토큰 — {wait:.0f}초 대기 후 진행)")
    time.sleep(wait)
    _state["remaining"] = None       # 리필됐다고 보고 다음 응답에서 다시 잰다
    return wait


def retry_wait(response, default: float = 20.0) -> float:
    """429 응답이 알려주는 대기 시간(초). retry-after 헤더가 가장 정확하다."""
    header = None
    try:
        header = response.headers.get("retry-after")
    except AttributeError:
        pass
    if header:
        try:
            return min(float(header) + 1.0, MAX_WAIT)
        except ValueError:
            pass                      # HTTP-date 형식이면 본문으로 넘어간다
    try:
        match = re.search(r"try again in ([\d.]+)s", response.text, re.IGNORECASE)
        if match:
            return min(float(match.group(1)) + 1.0, MAX_WAIT)
    except (AttributeError, ValueError):
        pass
    return default


def forget() -> None:
    """기록을 지운다 (429 를 맞은 직후처럼 잔여량을 못 믿을 때)."""
    _state["remaining"] = None
