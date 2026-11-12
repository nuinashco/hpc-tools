# Slurm on JUPITER

Sources: https://apps.fz-juelich.de/jsc/hps/jupiter/ (batchsystem, gpu-computing,
affinity, accounting, known-issues, faq), read 2026-11-12, and jobs run in November
2026. The system is in early access: limits change, so check with
`scontrol show partition booster` and `scontrol show reservation develbooster`.

## Contents

- Partition, test reservation, limits
- Options that matter
- GPUs and cores inside a job
- Cost and budget
- Watching jobs
- Why a job waits
- Why a job failed
- Interactive use, chains, arrays

## Partition, test reservation, limits

| | `booster` (the only partition, the default) | reservation `develbooster` |
|---|---|---|
| Use | everything | short tests: `--reservation=develbooster` |
| Time | 12 h at most, 1 h when `--time` is missing | 120 minutes at most |
| Nodes | 1 to the whole system | 4 per job, 4 per user over running and waiting jobs |
| Jobs | a per-user limit on running jobs exists, unpublished | 4 submitted per user |

- A node is never shared: the smallest job is one node of 288 cores.
- The reservation has 8 nodes for everyone. When they are taken or drained
  (`sinfo -h -n 'jpbo-101-[01-08]' -o '%n %T'`, or the node list that
  `scontrol show reservation develbooster` prints), a short job on the plain partition
  usually starts within minutes: `squeue --me --start` estimates when.
- Breaking a reservation limit is refused at submission with
  `job_submit_filter: job denied, exceeded reservation limit`.

## Options that matter

```bash
#SBATCH --account=<project>      # the budget; `jup submit` adds it when it is missing
#SBATCH --nodes=1
#SBATCH --time=02:00:00          # set it: the default is 1 h, and the estimate uses it
#SBATCH --output=logs/%x-%j.out  # Slurm creates no directory: `mkdir -p logs` first
```

- `--partition` may be left out. Examples in JSC's own pages that say `all` or `batch`
  are copied from other systems.
- `--gres=gpu:4` is added to every job that names no `gres`; the bill does not depend
  on the GPU count, and JSC expects production jobs to use all four.
- `-C` / `--constraint` is refused since 2026-11-04 (known issue, no workaround).
- A job that writes to a directory that does not exist for `--output` dies at once
  with no output file. Create it in the push or before `sbatch`.
- The working directory of the job is where `sbatch` ran: `jup submit` runs it in the
  project's directory on the cluster, so paths in the script are relative to that.
- Load modules inside the script, not before submitting: the nodes run ParaStation's
  `psslurm`, which reads the MPI type from the job's own environment.

## GPUs and cores inside a job

- **One task sees one GPU.** A job that starts a single process and wants all four
  sets `export CUDA_VISIBLE_DEVICES=0,1,2,3` itself.
- **Give `--cpus-per-task` on the `srun` line itself.** JSC's page says `srun` does not
  take it from `sbatch` (written for Slurm 23.02; upstream changed that again later,
  and JUPITER runs 25.05). Repeating the option is right either way.
- The layout that matches the hardware is four tasks of 72 cores with a GPU each:
  `srun --ntasks-per-node=4 --cpus-per-task=72 --gpus-per-task=1 ./app`. GPU 0 sits
  with cores 0-71, GPU 1 with 72-143, GPU 2 with 144-215, GPU 3 with 216-287.
- Several independent programs on one node: start each with
  `srun --exclusive -n 1 --gres=gpu:1 ./prog &` and end the script with `wait`; or run
  plain background processes and pin them yourself (`taskset`,
  `CUDA_VISIBLE_DEVICES`). Processes that each pin their own children must be given
  separate cores, or they all start from core 0.
- `mpiexec` is not supported; MPI programs start with `srun`.
- `/tmp` and `/dev/shm` on a node are memory. Files left there count against the
  job's memory and vanish with it: use `/dev/shm/$SLURM_JOB_ID` for scratch that must
  be fast, and remove it in an `EXIT` trap.
- A node reports about 858 GB of memory to Slurm, but 480 GB of it is CPU RAM: the
  rest is the four GPUs' memory.

## Cost and budget

- **Bill = nodes x 288 x hours of wall-clock time**, in core-hours, from start to end
  of the allocation, idle or not. Nothing is refunded when a job fails, also when the
  system caused it.
- `jup cost SBATCH_ARGS` prices a request before it is made: it reads the options and
  the script's `#SBATCH` lines and assumes the full time limit. The real bill is the
  elapsed time: `sacct -j JOB -X -o JobID,NNodes,Elapsed,State`.
- `jup budget` (`jutil project cpuquota -p <project>`) shows what was granted and
  used, **updated once a day around midnight**. Today's jobs are not in it: your own
  can be added up from `sacct -X --starttime today -o JobID,NNodes,Elapsed`; other
  members' cannot be seen that way.
  `jutil user cpuquota` gives one member's share. `sreport` returns wrong numbers on
  all JSC systems; JuDoor's project page (KontView) has the statistics.
- Most projects get their time in monthly portions and may use last month's, this
  month's and next month's. Time not used from the previous month expires. What
  happens after all three are used is unclear: JSC's accounting page says jobs still
  run, limited to 6 h and at low priority, while its batch page says that use is
  disabled during early access. Do not count on running over budget.

## Watching jobs

| Question | Command |
|---|---|
| What is queued, running, recently done? | `jup jobs` |
| Tell me when it starts, then when it ends | `jup wait JOB --until-running`, then `jup wait JOB`, each as a background command; the second exits 0 only if every task `COMPLETED` |
| What did it print? | `jup logs JOB`; later, `jup ssh -C 'tail -n 50 FILE'` |
| Everything Slurm knows | `jup ssh 'scontrol show job JOB'` (a few minutes after the end at most), then `sacct -j JOB` (kept a year) |
| When will it start? | `jup ssh 'squeue --me --start'` |
| A shell on its node | `sgoto JOB 0` from a login node (node numbers start at 0); needs a terminal, so it is for the user, not for `jup ssh` |

Polling is a command on a shared login node: once a minute is plenty, and `jup wait`
does that. JSC's web portal https://llview.fz-juelich.de/jupiter shows per-job GPU and
CPU use for three weeks; it needs the user's browser login.

## Why a job waits

`squeue` gives the reason in its last column.

| Reason | Meaning |
|---|---|
| `Priority`, `Resources` | normal queueing; `squeue --me --start` estimates the start |
| `ReqNodeNotAvail, UnavailableNodes:...` with a reservation | the reservation's nodes are busy or drained: cancel and submit to the plain partition |
| `AssocGrpCPUMinutesLimit`, `QOS...` | the budget or a per-user limit; check `jup budget` |
| `JobHeldUser` | held: `scontrol release JOB` |
| `BeginTime`, `Dependency` | waiting for a time or another job, as asked |

## Why a job failed

Read the end of the output first (`jup logs JOB`), then the state in `sacct`:

| State | Usual cause |
|---|---|
| `FAILED` with exit code `1:0` or similar | the script's own error: the output file has it |
| `TIMEOUT` | the time limit; a job cut this way does not count as successful for `afterok` |
| `OUT_OF_MEMORY` | CPU RAM (480 GB a node), including files in `/dev/shm` and `/tmp` |
| `NODE_FAIL` | the node: resubmit; the command below the table shows the node's state |
| `CANCELLED` | by the user, or by an administrator with a reason in `scontrol show job` |
| no output file at all | the `--output` directory did not exist, or the script path was wrong |

```bash
sinfo -n $(sacct -j JOB -o nodelist -P | sed -n 2p) --list-reasons --long
```

Occasional failures are expected while the system is in early access; JSC asks to
report only those that look like a general problem.

## Interactive use, chains, arrays

- `salloc --nodes=1 --time=00:30:00 --account=<project>` gives a shell on the login
  node with an allocation; `srun --pty /bin/bash -i` moves onto the node. Both need a
  terminal: they are for the user's own session, not for `jup ssh`. The allocation is
  billed while it exists, used or not: prefer batch jobs, and release an allocation
  as soon as the question is answered.
- Chains: `sbatch --dependency=afterok:JOB next.sbatch`.
- Job arrays, signals before the time limit and requeueing are not documented for
  JUPITER. Try a two-task array or a `--signal` on a short test job before building
  on them, and write results as the job goes so that a cut job can resume.
