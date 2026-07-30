"""Validate SP Group documentation lifecycle and local links."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REQUIRED_SECTIONS = ("## Status", "## Authority")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def main() -> int:
    errors: list[str] = []
    markdown = sorted(DOCS.rglob("*.md"))
    expected = set(markdown)
    if not markdown:
        errors.append("docs contains no Markdown files")

    for path in markdown:
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)
        if path != DOCS / "context-tracker.md" and path.parent.name in {
            "adr",
            "issues",
            "plans",
        }:
            for section in REQUIRED_SECTIONS:
                if section not in text:
                    errors.append(f"{relative}: missing {section}")
        for target in LINK_RE.findall(text):
            target = target.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("/"):
                resolved = ROOT / target.lstrip("/")
            else:
                resolved = (path.parent / target).resolve()
            if resolved.suffix == "":
                resolved = resolved.with_suffix(".md")
            if resolved not in expected and not resolved.exists():
                errors.append(f"{relative}: missing local link target {target}")

    if errors:
        print("Documentation validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"Documentation validation passed ({len(markdown)} Markdown files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
