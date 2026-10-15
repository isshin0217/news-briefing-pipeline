"""
profile_loader.py
회사/팀별 설정(브랜드, 회사 소개, 검색어, 키워드, 고객사, 경쟁사)을 불러온다.

- profile.json 이 있으면 그것을, 없으면 profile.example.json 을 사용한다.
- 환경변수 PROFILE_PATH 로 다른 파일을 지정할 수도 있다.
- profile.json 은 .gitignore 대상이라 실제 회사 정보가 커밋되지 않는다.
"""

import json
import os
from pathlib import Path

_BASE = Path(__file__).parent


def _resolve_path() -> Path:
    env = os.getenv("PROFILE_PATH")
    if env:
        return Path(env)
    real = _BASE / "profile.json"
    return real if real.exists() else _BASE / "profile.example.json"


PROFILE_PATH = _resolve_path()
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

BRAND = PROFILE["brand"]
QUERIES = PROFILE.get("queries", {})
KEYWORDS = PROFILE.get("keywords", {})
CUSTOMERS = PROFILE.get("customers", [])
CUSTOMER_GROUPS = PROFILE.get("customer_groups", {})
COMPETITORS = PROFILE.get("competitors", [])


def company_context() -> str:
    ctx = PROFILE.get("company_context", "")
    return "\n".join(ctx) if isinstance(ctx, list) else ctx


def analysis_rules() -> str:
    return "\n".join(f"  · {r}" for r in PROFILE.get("analysis_rules", []))


def customer_en(name: str) -> str:
    for c in CUSTOMERS:
        if c["name"] == name:
            return c.get("name_en", name)
    return name


def customer_group(name: str) -> tuple[str, str]:
    """고객사명 → (한글 구분 배지, 영문 구분 배지). 그룹이 없으면 ('', '')."""
    for c in CUSTOMERS:
        if c["name"] == name:
            g = CUSTOMER_GROUPS.get(c.get("group", ""), {})
            return (g.get("ko", ""), g.get("en", ""))
    return ("", "")
