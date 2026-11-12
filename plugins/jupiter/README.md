# jupiter

A Claude Code plugin for JSC's [JUPITER](https://apps.fz-juelich.de/jsc/hps/jupiter/) supercomputer: it checks
access, copies a project there, submits Slurm jobs with a cost check, watches them and reads the budget.

JUPITER has two properties that an agent gets wrong without help. Every new SSH connection needs a TOTP code only
you can type, and every job spends a budget you share with your project. The plugin is built around both: it
works through one connection that you open, and it prices a job before submitting it.

## Install

```text
/plugin marketplace add nuinashco/hpc-tools
```

```text
/plugin install jupiter@hpc-tools
```

Enter them one at a time: pasted together they are read as one command.

Then `/jupiter:setup`, or ask Claude to set up JUPITER for the repository you are in.

## What you get

| | |
|---|---|
| Skill `cluster` | facts and rules for JUPITER: access, the partition and its limits, billing, storage, modules, containers, pitfalls |
| Skill `setup` (`/jupiter:setup`) | first-time setup for a person or a repository |
| Skill `submit` (`/jupiter:submit`) | batch script, copy, cheap checks, a small test, then the real submission |
| Skill `jobs` (`/jupiter:jobs`) | queue, waiting, output, why a job failed or waits, cancelling, results, cost |
| Skill `feedback` (`/jupiter:feedback`) | a report of the session's plugin part and a short digest of it for the maintainers; only you can start it, and the digest is sent only on your yes |
| Agent `feedback-digest` | the small model (Haiku) that writes that digest from the report alone |
| Command `jup` | what the skills run; on the Bash tool's PATH while the plugin is enabled |
| Cost check (a hook before each Bash command) | Claude Code asks you before a submission to JUPITER above your limits, or one whose cost cannot be read for certain |
| Session note (a hook at session start) | in a repository with a `.jupiter.json`, tells Claude the account, the remote directory and whether the connection is open |

Claude loads the skills by itself when you mention JUPITER or ask to run something there.

## The `jup` command

```text
jup doctor [--probe]            check settings, SSH and the account
jup config [set KEY VALUE [--project]]
jup ssh [-C] COMMAND...         run a command on the login node (-C: in the project)
jup push [--dry-run] [--all]    copy the project to the cluster
jup pull PATTERN... [--to DIR] [--dry-run]
jup cost SBATCH_ARGS...         what a submission would reserve and cost
jup submit [--dry-run] [--stale-ok] SBATCH_ARGS...
jup jobs [--days N]             queued, running and recent jobs, and what each runs
jup wait JOB [--until-running]  print each state change until the job ends
jup gpus JOB                    a running job's GPUs now: load and memory
jup logs JOB [-n LINES]         the end of a job's output
jup cancel JOB...               stop jobs; says which had already ended
jup budget [--raw]              the account's budget in node-hours and GPU-hours: granted, used, left
jup feedback NOTE... | --digest REPORT | --send FILE | --discard FILE
```

It needs Python 3.9 or later (the one that comes with Apple's command line tools is enough), `ssh`, `rsync` and
`git`, and nothing else. With Claude Code's Bash sandbox switched on, `ssh` cannot reach the shared connection and
`jup` reports it as closed: allow `jup` to run outside the sandbox there.

- **The connection is yours.** Every command that reaches the cluster goes through the connection you opened with
  `ssh hpc-jupiter true`. When it is closed, `jup` says so and exits with code 66. It never asks for your TOTP
  code or your key's passphrase and never opens a connection itself, because JSC blocks addresses that connect
  too often. The one exception is `jup doctor --probe`: a single login attempt without prompts, to tell a refused
  key from a missing code, plus a request to api.ipify.org for your public address.
- **`jup push`** copies what git tracks or would add, so ignored files stay on your machine, and it leaves out
  files whose names say they hold secrets (`.env`, `*.pem`, `id_ed25519`, ...). What it copies can be read by the
  other members of your compute project. It replaces files that differ, deletes nothing on the cluster, and
  writes the commit it copied to `.synced_from` beside the copy. A directory that is no git repository is copied
  only with `--all`.
- **`jup pull`** takes paths inside the project and replaces local files that differ; `--dry-run` lists first.
- **`jup submit`** prices the copy of the script that is on the cluster and refuses when it asks for something
  else than the copy here. It also refuses when any file here differs from the copy that `jup push` made, and
  names the files: the job would run the cluster's copy. `--stale-ok` submits that copy as it is. The comparison
  is the one of `jup push --dry-run` (size and time of each file, a few seconds); a directory on the cluster that
  no push made is not compared.
- **Job names.** The submit skill has Claude name each run for what it is about, `PROJECT-METHOD-VARIANT`
  (`codeagent-rl-ppo-clip01` in a repository `codeagent-rl`), passed with `--job-name=` at every submission,
  since a script's own name is the same for every run of it. `jup submit` prints the name the job carries, and
  a note when a run with arguments keeps the script's name. `jup jobs` shows beside each name the script and
  arguments the job was submitted with (`RUNS`, from accounting's record of the submitted line).
- **Exit codes** of `jup` itself are 64 and up (65 settings, 66 no connection, 68 a failure on the cluster,
  69 from `jup wait` for a job whose GPUs sit idle), and
  its messages start with `jup:`; `jup ssh` hands through the remote command's own code.

To use `jup` in your own terminal, add a shell alias to the copy in a clone
(`alias jup=~/src/hpc-tools/plugins/jupiter/bin/jup`). Do not link it into a directory on your PATH: Claude Code
would then run that copy for the skills and the plugin's own for the hooks. `jup doctor` warns when another `jup`
comes first.

## Settings

Per person in `~/.config/hpc-tools/jupiter.json`, per repository in `.jupiter.json` at its root (worth
committing), and for one command as `JUP_<NAME>` in the environment. `jup config` prints the values in effect and
where each came from. Values are checked: names and paths may hold only plain characters.

| Setting | Default | Meaning | In a repository's file? |
|---|---|---|---|
| `host` | `hpc-jupiter` | the alias in `~/.ssh/config` | no, each person's own |
| `account` | none | the compute project, for `sbatch --account` and the remote directory | yes |
| `partition` | `booster` | added to a submission that names none | yes |
| `remote_dir` | `/e/project1/{account}/{user}/{project}` | where the project is copied; `{project}` is the directory's name here, so a clone or worktree under another name goes elsewhere unless the path is written out | yes |
| `sync_exclude` | none | directories (`notes/`) and globs (`*.ipynb`) that `jup push` leaves out | yes |
| `ask_above_nodes` | `1` | the cost check asks before a command that reserves more nodes | only to lower it |
| `ask_above_node_hours` | `1` | or more node-hours at most | only to lower it |
| `gpu_warmup`, `gpu_idle_minutes` | `00:10:00`, `5` | how long a job runs before `jup wait` reads its GPUs, and how long one must stay idle to be reported | no |
| `gpus_per_node` | `4` | the GPUs that come with a node, for the cost in GPU-hours; `0` leaves that out | no |
| `cores_per_node` | `288` | the core-hours `jutil` counts for a node-hour | no |
| `log` | `true` | whether `jup` keeps its local log of runs | no |
| `feedback_repo` | `nuinashco/hpc-tools` | where `jup feedback --send` files a report | no |
| `default_time`, `max_time` | `01:00:00`, `12:00:00` | what the partition gives a job without a time limit, and at most | no |

A repository you clone can therefore not point `jup` at another host, raise the limits of the cost check or
change what it prices with.

## Permissions and the cost check

A plugin cannot grant itself permissions, so Claude Code asks before each `jup` command. Rules for the commands
that only read, for your own `~/.claude/settings.json` or a repository's `.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(jup doctor)",
      "Bash(jup config)",
      "Bash(jup cost *)",
      "Bash(jup jobs*)",
      "Bash(jup wait *)",
      "Bash(jup logs *)",
      "Bash(jup budget)",
      "Bash(jup push --dry-run)",
      "Bash(jup submit --dry-run *)"
    ]
  }
}
```

Do not allow `Bash(jup *)`: `jup ssh` runs any command on the cluster as you, and `jup submit`, `push`, `pull`,
`cancel` and `config set` spend the budget or change files.

The cost check reads every Bash command before it runs. For `jup submit`, and for `sbatch`, `salloc` and `srun`
inside `jup ssh` or `ssh` to JUPITER, it works out nodes x time from the options and the script's `#SBATCH`
lines, in node-hours and GPU-hours, and makes Claude Code ask you when

- the command reserves more than `ask_above_nodes` nodes or `ask_above_node_hours` node-hours, added up over
  everything it submits;
- the cost cannot be read for certain: no `--nodes` or `--time`, a script that is not on your machine, a loop, a
  shell variable, a `SBATCH_*` variable, a script on standard input, a command it cannot parse;
- the request leaves GPUs it pays for unused, whatever its size: fewer GPUs asked for than a node has
  (`--gres=gpu:1`), or a script that shows its programs fewer (`CUDA_VISIBLE_DEVICES=0`);
- the command changes one of the check's own settings, or a job's limits with `scontrol update`.

What a command is given to read, in a here-document, a here-string or a pipe, counts by who reads it. For a shell
or `ssh` it is the commands it holds, on the machine that runs them. For `cat`, `git` and other programs that only
read, it is data, and so is a Python or Node script that starts no program. The check asks about a script in
another language that names a Slurm command or `jup` and can start programs, and about text it cannot read that
names one and goes to a program it does not know.

It is a second look, not a lock. It sees the command Claude is about to run and nothing behind it: a script on
the cluster that itself calls `sbatch`, or a file edited by other means, passes unseen. If the check cannot run
at all, it asks. The question is denied automatically in non-interactive runs (`claude -p`).

## GPUs that are paid for and idle

A node comes with all its GPUs and is billed with them, and no command can tell beforehand whether a job will load
them. So the plugin measures:

- **`jup wait JOB`** reads each GPU's load once a minute after the job has run for `gpu_warmup` (10 minutes: a
  model takes minutes to load). When a GPU stayed below 5% for `gpu_idle_minutes` (5) in a row, the command
  stops with exit code 69 and says which GPUs sit idle, what the job has cost and what it will cost at most.
  It cancels nothing: Claude asks you, in a question that holds those numbers, whether to cancel the job or
  keep it, and runs the command for your answer. In your own terminal the report names the two commands,
  `jup cancel JOB` and `jup wait JOB --idle-ok`.
  A job that ends within those 15 minutes, as a test does, is not judged.
- **The result is kept** for each batch script, in `~/.local/state/hpc-tools/jupiter/gpu-load.json`. When the
  cost check asks about a later submission of that script, its question says what the last measured job did:
  "loaded 4 of 4 GPUs", "loaded 1 of 4 GPUs", or that none was measured yet. `jup submit` prints the same.
- **`jup gpus JOB`** shows a running job's GPUs at any moment.

It measures only while `jup wait` runs, one node's reading takes about 3 seconds on the login node, and a GPU
that is busy with useless work still counts as loaded.

## Feedback

When the plugin gets something wrong, `/jupiter:feedback what went wrong` turns the session into a report for
its maintainers. It is how the skills and the helper get better, and it is built so that nothing leaves your
machine unseen:

- **A local log.** `jup` and the cost check note each run in `~/.local/state/hpc-tools/jupiter/log.jsonl`:
  the command, its exit code and duration, and what the check decided. It never leaves the machine by itself;
  `jup config set log false` turns it off.
- **A report you read first.** `jup feedback NOTE` writes a Markdown file next to the log. It holds your note,
  that log for the session, and the turns of the session's transcript in which the plugin was used: your
  requests in those turns, the `jup`, `ssh` and Slurm commands with their output, and Claude's answers. Other
  tool calls are only counted, and a long session keeps its latest turns (`--whole` includes every turn).
- **Names and secrets taken out.** Tokens, private keys, values of anything named like a password, e-mail and
  network addresses, your home path, and your login and project names become placeholders, and the report says
  how many of each. This works by pattern and can miss things, which is why you read the file before it goes.
- **A digest is what goes.** This repository is public, so an issue on it can be read by anyone. A small
  model that sees only the report writes a digest of at most a page: your note, the `jup` and Slurm commands
  that mattered with the line that went wrong, what was expected, the plugin's version. It leaves out what you
  were working on, and names and secrets are taken out of it once more. The report itself stays on your machine.
- **Sent only on your yes.** You are shown the whole digest and told who can read it where it goes.
  `jup feedback --send FILE` then files it as an issue on `feedback_repo` with your `gh` login; `--discard FILE`
  deletes the report and its digest. Leave `jup feedback --send` out of your permission rules, so that Claude
  Code asks you each time.

The skill cannot be started by Claude, only by you.

## Removing it

Uninstalling the plugin leaves what setup made outside it: `~/.config/hpc-tools/jupiter.json`, the log and
reports in `~/.local/state/hpc-tools/jupiter/`, a repository's `.jupiter.json`, the permission rules you added, the `Host` entry in `~/.ssh/config`, and the copies on the cluster.

## Where the facts come from

The reference files in `skills/cluster/references/` were written from JSC's JUPITER documentation as read on
2026-11-12 and from jobs run on the system in November 2026; each file names its sources. JUPITER is in an
early-access phase and its documentation changes: when a fact there is wrong, fix the reference file.
