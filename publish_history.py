"""
publish_history.py
지난 회차에 무엇을 실었는지 기억한다. 지금 쓰는 곳은 '기술 동향의 북극항로 자리'다.

왜 만들었나 (2026-09-21 피드백):
  "해진공, 쇄빙·내빙선 연간 1200억 투자…북극항로 개척 지원사격 — 이거는 몇 주째
   계속 북극항로 기사가 기술동향에 떠서 좀 사람들이 피곤해할것 같아"

  실측(기술 동향 TOP1, 6주 연속):
    08-31  북극항로 운항 '팬스타 아크로호' 31일 북극해 진입
    09-07  북극항로·필수선대 키운다… 정부, 공급망 '안전판' 구축
    09-10  북극해 더 깊이 간다…두 번째 쇄빙연구선 건조 본격화
    09-14  영국 도착한 북극항로 시범운항 선박…이 대통령 "역사적 첫길 축하"
    09-17  Russian Dark Fleet Tactics on the Northern Sea Route
    09-21  해진공, 쇄빙·내빙선 연간 1200억 투자…북극항로 개척 지원사격

  북극 자리는 우연이 아니라 코드가 보장한 것이다(5_analyze 의 '반드시 한 자리').
  아이스네비게이션이 아크메 사업이라 2026-08-25 에 그렇게 정했고, 그 판단 자체는
  지금도 맞다. 문제는 '매주'다 — 여섯 주 내내 1번 카드가 같은 주제면 독자는 그 자리를
  건너뛰고 읽는다. 그래서 보장은 남기되 연속 게재에 상한을 둔다(쉬어가기).

  회차마다 서로 다른 사건이라 '같은 사건 판정'으로는 못 막는다(실측: 위 6건은 전부
  다른 사건이다). 막을 수 있는 축은 '얼마나 연달아 실렸는가'뿐이고, 그건 지난 회차를
  기억해야만 알 수 있다 — 이 파일이 있는 이유다.

설계:
  · 파일이 없거나 깨져 있으면 빈 기록으로 본다. 이 기능 때문에 파이프라인이 멈추면 안 된다.
  · 같은 날짜로 다시 돌리면 덮어쓴다(재실행이 연속 횟수를 부풀리지 않게).
  · 최근 KEEP 회차만 남긴다. 오래된 기록은 쓰지 않는다.
"""

import json
from pathlib import Path

HISTORY_FILE = Path("publish_history.json")
KEEP = 12


def load(path: Path = HISTORY_FILE) -> list[dict]:
    """[{date, arctic, tech_titles}] 를 오래된 것부터 반환한다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    weeks = data.get("weeks") if isinstance(data, dict) else data
    return [w for w in (weeks or []) if isinstance(w, dict) and w.get("date")]


def record(date: str, tech_titles: list[str], arctic: bool,
           path: Path = HISTORY_FILE) -> None:
    """이번 회차를 기록한다. 같은 날짜가 있으면 덮어쓴다."""
    weeks = [w for w in load(path) if w.get("date") != date]
    weeks.append({"date": date, "arctic": bool(arctic),
                  "tech_titles": list(tech_titles)[:6]})
    weeks.sort(key=lambda w: w["date"])
    try:
        path.write_text(json.dumps({"weeks": weeks[-KEEP:]}, ensure_ascii=False,
                                   indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  [주의] 게재 이력을 저장하지 못했습니다 ({e})")


def arctic_streak(path: Path = HISTORY_FILE) -> int:
    """최근 회차부터 세어, 북극 기사가 기술 동향에 연달아 실린 횟수.

    오늘 회차는 아직 기록 전이므로 '지난 회차까지'의 연속 횟수다.
    """
    streak = 0
    for week in reversed(load(path)):
        if not week.get("arctic"):
            break
        streak += 1
    return streak
