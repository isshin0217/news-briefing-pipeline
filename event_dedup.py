"""
event_dedup.py
본문을 '사건 한 줄'로 요약한 뒤, 그 요약끼리 비교해 같은 사건 기사를 묶는다.

왜 필요한가 (2026-08-25 실측):
  같은 사건을 다룬 기사를 제목·본문 유사도로는 못 가른다. 북극항로 기사 7건을 재보니
    제목 공유 토큰   팬스타 출항 4건 서로 1~3개  /  '기후변화의 역설'도 2개   ← 구간 겹침
    본문 겹침        팬스타 출항 4건 서로 0.19~0.36 / '기후변화의 역설' 0.22  ← 구간 겹침
  임계값 하나로 "같은 사건"과 "같은 주제 다른 각도"를 나눌 수 없다.

  본문을 사건 문장으로 압축하면 표현 차이가 걷히고 사건만 남는다. 같은 실측에서
    팬스타 출항 6건 → 전부 "팬스타 아크로호가 부산에서 북극항로 시범운항을 시작" (겹침 1.00)
    기후변화의 역설  → "기후변화가 북극 해빙을 감소시켜 운항 가능 기간을 확대"   (겹침 0.17)
  1.00 대 0.17 로 구간이 완전히 갈린다.

설계:
  · LLM 호출이 실패하거나 키가 없으면 아무것도 하지 않고 원본을 그대로 돌려준다.
    중복 정리는 품질 개선이지 필수 단계가 아니다 — 여기서 파이프라인이 멈추면 안 된다.
  · 본문이 있는 기사만 대상으로 한다(요약할 근거가 없으면 판정하지 않는다).
  · 묶인 기사는 버리되 coverage 에 합산한다. 보도량 가점(1_fetch_news)이 그 수를 쓴다.
"""

import json
import time

import gemini_client as gemini
import keyword_match as km

# 배치 크기는 이제 토큰 한도가 아니라 '한 번에 비교하기 좋은 묶음'으로 정한다.
# Groq 시절에는 분당 8,000토큰 한도 때문에 6건까지만 보낼 수 있었다(2026-09-01 전환).
# Gemini 는 컨텍스트가 100만 토큰이라 이 값이 성능을 좌우하지 않는다.
BATCH_SIZE = 8            # 한 번에 요약할 기사 수
BODY_CHARS = 400          # 기사당 본문 앞부분만 보낸다 — 리드에 사건이 있다
SAME_EVENT_THRESHOLD = 0.6   # 실측 구간(같은 사건 1.00 / 다른 각도 0.17)의 중간

PROMPT = """다음 해사 뉴스 기사들을 각각 '무슨 일이 있었는지' 한 문장으로 요약하세요.
표현이 아니라 사건을 적으세요. 같은 사건을 다룬 기사는 같은 문장이 나와야 합니다.
주체·행위·대상만 쓰고 형용사·수식어는 빼세요.

각 기사의 성격도 판정하세요.
  "사건" — 실제로 일어난 일을 전하는 보도 (출항·수주·인도·계약·발표·사고)
  "분석" — 전망·과제·해설·칼럼·사설·기획 (일어난 일보다 해석이 중심)

형식(JSON만 출력):
{"summaries": [{"id": 0, "event": "...", "type": "사건"}, ...]}

기사 목록:
"""


# 응답 스키마. 이걸 API 에 넘기면 모델이 이 형태를 벗어난 응답을 낼 수 없다.
#
# Groq 시절에는 "JSON만 출력하세요"라고 부탁한 뒤, 앞뒤에 붙은 말을 정규식으로 걷어내고,
# 응답이 잘리면 개별 항목만 건져내는 복구 코드를 두고, 그래도 실패하면 배치를 통째로
# 버렸다. 8/31 사고가 바로 그 경로였다(36건 중 18건만 요약). 스키마를 강제하면
# 그 실패 자체가 불가능해져서 복구 코드가 전부 사라진다.
EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "event": {"type": "string"},
                    "type": {"type": "string", "enum": ["사건", "분석"]},
                },
                "required": ["id", "event", "type"],
            },
        }
    },
    "required": ["summaries"],
}


def _event_lines(batch: list[dict]) -> dict:
    """기사 묶음 → {목록 내 순번: (사건 문장, 성격)}."""
    lines = []
    for i, art in enumerate(batch):
        body = (art.get("body") or art.get("summary", ""))[:BODY_CHARS]
        lines.append(f"[id={i}] {art.get('title', '')}\n    {body}\n")
    try:
        # 기계적인 요약이라 빠른 모델을 쓴다. 실측(2026-09-01): flash-lite 가
        # 3/3 성공·2.3초·토큰 1,553(매번 동일)로 가장 안정적이었고 문장 품질도 같았다.
        data = gemini.ask_json(PROMPT + "\n".join(lines), EVENT_SCHEMA,
                               model=gemini.FAST_MODEL, max_output_tokens=4096)
    except Exception as exc:
        print(f"  [사건 요약] 호출 실패({type(exc).__name__}: {exc}) — 이 묶음은 건너뜁니다")
        return {}
    out = {}
    for item in data.get("summaries", []):
        try:
            out[int(item["id"])] = (str(item["event"]).strip(),
                                    str(item.get("type", "사건")).strip())
        except (KeyError, ValueError, TypeError):
            continue
    return out


def merge_same_event(articles: list[dict],
                     threshold: float = SAME_EVENT_THRESHOLD) -> list[dict]:
    """같은 사건 기사를 묶는다. 점수 높은 기사를 대표로 남기고 coverage 에 합산한다.

    입력 순서는 유지하지 않는다(점수 내림차순으로 정렬해 대표를 고른다).
    """
    if not gemini.available():
        print("  [사건 요약] GEMINI_API_KEY 없음 — 건너뜁니다")
        return articles

    # 본문이 없어도 제목·요약만으로 사건 문장을 뽑는다(2026-09-01).
    #
    # 예전에는 본문 있는 기사만 대상으로 했다. 그런데 구글 뉴스 링크는 원문 주소를
    # 알 수 없어 본문을 못 받는데, 국문 기사의 영문판이 대부분 그 경로로 들어온다.
    # 그래서 같은 사건의 국문·영문 기사가 서로 다른 집합에 놓여 영영 안 묶였다.
    # 실측(2026-09-01 LLM 검수가 적발): '삼성중공업 컨테이너선 수주'·'DNV 2050
    # 예측'·'팬스타 아크로호'가 각각 국문/영문 두 벌씩 실렸다.
    #
    # 제목만 있어도 "삼성중공업이 컨테이너선을 수주했다" 정도는 나오고, 그 문장끼리
    # 비교하면 언어가 달라도 만난다 — 애초에 이 단계가 표현 차이를 걷어내려고 있다.
    def has_text(art: dict) -> bool:
        return bool((art.get("body") or "").strip()
                    or (art.get("title") or "").strip())

    targets = [a for a in articles if has_text(a)]
    skipped = [a for a in articles if not has_text(a)]
    if len(targets) < 2:
        return articles

    targets.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    events: dict[int, tuple[str, str]] = {}
    for start in range(0, len(targets), BATCH_SIZE):
        batch = targets[start:start + BATCH_SIZE]
        for local_i, value in _event_lines(batch).items():
            if 0 <= local_i < len(batch):
                events[start + local_i] = value
        time.sleep(0.3)          # 연속 호출이 몰려 503(혼잡)이 나는 것을 줄인다

    if not events:
        print("  [경고] 사건 요약이 한 건도 안 나왔습니다 — 같은 사건 병합을 건너뜁니다.")
        print("         같은 사안을 다룬 기사가 여러 벌 실릴 수 있습니다"
              " (실측 2026-08-31: LPG 운반선 수주 기사 3건 중복 게재).")
        print("         GEMINI_API_KEY / 모델 응답을 위 로그에서 확인하세요.")
        return articles
    if len(events) < len(targets):
        print(f"  [사건 요약] {len(events)}/{len(targets)}건만 요약됨 "
              f"— 나머지는 병합 대상에서 빠집니다")

    # 대표는 '사건을 제목에 담은 기사'를 먼저 본다(2026-08-25).
    #
    # 점수만으로 고르면 같은 사건 묶음에서 전망·칼럼이 대표가 된다. 실측: 팬스타 출항
    # 315건을 묶었더니 '북극항로 첫발 뗐지만…상용화까지는 첩첩산중'(28점)이 대표로
    # 남았다. 사건 요약은 "팬스타 아크로가 북극항로 시범운항을 시작"으로 정확한데,
    # 정작 독자가 보는 제목에는 출항했다는 사실이 없다.
    #
    # 모델의 성격 판정(사건/분석)만으로는 부족했다 — 위 기사도 '사건'으로 분류됐다.
    # 그래서 제목이 사건 요약과 얼마나 겹치는지를 함께 본다. 사건을 그대로 쓴 제목일수록
    # 요약과 많이 겹친다('3만5천t 배 닻 올렸다…첫 북극항로 시범운항' → 시범운항·북극항로 공유).
    def _rank(i: int):
        event, kind = events.get(i, ("", "사건"))
        art = targets[i]
        title_tokens = km.dup_tokens(art.get("title", ""))
        event_tokens = km.dup_tokens(event)
        overlap = (len(title_tokens & event_tokens) / len(event_tokens)
                   if event_tokens else 0.0)
        return (kind == "분석", -round(overlap, 2),
                -art.get("relevance_score", 0))

    order = sorted(range(len(targets)), key=_rank)

    kept, kept_tokens, merged = [], [], 0
    for idx in order:
        art = targets[idx]
        event, _kind = events.get(idx, (None, "사건"))
        tokens = km.dup_tokens(event) if event else set()
        rep, best = None, 0.0
        if tokens:
            for prev, prev_tokens in zip(kept, kept_tokens):
                if not prev_tokens:
                    continue
                overlap = (len(tokens & prev_tokens)
                           / min(len(tokens), len(prev_tokens)))
                if overlap > best:
                    rep, best = prev, overlap
        # 경계값은 '같은 사건'이 아니다 — 초과일 때만 묶는다(2026-10-06 피드백).
        # 실측: 한국일보 'AI·친환경·자율운항 기술력 강화… 미래선박 시장 선도'(HD현대
        # 미래 기술 전략, 14점)가 다른 HD현대 기사와 정확히 0.60 으로 묶여 빠졌다.
        # 사건 문장이 짧으면 회사명·'건조' 같은 공통어 몇 개만으로 0.60 에 닿는다.
        # 같은 사건은 실측상 0.67 이상(대부분 1.00)이라 초과 판정으로 잃는 것이 없다.
        if rep is not None and best > threshold:
            rep["coverage"] = rep.get("coverage", 1) + art.get("coverage", 1)
            print(f"    · 같은 사건({best:.2f}) 제외: {art.get('title', '')[:44]}")
            merged += 1
            continue
        if event:
            art["event_summary"] = event
            art["event_type"] = _kind
        kept.append(art)
        kept_tokens.append(tokens)

    if merged:
        print(f"  [사건 요약] 같은 사건 {merged}건 병합")
    return kept + skipped


if __name__ == "__main__":
    from pathlib import Path

    # 단독 실행할 때는 여기서 .env 를 읽는다. 파이프라인으로 돌 때는 1_fetch_news 가
    # 이미 읽은 뒤에 import 하므로 필요 없지만, 이 파일만 따로 돌려 디버깅할 때
    # 키가 없다고 조용히 건너뛰는 일을 막는다(2026-09-01).
    from dotenv import load_dotenv
    load_dotenv()

    path = Path("raw_news.json")
    news = json.loads(path.read_text(encoding="utf-8"))
    before = len(news)
    news = merge_same_event(news)
    print(f"{before}건 → {len(news)}건")
    for art in news:
        if art.get("event_summary"):
            print(f"  [{art.get('coverage', 1):2d}개 매체] {art['event_summary'][:60]}")
