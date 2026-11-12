# Contributing

## Layout

```text
.claude-plugin/marketplace.json    the marketplace `hpc-tools`: one entry per plugin
plugins/<system>/                  one plugin per HPC system; everything its users get is in here
  .claude-plugin/plugin.json       manifest: name (the system's), version, description
  README.md                        what the plugin adds, its command, settings and permissions
  skills/cluster/                  the system's facts and rules; references/ holds one file per subject
  skills/setup|submit|jobs/        the tasks
  skills/feedback/                 a report for the maintainers; only the user can start it
  hooks/hooks.json                 before each Bash call: the cost check; at session start: the project note
  bin/<helper>                     launcher; puts lib/ on the path
  lib/<helper>/                    the helper's code
tests/<system>/                    one file per module of that plugin's helper, and one for the launcher
tools/                             checks for this repository, run by the git hooks
```

Today there is one plugin, `plugins/jupiter`, with the helper `jup`. Its code is in layers, each importing only
those after it: `cli` > `feedback`, `hooks`, `sync` > `ssh`, `config` > `slurm`, `journal` > `errors`.

## Rules for every plugin

- **A plugin stands alone.** Claude Code installs each plugin's directory by itself, so nothing in it may reach
  outside: no imports from another plugin, no file at the repository's top level.
- **Helper code runs on Python 3.9 with the standard library only.** It starts on a user's machine with whatever
  `python3` is there, and a hook must not wait for an install. So: `dataclasses` and `argparse` where the
  conventions guide would have attrs and click, and `make test-oldest` runs the tests on 3.9.
- **A helper never prompts and never opens a connection of its own.** Codes and passphrases are the user's to
  type, and centres block addresses that connect too often. A command that needs the connection checks for it
  first and exits with its own code when it is closed. The one login attempt (`doctor --probe`) happens only on
  request and is documented as one.
- **The cost check fails closed.** It asks whenever it cannot read a submission's cost for certain, and when it
  cannot run. It covers only submissions to its own system, so that two plugins never answer for one command. It
  runs before every Bash call, so a command that submits nothing must cost it a few milliseconds.
- **Values from files are checked before use.** Settings reach command lines on both machines and the text
  Claude is shown; `config.check` allows plain characters only. A repository's file is trusted least: it cannot
  set what is each person's own or loosen the cost check.
- **Errors a user can fix are `UserError` subclasses** with a message of the form `what is wrong; the remedy`
  and their own exit code, 64 and up. Skills branch on the codes, so do not reuse one.
- **What a command prints for parsing is marked.** A login shell may print a banner first; `Remote.read_lines`
  reads only the lines the command itself printed.
- **Facts about a system live in its reference files, with source and date**, not in `SKILL.md` bodies and not
  in code comments. A number in code that comes from the system (cores billed per node, the poll interval) has
  a comment saying where it comes from.
- **A skill's description is its trigger.** It says what the skill does and when to use it, in the third
  person, with the words a user would say and the system's name, and says it is not for other clusters. Keep
  `SKILL.md` to what Claude needs every time and move the rest to a file it names. A skill points at another
  skill's file as `${CLAUDE_PLUGIN_ROOT}/skills/...`.
- **Skills have the same names in every plugin** (`cluster`, `setup`, `submit`, `jobs`, `feedback`), so
  `/<system>:submit` means the same thing everywhere.
- **Nothing leaves the user's machine unseen.** The local log is local; a feedback report is a file the user
  reads before `--send`, with secrets and names replaced by pattern. The skill that prepares it cannot be started
  by the model (`disable-model-invocation`), and where reports go is each person's own setting.
- **The transcript's format is not ours.** `feedback.py` reads Claude Code's session files, which change
  between versions: read every field as optional, skip what does not fit, and never fail a report over one
  record.
- **A plugin's version** is `version` in its `plugin.json`. Users get an update only when it changes, so a
  commit that changes a plugin's files must change it; `tools/check_plugin_versions.py` refuses one that does not.
- Tests: pytest functions named as sentences, no network and no real ssh (each plugin's `conftest.py` has a
  stand-in for the programs its helper starts), one behaviour per case. `test_launcher.py` starts the launcher
  and the commands of `hooks.json` as Claude Code does.
- Commits: one line, `type(scope): imperative subject`, no body; the scope is the plugin's name.

## Adding a system

1. Copy `plugins/jupiter` to `plugins/<system>` and `tests/jupiter` to `tests/<system>`.
2. Rename what carries the helper's or the system's name, so that two plugins installed side by side never read
   each other's settings or answer for each other's commands:
   - `bin/jup`, `lib/jup/` and `slurm.HELPER`, by which the cost check knows its own helper in a command;
   - `config.PROJECT_FILE` (`.jupiter.json`), `config.user_file` (`~/.config/hpc-tools/jupiter.json`), the
     `JUP_` prefix of the environment variables in `Config.load`, `journal.state_dir` and `feedback.PLUGIN`;
   - `hooks._NODE_PREFIXES` and `hooks._DOMAIN`, by which the cost check knows the system's own hosts, and the
     system's name in the texts of `hooks.py`;
   - the commands in `hooks/hooks.json`.
3. Replace what is the system's: the defaults in `config.py` (host alias, partition, remote directory, cores
   billed per node, default and longest time), the remedies in the error messages of `ssh.py`, the budget
   command in `cli.py`, and the option tables in `slurm.py` if the scheduler's version differs.
4. Rewrite `skills/cluster` and its reference files from that centre's documentation and from use: do not keep
   a sentence about JUPITER that nobody checked there. Then go through `setup`, `submit` and `jobs` for every
   command and number.
5. Adapt the copied tests until they fail for the right reasons: every host name, file name and message in them
   is JUPITER's. Add the plugin to `.claude-plugin/marketplace.json`, its `lib` to `pythonpath`, `src` and
   `extraPaths` in `pyproject.toml`, and a row to the README's table.

The second plugin is a copy on purpose. What two systems share is not known until both exist, and a shared
library would have to be copied into each plugin anyway, since a plugin is installed alone. When a third system
arrives, or the two copies drift in a way that hurts, move the common code to one place at the top level and
have `make` copy it into each plugin, with a test that the copies are identical.

## Trying a change

```bash
make check
claude --plugin-dir plugins/jupiter     # then /reload-plugins after each edit
echo '{"tool_input":{"command":"jup submit -N 4 -t 12:00:00 job.sh"}}' | plugins/jupiter/bin/jup hook pre-bash
```

A skill change is worth trying on a real request before it is trusted: ask a session with the plugin for what a
user would ask ("run train.py on JUPITER for two hours") and read what it does.
