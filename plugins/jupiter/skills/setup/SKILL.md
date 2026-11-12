---
name: setup
description: Sets up access to JSC's JUPITER supercomputer for a person or a project - the ssh host entry with one shared connection, the key in JuDoor and its from= clause, the compute account, a project's .jupiter.json, and permission rules for the `jup` command. Use when the user has just installed the jupiter plugin, is new to JUPITER, wants a repository made ready for JUPITER, or when `jup doctor` reports a failure.
---

# Setting up JUPITER access

Setup has a part the user must do themselves (anything with a passphrase, a TOTP code
or JuDoor) and a part you can do. Run `jup doctor` first: it says which part is
missing, so that nobody repeats what already works. It reads local settings and uses
the shared connection if one is open; it makes no login attempt of its own.

```bash
jup doctor
```

Work down its lines in order; each section below fixes one. Stop at the first step
that needs the user and tell them exactly what to do.

## 1. The ssh host entry

`jup` talks to an alias in `~/.ssh/config` (setting `host`, default `hpc-jupiter`).
If doctor reports no entry or no shared connection for it, show the user this block
and add it to their `~/.ssh/config` only once they agree, since that file is theirs:

```
Host hpc-jupiter
  HostName login.jupiter.fz-juelich.de
  User <their JuDoor login>
  IdentityFile ~/.ssh/<their key>
  IdentitiesOnly yes
  AddressFamily inet
  ControlMaster auto
  ControlPath ~/.ssh/cm-%C
  ControlPersist 12h
  ServerAliveInterval 60
```

On macOS add `UseKeychain yes` and `AddKeysToAgent yes`, so the passphrase is asked
once. `AddressFamily inet` matches a `from=` clause written with IPv4 addresses; drop
it if theirs uses IPv6.

## 2. The key (the user's part)

If they have no key for JUPITER yet, they run, and choose a passphrase themselves
(JSC requires one):

```bash
ssh-keygen -a 100 -t ed25519 -f ~/.ssh/hpc-jupiter
```

They upload `~/.ssh/hpc-jupiter.pub` in JuDoor (https://judoor.fz-juelich.de, "Manage
SSH-keys" next to JUPITER) with a `from=` clause in front that covers the networks
they work from; the rules for the clause are in
`${CLAUDE_PLUGIN_ROOT}/skills/cluster/references/access.md`. The upload takes about
15 minutes to reach the login nodes. Never generate a key without a passphrase for
them and never ask for the passphrase.

## 3. The first login (the user's part)

```
ssh hpc-jupiter true
```

in a terminal of their own (`! ssh hpc-jupiter true` in Claude Code may not be able to
show the prompts). It asks for the TOTP code and leaves the shared connection open. If it answers
`Permission denied (publickey)`, the address they come from is not in the `from=`
clause yet, the 15 minutes are not over, or the key is not loaded (`ssh-add`).

To tell these apart, `jup doctor --probe` makes one login attempt without prompts and
prints this machine's public address (it asks api.ipify.org for it). Run it once at
most, with the user's agreement, and never again after a refusal: repeated attempts
get the address blocked at JSC's firewall.

## 4. The compute account

```bash
jup ssh 'jutil user projects'        # the projects this login belongs to
jup config set account <project>     # for this person, in ~/.config/hpc-tools/jupiter.json
jup doctor                           # now checks membership and the project directory
```

A person with several projects sets the account per repository instead (next step).

## 5. A repository

Settings that belong to a repository go into `.jupiter.json` at its root, which is
worth committing so that teammates get them:

```bash
jup config set account <project> --project
jup config set sync_exclude 'notebooks/,*.ipynb' --project   # optional
jup push --dry-run                    # lists what would be copied, changes nothing
```

A repository's file may set `account`, `partition`, `remote_dir` and `sync_exclude`,
and may lower the cost check's limits. The ssh alias and the numbers the cost check
prices with are each person's own and are refused there.

`jup push` copies what git tracks or would add, so anything in `.gitignore` stays
home, and it leaves out files whose names say they hold secrets (`.env`, `*.pem`,
`id_ed25519`, ...) and says so. Read the dry run's list with the user before the first
real push: look for large files and for anything private that neither rule catches.
The copy lands in a directory the project's other members can read. It goes to
`/e/project1/<account>/<login>/<repository>`; the `remote_dir` setting moves it. A
directory that is no git repository is copied only with `jup push --all`.

The settings and their values in effect: `jup config`.

## 6. Permission rules

A plugin cannot grant itself permissions, so Claude Code asks before each `jup`
command. Offer the user rules for the commands that only read, for the repository's
`.claude/settings.json` or their own `~/.claude/settings.json`, and add them only
when they agree:

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

Leave `jup ssh`, `jup submit`, `jup push`, `jup pull`, `jup cancel` and `jup config
set` to be asked about: `jup ssh` runs any command on the cluster as the user, and the
others change files or spend the budget. Do not offer `Bash(jup *)`.

The plugin's cost check asks before a submission above one node or 300 core-hours
(the user's own settings `ask_above_nodes`, `ask_above_core_hours`), and whenever it
cannot read a submission's cost for certain. It is a second look, not a lock: it reads
the command Claude is about to run, so it does not see a script on the cluster that
itself calls `sbatch`.

## 7. What a job will need

Check now what would otherwise fail in the first job:

- `jup budget`: the account has core-hours.
- Containers: `container` must be among the groups doctor lists. If not, the user
  requests "Container Runtime Engine" under Software in JuDoor.
- An environment and any downloads have to be made on a login node, because jobs
  have no internet: see `${CLAUDE_PLUGIN_ROOT}/skills/cluster/references/software.md`.

Finish by telling the user what is ready and what still waits on them.
