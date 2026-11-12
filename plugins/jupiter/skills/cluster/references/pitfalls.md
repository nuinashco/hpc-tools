# Mistakes already made on JUPITER

Each of these cost time or core-hours on this system in November 2026. The first
group burns budget, the second wastes a day, the third confuses.

## Jobs that pay for nothing

- **A job whose work is refused after start-up.** A batch was submitted with settings
  its own program rejected in the first second, after six minutes of loading a model
  on a billed node. Run the program's own argument check or a dry run on the login
  node first, with the exact arguments the job will get.
- **A job that finds nothing to do.** A resumable batch skips work that already has
  results; resubmitting a finished batch still pays for start-up. Look at what is
  already there before submitting again.
- **A full-size first run.** The first run of anything new goes to `develbooster`
  with a handful of inputs and a short time limit. Ten minutes on one node cost 48
  core-hours; a twelve-hour job that dies at minute seven costs about as much, but
  the next one may die at hour eleven.
- **A test reservation that cannot start.** All eight `develbooster` nodes were
  drained one afternoon and the job waited with `ReqNodeNotAvail`. Check the
  reservation's nodes before submitting to it; the plain partition started short jobs
  within minutes.
- **An idle interactive allocation.** `salloc` bills until it is released.
- **Output into a directory that does not exist.** Slurm creates no directory for
  `--output`; the job ends at once and leaves nothing to read.

## Access that stops working

- **A new network.** On a phone's hotspot the login node refused the key
  (`Permission denied (publickey)`), because the key's `from=` clause held only the
  home address. The shared connection had dropped with the network, so nothing worked
  until the user switched back. Before a trip, the user adds the other network's
  range in JuDoor.
- **Groups that seem not to arrive.** After the `container` group was granted,
  commands over the already open connection still lacked it. Close the shared
  connection and log in again.
- **Files a teammate cannot read.** `rsync -a` copied a file with mode 600 and the
  personal group; `chmod g+r` alone did not help, because the group was wrong too.
  See "Sharing" in `storage.md`.

## Results that mislead

- **Yesterday's budget.** `jutil` showed no trace of the day's jobs; its numbers
  change once a night.
- **A copy newer than what it describes.** `jup push` while a job runs changes the
  code under it: Python reads modules when a process starts, so later processes of
  the same job run the new code. Push before submitting, not during.
- **Overwritten measurements.** A push copies local files over the remote ones. A
  file a job changed on the cluster (a table of results kept in the repository, for
  one) is replaced by the stale local copy unless it is pulled first.
- **Processes fighting over the same cores.** Sixteen processes each pinned their
  children starting from core 0, and 64 containers shared 26 of 288 cores. Give each
  process its own share (`taskset`) when one node runs several.
- **A time limit in the middle of the last step.** A job cut by its limit skipped the
  bookkeeping at the end of its script. Do that in an `EXIT` trap, keep it to seconds
  (Slurm kills shortly after its warning), and write results as they come.
- **Docs for another machine.** Example scripts on JSC's JUPITER pages name
  partitions (`all`, `batch`), paths (`/p/...`) and compilers (Intel) of other
  systems. The commands on JUPITER itself are the reference.
