"""Optional - full XTTS-v2 fine-tune on a labelled dataset.

Instant cloning (clone.py) is enough for most books: it derives a speaker
embedding from a dozen reference clips in seconds. Fine-tuning buys the
speaker's own pauses, breaths and language-specific intonation, at the cost of
a GPU and roughly an hour on a 32 GB card.

Input is whatever `just label <voice>` produced:

    data/datasets/<voice>/
    ├── wavs/seg_0000.wav ...
    └── metadata.csv          seg_0000.wav|transcription

Output goes to training/<voice>/, and the run finishes by pointing the voice
profile at the new checkpoint so `just synth` picks it up with no further
steps.

Fine-tuning needs CUDA. The Coqui trainer has no MPS path, and on CPU a run
that takes an hour on a 5090 takes days.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

# Base checkpoint the fine-tune starts from. These are the XTTS v2.0.2 release
# files; the DVAE and mel-norm pair must match the checkpoint or the audio
# tokens come out meaningless.
XTTS_BASE = "https://coqui.gateway.scarf.sh/hf-coqui/XTTS-v2/main/"
DVAE_URL = XTTS_BASE + "dvae.pth"
MEL_NORM_URL = XTTS_BASE + "mel_stats.pth"
TOKENIZER_URL = XTTS_BASE + "vocab.json"
CHECKPOINT_URL = XTTS_BASE + "model.pth"
CONFIG_URL = XTTS_BASE + "config.json"

AUDIO_TIMEOUT = 60


def download_base_files(target: Path) -> dict[str, Path]:
    """Fetch the base XTTS files once and cache them under training/base/."""
    from TTS.utils.manage import ModelManager

    target.mkdir(parents=True, exist_ok=True)
    wanted = {
        "dvae": DVAE_URL,
        "mel_norm": MEL_NORM_URL,
        "tokenizer": TOKENIZER_URL,
        "checkpoint": CHECKPOINT_URL,
        "config": CONFIG_URL,
    }
    missing = [url for url in wanted.values() if not (target / Path(url).name).exists()]
    if missing:
        typer.echo(f"downloading {len(missing)} base file(s) to {target}")
        ModelManager._download_model_files(missing, str(target), progress_bar=True)
    return {key: target / Path(url).name for key, url in wanted.items()}


def make_formatter(language: str):
    """Read the auto-labeller's `filename|text` metadata.csv.

    Coqui's built-in ljspeech formatter expects a three-column file and its own
    naming, so this stays a local formatter rather than reshaping the dataset
    that clone.py also reads.
    """

    def formatter(root_path, meta_file, **_kwargs):
        root = Path(root_path)
        items = []
        for line in (root / meta_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            filename, text = line.split("|", 1)
            wav = root / "wavs" / filename
            if not wav.exists():
                continue
            items.append({
                "text": text.strip(),
                "audio_file": str(wav),
                "speaker_name": root.name,
                "language": language,
                "root_path": str(root),
                "audio_unique_name": f"{root.name}#{filename}",
            })
        return items

    return formatter


@app.command()
def main(
    voice: str = typer.Argument(..., help="Voice name; dataset at data/datasets/<voice>"),
    language: str = typer.Option("pl", help="Language code of the recordings"),
    epochs: int = typer.Option(10),
    batch_size: int = typer.Option(3, help="8-16 fits a 32 GB card at this sequence length"),
    grad_accum: int = typer.Option(84, help="batch_size * grad_accum ~= 252 is a good target"),
    lr: float = typer.Option(5e-6),
    max_audio_sec: float = typer.Option(11.6, help="Longer clips cost VRAM quadratically"),
    eval_split: float = typer.Option(0.02),
    device: str = typer.Option("auto", help="auto | cuda | cpu; cpu is impractically slow"),
) -> None:
    import torch

    root = Path(__file__).resolve().parents[3]
    dataset_dir = root / "data" / "datasets" / voice
    metadata = dataset_dir / "metadata.csv"
    if not metadata.exists():
        raise typer.BadParameter(
            f"missing {metadata}; run `just label {voice}` first"
        )

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device != "cuda":
        typer.echo(
            "fine-tuning needs CUDA. The Coqui trainer has no MPS backend, and on\n"
            "CPU this takes days rather than an hour.\n\n"
            f"On Apple Silicon use instant cloning instead:  just clone {voice}\n"
            "Or run this on the CUDA box after copying data/datasets/ across.",
            err=True,
        )
        raise typer.Exit(code=1)

    from trainer import Trainer, TrainerArgs
    from TTS.config.shared_configs import BaseDatasetConfig
    from TTS.tts.datasets import load_tts_samples
    from TTS.tts.layers.xtts.trainer.gpt_trainer import (
        GPTArgs,
        GPTTrainer,
        GPTTrainerConfig,
        XttsAudioConfig,
    )

    out_dir = root / "training" / voice
    out_dir.mkdir(parents=True, exist_ok=True)
    base = download_base_files(root / "training" / "base")

    dataset_config = BaseDatasetConfig(
        formatter="local",
        dataset_name=voice,
        path=str(dataset_dir),
        meta_file_train="metadata.csv",
        language=language,
    )

    # Coqui annotates `datasets` as a list of dicts, but every upstream recipe
    # passes BaseDatasetConfig and the function reads it as one. Upstream
    # annotation bug, not a call-site error.
    train_samples, eval_samples = load_tts_samples(
        [dataset_config],  # type: ignore[arg-type]
        eval_split=True,
        eval_split_size=eval_split,
        formatter=make_formatter(language),
    )
    if not train_samples:
        typer.echo(f"no usable samples in {metadata}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"{len(train_samples)} train / {len(eval_samples)} eval samples "
        f"from {dataset_dir.name}"
    )

    model_args = GPTArgs(
        # 11.6 s at 22.05 kHz. VRAM scales with the square of this, so it is the
        # first thing to lower if the run runs out of memory.
        max_wav_length=int(max_audio_sec * 22050),
        max_text_length=200,
        mel_norm_file=str(base["mel_norm"]),
        dvae_checkpoint=str(base["dvae"]),
        xtts_checkpoint=str(base["checkpoint"]),
        tokenizer_file=str(base["tokenizer"]),
        gpt_num_audio_tokens=1026,
        gpt_start_audio_token=1024,
        gpt_stop_audio_token=1025,
        gpt_use_masking_gt_prompt_approach=True,
        gpt_use_perceiver_resampler=True,
    )

    config = GPTTrainerConfig(
        output_path=str(out_dir),
        model_args=model_args,
        audio=XttsAudioConfig(sample_rate=22050, dvae_sample_rate=22050,
                              output_sample_rate=24000),
        run_name=f"xtts-{voice}",
        project_name="audiobook-factory",
        run_description=f"XTTS-v2 fine-tune on {len(train_samples)} clips of '{voice}'",
        epochs=epochs,
        batch_size=batch_size,
        batch_group_size=48,
        eval_batch_size=batch_size,
        num_loader_workers=8,
        eval_split_max_size=256,
        print_step=50,
        plot_step=100,
        save_step=1000,
        save_n_checkpoints=2,
        save_checkpoints=True,
        print_eval=False,
        optimizer="AdamW",
        optimizer_wd_only_on_weights=True,
        optimizer_params={"betas": [0.9, 0.96], "eps": 1e-8, "weight_decay": 1e-2},
        lr=lr,
        lr_scheduler="MultiStepLR",
        lr_scheduler_params={"milestones": [900000, 2700000, 5400000],
                             "gamma": 0.5, "last_epoch": -1},
        test_sentences=[],
    )

    model = GPTTrainer.init_from_config(config)

    trainer = Trainer(
        TrainerArgs(
            # No restore_path: base weights arrive through GPTArgs.xtts_checkpoint,
            # and the field defaults to "" rather than None.
            skip_train_epoch=False,
            start_with_eval=False,
            grad_accum_steps=grad_accum,
        ),
        config,
        output_path=str(out_dir),
        model=model,
        train_samples=train_samples,
        eval_samples=eval_samples,
    )

    typer.echo(
        f"effective batch {batch_size * grad_accum} "
        f"({batch_size} x {grad_accum} accumulation), lr {lr}, {epochs} epochs"
    )
    trainer.fit()

    # Point the voice profile at the result so `just synth` uses it directly.
    run_dir = Path(trainer.output_path)
    profile_path = root / "data" / "voices" / f"{voice}.json"
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["mode"] = "finetuned"
        profile["model_dir"] = str(run_dir.relative_to(root))
        profile_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    typer.echo(
        f"\ncheckpoints -> {run_dir}\n"
        f"profile updated: data/voices/{voice}.json now points at the fine-tune\n\n"
        f"The run directory needs config.json, model.pth and vocab.json side by side\n"
        f"for narrator.engine to load it. Copy the tokenizer in if it is absent:\n"
        f"  cp {base['tokenizer']} {run_dir}/vocab.json\n\n"
        f"Then compare against the base model:  just clone {voice} && just preview <book> {voice}"
    )


if __name__ == "__main__":
    app()
