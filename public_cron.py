"""Talk to cron through the public hermes cron command."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True

from typing import Callable

CRON_TIMEOUT_SECONDS = 60
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CREATED = re.compile(r"Created job:\s*(\S+)")
_ID_LINE = re.compile(r"^  (\S+) \[")
_NAME = re.compile(r"^\s+Name:\s*(.*)$")
_SCHEDULE = re.compile(r"^\s+Schedule:\s*(.*)$")
_DELIVER = re.compile(r"^\s+Deliver:\s*(.*)$")


class CronUnread(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Cmd:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def plain(text: str) -> str:
    return _ANSI.sub("", text or "")


def first_line(done: Cmd) -> str:
    text = plain(done.stderr or done.stdout).strip()
    line = text.splitlines()[0].strip() if text else ""
    if len(line) > 180:
        return line[:179] + "…"
    return line


def hermes_binary() -> str | None:
    sibling = os.path.join(os.path.dirname(sys.executable), "hermes")
    if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
        return sibling
    return shutil.which("hermes")


def resolved_hermes_home() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


def default_run(args: list[str]) -> Cmd:
    try:
        home = resolved_hermes_home()
    except (OSError, RuntimeError, ImportError):
        return Cmd(127, "", "The Hermes home could not be resolved, so hermes cron was not started.")
    if not home:
        return Cmd(127, "", "The Hermes home could not be resolved, so hermes cron was not started.")
    binary = hermes_binary()
    if not binary:
        return Cmd(127, "", "The hermes command was not found, so hermes cron was not started.")
    env = os.environ.copy()
    # copy() keeps PYTHONPATH. Hermes 0.21.6 and later pass dependency
    # locations to a child that way. This map does not drop them.
    env["HERMES_HOME"] = home
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    try:
        done = subprocess.run(
            [binary, "cron", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=CRON_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return Cmd(124, "", "hermes cron timed out, so the command was not left running.")
    except OSError:
        return Cmd(127, "", "Could not run hermes.")
    return Cmd(int(done.returncode), done.stdout or "", done.stderr or "")


def parse_created_id(stdout: str) -> str | None:
    match = _CREATED.search(plain(stdout))
    return match.group(1) if match else None


def parse_listed_jobs(stdout: str) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in plain(stdout).splitlines():
        head = _ID_LINE.match(line)
        if head:
            if current is not None:
                jobs.append(current)
            current = {"id": head.group(1), "name": "", "schedule": "", "deliver": ""}
            continue
        if current is None:
            continue
        name = _NAME.match(line)
        if name:
            current["name"] = name.group(1).strip()
            continue
        schedule = _SCHEDULE.match(line)
        if schedule:
            current["schedule"] = schedule.group(1).strip()
            continue
        deliver = _DELIVER.match(line)
        if deliver:
            current["deliver"] = deliver.group(1).strip()
    if current is not None:
        jobs.append(current)
    return jobs


class Store:
    def __init__(self, run: Callable[[list[str]], Cmd] | None = None) -> None:
        self._run = run or default_run
        self.last_args: list[str] | None = None

    def load_jobs(self) -> list[dict[str, str]]:
        done = self._run(["list", "--all"])
        if done.returncode != 0:
            raise CronUnread(first_line(done) or "hermes cron list failed")
        return parse_listed_jobs(done.stdout)

    def job_by_name(self, name: str) -> dict[str, str] | None:
        for job in self.load_jobs():
            if job.get("name") == name:
                return job
        return None

    def create_job(self, *, prompt: str, schedule: str, name: str, deliver: str, script: str) -> dict[str, str]:
        args = [
            "create", schedule, prompt, "--name", name, "--deliver", deliver,
            "--no-agent", "--script", script,
        ]
        self.last_args = args
        done = self._run(args)
        if done.returncode != 0:
            raise RuntimeError(first_line(done) or "hermes cron create failed")
        job_id = parse_created_id(done.stdout)
        if not job_id:
            raise RuntimeError("hermes cron create finished without a job id")
        return {"id": job_id, "name": name, "schedule": schedule, "deliver": deliver, "script": script, "prompt": prompt}

    def remove_job(self, job_id: str) -> bool:
        done = self._run(["remove", str(job_id)])
        return done.returncode == 0
