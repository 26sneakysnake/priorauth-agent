"""Optional export of case records to PDF (one PDF page per record page), for demos and manual review.

    python -m priorauth.data.pdf --split dev --out build/pdf

Requires `fpdf2` (dev dependency). The JSON pages stay the source of truth; PDFs are not committed.
"""

import argparse
from pathlib import Path

from fpdf import FPDF

from priorauth.data.schema import Case


def case_to_pdf(case: Case) -> bytes:
    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(left=18, top=18, right=18)
    for page in case.pages:
        pdf.add_page()
        pdf.set_font("Courier", size=9)
        # Generated text is ASCII; latin-1 is what the core PDF fonts accept.
        pdf.multi_cell(w=0, h=4.2, align="L", text=page.text.encode("latin-1", "replace").decode("latin-1"))
    return bytes(pdf.output())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--out", type=Path, default=Path("build/pdf"))
    args = parser.parse_args(argv)
    out = args.out / args.split
    out.mkdir(parents=True, exist_ok=True)
    paths = sorted((args.data / args.split / "cases").glob("*.json"))
    for path in paths:
        case = Case.model_validate_json(path.read_bytes())
        (out / f"{case.case_id}.pdf").write_bytes(case_to_pdf(case))
    print(f"wrote {len(paths)} PDFs to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
