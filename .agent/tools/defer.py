"""Defer a staged candidate without making a terminal rejection."""
import argparse
import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BASE, "memory"))
from review_state import mark_deferred

CANDIDATES = os.path.join(BASE, "memory/candidates")


def main():
    parser = argparse.ArgumentParser(description="Defer a staged candidate.")
    parser.add_argument("candidate_id")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--reviewer", default="host-agent")
    args = parser.parse_args()
    try:
        mark_deferred(args.candidate_id, args.reviewer, args.reason, CANDIDATES)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"deferred {args.candidate_id}")


if __name__ == "__main__":
    main()
