"""`priorauth`, the command line of this project. Synthetic data only; no command calls a model or spends money.

    priorauth policies list [--file FILE]     procedures, policy numbers and criteria
    priorauth policies check [FILE ...]       validate policy files (default: the packaged policies.yaml)
    priorauth decide FILE [--json]            the decision for one synthetic case file (data/<split>/cases/*.json)
                                              or FHIR Bundle, from the recorded model responses in eval/cassettes
    priorauth demo [--host H] [--port P]      the demo API (priorauth.demo)
    priorauth smoke --api URL [--ui URL]      smoke test of a running demo

`decide` runs the production path: agent v2 Haiku behind the model view, then the verifier and the policy engine.
Exit codes: 0 done; 1 invalid input; 2 `decide` got no reading from the agent (for instance no recording for this
record): the engine's fail-closed PEND is printed, but no model read the record.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from priorauth.api.fhir import BundleError, case_to_bundle, parse_bundle
from priorauth.data.schema import Case
from priorauth.pipeline import CaseRun, run_case
from priorauth.policy.criteria import POLICY_FILE, PolicyError, PolicySet, get_procedure, load_policies
from priorauth.policy.models import Status

QUOTE_CHARS = 90


def _policies_list(args: argparse.Namespace) -> int:
    policies = load_policies(args.file)
    print(f"{policies.payer} ({args.file})")
    for proc in policies.procedures.values():
        print(f"\n{proc.code}  {proc.name}  [{proc.policy_id}]")
        for c in proc.criteria:
            print(f"  {c.id:<40} {c.title}")
    return 0


def _summary(policies: PolicySet) -> str:
    criteria = sum(len(p.criteria) for p in policies.procedures.values())
    return f"{len(policies.procedures)} procedures, {criteria} criteria, {len(policies.parameters)} parameters"


def _policies_check(args: argparse.Namespace) -> int:
    failed = False
    for path in args.files or [POLICY_FILE]:
        try:
            print(f"ok    {path}: {_summary(load_policies(path))}")
        except (OSError, PolicyError) as e:
            failed = True
            print(f"FAIL  {path}: {e}", file=sys.stderr)
    return 1 if failed else 0


def _load_case(path: Path) -> Case:
    """A case file or a Bundle, as the API would receive it (so the model sees what the recordings were made from)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("resourceType") == "Bundle":
        return parse_bundle(payload, case_id=path.stem).case
    case = Case.model_validate(payload)
    return parse_bundle(case_to_bundle(case, case.case_id), case_id=case.case_id).case


def _print_run(case: Case, run: CaseRun) -> None:
    proc = get_procedure(case.procedure_code)
    print(f"{case.case_id}  {proc.code}  {proc.name}  [{proc.policy_id}]")
    print(f"decision: {run.decision.outcome.value}")
    findings = {f.criterion_id: f for f in run.verified.findings}
    for cid, status in run.decision.criteria.items():
        finding = findings.get(cid)
        quote = ""
        if finding and finding.verified and finding.evidence and status is not Status.INSUFFICIENT:
            first = finding.evidence[0]
            quote = f'p.{first.page} "{first.quote[:QUOTE_CHARS]}"'
        print(f"  {status.value:<13} {cid:<40} {quote}".rstrip())
    if run.decision.reasons:
        print("reasons:")
        for r in run.decision.reasons:
            print(f"  {r.code.value:<20} {r.criterion_id or '-':<40} {r.detail}".rstrip())
    print(f"model calls: {run.usage.llm_calls} replayed from recordings (recorded cost {run.usage.cost_usd:.6f} USD)")


def _decide(args: argparse.Namespace) -> int:
    from priorauth.demo.server import replay_system

    try:
        case = _load_case(args.file)
    except BundleError as e:  # the API's own checks: FHIR structure, synthetic tag, required resources
        print(f"{args.file}: not accepted: {'; '.join(err['msg'] for err in e.errors)}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:  # unreadable file, invalid JSON or an invalid case
        print(f"{args.file}: {e}", file=sys.stderr)
        return 1
    run = asyncio.run(run_case(replay_system(args.cassettes)(), case))
    if args.json:
        print(run.model_dump_json(indent=2))
    else:
        _print_run(case, run)
    if run.error:
        print(f"no reading from the agent ({run.error}); the engine pended the case (fail closed)", file=sys.stderr)
        return 2
    return 0


def _demo(args: argparse.Namespace) -> int:
    from priorauth.demo.server import DemoConfig, serve

    asyncio.run(serve(DemoConfig.from_env(), args.host, args.port))
    return 0


def _smoke(args: argparse.Namespace) -> int:
    from priorauth.demo import smoke

    return smoke.main(["--api", args.api, *(["--ui", args.ui] if args.ui else [])])


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="priorauth", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = root.add_subparsers(dest="command", required=True)

    policies = commands.add_parser("policies", help="list or check the payer policies")
    policy_commands = policies.add_subparsers(dest="policy_command", required=True)
    listing = policy_commands.add_parser("list", help="procedures, policy numbers and criteria")
    listing.add_argument("--file", type=Path, default=POLICY_FILE, help="a policy file (default: the packaged one)")
    listing.set_defaults(run=_policies_list)
    check = policy_commands.add_parser("check", help="validate policy files")
    check.add_argument("files", nargs="*", type=Path, help="policy files (default: the packaged one)")
    check.set_defaults(run=_policies_check)

    decide = commands.add_parser("decide", help="decide one synthetic case from recorded model responses")
    decide.add_argument("file", type=Path, help="a case file (data/<split>/cases/*.json) or a FHIR Bundle")
    decide.add_argument("--cassettes", type=Path, default=Path("eval/cassettes"), help="recorded responses")
    decide.add_argument("--json", action="store_true", help="print the whole run (findings, verification, decision)")
    decide.set_defaults(run=_decide)

    demo = commands.add_parser("demo", help="the demo API on synthetic data")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)
    demo.set_defaults(run=_demo)

    smoke = commands.add_parser("smoke", help="smoke test of a running demo")
    smoke.add_argument("--api", required=True, help="the API base URL")
    smoke.add_argument("--ui", help="the UI base URL")
    smoke.set_defaults(run=_smoke)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        code: int = args.run(args)
    except PolicyError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
