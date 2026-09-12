# Changelog

Versions exist so a twenty-hour render can be pinned to one. A tag is only
applied to a commit where `just check` and CI are both green, and where the
pipeline has produced a real audiobook rather than only passing its tests.

## v0.2.0 — 2026-09-12

Groundwork for choosing between synthesis engines. No audio in this release
sounds different from the last one.

### A second backend

Chatterbox Multilingual, in a fourth environment because it contradicts the
narrator on transformers, torch and pandas. It reads 23 languages including
both of this project's, so one environment serves both comparisons.

It reuses stage 4 rather than reimplementing it: resume, fingerprinting,
retries and levelling are engine-agnostic, and Coqui moved into a dependency
group so the narrator package can be installed without it. Nothing about the
XTTS environment changed; its lock resolves identically.

**Chatterbox has never generated a sample.** The adapter, the registry entry
and the wiring are verified as far as they can be without model weights.

### A benchmark that refuses to average

`just bench <language> <voice>` runs a checked-in corpus through every model
that claims to read that language, each in the environment that can load it.
The same text goes to every engine verbatim and unchunked.

Eleven passages per language across eight categories: narration, dialogue,
numbers, abbreviations, proper names, short headings, long sentences and
chapter transitions. Results are reported per category and never combined,
because a model that narrates well and cannot say a number is not a model that
reads well. Nothing it prints is a verdict.

### Settings pinned per engine

One settings block could not serve two engines whose controls have different
names, so three of Chatterbox's were never set at all. Each engine is now
pinned in the registry, in its own names, and a pin naming a control the engine
does not implement is refused when the registry loads.

These are baselines, not tuned values. XTTS is pinned at what the project has
been rendering with all along, checked to resolve identically so no finished
book is invalidated.

### Still open

Connected-narration and full-chapter soaks, the tuned settings, and the two
result sheets. All three need the CUDA machine and someone listening.

## v0.1.0 — 2026-09-12

The first tagged version. Everything the workplan sequenced is in it.

Pre-1.0 deliberately. The plan's own bar for completion is real narration
evidence in both Polish and English and at least one working alternative
model path, and neither exists yet. What is here is one backend, XTTS-v2,
proven on Apple Silicon.

### Reading a book

Encoding is established rather than guessed, and a file whose encoding cannot
be settled stops for a person instead of losing a letter to a replacement
character. Language is detected with calibrated confidence and abstains rather
than defaulting. Three representations are kept with anchors between them:
source bytes, extracted text, spoken text. A per-book pronunciation dictionary
corrects what the rules cannot know, and a proposed entry can be checked
against the whole book for free before anything is re-split.

### Voices

Cloned from MP3 or WAV, judged before the model is downloaded rather than
after. Each voice is measured from its own audition and levelled, so a cast
does not change volume mid-sentence. A voice can be auditioned on the prose of
a book you actually have, at the settings that book will be narrated with, and
every sample records what produced it.

### Rendering

Resumable, fingerprinted per fragment, so a run killed at hour six resumes
rather than restarts, and changing a voice or a setting invalidates exactly the
audio it affected. One workload at a time on the graphics card, counting voice
cloning and transcription. Disk space is checked before starting rather than
discovered at hour nine.

### Batches

A folder of independent books is scanned, deduplicated by content, reviewed and
queued. The queue is durable and survives the machine restarting. A book that
fails stops itself and nothing else.

### The library

Books, prepared text, chunk plans and every narration made from them are
catalogued, so which text, model and voice produced a given audiobook is a
question with an answer. Runs and books can be removed again, and the bytes
they held reclaimed.

### Exports

m4b, mp3 or wav, with chapter marks, title, author, narrator and the book's own
cover.

### Known limits

- One synthesis backend. No comparison between models has been made.
- Fine-tuning has never run; it needs CUDA.
- The dashboard has no authentication and is intended for localhost only.
- PDF, MOBI, OCR and splitting one file into several books are out of scope.
