"""
2_summarize.py
API 없이 키워드 분류 + Google 무료 번역으로 한국어 제목/요약 생성
실행: python 2_summarize.py
입력: raw_news.json
결과: summarized_news.json
"""

import json
import re
import time
from pathlib import Path
from deep_translator import GoogleTranslator

# Hiragana/Katakana + CJK Ext-A + CJK Unified + CJK Compat. Hangul(AC00-D7A3) excluded.
_CJK_RANGES = [(0x3040, 0x30ff), (0x3400, 0x4dbf), (0x4e00, 0x9fff), (0xf900, 0xfaff)]
CJK_PATTERN = re.compile('[' + ''.join(f'{chr(a)}-{chr(b)}' for a, b in _CJK_RANGES) + ']')

# 카테고리 분류 키워드 (도메인에 맞게 수정해서 사용)
KEYWORD_CATEGORY = {
    "자율운항/스마트십": ["autonomous", "smart ship", "mass", "ai", "digital", "자율운항", "스마트십"],
    "사이버보안/규정":   ["cyber", "imo", "iacs", "regulation", "compliance", "규정", "보안",
                         "tmsa", "rightship", "vetting", "sire", "ocimf", "베팅"],
    "신조/조선":         ["newbuilding", "vessel", "shipbuilding", "order", "delivery", "신조", "조선", "중공업"],
    "탈탄소/친환경":     ["lng", "ammonia", "methanol", "carbon", "emission", "green", "탈탄소", "친환경"],
    "해운시황/물류":     ["freight", "rate", "container", "shipping", "trade", "port", "해운", "물류"],
}

translator_ko = GoogleTranslator(source="en", target="ko")  # 영문 → 한국어
translator_en = GoogleTranslator(source="ko", target="en")  # 한국어 → 영문


def guess_category(title: str, summary: str) -> str:
    text = (title + " " + summary).lower()
    for cat, keywords in KEYWORD_CATEGORY.items():
        if any(kw in text for kw in keywords):
            return cat
    return "기타"


def _clean(text: str) -> str:
    """한자/가나 제거 후 남는 공백·괄호·구두점을 정리한다."""
    text = CJK_PATTERN.sub("", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)   # 빈 괄호 제거
    text = re.sub(r"\s{2,}", " ", text)            # 연속 공백 축소
    text = re.sub(r"\s+([,.)\]])", r"\1", text)    # 구두점 앞 공백 제거
    return text.strip()


def translate(text: str, translator) -> str:
    if not text or not text.strip():
        return ""
    for attempt in range(2):  # 한자가 섞이면 1회 재시도
        try:
            result = translator.translate(text[:4500]) or ""
        except Exception:
            time.sleep(0.6)
            continue
        # 한자가 없으면 바로 사용, 있으면 재시도(마지막엔 제거)
        if not CJK_PATTERN.search(result):
            return _clean(result)
        if attempt == 0:
            time.sleep(0.6)
            continue
        return _clean(result)
    return text  # 번역 실패 시 원문 유지


def run():
    raw_path = Path("raw_news.json")
    if not raw_path.exists():
        print("raw_news.json이 없습니다. 먼저 1_fetch_news.py를 실행하세요.")
        return False

    articles = json.loads(raw_path.read_text(encoding="utf-8"))
    print(f"\n[2단계] 분류 + 번역 시작 - 총 {len(articles)}건\n")

    summarized = []
    for i, art in enumerate(articles, 1):
        category = guess_category(art["title"], art["summary"])
        lang = art.get("lang", "en")

        # 모든 기사에 한/영 4개 필드를 채운다 (부족한 언어만 번역)
        if lang == "en":
            print(f"  [{i}/{len(articles)}] EN→KO 번역: {art['title'][:50]}...")
            title_en, summary_en = art["title"], art["summary"][:800]
            title_ko = translate(art["title"], translator_ko)
            summary_ko = translate(art["summary"][:1000], translator_ko)
            time.sleep(0.5)  # 무료 번역 rate limit 방지
        else:
            print(f"  [{i}/{len(articles)}] KO→EN 번역: {art['title'][:50]}...")
            title_ko, summary_ko = art["title"], art["summary"][:500]
            title_en = translate(art["title"], translator_en)
            summary_en = translate(art["summary"][:1000], translator_en)
            time.sleep(0.5)

        summarized.append({
            **art,
            "title_ko": title_ko,
            "title_en": title_en,
            "summary_ko": summary_ko,
            "summary_en": summary_en,
            "category": category,
            "importance": 4 if art.get("customer") else 2,
            "keywords": [],
            "error": None,
        })

    out = Path("summarized_news.json")
    out.write_text(json.dumps(summarized, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n완료 → {out}")

    from collections import Counter
    cats = Counter(a["category"] for a in summarized)
    print("\n카테고리별 건수:")
    for cat, cnt in cats.most_common():
        print(f"  {cat}: {cnt}건")
    return True


if __name__ == "__main__":
    run()
