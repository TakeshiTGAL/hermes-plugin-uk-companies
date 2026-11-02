"""Morning snapshot of company status, officers, and persons with significant control."""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import json
from collections import Counter
from pathlib import Path

if __package__:
    from .client import ApiError, Deadline
    from .match import company_number, number_argument
    from .safe_write import WriteRefused, write_bytes
    from . import sources
else:
    from client import ApiError, Deadline
    from match import company_number, number_argument
    from safe_write import WriteRefused, write_bytes
    import sources

PLUGIN = "uk-companies"
STATE_NAME = "watch.json"
MAX_NUMBERS = 20
_DATA_DIR: Path | None = None


def set_data_dir(path: Path | None) -> None:
    global _DATA_DIR
    _DATA_DIR = path


def data_dir() -> Path:
    if _DATA_DIR is not None:
        return _DATA_DIR
    try:
        from plugins.plugin_storage import plugin_data_dir
    except Exception as exc:
        raise ApiError(
            "scope",
            "plugin_data_dir is not available, so the watch file was not written.",
            "Run the command from an installed Hermes plugin.",
        ) from exc
    path = plugin_data_dir(PLUGIN)
    if not isinstance(path, Path):
        path = Path(path)
    return path


def _empty() -> dict:
    return {"version": 1, "numbers": {}}


def load_state(directory: Path | None = None) -> dict:
    root = directory or data_dir()
    path = root / STATE_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _empty()
    if path.is_symlink():
        raise ApiError("bad_input", "The watch file is a symlink, so it was not read.", "Remove the symlink and try again.")
    import stat
    if not stat.S_ISREG(info.st_mode):
        raise ApiError("bad_input", "The watch file is not a regular file, so it was not read.", "Remove that path and try again.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            "bad_shape",
            "The saved watch file is not valid JSON, so it was not replaced.",
            "Move the file aside and add the company numbers again.",
        ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("numbers"), dict):
        raise ApiError("bad_shape", "The saved watch file is not valid JSON, so it was not replaced.", "Move the file aside and add the company numbers again.")
    return payload


def save_state(payload: dict, directory: Path | None = None) -> None:
    root = directory or data_dir()
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    try:
        write_bytes(Path(root), STATE_NAME, raw, create=True)
    except WriteRefused as exc:
        raise ApiError("bad_input", f"The watch file was not written ({exc.kind}).", "Choose a real directory that is not a symlink.") from None


def _numbers(payload: dict) -> dict:
    numbers = payload.get("numbers")
    return numbers if isinstance(numbers, dict) else {}


def _accepted_number(number: object) -> tuple[str | None, str]:
    if isinstance(number, str):
        return number_argument(number)
    checked = company_number(number)
    if not checked:
        return None, "The company number is not in the expected form."
    return checked, ""


def add_number(number: object, directory: Path | None = None) -> dict:
    checked, message = _accepted_number(number)
    if not checked:
        return {"ok": False, "error_type": "bad_input", "message": message, "next_step": message}
    try:
        state = load_state(directory)
    except ApiError as exc:
        return {"ok": False, "error_type": exc.error_type, "message": exc.message, "next_step": exc.next_step}
    numbers = _numbers(state)
    if checked in numbers:
        return {"ok": True, "message": f"{checked} is already on the watch list.", "numbers": sorted(numbers)}
    if len(numbers) >= MAX_NUMBERS:
        return {
            "ok": False,
            "error_type": "bad_input",
            "message": "The watch list already holds 20 company numbers.",
            "next_step": "Remove one number, then add this one.",
        }
    numbers[checked] = None
    state["numbers"] = numbers
    state["version"] = 1
    try:
        save_state(state, directory)
    except ApiError as exc:
        return {"ok": False, "error_type": exc.error_type, "message": exc.message, "next_step": exc.next_step}
    return {"ok": True, "message": f"Added {checked}. The first morning run stores a baseline and prints no line.", "numbers": sorted(numbers)}


def remove_number(number: object, directory: Path | None = None) -> dict:
    checked, message = _accepted_number(number)
    if not checked:
        return {"ok": False, "error_type": "bad_input", "message": message, "next_step": message}
    try:
        state = load_state(directory)
    except ApiError as exc:
        return {"ok": False, "error_type": exc.error_type, "message": exc.message, "next_step": exc.next_step}
    numbers = _numbers(state)
    numbers.pop(checked, None)
    state["numbers"] = numbers
    try:
        save_state(state, directory)
    except ApiError as exc:
        return {"ok": False, "error_type": exc.error_type, "message": exc.message, "next_step": exc.next_step}
    return {"ok": True, "message": f"Removed {checked} from the watch list.", "numbers": sorted(numbers)}


def list_numbers(directory: Path | None = None) -> dict:
    try:
        state = load_state(directory)
    except ApiError as exc:
        return {"ok": False, "error_type": exc.error_type, "message": exc.message, "next_step": exc.next_step}
    rows = []
    for number in sorted(_numbers(state)):
        snap = state["numbers"][number]
        rows.append({"company_number": number, "baseline": isinstance(snap, dict)})
    return {"ok": True, "message": f"{len(rows)} company numbers are on the watch list.", "numbers": rows}


def _show(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "empty"
    if value is None or value == "":
        return "empty"
    return str(value)


def _list_delta(number: str, field: str, previous: list, current: list) -> list[str]:
    before = Counter(previous)
    after = Counter(current)
    removed: list[str] = []
    added: list[str] = []
    for item, count in before.items():
        extra = count - after.get(item, 0)
        if extra > 0:
            removed.extend([str(item)] * extra)
    for item, count in after.items():
        extra = count - before.get(item, 0)
        if extra > 0:
            added.extend([str(item)] * extra)
    lines = [f"{number} {field} removed: {item}" for item in sorted(removed)]
    lines.extend(f"{number} {field} added: {item}" for item in sorted(added))
    return lines


def _changed_lines(number: str, previous: dict, current: dict) -> list[str]:
    lines = []
    for field in ("status", "officers", "psc"):
        old = previous.get(field)
        new = current.get(field)
        if old == new:
            continue
        if isinstance(old, list) and isinstance(new, list):
            delta = _list_delta(number, field, old, new)
            if delta:
                lines.extend(delta)
                continue
        lines.append(f"{number} {field}: {_show(old)} -> {_show(new)}")
    return lines


def run_watch(directory: Path | None = None) -> str:
    """Return stdout for the morning job. An empty string sends no cron message."""
    try:
        state = load_state(directory)
    except ApiError as exc:
        return exc.message
    numbers = _numbers(state)
    lines: list[str] = []
    dirty = False
    for number in sorted(numbers):
        if not company_number(number):
            lines.append(f"{number} status: unread (bad_input), snapshot kept")
            continue
        try:
            read = sources.read_for_watch(number, Deadline())
        except ApiError as exc:
            lines.append(f"{number} status: unread ({exc.error_type}), snapshot kept")
            continue
        if not read.get("ok"):
            kind = str(read.get("error_type") or "upstream")
            lines.append(f"{number} status: unread ({kind}), snapshot kept")
            continue
        current = {
            "status": read["status"],
            "officers": list(read["officers"]),
            "psc": list(read["psc"]),
        }
        previous = numbers.get(number)
        if not isinstance(previous, dict):
            numbers[number] = current
            dirty = True
            continue
        lines.extend(_changed_lines(number, previous, current))
        numbers[number] = current
        dirty = True
    if dirty:
        state["numbers"] = numbers
        state["version"] = 1
        try:
            save_state(state, directory)
        except ApiError as exc:
            return exc.message
    return "\n".join(lines)


def watch_stdout() -> str:
    return run_watch(None)
