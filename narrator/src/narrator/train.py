"""Optional - full XTTS-v2 fine-tune on a labelled dataset.

Instant cloning (clone.py) is enough for most books. Fine-tuning buys the
speaker's own pauses, breaths and language-specific intonation, at the cost
of a GPU and roughly an hour on a 32 GB card. It is a no-go on Apple Silicon:
the trainer needs CUDA.

Expects data/datasets/<voice>/{wavs/,metadata.csv} from the auto-labeller.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    voice: str = typer.Argument(...),
    epochs: int = typer.Option(10),
    batch_size: int = typer.Option(8, help="32-64 fits comfortably in 32 GB VRAM"),
    grad_accum: int = typer.Option(4),
    lr: float = typer.Option(5e-6),
) -> None:
    import torch

    if not torch.cuda.is_available():
        typer.echo(
            "fine-tuning needs CUDA. On Apple Silicon use instant cloning:\n"
            f"  just clone {voice}",
            err=True,
        )
        raise typer.Exit(code=1)

    root = Path(__file__).resolve().parents[3]
    dataset = root / "data" / "datasets" / voice
    if not (dataset / "metadata.csv").exists():
        raise typer.BadParameter(f"missing {dataset / 'metadata.csv'}; run `just label {voice}` first")

    out_dir = root / "training" / voice
    out_dir.mkdir(parents=True, exist_ok=True)

    # Wiring the Coqui trainer is deliberately left as the one manual step:
    # the recipe depends on which XTTS checkpoint you start from, and the
    # upstream GPTTrainer API moved between 0.22 releases.
    #
    #   from trainer import Trainer, TrainerArgs
    #   from TTS.tts.layers.xtts.trainer.gpt_trainer import GPTTrainer, GPTTrainerConfig
    #   ... see TTS/recipes/ljspeech/xtts_v2/train_gpt_xtts.py
    #
    # Set mixed_precision=True and batch_size above to use the card properly.
    typer.echo(
        f"dataset: {dataset}\noutput:  {out_dir}\n"
        f"epochs={epochs} batch_size={batch_size} grad_accum={grad_accum} lr={lr}\n\n"
        "Trainer wiring is not filled in - see the comment in this file and adapt\n"
        "TTS/recipes/ljspeech/xtts_v2/train_gpt_xtts.py to this dataset layout.\n"
        "Then point data/voices/<voice>.json at the checkpoint:\n"
        '  {"mode": "finetuned", "model_dir": "training/<voice>/best_model"}'
    )
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
