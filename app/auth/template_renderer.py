from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.auth.constants import TEMPLATES_DIR


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


@dataclass(frozen=True)
class RenderedTemplate:
    filename: str
    content: str
    subtype: str  # "plain" or "html"
    path: Path


def resolve_template_path(template_filename: str) -> Path:
    if not template_filename or not template_filename.strip():
        raise ValueError("Template filename is required.")

    raw_path = Path(template_filename)
    if raw_path.is_absolute():
        raise ValueError("Absolute template paths are not allowed.")

    template_path = (TEMPLATES_DIR / raw_path).resolve()
    templates_root = TEMPLATES_DIR.resolve()

    try:
        template_path.relative_to(templates_root)
    except ValueError as exc:
        raise ValueError("Template path escapes templates directory.") from exc

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_filename}")

    if not template_path.is_file():
        raise ValueError(f"Template path is not a file: {template_filename}")

    if template_path.suffix.lower() not in {".txt", ".html"}:
        raise ValueError("Only .txt and .html templates are supported.")

    return template_path


def infer_template_subtype(template_path: Path) -> str:
    suffix = template_path.suffix.lower()
    if suffix == ".html":
        return "html"
    if suffix == ".txt":
        return "plain"
    raise ValueError(f"Unsupported template type: {template_path.name}")


def _find_missing_placeholders(
    template_text: str,
    replacements: Mapping[str, Any],
) -> list[str]:
    expected_keys = set(PLACEHOLDER_PATTERN.findall(template_text))
    provided_keys = set(replacements.keys())
    return sorted(expected_keys - provided_keys)


def render_template_text(
    template_text: str,
    replacements: Mapping[str, Any] | None = None,
) -> str:
    safe_replacements = replacements or {}
    missing_keys = _find_missing_placeholders(template_text, safe_replacements)
    if missing_keys:
        missing = ", ".join(missing_keys)
        raise KeyError(f"Missing template replacements: {missing}")

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        value = safe_replacements[key]
        return "" if value is None else str(value)

    return PLACEHOLDER_PATTERN.sub(replacer, template_text)


def render_template(
    template_filename: str,
    replacements: Mapping[str, Any] | None = None,
) -> RenderedTemplate:
    template_path = resolve_template_path(template_filename)
    raw_text = template_path.read_text(encoding="utf-8")
    rendered_content = render_template_text(raw_text, replacements=replacements)

    return RenderedTemplate(
        filename=template_path.name,
        content=rendered_content,
        subtype=infer_template_subtype(template_path),
        path=template_path,
    )