# hpc-tools

Claude Code plugins for the HPC systems we work on, one plugin per system. Install the plugin for a cluster once
and Claude knows how to work there from any repository: how to get in, what a job costs, how to submit and
follow one, and what has already gone wrong on that machine.

| Plugin | System | Scheduler | Docs |
|---|---|---|---|
| `jupiter` | [JUPITER](https://apps.fz-juelich.de/jsc/hps/jupiter/), Jülich Supercomputing Centre | Slurm | [plugins/jupiter](plugins/jupiter/README.md) |

Each plugin stands alone: it has its own skills, its own helper command and its own settings, and installing one
brings in nothing from the others. They are for Claude Code on macOS and Linux: the helpers need `python3` and
OpenSSH's shared connections, which Windows lacks, and claude.ai does not install plugins that ship a command.

## Install

In Claude Code:

```text
/plugin marketplace add nuinashco/hpc-tools
```

```text
/plugin install jupiter@hpc-tools
```

Enter them one at a time: pasted together they are read as one command.

Then ask Claude to set you up ("set up JUPITER for this repo"), or run the plugin's setup skill
(`/jupiter:setup`). The plugin's own page says what it adds and how to configure it.

The repository is private: Claude Code fetches it with your own git credentials (`gh auth login`, then
`gh auth setup-git`). A marketplace like this one does not update by itself: `/plugin marketplace update
hpc-tools`.

To have a repository suggest a plugin to everyone who works in it, commit this to its `.claude/settings.json`;
each person still runs `claude plugin install jupiter@hpc-tools --scope project` once in each clone (or installs
it for all their projects as above):

```json
{
  "extraKnownMarketplaces": {
    "hpc-tools": { "source": { "source": "github", "repo": "nuinashco/hpc-tools" } }
  },
  "enabledPlugins": { "jupiter@hpc-tools": true }
}
```

## What a plugin here gives you

The plugins follow one shape, so that moving between systems means learning the system and not the tool:

- **A knowledge skill** with the system's facts and working rules, and reference files that name their sources.
- **Task skills** for setting up access, submitting a job and following jobs.
- **A helper command** the skills run for the steps that must not go wrong: the connection check, copying a
  project, pricing and submitting a job, waiting for it.
- **A cost check**: Claude Code asks you before a submission above a limit you set, or one whose cost it cannot
  read for certain, and shows what it costs.
- **A feedback command**: when the plugin gets something wrong, `/<system>:feedback` prepares a report of the
  session's plugin part, with names and secrets taken out, and sends it to this repository's issues once you
  have read it and agreed. That is how the plugins improve.

## Development

```bash
make setup    # environment and git hooks
make check    # ruff, pyright, tests (also on Python 3.9), `claude plugin validate`
claude --plugin-dir plugins/jupiter    # try a working copy in a session, without installing
```

Layout, rules and how to add a system: [CONTRIBUTING.md](CONTRIBUTING.md).
