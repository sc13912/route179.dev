#!/bin/bash
#SBATCH --job-name=lora-finetune
#SBATCH --nodes=2                 # 2 nodes → exercises INTER-node NCCL (over network)
#SBATCH --ntasks-per-node=1       # ONE task per node; torchrun spawns the GPU workers
#SBATCH --gres=gpu:4              # 4 GPUs/node (g6.12xlarge) → exercises INTRA-node NCCL
#SBATCH --exclusive               # give the whole node to this job
#SBATCH --output=/scratch/logs/%x-%j.out
# Result: 2 nodes x 4 GPUs = 8 ranks. Intra-node ranks talk over PCIe (fast),
# inter-node ranks over the VPC network (slower) — the blog's 2-level hierarchy.

# --- Rendezvous setup -------------------------------------------------------
# The first node in the allocation acts as the coordinator ("master") that all
# ranks connect to for setting up the process group.
export MASTER_ADDR=$(scontrol show hostnames "$SLURM_JOB_NODELIST" | head -n 1)
export MASTER_PORT=29500

# GPUs per node. Must match --gres=gpu:N above.
GPUS_PER_NODE=4

# --- EFA / RDMA fast path (our g6.12xlarge nodes have EFA enabled) -----------
# NCCL reaches EFA through libfabric + the aws-ofi-nccl plugin (bundled in the
# DLAMI). These make inter-node all-reduce ride EFA instead of TCP sockets.
export FI_PROVIDER=efa
export LD_LIBRARY_PATH=/opt/amazon/ofi-nccl/lib:/opt/amazon/efa/lib:$LD_LIBRARY_PATH
# L4 has NO GPUDirect RDMA. On g6 you MUST tell NCCL this explicitly or it hangs
# on the first collective (NCCL init completes, then GPUs sit at 0% forever):
#   FI_EFA_USE_DEVICE_RDMA=0 → don't attempt GPU-direct DMA (L4 can't)
#   NCCL_PROTO=simple        → matches the plugin's forced "simple protocol" mode
# Verified: with these, an 8-rank cross-node all_reduce completes correctly.
export FI_EFA_USE_DEVICE_RDMA=0
export NCCL_PROTO=simple
# CRITICAL on g6/L4: the 4 local GPUs have no NVLink, so NCCL uses PCIe P2P for
# intra-node comms. On a VM (EC2), PCI ACS redirects P2P traffic in a way that
# HANGS the first collective (NCCL init completes, GPUs sit at 0%). You can't
# disable ACS on a VM, so disable NCCL's P2P transport instead:
export NCCL_P2P_DISABLE=1

# Interface for NCCL's bootstrap/socket traffic and the TCP fallback path.
# VERIFIED on our nodes it is enp39s0 (EFA device itself is rdmap47s0).
export NCCL_SOCKET_IFNAME=enp39s0
export NCCL_DEBUG=INFO            # verbose NCCL logs — great while learning
# With NCCL_DEBUG=INFO you should see a line like "NET/OFI ... using EFA" — that
# confirms the RDMA path. If you instead see "NET/Socket", EFA didn't engage.

# HF token only needed for GATED models. Qwen3-4B (the default) is ungated, so
# this can stay empty. Set it if you switch MODEL_ID to a gated model.
export HF_TOKEN=${HF_TOKEN:-}
export HF_HOME=/scratch/hf-cache  # shared cache on NFS so all nodes reuse it

# --- Launch -----------------------------------------------------------------
# `srun` starts ONE copy of the command on EACH node (ntasks-per-node=1).
# Inside each, torchrun launches GPUS_PER_NODE worker processes and assigns
# each a global rank. torchrun reads SLURM_* env to know node count & its index.
# NOTE: we call `python3 -m torch.distributed.run` (== torchrun) instead of the
# `torchrun` binary. pip --user puts that binary in ~/.local/bin, which is NOT on
# PATH in a non-login Slurm shell → "torchrun: No such file or directory". The
# module form doesn't depend on PATH.
srun python3 -m torch.distributed.run \
  --nnodes=$SLURM_JOB_NUM_NODES \
  --nproc_per_node=$GPUS_PER_NODE \
  --rdzv_backend=c10d \
  --rdzv_endpoint=$MASTER_ADDR:$MASTER_PORT \
  train.py
