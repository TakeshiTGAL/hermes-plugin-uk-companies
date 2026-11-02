"""Local CLI: hermes uk-companies watch|schedule|unschedule|status|call."""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
from pathlib import Path

if __package__:
    from .client import scrub
    from .handlers import invoke
    from .prefix import cron_lines
    from .public_cron import CronUnread, Store
    from .safe_write import WriteRefused, remove_file, write_bytes
    from . import watch
else:
    from client import scrub
    from handlers import invoke
    from prefix import cron_lines
    from public_cron import CronUnread, Store
    from safe_write import WriteRefused, remove_file, write_bytes
    import watch

JOB_NAME = "uk-companies-watch"
SCRIPT_NAME = "uk-companies-watch.py"
SCHEDULE = "0 8 * * *"
PROMPT = (
    "Compare the saved company snapshot with today's Companies House read for status, officers, "
    "and persons with significant control. [SILENT] Deliver the script stdout. "
    "The first successful read stores a baseline and prints no line."
)
GONE = "No cron job named uk-companies-watch was found."
LOCAL_ONLY = "This plugin delivers the morning line to local only, so it is not sent to another chat."

SCRIPT_TEXT = """#!/usr/bin/env python3
\"\"\"Morning Companies House watch. Empty stdout sends no message.\"\"\"
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def main() -> int:
    try:
        from hermes_constants import get_hermes_home
    except Exception:
        sys.stderr.write("uk-companies is not installed, so the morning watch did not run.\\n")
        return 1
    plugin = Path(get_hermes_home()) / "plugins" / "uk-companies"
    if not (plugin / "watch.py").is_file():
        sys.stderr.write("uk-companies is not installed, so the morning watch did not run.\\n")
        return 1
    sys.path.insert(0, str(plugin))
    try:
        import watch
    except Exception:
        sys.stderr.write("uk-companies is not installed, so the morning watch did not run.\\n")
        return 1
    text = watch.watch_stdout()
    if text:
        sys.stdout.write(text)
        if not text.endswith("\\n"):
            sys.stdout.write("\\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="command", required=True)
    watch_parser = sub.add_parser("watch", help="Add, remove, list, or run watched company numbers")
    watch_sub = watch_parser.add_subparsers(dest="watch_command", required=True)
    add = watch_sub.add_parser("add", help="Save one company number")
    add.add_argument("company_number")
    remove = watch_sub.add_parser("remove", help="Drop one company number")
    remove.add_argument("company_number")
    watch_sub.add_parser("list", help="List watched company numbers")
    watch_sub.add_parser("run", help="Compare today's read with the saved snapshot")
    schedule = sub.add_parser("schedule", help="Create the local morning job")
    schedule.add_argument("--deliver", default="local", help="Only local is accepted")
    sub.add_parser("unschedule", help="Remove the morning job if it exists")
    sub.add_parser("status", help="Show whether the morning job exists")
    call = sub.add_parser("call", help="Call one tool and print JSON")
    call.add_argument("tool")
    call.add_argument("--json", default="{}")


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def write_watch_script(home: Path | None = None) -> str:
    root = home or _home()
    write_bytes(Path(root), f"scripts/{SCRIPT_NAME}", SCRIPT_TEXT.encode("utf-8"), create=True)
    return f"scripts/{SCRIPT_NAME}"


def _lines(job_id: str | None = None) -> str:
    text = "\n".join(cron_lines(job_id))
    if not job_id:
        text += "\nThe cron output had no job id. Read the id from cron list, then run cron remove with that id."
    return text


def _drop_watch_script(home: Path | None) -> str:
    try:
        root = home or _home()
    except Exception:
        return "The watch script was not removed because the Hermes home could not be resolved."
    try:
        kind = remove_file(Path(root), f"scripts/{SCRIPT_NAME}")
    except WriteRefused as exc:
        return f"The watch script was not removed ({exc.kind})."
    except Exception:
        return "The watch script was not removed."
    if kind == "removed":
        return "Removed scripts/uk-companies-watch.py."
    return ""


def schedule_job(store: Store, deliver: str, home: Path | None = None) -> tuple[int, str]:
    if deliver != "local":
        return 1, LOCAL_ONLY
    try:
        write_watch_script(home)
    except WriteRefused as exc:
        return 1, f"The watch script was not written ({exc.kind})."
    except Exception:
        return 1, "The Hermes home could not be resolved, so the morning job was not created."
    try:
        existing = store.job_by_name(JOB_NAME)
    except CronUnread as exc:
        return 1, scrub(str(exc) or "hermes cron list failed")
    if existing:
        return 0, f"{JOB_NAME} is already scheduled.\n" + _lines(existing.get("id") or None)
    try:
        created = store.create_job(
            prompt=PROMPT,
            schedule=SCHEDULE,
            name=JOB_NAME,
            deliver="local",
            script=SCRIPT_NAME,
        )
    except Exception as exc:
        message = scrub(str(exc) or "hermes cron create failed")
        try:
            still_there = store.job_by_name(JOB_NAME)
        except Exception:
            still_there = {"id": ""}
        if not still_there:
            note = _drop_watch_script(home)
            if note:
                message = message + "\n" + note
        return 1, message
    return 0, f"Scheduled {JOB_NAME}.\n" + _lines(created.get("id") or None)


def job_status(store: Store) -> tuple[int, str]:
    try:
        job = store.job_by_name(JOB_NAME)
    except CronUnread as exc:
        return 1, scrub(str(exc) or "hermes cron list failed")
    if not job:
        return 0, GONE
    ident = job.get("id") or ""
    return 0, f"Cron job {ident} named {JOB_NAME} is scheduled.\n" + _lines(ident or None)


def unschedule_job(store: Store, home: Path | None = None) -> tuple[int, str]:
    try:
        job = store.job_by_name(JOB_NAME)
    except CronUnread as exc:
        return 1, scrub(str(exc) or "hermes cron list failed")
    if not job:
        note = _drop_watch_script(home)
        if note:
            return 0, GONE + "\n" + note
        return 0, GONE
    ident = job.get("id") or ""
    if not ident:
        return 1, "The cron job had no id, so it was not removed."
    if not store.remove_job(ident):
        return 1, f"The cron job {ident} was not removed."
    text = f"Removed cron job {ident}.\n" + _lines(ident)
    note = _drop_watch_script(home)
    if note:
        text += "\n" + note
    return 0, text


def _print_watch(payload: dict) -> tuple[int, str]:
    text = scrub(json.dumps(payload, ensure_ascii=False))
    return (0 if payload.get("ok") else 1), text


def handle(args: argparse.Namespace, store: Store | None = None) -> int:
    command = getattr(args, "command", None)
    holder = store or Store()
    if command == "schedule":
        code, text = schedule_job(holder, getattr(args, "deliver", "local"))
    elif command == "unschedule":
        code, text = unschedule_job(holder)
    elif command == "status":
        code, text = job_status(holder)
    elif command == "watch":
        action = getattr(args, "watch_command", None)
        if action == "add":
            code, text = _print_watch(watch.add_number(args.company_number))
        elif action == "remove":
            code, text = _print_watch(watch.remove_number(args.company_number))
        elif action == "list":
            code, text = _print_watch(watch.list_numbers())
        elif action == "run":
            text = watch.watch_stdout()
            code = 0
        else:
            print("Use watch add, remove, list, or run.")
            return 1
    elif command == "call":
        raw = getattr(args, "json", "{}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(invoke("", {}))
            return 1
        text = invoke(getattr(args, "tool", ""), payload)
        print(text)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            return 1
        return 0 if body.get("ok") else 1
    else:
        print("Use watch, schedule, unschedule, status, or call.")
        return 1
    print(text)
    return code
