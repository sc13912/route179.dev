"""
Minimal LoRA fine-tune of a small causal LM — distributed-ready for Slurm +
torchrun. Defaults to Qwen/Qwen3-4B (ungated, text-only Qwen3ForCausalLM).
Override with the MODEL_ID env var for any other HF AutoModelForCausalLM.

WHAT THIS DOES, step by step:
  1. Loads the base model (MODEL_ID) and its tokenizer.
  2. Loads a small instruction-tuning dataset (Alpaca) and formats each row
     into a single "prompt + answer" string the model learns to reproduce.
  3. Attaches LoRA adapters. Instead of updating all ~4B weights, we freeze
     the base model and train a few million small "adapter" matrices. This is
     what makes fine-tuning a 4B model possible on a single 24GB GPU.
  4. Runs supervised fine-tuning (SFT): the model reads each example and is
     penalized when its next-token prediction differs from the real answer.
     Backprop nudges the LoRA adapters to reduce that error. Repeat over the
     dataset = training.

WHY IT'S DISTRIBUTED-READY WITHOUT EXTRA CODE:
  When you launch this with `torchrun`, PyTorch sets env vars (RANK, WORLD_SIZE,
  LOCAL_RANK, MASTER_ADDR...). HuggingFace Trainer/TRL detects them automatically
  and wraps the model in DistributedDataParallel (DDP): each GPU trains on a
  different slice of each batch, then they average gradients over the network
  (NCCL) every step. You don't write any of that plumbing yourself.

RUN LOCALLY (single GPU, for a smoke test):
  python train.py

RUN UNDER SLURM (multi-node): see finetune.sbatch
"""

import os
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig
from trl import SFTConfig, SFTTrainer

# Default is Qwen3-4B (ungated, text-only). Override via env var if you want a
# different model, e.g. export MODEL_ID=... (see README).
MODEL_ID = os.environ.get("MODEL_ID", "Qwen/Qwen3-4B")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/scratch/lora-out")
# Keep this small for a learning run so it finishes in minutes, not hours.
N_EXAMPLES = int(os.environ.get("N_EXAMPLES", "2000"))


def main():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    # Some base models ship without a pad token; reuse EOS so batching works.
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype="bfloat16")

    # A tiny instruction dataset. "train[:N]" takes only the first N rows.
    dataset = load_dataset("tatsu-lab/alpaca", split=f"train[:{N_EXAMPLES}]")

    def format_example(ex):
        # Turn each row into one plain-text training string (Alpaca template).
        if ex["input"]:
            prompt = (f"### Instruction:\n{ex['instruction']}\n\n"
                      f"### Input:\n{ex['input']}\n\n### Response:\n")
        else:
            prompt = f"### Instruction:\n{ex['instruction']}\n\n### Response:\n"
        return {"text": prompt + ex["output"] + tokenizer.eos_token}

    dataset = dataset.map(format_example, remove_columns=dataset.column_names)

    # LoRA config: r = adapter rank (capacity); target_modules = which layers
    # get adapters (the attention projections is the standard, cheap choice).
    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        task_type="CAUSAL_LM",
    )

    cfg = SFTConfig(
        output_dir=OUTPUT_DIR,
        num_train_epochs=1,
        per_device_train_batch_size=2,      # per GPU
        gradient_accumulation_steps=8,      # effective batch = 2 * 8 * num_gpus
        learning_rate=2e-4,
        bf16=True,
        logging_steps=10,
        save_strategy="epoch",
        max_length=1024,                    # was max_seq_length in older TRL (<1.x)
        gradient_checkpointing=True,        # trades compute for memory
        dataset_text_field="text",
        report_to="none",
        ddp_find_unused_parameters=False,   # correct + faster for LoRA+DDP
    )

    trainer = SFTTrainer(
        model=model,
        args=cfg,
        train_dataset=dataset,
        peft_config=lora,
        processing_class=tokenizer,
    )

    trainer.train()
    # On multi-node, Trainer only writes from rank 0 — safe to call everywhere.
    trainer.save_model(OUTPUT_DIR)
    print(f"Done. LoRA adapters saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
