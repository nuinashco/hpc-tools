# Storage on JUPITER

Sources: https://apps.fz-juelich.de/jsc/hps/jupiter/ (filesystems, faq,
hpc-data-rules, environment, datamover), read 2026-11-12, and use of the project
directory in November 2026.

## File systems

| Path | Variable | Quota (soft) | Kept? | For |
|---|---|---|---|---|
| `/e/home/...` | `$HOME` | 20 GB, 80,000 files | yes, backed up | ssh files and dotfiles only |
| `/e/project1/<project>` | `$PROJECT` | 20 TB, 4 million files, per project | for the project's lifetime, backed up, snapshots | code, environments, results worth keeping |
| `/e/scratch/<project>` | `$SCRATCH` | 200 TB, 8 million files, per project | files untouched for 90 days are deleted | large temporary data |
| `/e/fscratch/<project>` | `$FSCRATCH` | 40 TB, per project | deleted after 30 days | fast temporary data (flash) |
| `/e/data1/<project>` | `$DATA` | as granted | yes | data projects |
| `/dev/shm`, `/tmp` on a node | | the node's memory | gone with the job | fast scratch within one job |

- Quotas are per project, not per person: one member's small files can use up the
  file count for everyone. Pack many small files into an archive.
- Home is small on purpose. Point every cache elsewhere (`references/software.md`).
- The plain variables (`$PROJECT`, `$SCRATCH`) are set only after
  `jutil env activate -p <project>`, and not in the non-interactive shells `jup ssh`
  starts unless the profile does it. Write paths out, or use the per-project form
  `$PROJECT_<project>`.
- `jup ssh 'jutil project dataquota -p <project>'` lists use and limits, refreshed
  every 8 hours.
- Snapshots of the last three days, weeks and months are under
  `/e/project1/.snapshots/<type>-<YYYYMMDD>/`; a deleted file can be copied back from
  there. Scratch has no snapshots and no backup.
- JSC's pages disagree on which file systems compute nodes see. `/e/project1` and
  `/e/scratch` were both there in jobs in November 2026; test anything else with a
  short job.

## Where a project lives

`jup push` copies a project to `/e/project1/<project>/<login>/<repository>` unless the
`remote_dir` setting says otherwise. Environments, caches and weights fit beside it
under `/e/project1/<project>/<login>/`.

## Sharing with the project's members

The project directory belongs to the project's Unix group with the setgid bit, so a
file created inside it gets that group. Two things break this:

- `rsync -a` from another machine keeps that machine's modes and tries to keep its
  group. Files arrive owner-only or in the personal group, and other members cannot
  read them. Copy shared data without those (`rsync -rlt`), or run
  `chgrp -R <project> DIR && chmod -R g+rX DIR` afterwards.
- `jup push` keeps modes and times but not owner or group, so new files take the
  project's group. A file that is owner-only on the user's machine stays owner-only
  there; everything else in the copy can be read by the project's members.
- A member can read a shared path only if every directory above it lets the group in.

For data several members use, a directory at the project's top level
(`/e/project1/<project>/data/...`) with a short `README.md` saying what it holds and
where it came from works better than pointing at one member's workspace.

## Moving data

- **From and to the user's machine**: `jup push`, `jup pull`, or `rsync` over the
  shared connection (`rsync -e 'ssh -o BatchMode=yes' ...`). Always start it on the
  user's machine: no SSH leaves JUPITER.
- **From the internet**: download on a login node (ports 80 and 443 are open). Large
  or long downloads belong on the data gateway `judac.fz-juelich.de`, which mounts
  the same `/e/` file systems and has no time limit; it needs its own key upload.
- **Between storage systems at JSC** (`/p/...` and `/e/...`): the data mover,
  `nd copy --alias exa-just-mover SOURCE DESTINATION`, runs on the servers.
- **Large transfers from outside**: UFTP (`module load uftp`), JSC's recommended tool;
  Globus is retired.
