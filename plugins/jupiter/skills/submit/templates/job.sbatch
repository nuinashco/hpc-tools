#!/bin/bash
#SBATCH --job-name=CHANGE-ME
#SBATCH --nodes=1
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x-%j.out
#
# Submit from the project with `jup submit job.sbatch [arguments]`: it runs sbatch in the project's directory on
# the cluster, adds the account, and creates logs/. A node is billed whole, its four GPUs used or not.
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"

# If the project keeps its caches and its environment's place in one file, read it here and on the login node.
# source scripts/env.sh

# Jobs have no internet: a library that tries a download should fail at once, not wait.
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

# This script and a single srun task see all four GPUs (CUDA_VISIBLE_DEVICES=0,1,2,3), and the node is billed
# with all of them: a program written for one GPU leaves three paid for and idle. Start one process a GPU, or
# use a launcher that spreads the work; `jup wait` reports GPUs that stay idle once the job has warmed up.

# Scratch in the node's memory, fast and gone with the job. It counts as the job's memory.
scratch=/dev/shm/${SLURM_JOB_ID:-$$}
# At the time limit Slurm sends TERM and kills some seconds later (how many is not published for JUPITER): leave
# through EXIT, so that the clean-up below runs. Keep it to seconds, and write results as the job goes.
trap 'exit 143' TERM INT
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch"

echo "job ${SLURM_JOB_ID:-none} on $(hostname); $(head -1 .synced_from 2>/dev/null || echo 'no .synced_from')"
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader

# The work. Environments are made on a login node beforehand; --no-sync keeps uv from reaching for the network.
uv run --no-sync python main.py "$@"
