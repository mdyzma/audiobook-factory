"""Give CTranslate2 the cuBLAS it was built against, on Windows.

WhisperX transcribes through faster-whisper, which runs on CTranslate2, and
CTranslate2 does not use torch's CUDA: it loads cuBLAS and cuDNN itself. The
4.8.x Windows wheel is built against CUDA 12 and asks for `cublas64_12.dll`,
while the torch this environment pins ships CUDA 13 on Windows. A working
torch is therefore no evidence that transcription will reach the GPU; it fails
at the first encode with "Library cublas64_12.dll is not found".

`nvidia-cublas-cu12` supplies that DLL on Windows. CTranslate2 resolves it by
bare name, which neither PATH nor `os.add_dll_directory` reaches, so it has to
be loaded into the process first. cuDNN needs nothing: torch has already
loaded `cudnn64_9.dll`, and CTranslate2 uses that copy.

Call before `import whisperx`. Elsewhere, or with the package absent, it does
nothing and transcription stays on whatever device it would have used.
"""

from __future__ import annotations

import ctypes
import importlib.metadata as metadata
import sys

DISTRIBUTION = "nvidia-cublas-cu12"
LIBRARIES = ("cublasLt64_12.dll", "cublas64_12.dll")


def preload_ctranslate2_cuda() -> list[str]:
    """Load CUDA 12 cuBLAS into this process. Returns the paths loaded."""
    if sys.platform != "win32":
        return []
    try:
        dist = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return []

    by_name = {f.name: f for f in dist.files or ()}
    loaded = []
    # cublasLt first: cublas64_12 links against it.
    for name in LIBRARIES:
        if name in by_name:
            path = str(dist.locate_file(by_name[name]))
            ctypes.CDLL(path)
            loaded.append(path)
    return loaded
