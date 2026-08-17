---
title: "Running Slurm on Kubernetes with Slinky"
date: 2026-08-16T10:00:00+10:00
draft: false
slug: slurm-on-kubernetes-with-slinky
tags: ["Slurm", "Slinky", "NCCL", "LoRA"]
cover:
  image: "slinky-post-title.png"
  alt: "Running Slurm on Kubernetes with Slinky"
  relative: true
---

A few weeks ago I built a Slurm GPU cluster [from scratch on bare
EC2](https://route179.dev/2026/08/03/slurm-qwen-finetune/) and fine-tuned Qwen3-4B
across two `g6.12xlarge` nodes. This post runs the **same** Slurm fine-tune — but using the
[Slinky operator](https://github.com/SlinkyProject/slurm-operator) on Amazon EKS.

Slinky is an open-source project for running Slurm inside Kubernetes: a Kubernetes operator, 
with custom controllers and CRDs, that manages the lifecycle of a Slurm cluster and its `NodeSet`s as native
Kubernetes resources. The point is to get the best of both worlds: combining Slurm's
deterministic, fine-grained batch scheduling with Kubernetes' dynamic resource
allocation and rapid scaling.

Slinky abstracts away almost all of the *operations* 
(config files, munge keys, hostnames, node registration) and changes *none* of the
*infrastructure* (NCCL, EFA, RDMA etc). 

---

## What we're building

| cpu nodegroup | gpu nodegroup | EFS `/shared` — mounted by both |
|---|---|---|
| 2× m6i.2xlarge (no GPU) | 2× g6.12xlarge — 4× L4 + 1× 40 Gbps EFA each | RWX network filesystem |
| slurmctld · slurmrestd · login · slurm-operator | slurmd × 2 (one per node) | model cache · sbatch scripts · LoRA output |

The Slinky operator replaces almost everything I did by hand last time:

| Bare EC2 | Slinky on EKS |
|---|---|
| `slurm.conf` written by hand on 3 boxes | one Helm values block |
| `munge.key` copied by `scp`, then `systemctl restart munge` | operator generates + mounts it |
| `hostnamectl set-hostname` + `preserve_hostname` | pod names, nothing to do |
| hand-written `gres.conf` | `Gres: ["gpu:nvidia_l4:4"]` + `GresTypes: ["gpu"]` |
| NFS export + `fstab _netdev,nofail` | EFS + a RWX PVC |
| adding a node = launch, install, configure | `replicas: 2 → 3` |
| **NCCL tuning: 7 env vars** | **the same 7 env vars** |


---

## Prerequisites

This walkthrough assumes that you prepare an EKS cluster with the following prerequisites. 
So I won't repeat the basics here — the config files are in the repo for reference. You need:

- **An EKS cluster (k8s 1.36)** with two nodegroups:
  - **2× `g6.12xlarge`** GPU nodes — 4× NVIDIA L4 each, **EFA enabled**, in a
    **single AZ** (EFA can't cross AZ), tainted so only `slurmd` lands on them.
  - **2× `m6i.2xlarge`** CPU nodes labelled `role: core` — for `slurmctld`,
    `slurmrestd`, the login pod and the operator.
  - The **EFA and NVIDIA device plugins** healthy on the GPU nodes: `nvidia.com/gpu: 4`
    and `vpc.amazonaws.com/efa: 1` per node. (If you taint the GPU nodes, patch the
    EFA plugin's toleration — it ships without one.)
- **The AWS Load Balancer Controller** installed — the login node is exposed through
  a real NLB, and a locked-down security group that allows SSH (22) from **your IP
  only** (`<YOUR_IP>/32`). You supply that SG's id later as `LOGIN_SG`.
- **An EFS filesystem** with mount targets in your node subnets, a `gp3` StorageClass
  for the controller's state PVC, and a **static `/shared` RWX PV+PVC** bound in the
  `slurm` namespace. Both `slurmd` pods mount the same `/shared` — that's the Slurm
  shared directory.
- **A custom `slurmd` container image** in ECR (more below).

> Reference files in the repo (not walked through here): `cluster.yaml` (the eksctl
> cluster), `alb-install.sh` (load-balancer controller + login SG),
> `efs-setup.sh` (filesystem → mount targets → StorageClass → `/shared`),
> `gp3-storageclass.yaml`, and `shared-volume.yaml`.

### Prepare the slurmd image

Slinky runs `slurmd` *as* the container, so everything a job needs at runtime —
torch, CUDA, the EFA/NCCL userspace, `peft`/`trl` — has to be baked into the image.
The AWS Deep Learning Container has that stack but no `slurmd`; the Slinky base has
`slurmd` but no torch. So `image/build-image.sh` does a two-stage build that harvests
the DLC's accelerator stack onto the Slinky base and pushes the result to ECR.

The `Dockerfile` carries the fiddly bits — a handful of EFA/NCCL library paths that
have to line up, or EFA silently falls back to TCP — and asserts them at build time,
so a broken image fails the build rather than a GPU job 20 minutes later. Reference
the pushed image in the values file (next step) as
`<ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/dlc-slurmd-qwen:<tag>`.

---

## Step 1 — Install Slinky

Everything below is upstream SchedMD charts (`oci://ghcr.io/slinkyproject/charts/*`),
pinned to **chart 1.2.1 = Slurm 26.05.2**. The chart and the `slurmd` image tag are a
**matched pair** — 1.2.x expects the 26.05 image; mixing them leaves workers in
`ImagePullBackOff`.

### Cert-manager 

```bash
helm repo add jetstack https://charts.jetstack.io --force-update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version v1.16.2 --set crds.enabled=true \
  --set nodeSelector.role=core --wait --timeout 10m
```

### The Operator

```bash
CHART=1.2.1

helm install slurm-operator-crds \
  oci://ghcr.io/slinkyproject/charts/slurm-operator-crds \
  --version $CHART --namespace slinky --create-namespace --wait --timeout 5m

helm install slurm-operator \
  oci://ghcr.io/slinkyproject/charts/slurm-operator \
  --version $CHART --namespace slinky \
  --set operator.nodeSelector.role=core \
  --set webhook.nodeSelector.role=core --wait --timeout 10m
```

### SSH key for the login pod

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_slurm -C slurm-login -N ""
cat ~/.ssh/id_ed25519_slurm.pub          # goes into the values file below
```

### Render the values file, then install the cluster

The values file contains the entire cluster definition — nodesets, login service,
partition, GRES. A handful of fields are environment-specific, so it's a template
with `sed` placeholders:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2
LOGIN_SG=sg-xxxxxxxx      # the SG that allows SSH from your IP (a prerequisite)

sed -e "s|__ACCOUNT__|${ACCOUNT}|g" \
    -e "s|__REGION__|${REGION}|g" \
    -e "s|__LOGIN_SG__|${LOGIN_SG}|g" \
    -e "s|__SSH_PUBKEY__|$(cat ~/.ssh/id_ed25519_slurm.pub)|g" \
    slurm-values.yaml.in > slurm-values.yaml

helm install slurm oci://ghcr.io/slinkyproject/charts/slurm \
  --version $CHART --namespace slurm --create-namespace \
  --values slurm-values.yaml --wait --timeout 20m
```

**This is where the image you built is used** — the `nodeset`'s `slurmd`
image in `slurm-values.yaml.in`:

```yaml
nodesets:
  slinky:
    replicas: 2                 # 2 pods x 4 GPUs = 8 ranks
    slurmd:
      image:
        repository: __ACCOUNT__.dkr.ecr.__REGION__.amazonaws.com/dlc-slurmd-qwen
        tag: 26.05.2-torch2.9-cu130-mpi
```

That single `helm install` generates everything I hand-wrote on bare EC2 —
`slurm.conf`, `gres.conf`, `cgroup.conf`, the `munge.key`,
`hostnamectl` on every node — and mounts it into the pods for you.

**One value in `slurm-values.yaml.in` that will silently ruin your day if you miss
it** — `GresTypes`, under `controller`, and the matching `Gres` on the nodeset:

```yaml
controller:
  extraConfMap:
    GresTypes: ["gpu"]              # miss this and ALL GRES is silently discarded
nodesets:
  slinky:
    extraConfMap:
      Gres: ["gpu:nvidia_l4:4"]     # type MUST be nvidia_l4 (what `slurmd -C` reports), not l4
```

**What you should see:**

```bash
kubectl -n slurm get pods
# slurm-controller-0        2/2  Running
# slurm-login-slinky-...    1/1  Running
# slurm-restapi-...         1/1  Running
# slurm-worker-slinky-0     2/2  Running
# slurm-worker-slinky-1     2/2  Running
```

Five pods, `slurm-worker-slinky-[0-1]` on the two GPU nodes. The login Service comes
up behind the NLB (takes 2–3 min for `EXTERNAL-IP` to resolve):

```bash
kubectl -n slurm get svc slurm-login-slinky      # note the hostname → <login-endpoint>
```

---

## Step 2 — Verify cluster status

Before submitting anything, confirm the cluster is actually
wired the way you think. The checks that matter:

```bash
kubectl -n slurm get pods -o wide                       # all Running, workers on 2 DIFFERENT nodes
kubectl -n slurm exec $LOGIN -- sinfo -N -o "%N %t %C %G"  # idle, GRES = gpu:nvidia_l4:4 (NOT null)
kubectl -n slurm exec $WORKER -- nvidia-smi -L           # 4x NVIDIA L4
kubectl -n slurm exec $WORKER -- \
  bash -c 'LD_LIBRARY_PATH=/opt/amazon/efa/lib /opt/amazon/efa/bin/fi_info -p efa'   # provider: efa
kubectl -n slurm exec $LOGIN -- srun --mpi=list          # must list pmix
```

**`sinfo` must show `idle` with real GRES** — `(null)` means the `GresTypes` trap above. 
And **`srun --mpi=list` must offer `pmix`** — the benchmark can't launch without it.

---

## Step 3 — Run the jobs

You're a Slurm-on-k8s user now: **ssh into the login node and `sbatch`** — exactly like any other Slurm login node. 
The endpoint is the NLB hostname; the SSH key is the one you generated in Step 1.

Stage the job files onto `/shared` once, then log in and submit — in this order:

```bash
# once, from your machine:
scp -i ~/.ssh/id_ed25519_slurm slurm-scripts/* root@<login-endpoint>:/shared/

ssh -i ~/.ssh/id_ed25519_slurm root@<login-endpoint>
cd /shared
sbatch sanity.sbatch            # 1. is the fabric correct?
sbatch nccl-tests-build.sbatch  # 2. compile the NCCL benchmark (once)
sbatch nccl-allreduce.sbatch    # 3. is EFA fast, and not silently on TCP?
sbatch nccl-allgather.sbatch    # 4. same check, second collective
sbatch finetune.sbatch          # 5. the real LoRA run
```

Watch any job with `tail -f /shared/logs/<name>-<jobid>.out`; `squeue` between them.

### 3.1 — Smoke test

`sanity.sbatch` runs an 8-rank, 256 MB all-reduce over torchrun and checks the
result is numerically correct. It ends with:

```
NET/OFI ... Selected provider is efa
all_reduce OK: value 8.0 == world_size 8.0
```

`value 8.0 == world_size 8.0` (8 GPUs each contributed 1.0). If you see `NET/Socket`
instead of `NET/OFI`, EFA didn't engage. This is a fast correctness gate — but it
does *not* tell you the interconnect is **fast**, only that it works. That's why the
benchmark comes next.

### 3.2 — NCCL benchmark

To measure the interconnect you want NVIDIA's `nccl-tests`, which is source-only, 
so you compile it once (`nccl-tests-build.sbatch`) onto `/shared`. It ends with the 11 `*_perf` binaries and this line:

```
libmpi.so.40 => /lib/x86_64-linux-gnu/libmpi.so.40     # the SYSTEM MPI, NOT /opt/amazon/openmpi
```

What worked for me: build `MPI=1` against the **system** OpenMPI and launch with
`srun --mpi=pmix`. Slurm's `mpi_pmix_v5.so` and that MPI both link the same
`libpmix.so.2`, so they agree. (You'll see a `gds/shmem` PMIx warning alongside — it
is harmless and *not* the fault; ignore it.)

Then the benchmarks print a size sweep and a summary. Results on 2× `g6.12xlarge`,
8× L4, over 1× 40 Gbps EFA:

| collective   | peak algbw | peak busbw | avg busbw    |
|--------------|------------|------------|--------------|
| `all_reduce` | 2.49 GB/s  | 4.36 GB/s  | **4.11 GB/s** |
| `all_gather` | 5.82 GB/s  | 5.09 GB/s  | **5.01 GB/s** |

`nranks 8`, `#wrong 0` on every row, `Out of bounds: 0 OK`, and EFA on every rank
(zero socket lines).

**Why is `all_gather` higher than `all_reduce`?** `busbw` is `algbw` times a
collective-specific factor (`2(N-1)/N` for all-reduce, `(N-1)/N` for all-gather).
all-reduce = reduce-scatter **+** all-gather **+** the reduction arithmetic — two
phases and compute — so on a host-RAM-staged path it lands lower. all-reduce is the
heaviest common collective and almost always shows the lowest busbw. It's also the
one DDP uses, so **4.11 GB/s is the number that predicts training throughput.**


### 3.3 — The fine-tune

```bash
sbatch finetune.sbatch
```

`finetune.sbatch` is the job wrapper (Slurm directives + environment + launcher);
`train.py` is the workload. Slurm places **one** torchrun per node
(`--ntasks-per-node=1`), torchrun fans out to **4** workers per node
(`--nproc_per_node=4`) → 8 ranks, joined via c10d rendezvous at `MASTER_ADDR`. It
ends with:

```
{'loss': 1.98,       'mean_token_accuracy': 0.5764, 'epoch': 0.64}   # partway through
{'train_loss': 1.798, 'mean_token_accuracy': 0.6144, 'epoch': 1}     # final
Done. LoRA adapters saved to /shared/lora-out
```

Over the 16 steps, `mean_token_accuracy` rose from **0.5764 to 0.6144** and loss fell
from 1.98 to a final `train_loss` of **1.798** — both moving the right way, so the
model is learning. `ls /shared/lora-out` shows `adapter_model.safetensors` (~47 MB).
Read that as a *pipeline-works* signal, not a good model: it's a deliberately tiny
16-step run on training data with no eval split, exactly as in the bare-EC2 post.

---

## Results, and how they compare to bare EC2

Same model, same `train.py`, same 8× L4 — so the training **outcome** is identical:

| metric (16 steps, 1 epoch, 8 GPUs) | bare EC2 | Slinky / EKS |
|---|---|---|
| `train_loss` (from ~1.99) | 1.803 | 1.798 |
| `mean_token_accuracy` | 0.614 | 0.614 |
| LoRA adapter | ~47 MB | 47 MB |
| `train_runtime` | 95.07 s | 60.65 s |

Loss, accuracy and adapter size match to the third digit — which is the point:
**Slinky changed *how the job is scheduled*, not *what it computes*.**


---

## What changed, and what didn't

The operator abstracted away every bit of *Slurm* toil: the config files, the munge
key, hostnames, node registration, adding capacity. It abstracted away exactly none
of the *physics*:

- L4 still has no NVLink → `NCCL_P2P_DISABLE=1` intra-node (EC2 PCI ACS hangs P2P).
- L4 still has no GPUDirect RDMA → `FI_EFA_USE_DEVICE_RDMA=0`, data through host RAM.
- EFA still can't cross an AZ → both GPU nodes pinned to one AZ.
- The **same seven NCCL/EFA env vars** from the bare-EC2 job carried over verbatim.

`train.py` needed **zero** changes. Only the `.sbatch` moved four lines
(`NCCL_SOCKET_IFNAME`, `/scratch`→`/shared`, `HF_HOME`, `LD_PRELOAD`), and even those
are environment plumbing, not training logic.

### Where did my `slurm.conf` and `gres.conf` go?

You never write them. The operator renders them from the Helm values and mounts them
into the pods. Here's what it looks like from the running controller:

```ini
# /etc/slurm/slurm.conf  — generated by the operator
ClusterName=slurm_slurm
SlurmctldHost=slurm-controller-0(slurm-controller.slurm)
SlurmctldParameters=enable_configless,reconfig_on_restart
AccountingStorageType=accounting_storage/none
NodeSet=slinky Feature=slinky
PartitionName=slinky Nodes=slinky Default=YES MaxTime=24:00:00 State=UP
GresTypes=gpu

# /etc/slurm/gres.conf
AutoDetect=nvidia
```

Notice there are **no `NodeName=` lines**. These are *dynamic* nodes (`slurmd -Z`):
each worker self-registers, and `AutoDetect=nvidia` finds its GPUs. `scontrol show
node` confirms what got picked up — GRES I never declared:

```
NodeName=slinky-0  Gres=gpu:nvidia_l4:4(S:0)  CfgTRES=cpu=48,mem=186130M,billing=48
```

On bare EC2 every one of those lines was something I hand-wrote and `scp`-ed to each
node. Here the `slurmd` binary comes from the image, and this config comes from the
release — the two layers from Step 1, doing their separate jobs.

Kubernetes changed the interface to the cluster. It did not change the machine.

*(One operational note that will bite you: after any image change, the dynamic Slurm
nodes come back `DOWN+DYNAMIC_NORM` — the operator cordons them to drain the pod and
the replacement `slurmd` re-registers into the stale state. Clear it with
`scontrol update nodename=slinky-0,slinky-1 state=resume`, or `sbatch` sits `PENDING`
with no stated reason.)*

---

## References

- **[SlinkyProject/slurm-operator](https://github.com/SlinkyProject/slurm-operator)**
  — SchedMD's operator. Everything Slurm-side here is their upstream charts
  (`slurm-operator-crds`, `slurm-operator`, `slurm`), chart 1.2.1 = Slurm 26.05
- **[awslabs/ai-on-eks](https://github.com/awslabs/ai-on-eks)** — the `Dockerfile`
  started as their two-stage DLC→slurmd build; I fixed three stale library paths that
  silently disable EFA plus the two MPI defects above.
- **[Building a Slurm GPU cluster from scratch](https://route179.dev/2026/08/03/slurm-qwen-finetune/)**
  — the bare-EC2 predecessor this post compares against. `train.py` and
  `finetune.sbatch` come from there, essentially unchanged.

*Repo (cluster config, EFS/storage manifests, Dockerfile, and all the scripts):
[github.com/sc13912/slinky-eks-finetune-qwen](https://github.com/sc13912/slinky-eks-finetune-qwen).*
