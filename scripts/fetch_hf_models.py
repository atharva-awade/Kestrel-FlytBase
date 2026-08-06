"""Fetch HuggingFace models onto a network where the Hub is filtered.

`huggingface.co` is SNI-filtered on some networks: TCP connects, then the
connection is reset the instant TLS reveals the hostname. Changing DNS does not
help because the name already resolves.

Two things make it work anyway:

1.  **A mirror endpoint.** `hf-mirror.com` is not filtered, but it is a single
    server and connections to it fail intermittently — measured here as roughly
    one success in three attempts.
2.  **Stubborn retries.** Because the failure is transient rather than a hard
    block, retrying with backoff gets through. That is the whole trick.

Once a model is in the local cache it never needs the network again, so this is a
one-time cost. The cache is portable: run this anywhere with working internet and
copy `~/.cache/huggingface` across.

    uv run python scripts/fetch_hf_models.py
    uv run python scripts/fetch_hf_models.py --endpoint https://hf-mirror.com
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Must be set before huggingface_hub is imported — it reads these at import time.
DEFAULT_ENDPOINT = "https://hf-mirror.com"

MODELS = [
    {
        "repo": "IDEA-Research/grounding-dino-tiny",
        "why": "Open-vocabulary detection — lets a rule carry its own detector "
               "prompt, locally, instead of routing through the VLM.",
        "required": True,
        "allow": ["*.json", "*.txt", "*.safetensors", "*.model"],
    },
    {
        "repo": "PekingU/rtdetr_r50vd_coco_o365",
        "why": "Closed-set detector fallback (Apache-2.0). Optional — YOLO11 "
               "already covers this path.",
        "required": False,
        "allow": ["*.json", "*.safetensors"],
    },
]


def fetch(repo: str, allow: list[str], attempts: int, delay: float) -> tuple[bool, str]:
    """Download one repo, retrying hard through an intermittent connection."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import LocalEntryNotFoundError

    last = ""
    for i in range(1, attempts + 1):
        try:
            path = snapshot_download(
                repo,
                allow_patterns=allow,
                # One worker: parallel connections to a flaky single-host mirror
                # multiply the chance that at least one fails and aborts the whole
                # download. Serial is slower and far more likely to finish.
                max_workers=1,
                etag_timeout=30,
            )
            return True, path
        except (LocalEntryNotFoundError, OSError, Exception) as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
            # Partial progress is kept in the cache, so each attempt resumes rather
            # than restarting — which is why persistence works here.
            print(f"    attempt {i:>2}/{attempts}  {last}")
            if i < attempts:
                time.sleep(delay)
    return False, last


def already_cached(repo: str) -> Path | None:
    try:
        from huggingface_hub import snapshot_download

        return Path(snapshot_download(repo, local_files_only=True))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help="Hub mirror. Use https://huggingface.co on an unfiltered network.")
    ap.add_argument("--attempts", type=int, default=25,
                    help="Retries per model. The mirror succeeds ~1 in 3, so be generous.")
    ap.add_argument("--delay", type=float, default=2.5)
    ap.add_argument("--all", action="store_true", help="include optional models")
    args = ap.parse_args()

    os.environ["HF_ENDPOINT"] = args.endpoint
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ["HF_HUB_DISABLE_XET"] = "1"          # xet storage adds another host
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"   # the fast path is less resilient
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "60"

    print("\nFetching HuggingFace models")
    print("=" * 84)
    print(f"  endpoint : {args.endpoint}")
    print(f"  retries  : {args.attempts} per model, {args.delay}s apart")
    print(f"  cache    : {Path.home() / '.cache' / 'huggingface'}")
    print("=" * 84)

    wanted = MODELS if args.all else [m for m in MODELS if m["required"]]
    ok, failed = [], []

    for m in wanted:
        print(f"\n{m['repo']}")
        print(f"  {m['why']}")

        cached = already_cached(m["repo"])
        if cached:
            print(f"  already cached -> {cached}")
            ok.append(m["repo"])
            continue

        print("  downloading (a flaky mirror means several attempts is normal)…")
        success, detail = fetch(m["repo"], m["allow"], args.attempts, args.delay)
        if success:
            print(f"  OK -> {detail}")
            ok.append(m["repo"])
        else:
            print(f"  FAILED after {args.attempts} attempts: {detail}")
            failed.append(m["repo"])

    print("\n" + "=" * 84)
    if ok:
        print(f"  {len(ok)} model(s) available locally.")
        print("\n  Enable them by removing HF_HUB_OFFLINE from .env, or set:")
        print("    HF_HUB_OFFLINE=0")
        print(f"    HF_ENDPOINT={args.endpoint}")
        print("\n  Then confirm with:  uv run kestrel doctor")
        print("  The detector line should read 'grounding-dino' with open_vocabulary=True.")
    if failed:
        print(f"\n  {len(failed)} model(s) could not be fetched: {', '.join(failed)}")
        print("\n  The connection to the mirror is intermittent rather than blocked, so")
        print("  simply running this again often succeeds. If it keeps failing:")
        print("    · tether to a mobile hotspot — a different ISP is usually unfiltered")
        print("    · use any VPN — it encrypts the SNI the filter keys on")
        print("    · run this on another machine and copy ~/.cache/huggingface across")
        print("\n  KESTREL runs fine without these. Detection uses YOLO11 (GitHub-hosted),")
        print("  and open-vocabulary queries route through the VLM instead.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
