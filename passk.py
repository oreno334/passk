#!/usr/bin/env python3
"""passk - measure whether a coding agent succeeds at the same task every time.

Three subcommands:

    verify   run each spec's acceptance check at its base commit and require
             it to FAIL. A spec that already passes measures nothing.
    run      for each spec, k independent trials in a fresh git worktree.
    score    pass^1 .. pass^n from the recorded trials.

Standard library only. Python 3.10+.

MIT licensed. See LICENSE.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from math import comb
from pathlib import Path

REQUIRED_FIELDS = ("id", "base_ref", "prompt_file", "acceptance")


# --------------------------------------------------------------------------
# specs
# --------------------------------------------------------------------------

def load_specs(specs_dir: Path, only: str | None = None) -> list[dict]:
    specs = []
    for path in sorted(specs_dir.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        missing = [f for f in REQUIRED_FIELDS if not spec.get(f)]
        if missing:
            sys.exit(f"{path.name}: missing required field(s) {missing}")
        if not spec.get("acceptance", {}).get("command"):
            sys.exit(f"{path.name}: acceptance.command is empty")
        spec["_file"] = path
        specs.append(spec)
    if only:
        specs = [s for s in specs if s["id"] == only]
    if not specs:
        sys.exit(f"no specs found in {specs_dir}")
    return specs


def require_signoff(spec: dict) -> None:
    """A spec runs k times. If the task can touch the outside world, running it
    k times touches the outside world k times. The text filters that shortlist
    candidates are not an approval, so a human has to say so explicitly."""
    who = spec.get("reversible_reviewed_by", "")
    if who in ("", "TODO", None):
        sys.exit(
            f"{spec['id']}: refusing to run.\n"
            f"  Set reversible_reviewed_by to the name of the human who confirmed\n"
            f"  this task cannot send a message, publish, move money, or write to\n"
            f"  a live external system."
        )


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------

def sh(cmd, cwd: Path, timeout: int, env: dict | None = None):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        timeout=timeout,
        shell=isinstance(cmd, str),
        capture_output=True,
        text=True,
        errors="replace",
        env={**os.environ, **(env or {})} if env else None,
    )


class Worktree:
    """A throwaway checkout at a specific commit.

    Independence is the point of the whole exercise. If trial 2 can see what
    trial 1 built, you are measuring retry-with-hindsight, which is a different
    and much easier thing than pass^k.
    """

    def __init__(self, repo: Path, ref: str, label: str):
        self.repo = repo
        self.ref = ref
        self.path = Path(tempfile.mkdtemp(prefix=f"passk-{label[:32]}-"))

    def __enter__(self) -> Path:
        r = sh(["git", "worktree", "add", "--detach", str(self.path), self.ref],
               self.repo, 300)
        if r.returncode != 0:
            shutil.rmtree(self.path, ignore_errors=True)
            raise RuntimeError(f"git worktree add failed: {r.stderr.strip()[-400:]}")
        return self.path

    def __exit__(self, *exc):
        subprocess.run(["git", "worktree", "remove", "--force", str(self.path)],
                       cwd=str(self.repo), capture_output=True)
        shutil.rmtree(self.path, ignore_errors=True)
        return False


# --------------------------------------------------------------------------
# verify: the negative control
# --------------------------------------------------------------------------

def cmd_verify(args) -> int:
    """Run each acceptance check at its base_ref and require failure.

    This is the check that decides whether the whole measurement means
    anything. If base_ref is even one commit too late it already contains the
    solution, the acceptance command passes without the agent doing anything,
    and you publish a number that measured nothing.
    """
    repo = args.repo.resolve()
    specs = load_specs(args.specs, args.only)
    bad = []

    print(f"negative control over {len(specs)} spec(s)")
    print("each acceptance command must FAIL at its base_ref\n")

    for spec in specs:
        acc = spec["acceptance"]
        try:
            with Worktree(repo, spec["base_ref"], spec["id"]) as wt:
                r = sh(acc["command"], wt, int(acc.get("timeout_sec", 300)))
                exit_code = r.returncode
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"  ERROR  {spec['id']}: {e}")
            bad.append((spec["id"], "harness error"))
            continue

        if exit_code == 0:
            print(f"  🔴 INVALID  {spec['id']}  acceptance PASSED at base_ref")
            bad.append((spec["id"], "passes at base_ref"))
        else:
            print(f"  ✅ ok       {spec['id']}  exit {exit_code} at base_ref")

    if bad:
        print(f"\n{len(bad)} spec(s) unusable:")
        for sid, why in bad:
            print(f"  - {sid}: {why}")
        print("\nFix base_ref or drop the spec. Do not weaken the acceptance")
        print("command to make it fail; that fakes the control.")
        return 1

    print("\nall specs failed at base_ref, as required.")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run(args) -> int:
    repo = args.repo.resolve()
    specs = load_specs(args.specs, args.only)
    for spec in specs:
        require_signoff(spec)

    total = len(specs) * args.k
    print(f"{len(specs)} spec(s) x k={args.k} = {total} trial(s)")
    if args.dry_run:
        print("dry run: no agent will be invoked, acceptance only\n")
    else:
        print(f"agent: {args.agent}\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fh = args.out.open("a", encoding="utf-8")

    for spec in specs:
        for trial in range(1, args.k + 1):
            rec = {
                "task_id": spec["id"],
                "trial": trial,
                "k": args.k,
                "base_ref": spec["base_ref"],
                "agent": None if args.dry_run else args.agent,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "passed": False,
                "harness_error": None,
            }
            t0 = time.time()
            try:
                with Worktree(repo, spec["base_ref"], spec["id"]) as wt:
                    if not args.dry_run:
                        prompt_path = (repo / spec["prompt_file"])
                        if not prompt_path.exists():
                            raise RuntimeError(f"prompt_file not found: {spec['prompt_file']}")
                        prompt = prompt_path.read_text(encoding="utf-8", errors="replace")
                        cmd = args.agent.replace("{prompt_file}", str(prompt_path))
                        a = sh(cmd, wt, args.agent_timeout,
                               env={"PASSK_EVAL": "1", "CI": "1", "GIT_TERMINAL_PROMPT": "0"})
                        rec["agent_exit"] = a.returncode
                        rec["agent_tail"] = (a.stdout or "")[-2000:]
                        del prompt  # read only to fail fast on unreadable files

                    acc = spec["acceptance"]
                    chk = sh(acc["command"], wt, int(acc.get("timeout_sec", 300)))
                    rec["acceptance_exit"] = chk.returncode
                    rec["acceptance_tail"] = ((chk.stdout or "") + (chk.stderr or ""))[-2000:]
                    rec["passed"] = chk.returncode == 0

            except subprocess.TimeoutExpired as e:
                rec["harness_error"] = f"timeout: {e}"
            except Exception as e:  # noqa: BLE001
                rec["harness_error"] = f"{type(e).__name__}: {e}"

            rec["duration_sec"] = round(time.time() - t0, 1)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()

            mark = "PASS" if rec["passed"] else ("ERR " if rec["harness_error"] else "FAIL")
            print(f"  [{mark}] {spec['id']}  {trial}/{args.k}  {rec['duration_sec']}s")

    fh.close()
    print(f"\nwrote {args.out}")
    print(f"next: python passk.py score {args.out}")
    return 0


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------

def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Probability that a randomly chosen k of the n trials all succeeded.

    C(c,k)/C(n,k). This is the unbiased estimator used in the tau-bench line
    of work, and it gives you pass^1..pass^n from a single run of n trials.
    """
    if k > n:
        return None
    if k > c:
        return 0.0
    return comb(c, k) / comb(n, k)


def cmd_score(args) -> int:
    per_task: dict[str, list[bool]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)

    for line in args.runs.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("harness_error"):
            # A trial the harness could not complete is not an agent failure.
            # Folding infrastructure breakage into the score is exactly the
            # kind of laundering this tool exists to prevent.
            errors[rec["task_id"]] += 1
            continue
        per_task[rec["task_id"]].append(bool(rec["passed"]))

    if not per_task:
        print("no completed trials in file")
        return 1

    trials = {t: len(v) for t, v in per_task.items()}
    succ = {t: sum(v) for t, v in per_task.items()}
    n_min = min(trials.values())
    total_runs = sum(trials.values())
    total_succ = sum(succ.values())

    print(f"tasks              {len(per_task)}")
    print(f"completed trials   {total_runs}")
    print(f"harness errors     {sum(errors.values())}  (excluded, not failures)")
    print(f"max reportable k   {n_min}")
    print(f"per-trial success  {100 * total_succ / total_runs:.1f}%\n")

    curve = {}
    for k in range(1, n_min + 1):
        vals = [pass_at_k(trials[t], succ[t], k) for t in per_task]
        vals = [v for v in vals if v is not None]
        curve[k] = sum(vals) / len(vals)
        print(f"  pass^{k}  {curve[k] * 100:5.1f}%  {'#' * round(curve[k] * 40)}")

    if n_min >= 2:
        print(f"\n  pass^1 to pass^{n_min} drop: {(curve[1] - curve[n_min]) * 100:.1f} points")

    always = [t for t in per_task if succ[t] == trials[t]]
    never = [t for t in per_task if succ[t] == 0]
    flaky = sorted((t for t in per_task if 0 < succ[t] < trials[t]),
                   key=lambda t: succ[t] / trials[t])

    print(f"\nalways passed  {len(always)}")
    print(f"flaky          {len(flaky)}   <- the interesting ones")
    print(f"never passed   {len(never)}")
    for t in flaky:
        print(f"    {succ[t]}/{trials[t]}  {t}")

    if args.json:
        args.json.write_text(json.dumps({
            "tasks": len(per_task),
            "trials": total_runs,
            "harness_errors": sum(errors.values()),
            "per_trial_success_rate": total_succ / total_runs,
            "curve": curve,
            "always": sorted(always),
            "flaky": {t: f"{succ[t]}/{trials[t]}" for t in flaky},
            "never": sorted(never),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    print("\nWhen you report this number, report k, report the sample size, and")
    print("say how the tasks were chosen. A curated reversible sample is not a")
    print("random sample of everything the agent does.")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="passk", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=Path("."),
                    help="git repository under test (default: cwd)")
    ap.add_argument("--specs", type=Path, default=Path("specs"),
                    help="directory of spec JSON files (default: ./specs)")
    ap.add_argument("--only", help="run a single spec by id")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="negative control: acceptance must fail at base_ref")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("run", help="k independent trials per spec")
    r.add_argument("--k", type=int, default=5)
    r.add_argument("--agent", default="",
                   help="shell command to invoke the agent, cwd is the worktree. "
                        "Use {prompt_file} as a placeholder for the work-order path.")
    r.add_argument("--agent-timeout", type=int, default=3600)
    r.add_argument("--out", type=Path, default=Path("runs.jsonl"))
    r.add_argument("--dry-run", action="store_true",
                   help="skip the agent, only prepare the worktree and run acceptance")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("score", help="pass^1..pass^n from runs.jsonl")
    s.add_argument("runs", type=Path)
    s.add_argument("--json", type=Path)
    s.set_defaults(func=cmd_score)

    args = ap.parse_args()
    if args.cmd == "run" and not args.dry_run and not args.agent:
        ap.error("run needs --agent (or --dry-run)")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
