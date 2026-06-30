---
title: "Optimizing vLLM Cold Start with Model Streaming and Compile Caching"
date: 2026-06-30T00:00:00
slug: optimizing-vllm-cold-start-with-model-streaming-and-compile-caching
tags: ["vLLM", "GenAI"]
---
I've recently been working on an AI-at-the-edge project ([SafetyLens](https://github.com/sc13912/safetylens_v2/)) — that's a topic for another post. During the demo development, I frequently needed to tweak the model serving engine, swap models in and out, and refine configurations. Every one of those changes meant restarting vLLM, and each restart triggered a full cold start that took five minutes or more before the server was ready to serve again.

And that's with just a single `Qwen3.6-35B-A3B` model running off local NVMe storage on 1x NVIDIA DGX Spark node. This will only get worse once I scale the deployment to a multi-node cluster backed by an external PVC, where the weights have to travel over the network on every start. So I went looking for ways to cut down the vLLM cold-start time — and it turns out most of it is recoverable, with no changes to the model or its output quality.

In this post, I'll show you two common methods — concurrent model streaming and a persistent Torch compile cache — that together nearly halved my vLLM cold-start time. I'll explain how each one works along the way, so you can apply them to your own deployment and know exactly which phase you're targeting.

## Understanding the cold-start phases

Before optimizing anything, it helps to understand what vLLM is actually doing during those minutes. A cold start breaks down into three distinct phases:

- **Loading weights** — reading the model parameters off storage and into GPU memory. For a 35B model with NVFP4 quantization, that's 20+ GB to move (a standard FP16 35B model would be far larger).
- **`torch.compile`** — vLLM compiles the model's computation graph into optimized GPU code (more on this later).
- **Profiling and warmup** — vLLM runs a few dummy passes to measure peak memory, size the KV cache, and bring the engine to a steady state.

When I measured each phase on my baseline deployment, the breakdown looked like this:

| Phase | Time |
|---|---|
| Loading weights | ~142 s |
| `torch.compile` | ~39 s |
| Profiling / warmup / other | ~136 s |
| **Total cold start** | **~317 s (5:17)** |

This post focuses on optimizing the first two phases: **weight loading** (the single largest chunk) and **compilation**. The profiling and warmup phase is largely fixed engine overhead that neither optimization touches — a point I'll return to at the end.

## Testing setup

For this demo, I'm running a single **NVIDIA DGX Spark** as an Amazon EKS Hybrid Node, with the model weights stored on its local NVMe disk. The hardware and edge architecture aren't the focus of this post — refer to the below blog posts to learn more:

- [Deploy production generative AI at the edge using Amazon EKS Hybrid Nodes with NVIDIA DGX](https://aws.amazon.com/blogs/containers/deploy-production-generative-ai-at-the-edge-using-amazon-eks-hybrid-nodes-with-nvidia-dgx/)
- [SafetyLens demo project on GitHub](https://github.com/sc13912/safetylens_v2)

Everything below applies to any vLLM deployment, regardless of where it runs. My specific environment:

- **Compute**: 1x NVIDIA DGX Spark (GB10, unified memory), running as an EKS Hybrid Node
- **Storage**: model weights on local NVMe
- **Inference engine**: vLLM `v0.22.1` (serving via `vllm serve` in a Kubernetes Deployment)
- **Model**: `nvidia/Qwen3.6-35B-A3B-NVFP4` — a 35B Mixture-of-Experts model (3B active parameters), NVFP4-quantized
- **Serving features enabled**: MTP speculative decoding (`num_speculative_tokens: 3`), Marlin MoE backend, FlashInfer attention, and an FP8 KV cache

A couple of these features are worth calling out: because I use **MTP (Multi-Token Prediction) speculative decoding**, vLLM loads *two* sets of weights at startup — the main model plus a small MTP draft module — and compiles both.

## Optimization 1: Stream the weights with Run:ai Model Streamer

### The problem

By default, vLLM loads model weights largely sequentially — reading the safetensors files in order, one chunk at a time. The trouble is that modern storage is fast when you read it in parallel, and slow when you read it serially. I tested my NVMe directly: read with a single thread, it delivered about **1.2 GB/s**; read with many parallel threads, it sustained nearly **10 GB/s**. The default loader was leaving roughly 8x of available bandwidth on the table.

### The fix

[Run:ai Model Streamer](https://github.com/run-ai/runai-model-streamer) is an open-source Python SDK created by [Run:ai](https://www.run.ai/) (now part of NVIDIA) to accelerate model loading. It reads tensors from safetensors files **concurrently** — multiple worker threads pulling different chunks at once into a CPU buffer and streaming them on to GPU memory as they arrive. vLLM has built-in support for it as a model loader, so adopting it is just a matter of installing the package and flipping a flag. It's purpose-built to saturate your storage bandwidth, and it reads native safetensors directly, with no format conversion or pre-processing step.

There are two steps to adopt it.

First, the streamer ships as a separate Python package (with prebuilt wheels, including for ARM64), and it is **not** bundled with vLLM by default. I'll add it to my vLLM container image:

```dockerfile
RUN pip install --no-cache-dir runai-model-streamer runai-model-streamer-s3
```

Note: I recommend baking the package into the image rather than running `pip install` at pod startup. My deployment runs with `HF_HUB_OFFLINE=1` for deterministic, offline starts — installing at runtime would reintroduce a network dependency on every cold start and add latency to the very thing I'm trying to speed up.

Next, I tell vLLM to use the streamer by adding two flags to the `vllm serve` command:

```bash
--load-format runai_streamer \
--model-loader-extra-config '{"concurrency":8}'
```

The `concurrency` value is the number of parallel read threads. **More is not always better** — throughput scales with concurrency until your storage saturates, after which extra threads only add contention. On my local NVMe, throughput plateaued at around 8 threads, so that's what I used. If your weights live on higher-latency storage such as a network file system or object storage, higher values (16–64) tend to help, because concurrency hides per-request latency. This is worth tuning for your specific storage backend.

### The result

Weight loading dropped from **~142 s to ~59 s** — a 2.4x improvement on that phase, which pulled total cold start from 317 s down to **221 s**. I didn't change the model or the weights at all; I'm simply reading the same bytes much faster.

## Optimization 2: Cache the compilation with a persistent path

### What `torch.compile` does, and why it can be cached

This optimization needs a little background, because `torch.compile` is the part of vLLM startup that's most often misunderstood.

When PyTorch runs a model in its default "eager" mode, it dispatches each operation — every matrix multiply, every normalization — one at a time from Python, at runtime. That's flexible but carries overhead. `torch.compile` removes that overhead by doing ahead-of-time work: it traces the model into a computation graph, then generates and compiles optimized GPU kernels for that graph, fusing operations together and selecting the fastest implementations. The payoff is faster inference; the cost is that this compilation takes time at startup — about 39 seconds in my case.

Here's the key insight: **the output of `torch.compile` depends only on the model's structure — its layer shapes, data types, and the target GPU — not on the actual weight values.** A matrix multiply of a given shape compiles to the same kernel whether the weights are your trained values or random numbers. That means the compiled result is **reusable**: compile it once, save the artifacts to disk, and every future startup can load the precompiled kernels instead of recompiling from scratch.

Let me use a cooking analogy to explain:

- Your model is a **recipe** (the operations) plus **ingredients** (the weights).
- `torch.compile` is **translating that recipe into precise, optimized steps for your specific kitchen** (in my case, the DGX Spark's GB10 GPU).
- That translation depends on the recipe and the kitchen.
- So you can write down the translated recipe once and reuse it forever. But you still have to carry the ingredients into the kitchen every time you cook.

In other words: **loading weights means bringing in the ingredients (every time); compiling means translating the recipe (once, then cached).** The two are independent, because the generated code doesn't depend on the numbers inside the weights. This is also why fine-tuning is safe: swap the trained weights and redeploy, and the compile cache still hits — you've changed the ingredients, not the recipe. You only pay for a recompile when you change something structural: the model architecture, the data type, the GPU, or the vLLM/PyTorch version.

How does the cache know when it can be reused? On the first run, vLLM (via PyTorch's Inductor compiler) computes a **hash** over everything the compiled code depends on — the model architecture and layer shapes, the dtype and quantization scheme, the compile flags, the GPU compute capability, and the vLLM/PyTorch versions — and writes the compiled artifacts to disk under that hash. On every later start, vLLM still loads the weights, but when it reaches the compile phase it finds a matching hash and **loads the precompiled kernels from disk instead of re-running the compiler** — turning tens of seconds of code generation into a quick deserialize. Change any of those inputs and the hash changes, so you simply get a fresh compile rather than a stale or broken one.

### The fix (and a gotcha to avoid)

vLLM already supports this compile cache — but there's a trap that's easy to fall into. By default, vLLM writes the cache to a path *inside the container* (`/root/.cache/vllm`), which is wiped every time the pod is recreated. So out of the box, you recompile on every single cold start and never see the benefit.

The fix is a single environment variable that points the cache at **persistent storage** that survives pod restarts — in my case, the same host path where the model weights are cached:

```yaml
env:
  - name: VLLM_CACHE_ROOT
    value: /root/.cache/huggingface/vllm_compile_cache
```

That's all it takes. The first startup after a new image build still compiles (and populates the cache); every startup after that loads the precompiled kernels directly. This is also safe by design: if anything in the cache key changes — a new model, a vLLM upgrade — it's simply a cache *miss*, and vLLM recompiles and writes a fresh entry. A stale cache can never cause a failure, only a one-time slow start.

### The result

With a warm compile cache, `torch.compile` dropped from **~39 s to ~10 s**, bringing total cold start from 221 s down to **~167 s**.

## Results

I ran each configuration three times to get reliable averages. All times are wall-clock, from process start to the server reporting ready.

**Per-phase breakdown (average of 3 runs):**

| Phase | Baseline | + Streamer | + Streamer & Compile Cache |
|---|---|---|---|
| Weight load | 142 s | 59 s | 59 s |
| `torch.compile` | 39 s | 38 s | 10 s |
| Profiling / warmup | 136 s | 124 s | 98 s |
| **Total** | **317 s** | **221 s** | **167 s** |

**End-to-end cold start:**

| Configuration | Cold start | Speedup | Time saved |
|---|---|---|---|
| Baseline (default vLLM loader) | **317 s** (5:17) | 1.0x | — |
| + Model Streamer | **221 s** (3:41) | **1.43x** | −96 s (−30%) |
| + Streamer & Compile Cache | **167 s** (2:47) | **1.90x** | −150 s (−47%) |

The streaming result was remarkably consistent across runs. The combined result varied a little more, and that variance lives almost entirely in the profiling and warmup phase — which is the noisiest part of startup, since it runs actual GPU work (a memory-profiling pass, warmup passes, and CUDA graph capture) whose timing shifts with GPU clocks, memory state, and autotuning. It's also worth noting that this phase isn't fully separable from compilation: the warmup passes trigger some additional compilation and autotuning of their own, so the compile cache shaves a little off here too, not just off the `torch.compile` line. Either way, it's largely fixed engine overhead that the two optimizations don't directly target — effectively the floor for this approach. Getting below it would require a different class of optimization.

## Conclusion

With two simple and low-risk changes — without altering the model or its output — I reduced vLLM cold start for a 35B (NVFP4) model from about 5 minutes to under 3 minutes, a 1.9x speedup. Model streaming delivered the larger and more consistent gain (~30% on its own) and is the simplest to adopt, so it's the place to start. The persisted compile cache adds further savings, as long as you avoid the default-path trap and point `VLLM_CACHE_ROOT` at storage that survives restarts.

The broader takeaway is to **measure your own cold start by phase before optimizing**. These two changes worked for me because each targets a specific, measurable portion of startup. Your bottleneck may be weighted differently depending on your model, storage, and serving configuration.

### References

- [Deploy production generative AI at the edge using Amazon EKS Hybrid Nodes with NVIDIA DGX](https://aws.amazon.com/blogs/containers/deploy-production-generative-ai-at-the-edge-using-amazon-eks-hybrid-nodes-with-nvidia-dgx/)
- [SafetyLens demo project on GitHub](https://github.com/sc13912/safetylens_v2)
- [Run:ai Model Streamer](https://github.com/run-ai/runai-model-streamer)
- [vLLM documentation: Run:ai Model Streamer](https://docs.vllm.ai/en/latest/models/extensions/runai_model_streamer.html)
