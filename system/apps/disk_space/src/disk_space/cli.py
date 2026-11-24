"""The ``disk-space`` command: serve the page, or check, measure, report and delete from a shell.

disk-space                         serve the page (what the workspace runs)
disk-space check                   the routine's check: start the cleanup helper over the alert line
disk-space scan                    measure the disk now and save the measurement
disk-space status [--json]         how full the disk is and what can go (what the Caretaker reads)
disk-space delete PATH... [--user-approved] [--by helper]
                                   delete items of the last measurement (ask-first ones need --user-approved)
disk-space helper [--item PATH]    start the cleanup helper chat now
"""

import argparse
import json
import sys

from loguru import logger

from disk_space import check, cleanup, helper, scan, status


def _use_plain_output() -> None:
    """The command's answers go to stdout and its refusals to stderr, as bare lines."""
    logger.remove()
    logger.add(
        sys.stdout,
        format="{message}",
        level="INFO",
        filter=lambda record: record["level"].no < 30,
    )
    logger.add(sys.stderr, format="{message}", level="WARNING")


def _out(text: str) -> None:
    logger.info(text)


def _err(text: str) -> None:
    logger.warning(text)


def _gb(size: int) -> str:
    return f"{size / 1024**3:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="disk-space",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("scan")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    delete_parser = commands.add_parser("delete")
    delete_parser.add_argument("paths", nargs="+")
    delete_parser.add_argument("--user-approved", action="store_true")
    delete_parser.add_argument("--by", default="command")
    helper_parser = commands.add_parser("helper")
    helper_parser.add_argument("--item")
    args = parser.parse_args(argv)
    _use_plain_output()

    if args.command == "check":
        result = check.run_check()
        check.write_routine_result(result)
        _out(result["summary"])
        return 0
    if args.command == "scan":
        snapshot = scan.scan_locked()
        if snapshot is None:
            _out("A measurement is already running.")
            return 0
        _out(f"Measured in {snapshot['took_seconds']}s; snapshot {snapshot['id']}.")
        return 0
    if args.command == "status":
        report = status.build_status()
        if args.json:
            _out(json.dumps(report, indent=2))
        else:
            _out(
                f"{report['percent_used']}% full, {_gb(report['free_bytes'])} free "
                f"(alert line {report['threshold_percent']}%)."
            )
            _out(
                f"Safe to delete: {_gb(report['safe_to_delete_bytes'])}; ask first: {_gb(report['ask_first_bytes'])}."
            )
            for item in report["safe_to_delete"]:
                _out(
                    f"  safe  {_gb(item['bytes']):>8}  {item['name']}  ({item['path']})"
                )
            for item in report["ask_first"]:
                _out(
                    f"  ask   {_gb(item['bytes']):>8}  {item['name']}  ({item['path']})"
                )
        return 0
    if args.command == "delete":
        result = cleanup.delete(
            args.paths, user_approved=args.user_approved, by=args.by
        )
        for item in result["deleted"]:
            _out(f"deleted {item['path']} ({_gb(item['bytes'])})")
        for item in result["refused"]:
            _err(f"refused {item['path']}: {item['reason']}")
        _out(f"Freed {_gb(result['freed_bytes'])}.")
        return 0 if not result["refused"] else 1
    if args.command == "helper":
        started = helper.start(
            "The user asked for help freeing disk space.", "command", args.item
        )
        _out(json.dumps(started))
        return 0 if started["chat_id"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
