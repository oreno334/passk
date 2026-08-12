# passk

**Does your coding agent succeed at the same task *every* time?**

An 80% success rate sounds good until you need the task to work three times in
a row:

```
0.8 × 0.8 × 0.8 = 51%
```

`pass^k` is the probability that **all k** independent attempts succeed. It is
a different number from "it worked once", and it is the number that decides
whether you can put an agent into a business process.

This is a small harness for measuring it on **your own repository, with your
own tasks**. No dependencies, one file, Python 3.10+.

---

## Why not just use a benchmark

Public agent benchmarks measure closed tasks with a known correct answer, run
in an environment built for measuring. That is useful, and it is not the
question most teams actually have, which is:

> When I hand my agent a real work order from my own backlog, how often does it
> land, and how often does it land *every single time*?

`passk` answers that against real commits in a real repo.

---

## How it works

You write a **spec** per task: a work order, the commit to start from, and an
acceptance command.

```json
{
  "id": "add-env-name-detector",
  "base_ref": "a1b2c3d",
  "prompt_file": "tasks/add-env-name-detector.md",
  "acceptance": {
    "command": "python -m pytest tests/test_env_name_detector.py -q",
    "timeout_sec": 300
  },
  "reversible_reviewed_by": "TODO"
}
```

Then:

```bash
python passk.py --repo /path/to/repo verify
python passk.py --repo /path/to/repo run --k 5 --agent 'your-agent-cli exec < {prompt_file}'
python passk.py score runs.jsonl
```

Output:

```
tasks              30
completed trials   150
per-trial success  71.3%

  pass^1   71.3%  ############################
  pass^2   58.9%  ########################
  pass^3   49.1%  ####################
  pass^4   41.8%  #################
  pass^5   36.0%  ##############

  pass^1 to pass^5 drop: 35.3 points

flaky          11   <- the interesting ones
```

---

## The three things this harness insists on

Most of the value is not in the arithmetic. It is in refusing to produce a
number that looks good and means nothing.

### 1. A fresh worktree per trial

Every trial gets its own `git worktree` at `base_ref`. Trial 2 cannot see what
trial 1 built.

If your trials share state, you are measuring *retry with hindsight*, where the
second attempt knows why the first failed. That is a real and useful thing, and
it is not `pass^k`, and it produces much better looking numbers.

### 2. A negative control, enforced

`passk verify` runs each acceptance command at `base_ref` and **requires it to
fail**.

This is the check that decides whether the whole measurement means anything. If
`base_ref` is one commit too late it already contains the solution: the
acceptance command passes, the agent does nothing, and you publish a perfect
score that measured nothing at all. `verify` exits non-zero on any spec that
passes before the work starts.

Do not weaken the acceptance command to make the control fail. That fakes it.

### 3. Reversibility sign-off, in code

`run` refuses to start any spec whose `reversible_reviewed_by` is still `TODO`.

A spec runs k times. **If the task can send an email, the harness sends k
emails.** Text filters can shortlist candidates, but only a human can approve
one, so the approval is a required field rather than a convention.

Tasks that are safe to repeat: building a tool, refactoring, analysis, data
extraction, writing tests, auditing code. Work that starts and ends inside a
copy of the repository.

Tasks that are not: anything that messages a person, publishes, moves money,
writes to a live external system, or depends on today's date.

---

## Reading the output honestly

**`pass^1` here is the per-trial success rate**, not "succeeded at least once".
Those are different numbers and the second one is not a reliability metric.

**Harness errors are excluded, not counted as failures.** A trial that died
because a worktree could not be created says nothing about the agent. They are
reported separately so you can see how many there were.

**Report `k`.** `pass^5` and `pass^3` are not comparable.

**Report how you chose the tasks.** A set curated for being reversible and
automatically checkable is biased toward code work with tests, which is the
work agents are best at. Your number will be higher than the agent's real
reliability across everything it does. Say so.

---

## Scoring

For a task with `n` trials of which `c` succeeded, the probability that a
randomly chosen `k` of them all succeeded is:

```
pass^k = C(c, k) / C(n, k)
```

This is the unbiased estimator from the τ-bench line of work. It means a single
run of `n` trials gives you the whole `pass^1 .. pass^n` curve, which is why
`--k 5` is a good default: five trials, five points, one gap to look at.

---

## Install

```bash
curl -O https://raw.githubusercontent.com/oreno334/passk/main/passk.py
```

That is the whole install. Standard library only.

---

## Where this came from

Built while measuring an agentic system that has been running a real business
in production since May 2026. The internal corpus had 1,195 delegated work
orders and 63.4% first-attempt clean completion, but **833 of 839 measurable
tasks had been run exactly once** — so there was no `pass^k` in it, and no way
to derive one without re-running the work from scratch.

Hence this.

---

## License

MIT. See [LICENSE](LICENSE).
