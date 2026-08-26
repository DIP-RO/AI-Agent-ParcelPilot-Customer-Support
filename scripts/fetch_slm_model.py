"""Download the local SLM fallback model (GGUF) used by app/slm.py.

Not committed to git (see .gitignore: data/models/) -- a ~260MB binary has no
place in a source repo. Both local dev and the Dockerfile call this script
instead, so the model is fetched fresh from its public Hugging Face repo the
same way any other pip/npm dependency is.

Model: HuggingFaceTB/SmolLM2-135M-Instruct, Q8_0 GGUF quantization
(bartowski's build). Two things drove this choice over a larger model:

1. Its small (49k-token) vocabulary keeps the GGUF file small (~145MB even at
   Q8_0) -- comfortably inside a 512MB-RAM free-tier container alongside the
   rest of the app -- while still being instruction-tuned, unlike
   similarly-sized base models.
2. Measured directly on a Render-free-equivalent 0.1 vCPU / 512MB container
   (see docs/ARCHITECTURE.md "Local fallback mode"): this model generated at
   ~0.5-0.6 tokens/sec, vs. ~0.16 tok/s for SmolLM2-360M-Instruct -- more than
   3x slower for a model that still isn't reliable at anything harder than
   short phrasing. On a CPU this constrained, latency (and therefore actually
   returning an answer instead of timing out) matters more than the modest
   quality gain from a bigger model, so 135M is the deliberate choice. Q8_0
   over the even-smaller Q4_K_M because it measured no faster here but
   produced noticeably more coherent, better-grounded phrasing in practice.

License: Apache 2.0.

Run from the repo root: .venv/bin/python scripts/fetch_slm_model.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402

# Single source of truth for the model URL/path is app/config.py -- app/slm.py
# downloads the same file lazily at runtime (e.g. into Vercel's /tmp) if it's
# ever missing, so both paths always agree on exactly what model is running.


def main() -> None:
    dest = config.SLM_MODEL_PATH
    if dest.exists() and "--force" not in sys.argv:
        print(f"{dest} already exists ({dest.stat().st_size / 1e6:.1f} MB) -- skipping. Use --force to re-download.")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    print(f"Downloading {config.SLM_MODEL_URL} ...")

    def _progress(count: int, block_size: int, total_size: int) -> None:
        done = count * block_size
        pct = min(100, done * 100 // total_size) if total_size > 0 else 0
        print(f"\r  {done / 1e6:.0f}MB / {total_size / 1e6:.0f}MB ({pct}%)", end="", flush=True)

    try:
        urllib.request.urlretrieve(config.SLM_MODEL_URL, tmp, reporthook=_progress)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    print()
    tmp.rename(dest)
    print(f"Saved to {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
