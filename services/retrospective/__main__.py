import argparse
import sys

from services.retrospective.autopilot import AUTOPILOT_BRANCH, check_autopilot_allowed


def main() -> None:
    parser = argparse.ArgumentParser(description="EVE Trader Retrospective & Autopilot CLI")
    parser.add_argument("--check-branch", action="store_true", help="Exit 0 if on AUTOPILOT_BRANCH, else 1")
    parser.add_argument("--print-branch", action="store_true", help="Print configured AUTOPILOT_BRANCH name")
    args = parser.parse_args()

    if args.print_branch:
        print(AUTOPILOT_BRANCH)
        sys.exit(0)

    if args.check_branch:
        allowed, _, reason = check_autopilot_allowed()
        if allowed:
            sys.exit(0)
        print(f"[=] {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
