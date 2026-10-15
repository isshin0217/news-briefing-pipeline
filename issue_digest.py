"""
issue_digest.py
뉴스레터에 실릴 기사를 '이슈' 단위로 묶고, 기사마다 개조식 불릿 요약을 만든다.

왜 만들었나 (2026-09-07 피드백):
  "뉴스 기사 본문을 가져오니까 잘 안 읽어진대."
  본문(article_body.py)을 붙이기 시작한 뒤 카드 하나가 200자 통문장이 됐다.
  문장이 길고 중간에서 끊겨, 읽는 사람이 무슨 일이 있었는지 잡아내지 못한다.
  그래서 두 가지를 바꾼다.
    1. 카테고리 밑에 '이슈' 한 줄을 세우고, 기사는 그 밑에 접어 둔다.
    2. 기사 요약을 통문장 대신 개조식 불릿 2~3줄로 만든다.

  요청된 화면:
    고객사 동향 (해운사 · Big3 조선소)
      [HD현대중공업] 5154억 규모 LPG운반선 건조 계약        ▲
          HD현대중공업, 5154억원 규모...LPG 운반선 4척 건조 계약
            - 2030년 상반기 인도 예정인 건조 계약 체결, 24일 공시
            - 연간 수주 목표(233억1000만달러)의 77.6% 달성

호출은 한 번뿐이다. Gemini 컨텍스트가 100만 토큰이라 이번 주 실릴 기사(40~50건)를
한 번에 넣을 수 있다. 5_analyze 의 분석·결론 호출과 같은 방식이다.

실패해도 뉴스레터는 나간다. issues 가 없으면 두 빌더가 기존 평면 목록으로 되돌아간다
(build_issue_html / build_issue_email 을 부르는 쪽에서 판정). 새 기능이 파이프라인을
멈추게 하지 않는 것이 이 파일의 유일한 안전 규칙이다.
"""

import json
import re
import time

import gemini_client as gemini
import keyword_match as km

# 불릿을 만들 때 모델에게 보여줄 기사 본문 길이. 본문 평균이 2,000자라 대부분 통째로
# 들어간다. 결론 호출(800자)보다 넉넉한 이유: 요약할 사실을 고르려면 기사 전체가 필요하다.
BODY_LIMIT = 1600
# 이슈·불릿을 합친 출력. 기사 45건 × (불릿 2~3줄 × 한/영) 이라 기본값 8,192로는 잘린다.
# 잘리면 gemini_client 가 finishReason=MAX_TOKENS 로 예외를 던진다(조용히 실패하지 않는다).
OUT_TOKENS = 32768

# 한 번의 호출에 넣을 기사 수 상한 (2026-09-21).
#
# 왜 생겼나: 2026-09-21 회차가 통째로 실패했다.
#   이슈·불릿: 실릴 기사 40건 / 9개 그룹 (프롬프트 58,683자)
#   이슈·불릿 생성 실패 (GeminiError: finishReason=MAX_TOKENS)
# 한 번에 다 넣던 구조라 40건분 출력이 32,768 토큰을 넘었고, 예외 하나로 그 주
# 이슈·불릿이 전부 사라졌다. 뉴스레터는 나갔지만 두 빌더가 평면 카드로 되돌아가
# 고객사 동향에서 사진이 통째로 빠졌다(2026-09-21 피드백). 실패 한 번의 대가가 회차 전체라는 게 문제였다.
#
# 이제 그룹 단위로 나눠 여러 번 부른다. 한 호출이 실패해도 그 배치의 그룹만
# 1건짜리 이슈로 떨어지고(사진·제목은 그대로 나온다) 나머지 그룹은 온전하다.
# 그룹을 쪼개지는 않는다 — 이슈 묶기는 같은 그룹 기사를 한자리에서 봐야 한다.
#
# 15건인 이유: 실패한 회차의 최대 그룹이 12건이었고, 성공한 회차들(30~35건/8그룹)의
# 출력이 그 절반쯤에서 여유 있게 끝났다. 그룹 하나가 15건을 넘으면 그 그룹만 단독
# 배치로 보낸다(쪼개는 것보다 한 번 더 부르는 편이 안전하다).
MAX_ARTICLES_PER_CALL = 15
# 이슈 하나에 묶을 수 있는 기사 수 상한. 넘으면 사람이 펼쳤을 때 목록이 다시 길어진다.
MAX_PER_ISSUE = 5
# 불릿 줄 수 상한. 기술 동향(TOP 카드)은 한 건씩 크게 싣는 자리라 한 줄 더 준다
# — 2026-09-07 피드백.
MAX_BULLETS = 3
MAX_BULLETS_DETAIL = 4


PROMPT = """당신은 아크메 기술개발팀 주간 해사 뉴스레터의 편집자입니다.
아래는 이번 주 뉴스레터에 실릴 기사 목록이며, [그룹]으로 나뉘어 있습니다.

할 일은 두 가지입니다.

■ 1. 이슈 묶기
그룹 안에서 **같은 사안을 다룬 기사끼리 묶어** 이슈를 만드세요.
 - 이슈 제목(title_ko)은 25자 이내 한 줄. '누가 무엇을 했는지'를 담되 주체는 subject 로
   빼고 행위만 씁니다. (좋은 예: "5154억 규모 LPG운반선 건조 계약" / 나쁜 예: "수주 소식")
 - subject_ko 는 그 이슈의 주체입니다. **모든 이슈에 반드시 채우세요.** 회사·기관이
   주체면 그 이름을, 아니면 국가·지역·시장 이름이라도 쓰세요.
   (2026-09-07 피드백: '중국 LNG 수입 시장 환경 변화 동향 분석'에 머릿말이 비어
   있었다. 이런 기사의 주체는 '중국' 또는 '중국 LNG 시장'이다.)
   정말 아무 주체도 없을 때만 빈 문자열로 두세요.
 - 묶을 기사가 없으면 기사 1건짜리 이슈로 두세요. 억지로 묶지 마세요.
 - 다른 그룹의 기사끼리는 절대 묶지 마세요. ids 는 반드시 같은 group 안에서만 고릅니다.
 - **모든 기사가 정확히 한 이슈에 들어가야 합니다.** 빠뜨리거나 두 번 넣지 마세요.
 - 한 이슈에 기사 {max_per_issue}건을 넘기지 마세요.
 - 그룹 안에서 중요한 이슈를 앞에 두세요.

■ 2. 기사별 불릿 요약
기사마다 개조식 불릿을 쓰세요. 통문장 요약이 읽히지 않는다는 피드백에서 나온
작업이므로, 본문을 옮겨 적지 말고 **사실을 골라 압축**하세요.
 - 본문이 온전히 주어진 기사는 3줄을 채우세요. 스니펫뿐이면 확실한 만큼만 쓰고,
   2줄이든 1줄이든 줄 수를 채우려고 없는 사실을 만들지는 마세요.
{detail_rule}
 - 한 줄 60자 이내. 명사형으로 끝냅니다.
   허용: "~함", "~임", "~됨", "~있음", "~전망", "~계획"  /  금지: "~했다", "~이다", "~합니다"
 - 금액·척수·날짜·비율·선박명·항로는 기사에 나온 그대로 넣으세요. 숫자가 있는 문장이
   가장 쓸모 있습니다. (좋은 예: "2030년 상반기 인도 예정, 24일 공시")
 - **제목이 가리키는 사건을 첫 줄에 쓰세요.** 본문을 끝까지 읽고, 이 기사가 전하는
   사건이 무엇인지 먼저 정하세요. 본문 중간의 배경 설명(과거 규제 연혁, 다른 기관의
   지난 발표, 일반 통계)은 그 사건을 설명할 때만 곁들이고, 그것만으로 불릿을 채우지
   마세요. (나쁜 예, 2026-09-07 실측: 제목이 '북극항로·필수선대 키운다… 정부, 공급망
   안전판 구축'인데 불릿이 'IMO가 지난 5월 MASS Code를 채택함'뿐이었다. 이 기사의
   사건은 정부 2027년 예산안이고, MASS Code 는 본문 중간의 배경 문단이다.)
 - 기사에 없는 사실을 지어내지 마세요. 전망·평가를 덧붙이지 마세요.
 - **[여기서 잘림] 표시 뒤를 추측해 채우지 마세요.** 내용이 스니펫뿐이라 확실한 사실이
   한 줄도 없으면 ko 를 빈 배열로 두세요. 빈 배열이 지어낸 문장보다 낫습니다.
 - en 은 같은 내용의 자연스러운 영어입니다.
 - 한자(漢字)·중국어·일본어 문자를 쓰지 마세요. 영문 고유명사(HMM, IMO, LNG)는 그대로.

[기사 목록]
{news_list}
"""


SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group": {"type": "string"},
                    "subject_ko": {"type": "string"},
                    "subject_en": {"type": "string"},
                    "title_ko": {"type": "string"},
                    "title_en": {"type": "string"},
                    "ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["group", "subject_ko", "subject_en",
                             "title_ko", "title_en", "ids"],
            },
        },
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "ko": {"type": "array", "items": {"type": "string"}},
                    "en": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "ko", "en"],
            },
        },
    },
    "required": ["issues", "bullets"],
}


def build_prompt(groups: list[tuple[str, list]],
                 detail_groups=()) -> tuple[str, dict]:
    """(그룹명, 기사목록) 목록을 프롬프트와 id→기사 매핑으로 만든다.

    본문(body)이 있으면 본문을, 없으면 스니펫 요약을 넣는다. 이번 주 실측으로
    42건 중 22건만 본문이 있다 — 나머지는 구글 뉴스 리다이렉트라 크롤이 실패한다.
    스니펫만 있는 기사는 잘림 표시를 붙여, 모델이 뒷말을 지어내지 않게 한다.
    """
    id_map, lines = {}, []
    idx = 0
    for group, arts in groups:
        lines.append(f"[그룹] {group}")
        for art in arts:
            id_map[idx] = art
            body = art.get("body") or ""
            text = (body or art.get("summary_ko") or art.get("summary", ""))[:BODY_LIMIT]
            if not body or len(art.get("body") or "") > BODY_LIMIT:
                text = text.rstrip().rstrip(".…") + " …[여기서 잘림]"
            title = art.get("title_ko") or art.get("title", "")
            tag = f"[고객사: {art['customer']}] " if art.get("customer") else ""
            lines.append(f"  [id={idx}] {tag}{title}")
            lines.append(f"      출처: {art.get('source', '')}")
            lines.append(f"      내용: {text}")
            idx += 1
        lines.append("")
    detail_rule = ""
    if detail_groups:
        names = " · ".join(f"'{g}'" for g in detail_groups)
        detail_rule = (f" - 단 {names} 그룹의 기사는 한 건씩 크게 싣는 카드라 "
                       f"{MAX_BULLETS_DETAIL}줄까지 씁니다. 본문이 있으면 "
                       f"{MAX_BULLETS_DETAIL}줄을 채우세요.")
    prompt = PROMPT.format(news_list="\n".join(lines), max_per_issue=MAX_PER_ISSUE,
                           detail_rule=detail_rule)
    return prompt, id_map


def _normalize(data: dict, id_map: dict, groups: list[tuple[str, list]],
               detail_groups=()) -> dict:
    """모델 응답을 링크 기반 구조로 바꾸고, 빠진 기사를 되살린다.

    모델이 기사를 빠뜨리거나 두 번 넣을 수 있다. 그대로 렌더링하면 기사가 조용히
    사라지거나 두 번 실린다 — 둘 다 지금 평면 목록에서는 일어나지 않던 사고다.
    그래서 여기서 강제로 맞춘다: 중복은 첫 이슈만 남기고, 어느 이슈에도 안 들어간
    기사는 원래 그룹에 1건짜리 이슈로 되돌린다.
    """
    # 기사 링크 → 그룹. 그룹마다 불릿 상한이 달라서 필요하다.
    link_group = {a.get("link"): g for g, arts in groups for a in arts}
    id_map_by_link = {a.get("link"): a for a in id_map.values() if a.get("link")}

    bullets = {}
    for row in data.get("bullets", []):
        art = id_map.get(int(row.get("id", -1)))
        if not art or not art.get("link"):
            continue
        ko = [b.strip() for b in (row.get("ko") or []) if b and b.strip()]
        en = [b.strip() for b in (row.get("en") or []) if b and b.strip()]
        cap = (MAX_BULLETS_DETAIL if link_group.get(art["link"]) in detail_groups
               else MAX_BULLETS)
        if ko:
            bullets[art["link"]] = {"ko": ko[:cap], "en": en[:cap]}

    group_links = {g: {a.get("link") for a in arts} for g, arts in groups}
    issues, used = [], set()
    for row in data.get("issues", []):
        group = row.get("group", "")
        if group not in group_links:
            continue
        links = []
        for raw in (row.get("ids") or []):
            art = id_map.get(int(raw))
            if not art or not art.get("link") or art["link"] in used:
                continue
            # 모델이 다른 그룹의 기사를 끌어오면 버린다 (그룹 경계는 우리가 정한 것이다)
            if art["link"] not in group_links[group]:
                continue
            used.add(art["link"])
            links.append(art["link"])
        if not links or not (row.get("title_ko") or "").strip():
            continue
        # 머릿말이 비면 카드에 태그 칩이 통째로 빠져 다른 카드와 어긋난다.
        # 모델이 비워 보내면 우리가 아는 주체(고객사·경쟁사 태그)로 채운다.
        subject = (row.get("subject_ko") or "").strip()
        if not subject:
            first = id_map_by_link.get(links[0], {})
            subject = first.get("customer") or first.get("competitor") or ""
        issues.append({
            "group": group,
            "subject_ko": subject,
            "subject_en": (row.get("subject_en") or "").strip() or subject,
            "title_ko": row["title_ko"].strip(),
            "title_en": (row.get("title_en") or row["title_ko"]).strip(),
            "links": links[:MAX_PER_ISSUE],
        })

    # 어느 이슈에도 안 들어간 기사를 되살린다 (그룹 순서를 지켜 뒤에 붙인다)
    for group, arts in groups:
        for art in arts:
            link = art.get("link")
            if not link or link in used:
                continue
            used.add(link)
            subject = guess_subject(art)
            issues.append({
                "group": group,
                "subject_ko": subject,
                "subject_en": subject,
                "title_ko": art.get("title_ko") or art.get("title", ""),
                "title_en": art.get("title_en") or art.get("title", ""),
                "links": [link],
            })

    return {"issues": issues, "bullets": bullets}


def _batches(groups: list[tuple[str, list]],
             limit: int = MAX_ARTICLES_PER_CALL) -> list[list[tuple[str, list]]]:
    """그룹을 기사 수 기준으로 묶어 호출 단위를 만든다.

    그룹은 원래 쪼개지 않았다. 그런데 AI 융합 기사가 카테고리 상한 없이 실리면서
    (2026-10-06, 3_build_html.select_all_news) AI 칸 하나가 40건 안팎이 된다. 한 호출에
    40건을 넣으면 출력 토큰 한도에 걸릴 수 있어, limit 를 넘는 그룹만 같은 이름으로
    나눠 보낸다. 나뉜 조각끼리는 이슈로 묶이지 않는다 — 점수순으로 나누므로 상위 기사끼리는
    같은 조각에 든다."""
    split = []
    for group, arts in groups:
        if len(arts) > limit:
            split += [(group, arts[i:i + limit]) for i in range(0, len(arts), limit)]
        else:
            split.append((group, arts))
    groups = split
    out, cur, count = [], [], 0
    for group, arts in groups:
        if cur and count + len(arts) > limit:
            out.append(cur)
            cur, count = [], 0
        cur.append((group, arts))
        count += len(arts)
    if cur:
        out.append(cur)
    return out


def build(groups: list[tuple[str, list]], detail_groups=()) -> dict:
    """이슈·불릿을 만든다. 실패는 호출자가 처리한다(기존 평면 목록으로 되돌아간다).

    detail_groups 에 든 그룹은 불릿을 한 줄 더 받는다(MAX_BULLETS_DETAIL).
    그룹 이름은 빌더가 정하므로(3_build_html) 호출부에서 넘긴다.

    호출은 그룹 단위로 나눈다(MAX_ARTICLES_PER_CALL 주석). 한 배치가 실패하면 그
    배치의 기사만 불릿 없이 1건짜리 이슈로 떨어지고, 나머지 배치는 그대로 살아남는다.
    전부 실패했을 때만 예외를 올린다 — 그때는 호출자가 평면 목록으로 되돌아가는 게 맞다.
    """
    groups = [(g, arts) for g, arts in groups if arts]
    if not groups:
        return {"issues": [], "bullets": {}}

    batches = _batches(groups)
    total = sum(len(arts) for _, arts in groups)
    print(f"  이슈·불릿: 실릴 기사 {total}건 / {len(groups)}개 그룹 "
          f"/ {len(batches)}회 호출")

    issues, bullets, failed, last_error = [], {}, 0, None
    for i, batch in enumerate(batches, 1):
        prompt, id_map = build_prompt(batch, detail_groups)
        names = " · ".join(g for g, _ in batch)
        try:
            try:
                data = gemini.ask_json(prompt, SCHEMA, model=gemini.SMART_MODEL,
                                       max_output_tokens=OUT_TOKENS, temperature=0.2)
            except gemini.GeminiError as e:
                # 분당 한도(429)·혼잡(503)은 1분 기다리면 풀린다. 클라이언트 재시도는
                # 지수 백오프라 합쳐도 십여 초뿐이라 분당 한도를 못 넘긴다.
                # 실측(2026-10-06 미리보기): 3배치가 전부 429·503 으로 실패해 메일이
                # 이슈 한 줄 목록 대신 기사 요약 카드(폴백)로 나갔다 — "왜 다시 기사
                # 본문이 끼어들었어". 같은 날 몇 분 뒤 같은 키로 정상 응답했다.
                if not any(code in str(e) for code in ("429", "503")):
                    raise
                print(f"    [{i}/{len(batches)}] {names} — 한도/혼잡, 65초 후 한 번 더 시도")
                time.sleep(65)
                try:
                    data = gemini.ask_json(prompt, SCHEMA, model=gemini.SMART_MODEL,
                                           max_output_tokens=OUT_TOKENS, temperature=0.2)
                except gemini.GeminiError as e2:
                    # 그래도 막히면 가벼운 모델로 간다. 한도는 모델별로 따로 잡힌다.
                    # 실측(2026-10-06 16:05): SMART_MODEL 이 1분 뒤에도 429 로 6배치 중
                    # 5배치가 실패했는데, 같은 시각 FAST_MODEL 은 정상 응답했다.
                    # 이슈 한 줄·불릿은 요약 작업이라 가벼운 모델로도 형식이 유지된다.
                    if not any(code in str(e2) for code in ("429", "503")):
                        raise
                    print(f"    [{i}/{len(batches)}] {names} — {gemini.FAST_MODEL} 로 대체")
                    data = gemini.ask_json(prompt, SCHEMA, model=gemini.FAST_MODEL,
                                           max_output_tokens=OUT_TOKENS, temperature=0.2)
        except Exception as e:
            # 이 배치만 포기한다. _normalize 에 빈 응답을 넘기면 '어느 이슈에도 안 들어간
            # 기사' 되살리기가 돌아, 기사마다 1건짜리 이슈가 생긴다 — 불릿은 없지만
            # 사진·제목 카드는 정상으로 나간다.
            failed += 1
            last_error = e
            print(f"    [{i}/{len(batches)}] {names} — 실패 ({type(e).__name__}: {e})")
            print("      → 이 그룹은 불릿 없이 기사별 카드로 나갑니다")
            data = {"issues": [], "bullets": []}
        part = _normalize(data, id_map, batch, detail_groups)
        issues.extend(part["issues"])
        bullets.update(part["bullets"])

    if failed == len(batches):
        raise gemini.GeminiError(f"이슈·불릿 호출이 모두 실패했습니다 ({last_error})")

    merged = sum(1 for i in issues if len(i["links"]) > 1)
    print(f"    → 이슈 {len(issues)}개 (묶인 이슈 {merged}개) / 불릿 {len(bullets)}건"
          + (f" / 실패한 호출 {failed}회" if failed else ""))
    return {"issues": issues, "bullets": bullets}


# 제목에서 주체를 뽑는 규칙 (모델이 subject_ko 를 못 채웠을 때의 대체값).
#
# 2026-10-06 피드백.
# 이슈 호출이 한도(429)로 실패하면 기사마다 1건짜리 이슈로 되살리는데, 그때 주체를
# 고객사·경쟁사 태그에서만 가져와서 태그 없는 기사(대부분)는 '[주체]'가 빈 채로 나갔다.
# 한국어 헤드라인은 '주체, 내용' 꼴이 많고('HD현대중공업, 태안 해상에…'), 영문은
# 첫 고유명사가 주체다('Flexport launches…'). 못 찾으면 빈 값 — 지어내지 않는다.
#
# 실측(2026-10-06 92건)으로 거른 오답: '[부산에서 아시아로]'(조사로 끝나는 구절),
# '[사람 부족한 조선소]'(수식 구절, 세 단어), '[AI]'(일반명사),
# '[Maritime Uses AI]'·'[Watch When AI]'(영문 제목이 단어마다 대문자).
_KO_SUBJECT = re.compile(r"^([^,，…·\s][^,，…]{0,14}?)\s*[,，]")
_KO_PARTICLE_END = ("로", "서", "한", "는", "은", "을", "를", "의", "에", "와", "과", "도", "만")
_GENERIC_SUBJECTS = {"ai", "ax", "dx", "해운", "조선", "해양", "정부", "업계"}
_EN_STOP = {"and", "to", "for", "of", "in", "on", "at", "with", "the", "a", "an"}


def guess_subject(art: dict) -> str:
    name = km.entity_name(art)
    if name:
        return name
    title = km.strip_corner(km.strip_outlet(art.get("title_ko") or art.get("title", "")))
    m = _KO_SUBJECT.match(title)
    if m:
        s = m.group(1).strip()
        if (len(s.split()) <= 2 and not s.endswith(_KO_PARTICLE_END)
                and s.lower() not in _GENERIC_SUBJECTS):
            return s
        return ""
    # 영문: 첫 고유명사 뒤에 소문자 동사가 와야 주체로 본다('Flexport launches …').
    # 단어마다 대문자인 제목('Maritime Uses AI Every Day')은 판별할 수 없어 비운다.
    raw = (art.get("title") or "").split()
    words = []
    for w in raw:
        if w.lower() in _EN_STOP or not w[:1].isupper() or w.endswith(":") or len(words) >= 3:
            break
        words.append(w.strip("'\""))
    nxt = raw[len(words)] if len(raw) > len(words) else ""
    if (words and re.match(r"[A-Za-z]", words[0]) and nxt[:1].islower()
            and " ".join(words).lower() not in _GENERIC_SUBJECTS):
        return " ".join(words)
    return ""


def index(analysis: dict) -> tuple[dict, dict]:
    """빌더가 쓰기 좋은 형태로 꺼낸다: (그룹명 → 이슈목록, 링크 → 불릿).

    issues 가 없으면 ({}, {}) 를 돌려준다. 빌더는 이걸 보고 기존 평면 목록으로 간다.
    """
    by_group = {}
    for issue in analysis.get("issues") or []:
        by_group.setdefault(issue.get("group", ""), []).append(issue)
    return by_group, (analysis.get("bullets") or {})
