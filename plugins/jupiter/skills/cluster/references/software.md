# Software on JUPITER

Sources: https://apps.fz-juelich.de/jsc/hps/jupiter/ (software-modules,
stages-changelog, compile, container-runtime, ai-overview, known-issues) and JSC's
module browser https://apps.fz-juelich.de/jsc/software/jupiter/, read 2026-11-12, plus
what was built there in November 2026.

## The machine is Arm

Nodes are `aarch64` (NVIDIA Grace, Neoverse-V2) with RHEL 9. Everything compiled must
exist for `linux/arm64`: Python wheels, container images, binaries. A package with
only x86 wheels has to be built from source or replaced. Login and compute nodes have
the same CPU, so what builds on a login node runs in a job.

## Modules

```bash
module avail                 # what the loaded compiler and MPI offer
module spider PyTorch        # every version, and what to load first
module load Stages/2026      # the default stage; `module use $OTHERSTAGES` reaches others
```

- `module` is a shell function set up by the login profile. `jup ssh` runs commands in
  a login shell, so it is there; a batch script gets it too.
- Stage 2026 (the default): GCC 14.3.0, Python 3.13.5, `CUDA/13`, `PyTorch/2.9.1`,
  `uv/0.8.17`, OpenMPI 5.0.8 and ParaStationMPI, both CUDA-aware. Stage 2025 has
  Python 3.12.3, PyTorch 2.5.1 and `CUDA/12`.
- Compilers: GCC, LLVM and NVIDIA's (`nvidia-compilers`); no Intel. GCC cannot build
  OpenMP or OpenACC offloading for this CPU; use LLVM or NVIDIA's for that.
- The example listings on JSC's modules page come from an x86 system. Trust
  `module spider` on JUPITER and the module browser instead.

## Python

Two ways that work:

- **JSC's modules plus a virtual environment on top**: `module load Python PyTorch`,
  then `uv venv --system-site-packages`. PyTorch and CUDA come built for the machine.
  JSC recommends this over conda; conda's default channel is blocked.
- **An environment of your own with `uv`**: `uv sync` on a login node, from a lock
  file. Use `uv`'s own interpreter (`UV_PYTHON_PREFERENCE=only-managed`): the system
  Python lacks the headers that packages compiling a C helper at start-up need.

Either way:

- Create and update environments on a login node; jobs have no internet. Run jobs
  with `uv run --no-sync` or the environment's own `python`.
- Keep caches out of the 20 GB home, in the project directory:

  ```bash
  export UV_CACHE_DIR=/e/project1/<project>/<login>/cache/uv
  export UV_PYTHON_INSTALL_DIR=/e/project1/<project>/<login>/cache/python
  export HF_HOME=/e/project1/<project>/<login>/cache/huggingface
  export UV_PROJECT_ENVIRONMENT=/e/project1/<project>/<login>/envs/<name>
  ```

  A small script that sets these, sourced by the batch scripts and by hand on the
  login node, keeps both in step.
- In jobs, turn network use off so libraries fail fast instead of waiting:
  `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and the like for other tools.

## Containers

Apptainer 1.5 is on the PATH; no Docker or Podman was found there in November 2026.

- Running a container needs the Unix group `container`: JuDoor, Software, "Request
  access to restricted software", Container Runtime Engine. It can take hours to
  arrive, and an open ssh connection does not see it until it is reopened.
- Pull or copy images on a login node, with the cache away from home:
  `export APPTAINER_CACHEDIR=... APPTAINER_TMPDIR=...`, then
  `apptainer pull image.sif docker://...`. Images must be `linux/arm64`.
- GPUs: `apptainer exec --nv image.sif ...` binds the host's driver libraries.
- Observed in November 2026, not in the docs: user namespaces are off and Apptainer
  runs in setuid mode. Overlay directories do not work and `--writable-tmpfs` is tiny;
  a writable container is a sandbox directory (`apptainer exec --writable DIR ...`),
  best unpacked under `/dev/shm` inside the job. Build images on another Arm machine
  and copy them in; JSC's page contradicts itself on building there.

## Building and tools

- Build on login nodes with a bounded job count: `make -j 12`, never a bare `make -j`
  (20 processes per user, then processes are killed).
- `git` on the cluster works over HTTPS with a token only, since no SSH leaves it.
  Most projects do not need git there: `jup push` copies the working tree and writes
  the commit it came from to `.synced_from` beside it.
- Attaching a debugger to a running process fails (`ptrace_scope` is 2).
