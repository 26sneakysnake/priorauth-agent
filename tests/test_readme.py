"""README.md is generated, every statement in it cites a file that exists, and no document claims compliance."""

import re
from pathlib import Path

from eval import readme

ROOT = Path(__file__).resolve().parents[1]
LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
CLAIM = re.compile(r"\b(complian\w*|certif\w*|conform\w*)", re.IGNORECASE)
NEGATION = re.compile(r"\b(no|not|nothing|never|without|none|non)\b|n't", re.IGNORECASE)


def units(text: str) -> list[str]:
    """Paragraphs, list items, block quotes and table rows of a Markdown text; code, headings and images excluded."""
    out: list[str] = []
    current: list[str] = []
    in_code = False

    def flush() -> None:
        if current:
            out.append(" ".join(current))
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith("#") or stripped.startswith(("![", "[![")):
            flush()
            continue
        if stripped.startswith("|"):
            flush()
            if not set(stripped) <= set("|-: "):
                out.append(stripped)
            continue
        if stripped.startswith(("- ", "> ")) or stripped == ">":
            flush()
        current.append(stripped.removeprefix("> ").removeprefix(">"))
    flush()
    return [u for u in out if u.strip()]


def test_readme_is_generated_from_results() -> None:
    assert (ROOT / "README.md").read_text(encoding="utf-8") == readme.render(), "run make readme"


def test_every_statement_of_the_readme_cites_an_existing_file() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    header_row = "| system | split |"
    for unit in units(text):
        if unit.endswith(":") or unit.startswith(header_row):
            continue  # a lead-in ("They show:") or the header of the results table
        targets = [t for t in LINK_TARGET.findall(unit) if not t.startswith(("http://", "https://", "#"))]
        assert targets, f"no source cited: {unit[:120]}"
    for target in LINK_TARGET.findall(text):
        if not target.startswith(("http://", "https://", "#")):
            assert (ROOT / target).exists(), f"broken link: {target}"


def test_the_readme_opens_with_the_warnings_then_the_demo_account() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    head = text[: text.index("## What it does")]
    assert head.index("Synthetic data only") < head.index("No compliance claimed") < head.index("## Try it")
    assert "demo-reviewer" in head and "resets on every restart" in head and "docs/img/" in head


def test_a_live_demo_link_comes_with_its_recorded_smoke_test() -> None:
    url = readme.demo_url()
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    if not url:
        assert "Live demo: not deployed yet" in text
        return
    assert f"<{url}>" in text and "not deployed yet" not in text and "The demo is not deployed" not in text
    assert f"passed against its live API and UI on {readme.LIVE_SMOKE_DATE}" in text
    assert "deployed by hand, not by `deploy.yml`" in text  # the workflow must not be credited with it
    progress = (ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")
    record = progress[progress.index("## Demo deployment") :]
    assert url in record and readme.LIVE_SMOKE_DATE in record


def test_no_document_claims_compliance_or_certification() -> None:
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        text = " ".join(path.read_text(encoding="utf-8").split())
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if CLAIM.search(sentence):
                assert NEGATION.search(sentence), f"{path.name}: {sentence[:200]}"


def test_units_splits_markdown_as_expected() -> None:
    md = (
        "# T\n\nOne sentence [a](README.md).\nsame paragraph.\n\n- item [b](LICENSE)\n  more\n\n"
        "| x | y |\n|---|---|\n```\ncode\n```"
    )
    assert units(md) == ["One sentence [a](README.md). same paragraph.", "- item [b](LICENSE) more", "| x | y |"]
