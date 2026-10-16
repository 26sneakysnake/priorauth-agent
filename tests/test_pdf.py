from pathlib import Path

from priorauth.data.pdf import case_to_pdf
from priorauth.data.schema import Case

CASE = Path(__file__).resolve().parents[1] / "data" / "dev" / "cases" / "dev-001.json"


def test_pdf_has_one_page_per_record_page() -> None:
    case = Case.model_validate_json(CASE.read_bytes())
    pdf = case_to_pdf(case)
    assert pdf.startswith(b"%PDF-")
    assert pdf.count(b"/Type /Page\n") == len(case.pages)
