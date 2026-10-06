"""A stand-in program for the local-command image backend (test fixture only).

This is **not** an image model and it does not pretend to be one.  It exists so
the command adapter can be exercised end to end - argument substitution, exit
codes, output verification, cancellation - without installing a diffusion model.

The image it writes is derived from the prompt and seed it was given, so a test
can prove that "the same prompt and seed produce the same file" through the real
adapter code path rather than through a mock.

Run it the way the backend would:

    python tests/fake_image_generator.py --prompt "a cat" --seed 42 \
        --width 512 --height 512 --out /tmp/out.png
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic test image writer (not an AI model).")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--negative-prompt", default="")
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--steps", type=int, default=0)
    parser.add_argument("--guidance", type=float, default=0.0)
    parser.add_argument("--sampler", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-image", default="")
    parser.add_argument("--mask-image", default="")
    parser.add_argument("--reference-image", default="")
    #: Lets a test check that cancellation really kills the child process.
    parser.add_argument("--sleep", type=float, default=0.0)
    #: Lets a test check that a non-zero exit is reported as a failure.
    parser.add_argument("--fail", action="store_true")
    #: Lets a test check that a missing output file is caught.
    parser.add_argument("--write-nothing", action="store_true")
    return parser.parse_args(argv)


def colour_for(prompt: str, seed: int, index: int) -> tuple[int, int, int]:
    """A stable colour from the prompt, the seed and the position."""
    digest = hashlib.sha256(f"{prompt}|{seed}|{index}".encode("utf-8")).digest()
    return digest[0], digest[1], digest[2]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.fail:
        print("fake backend: asked to fail", file=sys.stderr)
        return 3

    if args.sleep:
        # A real model takes time; sleeping lets a test cancel mid-generation and
        # prove the child is terminated rather than left running.
        time.sleep(args.sleep)

    if args.write_nothing:
        # Exit 0 without writing anything: the adapter must still refuse to
        # report success.
        return 0

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a core requirement
        print("Pillow is not available", file=sys.stderr)
        return 4

    width = max(1, int(args.width))
    height = max(1, int(args.height))
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    # 8-pixel blocks: cheap, and still visibly different per prompt and seed.
    block = 8
    for row in range(0, height, block):
        for column in range(0, width, block):
            colour = colour_for(args.prompt, args.seed, (row // block) * 1000
                                + (column // block))
            for offset_row in range(row, min(row + block, height)):
                for offset_column in range(column, min(column + block, width)):
                    pixels[offset_column, offset_row] = colour
    image.save(args.out, format="PNG")
    print(f"fake backend: wrote {args.out} ({width}x{height}, seed {args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
