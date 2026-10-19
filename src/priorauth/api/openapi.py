"""Export the OpenAPI schema of the API (the UI client is generated from it).

python -m priorauth.api.openapi            # writes docs/openapi.json
python -m priorauth.api.openapi --check    # exit 1 if docs/openapi.json is out of date
"""

import argparse
import json
import sys
from pathlib import Path

from priorauth.api.app import Settings, create_app

OUTPUT = Path("docs/openapi.json")


def render() -> str:
    """The API's schema, plus the /demo routes (tag "demo"), which only the demo server (priorauth.demo) mounts."""
    from priorauth.demo.routes import router as demo_router

    app = create_app(Settings(run_workers=False))
    app.include_router(demo_router)
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != text:
            print(f"{OUTPUT} is out of date; run python -m priorauth.api.openapi", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
