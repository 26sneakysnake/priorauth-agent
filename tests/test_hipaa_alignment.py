"""docs/HIPAA_ALIGNMENT.md: every "covered" row names code and tests that exist; the disclaimers are there."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/HIPAA_ALIGNMENT.md"
TICKED = re.compile(r"`([^`]+)`")


def rows() -> list[list[str]]:
    out = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 5 and cells[0] not in ("Reference", "---"):
            out.append(cells)
    return out


def module_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}


def test_every_covered_row_points_to_existing_code_and_tests() -> None:
    table = rows()
    covered = [r for r in table if r[2] == "covered"]
    assert len(table) >= 20 and len(covered) >= 12
    for reference, control, status, code, tests in table:
        assert status == "covered" or status.startswith("not covered"), (reference, status)
        if status != "covered":
            assert code == "" and tests == "", f"{reference}: a row that is not covered names no code or test"
            continue
        paths, refs = TICKED.findall(code), TICKED.findall(tests)
        assert paths and refs, f"{reference} {control[:40]}: covered needs code and tests"
        for p in paths:
            assert (ROOT / p).is_file(), p
        for ref in refs:
            file, _, name = ref.partition("::")
            assert (ROOT / file).is_file(), ref
            functions = module_tests(ROOT / file)
            assert (name in functions) if name else functions, ref


def test_the_document_claims_no_compliance() -> None:
    head = " ".join(DOC.read_text(encoding="utf-8").split("## ")[0].split())
    for required in (
        "Aligned with the principles of the HIPAA Security Rule",
        "Synthetic data only",
        "No claim of compliance or certification",
        "no Business Associate Agreement",
    ):
        assert required in head, required
    for doc in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        text = doc.read_text(encoding="utf-8").lower()
        assert not re.search(r"hipaa[- ]compliant|compliant with hipaa|hipaa[- ]certified", text), doc.name


def covered_test_ids() -> list[str]:
    """The pytest ids named by covered rows (`make hipaa-tests` runs exactly these)."""
    ids = {ref for r in rows() if r[2] == "covered" for ref in TICKED.findall(r[4])}
    return sorted(ids)
