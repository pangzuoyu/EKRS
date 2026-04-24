"""CLI for EKRS RAG — query engineering constraints via the three-gate pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _build_payload(query: str, context: dict, strict: bool, top_k: int) -> dict:
    """Build the request payload matching ConstraintQuery."""
    return {
        "query": query,
        "context": context,
        "strict": strict,
        "replay": False,
        "trace_id": None,
        "top_k": top_k,
    }


def _make_request(api_url: str, payload: dict) -> dict:
    """POST to /v1/constraints and return parsed JSON response."""
    url = f"{api_url}/v1/constraints"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            err_body = json.loads(body)
            detail = err_body.get("detail", body)
        except Exception:
            detail = body or str(e)
        print(f"Error: HTTP {e.code} — {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Connection failed — {e.reason}", file=sys.stderr)
        sys.exit(1)


def _print_response(data: dict) -> None:
    """Print the API response in a human-readable format."""
    mode = data.get("mode", "unknown")
    branches = data.get("branches", {})
    conflicts = data.get("conflicts", [])
    trace = data.get("trace", [])

    print(f"[mode: {mode}]")
    print(f"[branches: {len(branches)} items]")
    for branch_key, branch_params in branches.items():
        marker = " *" if branch_key == data.get("primary_branch") else ""
        print(f"  [{branch_key}]{marker}")
        for key, val in branch_params.items():
            print(f"    {key}: {val}")

    if conflicts:
        print(f"\n[conflicts: {len(conflicts)}]")
        for i, conflict in enumerate(conflicts, 1):
            print(f"  {i}. {conflict}")
    else:
        print("\n[no conflicts]")

    if trace:
        print(f"\n[trace: {len(trace)} steps]")
        for step in trace:
            print(f"  - {step}")


def _print_json(data: dict) -> None:
    """Print the raw JSON response."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def query(args: argparse.Namespace) -> None:
    """Execute the query subcommand."""
    api_url = os.environ.get("EKRS_API_URL", "http://localhost:8000")

    context = {}
    if args.context:
        try:
            context = json.loads(args.context)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid --context JSON: {e}", file=sys.stderr)
            sys.exit(1)

    payload = _build_payload(
        query=args.query,
        context=context,
        strict=args.strict,
        top_k=args.top_k,
    )

    response = _make_request(api_url, payload)

    if args.json:
        _print_json(response)
    else:
        _print_response(response)


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="ekrs_rag.cli",
        description="EKRS RAG CLI — query engineering constraints.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    query_parser = subparsers.add_parser("query", help="Query constraints via the three-gate pipeline")
    query_parser.add_argument("query", type=str, help="Natural language query string")
    query_parser.add_argument(
        "--context",
        type=str,
        default=None,
        help="JSON object with context (e.g., '{\"material\": \"Q345\"}')",
    )
    query_parser.add_argument(
        "--strict",
        action="store_true",
        help="Enable strict mode (forbid inference, missing context returns 400)",
    )
    query_parser.add_argument(
        "--top-k",
        type=int,
        default=40,
        help="Number of chunks to retrieve (default: 40)",
    )
    query_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of human-readable format",
    )

    parsed = parser.parse_args()

    if parsed.command == "query":
        query(parsed)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    cli()
