"""Procesamiento de archivos de subtítulos .ass con pysubs2."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pysubs2

UNASSIGNED_CHARACTER = "Sin asignar (Por defecto)"
GroupByMode = Literal["name", "style"]

_WHITESPACE_RE = re.compile(r"\s+")
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass(frozen=True)
class SubtitleLine:
    """Representa una línea válida del guion .ass ya ordenada y limpia."""

    index: int
    start_ms: int
    end_ms: int
    character: str
    text: str

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True)
class ParsedScript:
    """Resultado completo del análisis de un archivo .ass."""

    file_path: Path
    group_by: GroupByMode
    lines: list[SubtitleLine]
    character_counts: dict[str, int]

    @property
    def total_lines(self) -> int:
        return len(self.lines)

    @property
    def total_characters(self) -> int:
        return len(self.character_counts)


def clean_ass_text(event: pysubs2.SSAEvent) -> str:
    """Extrae el texto limpio sin etiquetas ASS y normaliza saltos de línea."""
    plain = event.plaintext or ""
    normalized = _WHITESPACE_RE.sub(" ", plain.replace("\r", " ").replace("\n", " "))
    return normalized.strip()


def sanitize_filename(name: str, max_length: int = 40) -> str:
    """Convierte un nombre de personaje en un fragmento seguro para nombres de archivo."""
    cleaned = _INVALID_FILENAME_CHARS_RE.sub("", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "SinAsignar"
    return cleaned[:max_length]


def format_ms(ms: int) -> str:
    """Formatea milisegundos como HH:MM:SS.mmm para los logs."""
    sign = "-" if ms < 0 else ""
    total_ms = abs(int(ms))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def load_ass_script(
    file_path: str | Path,
    group_by: GroupByMode = "name",
) -> ParsedScript:
    """Carga y analiza un archivo .ass, filtrando comentarios y líneas vacías.

    Args:
        file_path: Ruta al archivo .ass.
        group_by: 'name' para agrupar por Actor (evento.name) o 'style' por Estilo (evento.style).
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"No se encontró el archivo de subtítulos: {path}")

    subs = _load_with_fallback_encoding(path)

    raw_events: list[tuple[int, int, str, str]] = []
    for event in subs.events:
        if event.is_comment:
            continue

        cleaned_text = clean_ass_text(event)
        if not cleaned_text:
            continue

        raw_char = (event.name if group_by == "name" else event.style) or ""
        character = raw_char.strip() or UNASSIGNED_CHARACTER

        start_ms = max(0, int(event.start))
        end_ms = max(start_ms, int(event.end))
        raw_events.append((start_ms, end_ms, character, cleaned_text))

    # Orden cronológico por tiempo de inicio (y fin como desempate)
    raw_events.sort(key=lambda item: (item[0], item[1]))

    lines: list[SubtitleLine] = []
    counter: Counter[str] = Counter()

    for idx, (start_ms, end_ms, character, text) in enumerate(raw_events, start=1):
        lines.append(
            SubtitleLine(
                index=idx,
                start_ms=start_ms,
                end_ms=end_ms,
                character=character,
                text=text,
            )
        )
        counter[character] += 1

    # Ordenar personajes: primero los de más líneas (y dejar Sin asignar al final)
    sorted_chars = sorted(
        counter.items(),
        key=lambda kv: (kv[0] == UNASSIGNED_CHARACTER, -kv[1], kv[0].lower()),
    )

    return ParsedScript(
        file_path=path,
        group_by=group_by,
        lines=lines,
        character_counts=dict(sorted_chars),
    )


def _load_with_fallback_encoding(path: Path) -> pysubs2.SSAFile:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return pysubs2.load(str(path), encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(
        f"No se pudo decodificar el archivo '{path.name}' con codificaciones estándar."
    ) from last_error
