"""
gemini_client.py
Gemini API 호출 공용 모듈. Groq 를 대체한다(2026-09-01).

왜 바꿨나:
  Groq 무료 티어의 병목은 분당 토큰 8,000이었다. 이 파이프라인은 한 번 도는 데
  25,000 토큰쯤 쓰는데 그게 분당 한도의 세 배라, 단계마다 429 를 맞고 기다렸다.
  8/31 에는 재시도가 모자라 중복 제거가 통째로 죽었고 같은 LPG 수주 기사가 3벌 실렸다.

  Gemini 는 같은 무료지만 컨텍스트가 100만 토큰이라 그 벽이 없다.
  실측(2026-09-01, 페이싱 없이 6배치 연속): 429 는 한 번도 안 났다.

Groq 대비 달라지는 것:
  1. 페이싱이 필요 없다 — groq_throttle 의 TPM 대기 로직이 통째로 사라진다.
  2. JSON 파싱 실패가 원천 차단된다. responseSchema 로 스키마를 강제하므로 응답이
     스키마를 벗어날 수 없다. Groq 시절의 '응답이 잘려 JSON 이 안 닫힘 → 배치 통째로
     폐기'(8/31 사고의 실제 원인)가 구조적으로 불가능해진다. 정규식 복구도 필요 없다.
  3. 프롬프트를 잘라 넣지 않아도 된다. 5_analyze 가 후보를 30건으로 줄이고 요약을
     110자로 자르던 것은 전부 8K TPM 때문이었다.

남는 실패 모드는 429 가 아니라 503(모델 혼잡)이다. 일시적이라 재시도로 해결된다
 — 실측에서 6배치 중 2번 났고 둘 다 재시도로 통과했다.

모델 선택(2026-09-01 실측):
  gemini-3.1-flash-lite  3/3 성공, 2.3초, 토큰 1,553(매번 동일) ← 기계적 작업
  gemini-3.6-flash       3/3 성공, 10.3초, 토큰 ~2,900          ← 판단이 필요한 작업
  gemini-flash-latest    2/3 성공(503 1회), 23.3초              ← 쓰지 않는다
"""

import json
import os
import random
import time

import requests

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 기계적인 작업(본문 리드를 한 문장으로 줄이기 등). 빠르고 결과가 일정하다.
FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-3.1-flash-lite")
# 판단이 필요한 작업(카드 선정, 시사점, 한 줄 결론).
SMART_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

MAX_RETRY = 5
REQUEST_TIMEOUT = 180
# 재시도할 상태코드. 503(혼잡)이 실제로 나는 것이고, 나머지는 방어적으로 넣는다.
RETRY_STATUS = {429, 500, 502, 503, 504}


def api_key() -> str | None:
    """키는 쓰는 시점에 읽는다. 호출부가 import 를 마친 뒤 load_dotenv() 를 부르므로
    모듈 최상단에서 읽으면 None 이 된다(Groq 때와 같은 이유)."""
    return os.getenv("GEMINI_API_KEY")


def available() -> bool:
    """호출 가능한 키가 있는가. 없으면 호출부가 조용히 건너뛴다."""
    return bool(api_key())


class GeminiError(RuntimeError):
    """호출 실패. 재시도를 다 쓴 뒤에도 실패하면 던진다."""


def ask_json(prompt: str, schema: dict, *, model: str | None = None,
             max_output_tokens: int = 8192, temperature: float = 0.1,
             quiet: bool = False) -> dict:
    """프롬프트를 보내고 schema 를 만족하는 dict 를 받는다.

    responseSchema 를 쓰므로 반환값은 반드시 schema 를 만족한다. 응답에서 JSON 을
    정규식으로 긁어내거나, 잘린 응답을 복구하거나, 파싱 실패 안전망을 둘 필요가 없다.

    503(혼잡)은 지수 백오프로 재시도한다. 한도(429)와 달리 기다릴 시간이 정해져
    있지 않아서, 서버가 알려주는 값 대신 우리가 늘려 가며 기다린다.
    """
    key = api_key()
    if not key:
        raise GeminiError("GEMINI_API_KEY 가 없습니다")

    url = f"{API_BASE}/{model or SMART_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }

    last = ""
    for attempt in range(MAX_RETRY):
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=payload,
                              timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt == MAX_RETRY - 1:
                break
            _sleep(attempt, last, quiet)
            continue

        if r.status_code == 200:
            return _parse(r.json())

        last = f"HTTP {r.status_code}: {r.text[:200]}"
        if r.status_code not in RETRY_STATUS or attempt == MAX_RETRY - 1:
            break
        _sleep(attempt, f"HTTP {r.status_code}", quiet)

    raise GeminiError(last or "알 수 없는 오류")


def _sleep(attempt: int, why: str, quiet: bool) -> None:
    """지수 백오프 + 지터. 여러 호출이 동시에 재시도해 다시 몰리는 것을 막는다."""
    wait = min(2 ** attempt, 30) + random.uniform(0, 1)
    if not quiet:
        print(f"    ({why} — {wait:.0f}초 후 재시도 {attempt + 1}/{MAX_RETRY - 1})")
    time.sleep(wait)


def _parse(data: dict) -> dict:
    """응답에서 JSON 본문을 꺼낸다.

    responseSchema 를 걸었으므로 텍스트는 반드시 유효한 JSON 이다. 다만 출력이
    maxOutputTokens 에서 잘리면(finishReason=MAX_TOKENS) 그 보장이 깨지므로,
    조용히 빈 결과를 돌려주지 말고 오류로 올린다 — 8/31 사고가 딱 그 조용한 실패였다.
    """
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback", {})
        raise GeminiError(f"응답에 candidate 가 없습니다 (promptFeedback={feedback})")

    cand = candidates[0]
    reason = cand.get("finishReason")
    if reason and reason not in ("STOP", "FINISH_REASON_STOP"):
        raise GeminiError(f"생성이 비정상 종료됐습니다 (finishReason={reason}) "
                          "— maxOutputTokens 를 늘리거나 입력을 줄이세요")

    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        raise GeminiError("응답이 비어 있습니다")
    return json.loads(text)
