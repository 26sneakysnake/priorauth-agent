import argparse
import asyncio

from priorauth.demo.server import DemoConfig, serve


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo API on synthetic data (see priorauth.demo.server)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    asyncio.run(serve(DemoConfig.from_env(), args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
