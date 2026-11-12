---
name: submit
description: Submits a Slurm job to JSC's JUPITER supercomputer without wasting the shared budget - writes or reviews the batch script, copies the project, prices the request, tests small first, submits with `jup submit` and starts watching. Use when the user wants to run, launch, submit, queue or schedule something on JUPITER (or "on the cluster" in a project with a .jupiter.json), asks for an sbatch script for JUPITER, or asks what a run there would cost. Not for other clusters.
---

# Submitting a job to JUPITER

A job spends core-hours the user shares with their project from the second it starts,
and nothing comes back when it fails. So the work here is mostly before `sbatch`: make
the cheap checks first, then spend. If you have not read the `cluster` skill in this
session, read it now; the options and limits are in
`${CLAUDE_PLUGIN_ROOT}/skills/cluster/references/slurm.md`.

## 1. Know the job

Before writing anything, be able to say: what command runs, on how many nodes, for
how long at most, what it reads (code, environment, data, weights) and where its
results go. Ask the user for whatever of that the project does not answer. A time
limit that is a guess should be a generous one for the real run and a tight one for
the test.

## 2. The batch script

Start from `${CLAUDE_SKILL_DIR}/templates/job.sbatch` (copy it into the project)
unless the project has its own. What a script for JUPITER needs:

- `--nodes` and `--time` written out. Without them the cost check cannot price the
  job and asks the user every time.
- `--output` into a directory. `jup submit` creates it; plain `sbatch` does not, and
  the job then dies with nothing to read.
- No `--account` unless the script is tied to one project: `jup submit` adds the
  configured one.
- Offline mode for anything that would download. Jobs have no internet.
- Results written as the job goes, so that a job the time limit cuts leaves something
  usable and can be resumed. Clean-up in an `EXIT` trap gets only the seconds between
  Slurm's warning and its kill.
- Paths relative to the project's directory: the job starts there.

## 3. What must already be on the cluster

Anything that needs the internet or minutes of set-up happens on a login node before
the job, not inside it on a billed node: the Python environment, model weights,
datasets, container images.
`${CLAUDE_PLUGIN_ROOT}/skills/cluster/references/software.md` has the patterns. Check
they are there (`jup ssh -C 'ls ...'`) instead of assuming.

## 4. Copy the project

```bash
jup push --dry-run     # the first time, or after adding large or private files
jup push
```

Push before submitting and not while a job of this project runs: the job reads the
same files. `jup submit` compares the script here with the copy there and refuses when
their `#SBATCH` lines differ, so a forgotten push shows up before anything is spent.

## 5. Check without spending

Each of these costs nothing and catches a failure that would otherwise cost start-up
time on a billed node:

```bash
jup ssh -C 'bash -n job.sbatch'                 # the script parses
jup ssh -C '<the program> --help'               # it starts in that environment at all
jup submit --dry-run job.sbatch ARGS            # the cost, and the sbatch command
jup ssh -C 'sbatch --test-only job.sbatch'      # Slurm accepts it and says when it could start
```

If the program can validate its arguments or do a dry run, do that on the login node
with the exact arguments the job will get. Keep it light: login nodes allow 20
processes and a quarter of the memory.

## 6. Test small first

Unless this exact script and arguments have already run, the first submission is a
test: the fewest inputs that exercise every stage, a time limit of minutes, on the
test reservation.

```bash
jup submit --reservation=develbooster --time=00:15:00 job.sbatch SMALL-ARGS
```

Then follow it as in step 8. The reservation allows 4 nodes and 4 jobs per user, and
120 minutes per job. If the job waits there with `ReqNodeNotAvail`, its nodes are busy or
drained: cancel it and submit the same test without `--reservation`.

Read the test's output to the end before going on. A test that passed stage one and
was cut by its time limit has not tested stage two.

## 7. Say the cost, then submit

Tell the user in one sentence what the real run reserves and costs (`jup cost` prints
it: nodes, time limit, core-hours at most) and what the budget shows (`jup budget`,
a day behind). Above one node or 300 core-hours, and whenever the cost cannot be read
for certain, Claude Code asks them to confirm; the question should not be the first
they hear of it.

```bash
jup submit job.sbatch ARGS
```

Options before the script go to `sbatch` (`--time=`, `--nodes=`, `--job-name=`,
`--export=ALL,NAME=value`, `--dependency=afterok:JOB`); everything after the script's
name goes to the script.

## 8. Watch it

A command started in the background tells the session only when it ends, so watch in
two steps:

```bash
jup wait JOB --until-running    # in the background: ends when the job starts
jup logs JOB                    # then: did it get past its first minute?
jup wait JOB                    # in the background again: ends with the job, exit 0 only if COMPLETED
```

Most failures show in the first lines of output, and a job that is wrong should be
cancelled then, not found dead an hour later. Tell the user the job number and when it
ends at the latest. When it ends, continue with the `jobs` skill: read the output,
report the result and what it cost, and bring results back with `jup pull`.

If something was submitted by mistake, cancel it at once (`jup cancel JOB`) and say
what it cost: minutes on a node are cheap, a forgotten job is not.
