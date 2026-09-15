"""
Runs the automation as a SEPARATE PROCESS and streams its progress back.

Why a subprocess rather than a thread:

  - Playwright's synchronous API refuses to start inside a thread that already
    has a running asyncio event loop, and Streamlit's runtime is built on one.
    A subprocess sidesteps the whole question.
  - Streamlit re-executes its script top to bottom on every interaction. A run
    must therefore outlive the script run that started it, which a detached
    process does naturally.
  - A wedged browser can be killed without taking the web app down with it.

Progress arrives as JSON lines written by `cli.py --events`, which the UI polls.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .. import PROJECT_ROOT, settings
from . import results as R


@dataclass
class Job:
    run_id: str
    target_key: str
    events_path: str
    artifacts_dir: str
    headed: bool = False
    case_id: str = ""
    proc: Optional[subprocess.Popen] = None
    _cursor: int = 0                       # bytes of the events file consumed
    events: list[dict] = field(default_factory=list)

    # ---- lifecycle -----------------------------------------------------
    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def exit_code(self) -> Optional[int]:
        return None if self.proc is None else self.proc.poll()

    def stop(self) -> None:
        if not self.running:
            return
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - killing is best-effort
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ---- progress ------------------------------------------------------
    def poll(self) -> list[dict]:
        """Read whatever new events have been written since last time."""
        new: list[dict] = []
        if not os.path.exists(self.events_path):
            return new
        try:
            with open(self.events_path, "r", encoding="utf-8") as fh:
                fh.seek(self._cursor)
                chunk = fh.read()
                self._cursor = fh.tell()
        except OSError:
            return new
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                new.append(json.loads(line))
            except json.JSONDecodeError:
                # A partial final line while the writer is mid-flush; it will be
                # re-read next poll because the cursor only advances on success.
                self._cursor -= len(line) + 1
                break
        self.events.extend(new)
        return new

    # ---- derived views for the UI --------------------------------------
    @property
    def steps(self) -> list[dict]:
        return [e for e in self.events if e.get("kind") == "step_done"]

    @property
    def screens(self) -> list[dict]:
        return [e for e in self.events if e.get("kind") == "screen_done"]

    @property
    def current_screen(self) -> Optional[dict]:
        for e in reversed(self.events):
            if e.get("kind") == "screen_start":
                return e
        return None

    @property
    def planned_screens(self) -> list[str]:
        for e in self.events:
            if e.get("kind") == "start":
                return e.get("screens") or []
        return []

    @property
    def logs(self) -> list[str]:
        return [e["text"] for e in self.events if e.get("kind") == "log"]

    @property
    def latest_shot(self) -> Optional[str]:
        for e in reversed(self.events):
            shot = e.get("shot")
            if shot and os.path.exists(shot):
                return shot
        return None

    @property
    def current_step(self) -> Optional[dict]:
        for e in reversed(self.events):
            if e.get("kind") == "step_start":
                return e
        return None

    @property
    def finished_event(self) -> Optional[dict]:
        for e in reversed(self.events):
            if e.get("kind") == "done":
                return e
        return None

    def result(self) -> Optional[dict]:
        """The persisted report, once the process has written it."""
        path = os.path.join(self.artifacts_dir, "result.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def crashed(self) -> Optional[str]:
        """A process that exited without writing a report. Distinguished from a
        BLOCKED run so the UI never presents a crash as a test outcome."""
        if self.running or self.proc is None:
            return None
        if self.result() is not None:
            return None
        err = ""
        try:
            if self.proc.stderr:
                err = self.proc.stderr.read() or ""
        except Exception:  # noqa: BLE001
            pass
        return (f"The run stopped unexpectedly (exit code {self.exit_code}) without "
                f"producing a report.\n{err[-800:]}")


def start_verify(target_key: str, headed: bool = False,
                 case_id: Optional[str] = None) -> Job:
    """Launch a read-only verification run in its own process."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"verify-{stamp}"
    artifacts_dir = os.path.join(settings.ARTIFACTS_DIR, run_id)
    os.makedirs(artifacts_dir, exist_ok=True)
    events_path = os.path.join(artifacts_dir, "events.jsonl")

    cmd = [sys.executable, "-u", "-m", "los_automation.runner.cli",
           "--verify", target_key, "--run-id", run_id,
           "--case-id", case_id or settings.CASE_ID,
           "--events", events_path, "--quiet"]
    if headed:
        cmd.append("--headed")

    kwargs: dict = {}
    if os.name == "nt":
        # Its own process group, so stop() can interrupt the child without
        # signalling the Streamlit server too.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd, cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, **kwargs)

    return Job(run_id=run_id, target_key=target_key, events_path=events_path,
               artifacts_dir=artifacts_dir, headed=headed, proc=proc,
               case_id=case_id or settings.CASE_ID)


def start_create(dry_run: bool = True, obligor_name: str = "",
                 headed: bool = False, stop_after_save: bool = False) -> Job:
    """
    Launch the PHASE 2 create flow in its own process.

    Same subprocess machinery as start_verify — the reasons for it are
    unchanged — so the page streams progress and shows results through exactly
    the same code. The only difference is which CLI flag is passed, and that
    this one can write.

    The host allowlist is NOT re-checked here on purpose: the check belongs in
    widgets.assert_writable, which runs inside the child before a browser is
    launched. Duplicating it here would create a second place to keep in step.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"{'dryrun' if dry_run else 'create'}-{stamp}"
    artifacts_dir = os.path.join(settings.ARTIFACTS_DIR, run_id)
    os.makedirs(artifacts_dir, exist_ok=True)
    events_path = os.path.join(artifacts_dir, "events.jsonl")

    cmd = [sys.executable, "-u", "-m", "los_automation.runner.cli",
           "--create-obligor", "--run-id", run_id,
           "--events", events_path, "--quiet"]
    if dry_run:
        cmd.append("--dry-run")
    if stop_after_save:
        cmd.append("--stop-after-save")
    if obligor_name:
        cmd += ["--obligor-name", obligor_name]
    if headed:
        cmd.append("--headed")

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd, cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, **kwargs)

    return Job(run_id=run_id, target_key="obligor.create",
               events_path=events_path, artifacts_dir=artifacts_dir,
               headed=headed, proc=proc, case_id=obligor_name)


def start_case_fill(screen: str, case_id: str, dry_run: bool = True,
                    headed: bool = False, verify: bool = True) -> Job:
    """
    Launch the PHASE 2b case-screen flow in its own process.

    `screen` is one of request_details / facilities / observations /
    litigation, or "all". One button per screen is the point: Facilities is
    the slow one — it requests a facility and then walks every tab of it — and
    someone checking only Request Details should not have to wait for it, nor
    have a facility left on the case they did not ask for.

    Same subprocess machinery as the other two starters, for the same reasons,
    and the host allowlist is again left to widgets.assert_writable inside the
    child rather than duplicated here.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    kind = "casedry" if dry_run else "case"
    run_id = f"{kind}-{screen}-{stamp}"
    artifacts_dir = os.path.join(settings.ARTIFACTS_DIR, run_id)
    os.makedirs(artifacts_dir, exist_ok=True)
    events_path = os.path.join(artifacts_dir, "events.jsonl")

    cmd = [sys.executable, "-u", "-m", "los_automation.runner.cli",
           "--fill-case", screen,
           "--case-id", case_id or settings.CASE_ID,
           "--run-id", run_id, "--events", events_path, "--quiet"]
    if dry_run:
        cmd.append("--dry-run")
    if not verify:
        cmd.append("--no-verify")
    if headed:
        cmd.append("--headed")

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd, cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, **kwargs)

    return Job(run_id=run_id, target_key="case.screens",
               events_path=events_path, artifacts_dir=artifacts_dir,
               headed=headed, proc=proc, case_id=case_id or settings.CASE_ID)


def status_word(result: dict) -> str:
    return (result or {}).get("overall", R.BLOCKED)
