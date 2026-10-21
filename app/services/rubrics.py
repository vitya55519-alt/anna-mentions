from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUBRICS_DIR = ROOT / "rubrics"

VERDICT_LIKELY = "likely"
VERDICT_BORDERLINE = "borderline"
VERDICT_CLEAN = "clean"
VERDICT_PENDING = "pending"


def verdict_for(score: float) -> str:
    if score >= 0.8:
        return VERDICT_LIKELY
    if score >= 0.5:
        return VERDICT_BORDERLINE
    return VERDICT_CLEAN


@dataclass(frozen=True, slots=True)
class Category:
    code: str
    title: str
    description: str
    weight: float
    patterns: tuple[str, ...]
    phrases: tuple[str, ...]
    rationale_hint: str


@dataclass(frozen=True, slots=True)
class CategoryHit:
    code: str
    title: str
    score: float
    markers: tuple[str, ...]
    rationale_hint: str


@dataclass(frozen=True, slots=True)
class RubricSet:
    id: str
    version: int
    title: str
    categories: tuple[Category, ...]
    compiled: dict[str, tuple[re.Pattern[str], ...]]
    phrase_re: dict[str, tuple[re.Pattern[str], ...]]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(category.code for category in self.categories)

    def category(self, code: str) -> Category | None:
        return next((item for item in self.categories if item.code == code), None)

    def match(self, text: str) -> list[CategoryHit]:
        """Дешёвый предварительный проход по маркерам — без сети и без LLM."""
        if not text:
            return []
        normalized = " ".join(text.split()).lower()
        hits: list[CategoryHit] = []

        for category in self.categories:
            markers: list[str] = []
            for regex in self.compiled.get(category.code, ()):
                markers.extend(match.group(0).lower() for match in regex.finditer(normalized))
            for regex in self.phrase_re.get(category.code, ()):
                found = regex.search(normalized)
                if found:
                    markers.append(found.group(0).lower())

            unique = tuple(dict.fromkeys(markers))
            if not unique:
                continue

            score = min(0.97, category.weight * (1 - 0.35 ** len(unique)))
            hits.append(
                CategoryHit(
                    code=category.code,
                    title=category.title,
                    score=round(score, 2),
                    markers=unique,
                    rationale_hint=category.rationale_hint,
                )
            )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """`взорв*` → префикс с любым окончанием; слово не должно начинаться с середины другого."""
    body = re.escape(pattern.strip().lower()).replace(r"\*", r"[\w-]*")
    return re.compile(rf"(?<!\w){body}", re.UNICODE)


def _compile_phrase(phrase: str) -> re.Pattern[str]:
    words = [re.escape(word).replace(r"\*", r"[\w-]*") for word in phrase.lower().split()]
    return re.compile(r"\s+".join(words), re.UNICODE)


def load_rubric_set(file_name: str = "uk_rf.json") -> RubricSet:
    path = Path(file_name)
    if not path.is_absolute():
        path = RUBRICS_DIR / file_name
    if not path.exists():
        raise FileNotFoundError(f"Файл рубрик не найден: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    categories: list[Category] = []
    compiled: dict[str, tuple[re.Pattern[str], ...]] = {}
    phrase_re: dict[str, tuple[re.Pattern[str], ...]] = {}

    for item in raw.get("categories", []):
        code = str(item["code"]).upper()
        category = Category(
            code=code,
            title=item.get("title", code),
            description=item.get("description", ""),
            weight=float(item.get("weight", 0.8)),
            patterns=tuple(item.get("patterns", ())),
            phrases=tuple(item.get("phrases", ())),
            rationale_hint=item.get("rationale_hint", ""),
        )
        categories.append(category)
        compiled[code] = tuple(_compile_pattern(p) for p in category.patterns)
        phrase_re[code] = tuple(_compile_phrase(p) for p in category.phrases)

    return RubricSet(
        id=str(raw.get("id", path.stem)),
        version=int(raw.get("version", 1)),
        title=raw.get("title", "Рубрики"),
        categories=tuple(categories),
        compiled=compiled,
        phrase_re=phrase_re,
    )


@lru_cache(maxsize=4)
def get_rubric_set(file_name: str = "uk_rf.json") -> RubricSet:
    return load_rubric_set(file_name)


def rule_rationale(hit: CategoryHit, alternatives: Sequence[CategoryHit] = ()) -> str:
    markers = ", ".join(f"«{marker}»" for marker in hit.markers[:4])
    hint = hit.rationale_hint or hit.title
    text = f"сработали маркеры {markers} — {hint}."
    if alternatives:
        others = ", ".join(f"{item.code} ({item.score:.2f})" for item in alternatives[:3])
        text += f" Ближайшие совпадения: {others}."
    return text + " Предварительная оценка по правилам — нужна проверка юристом."
