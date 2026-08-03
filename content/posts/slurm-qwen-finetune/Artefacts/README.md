# Artefacts — Slurm + Qwen3-4B distributed fine-tune

Companion files for the blog post *"Building a Slurm GPU Cluster on AWS — and
Fine-Tuning Qwen3-4B Across It."* These are the actual scripts and the real run
log from the cluster (2× g6.12xlarge, 8× L4, EFA, ap-northeast-2).

| File | What it is |
|---|---|
| `finetune.sbatch` | The Slurm batch script. Requests 2 nodes × 4 GPUs, sets the EFA/NCCL env (`FI_PROVIDER=efa`, `NCCL_P2P_DISABLE=1`), and launches `train.py` across all 8 GPUs via `srun` + `torch.distributed.run`. |
| `train.py` | Minimal LoRA fine-tune of Qwen3-4B on Alpaca (HuggingFace TRL `SFTTrainer`). No distributed boilerplate — the framework wires up DDP from the torchrun environment. |
| `lora-finetune-14.out` | The real Slurm job log from the run in the post. Contains the NCCL topology (intra-node `SHM` vs inter-node `NET/Libfabric`/EFA) and the final training metrics. |
| `lora-out.png` | Screenshot of the produced adapter directory `/scratch/lora-out` after the run. |

## The run

```
{'train_runtime': 95.07, 'train_loss': 1.803, 'mean_token_accuracy': 0.614, 'epoch': 1}
Done. LoRA adapters saved to /scratch/lora-out
```

16 steps, one epoch across 8 GPUs in ~95 s, producing a 47 MB LoRA adapter
(`adapter_model.safetensors`). A deliberately small run to demonstrate the
distributed pipeline end to end — scale up via `N_EXAMPLES` and `num_train_epochs`.
