"""The Windows cuBLAS preload for CTranslate2.

Nothing here loads a real DLL: the loader is replaced, so these run anywhere
and prove only which files would be loaded, in which order, and when not.
"""

from __future__ import annotations

import importlib.metadata as metadata
from pathlib import PurePosixPath

from transcriber import cuda_libs


class FakeDist:
    files = [
        PurePosixPath("nvidia/cublas/bin/cublas64_12.dll"),
        PurePosixPath("nvidia/cublas/bin/nvblas64_12.dll"),
        PurePosixPath("nvidia/cublas/bin/cublasLt64_12.dll"),
    ]

    def locate_file(self, f):
        return f"/site/{f}"


def _record_loads(monkeypatch):
    loaded: list[str] = []
    monkeypatch.setattr(cuda_libs.ctypes, "CDLL", loaded.append)
    return loaded


class TestPreload:
    def test_does_nothing_off_windows(self, monkeypatch):
        loaded = _record_loads(monkeypatch)
        monkeypatch.setattr(cuda_libs.sys, "platform", "linux")
        monkeypatch.setattr(metadata, "distribution", lambda name: FakeDist())
        assert cuda_libs.preload_ctranslate2_cuda() == []
        assert loaded == []

    def test_does_nothing_without_the_package(self, monkeypatch):
        loaded = _record_loads(monkeypatch)
        monkeypatch.setattr(cuda_libs.sys, "platform", "win32")

        def missing(name):
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setattr(metadata, "distribution", missing)
        assert cuda_libs.preload_ctranslate2_cuda() == []
        assert loaded == []

    def test_loads_cublaslt_before_cublas_and_skips_nvblas(self, monkeypatch):
        loaded = _record_loads(monkeypatch)
        monkeypatch.setattr(cuda_libs.sys, "platform", "win32")
        monkeypatch.setattr(metadata, "distribution", lambda name: FakeDist())
        expected = [
            "/site/nvidia/cublas/bin/cublasLt64_12.dll",
            "/site/nvidia/cublas/bin/cublas64_12.dll",
        ]
        assert cuda_libs.preload_ctranslate2_cuda() == expected
        assert loaded == expected
