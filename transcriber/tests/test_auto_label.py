"""Device selection for WhisperX.

float16 needs CUDA; anything else must fall back to int8 or the run crashes
inside CTranslate2 with an unhelpful error.
"""

from __future__ import annotations

import pytest

from transcriber.auto_label import pick_device


class TestPickDevice:
    def test_cuda_uses_float16(self):
        assert pick_device("cuda") == ("cuda", "float16")

    @pytest.mark.parametrize("requested", ["cpu", "mps"])
    def test_non_cuda_falls_back_to_int8(self, requested):
        assert pick_device(requested)[1] == "int8"

    def test_auto_prefers_cuda(self, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        assert pick_device("auto") == ("cuda", "float16")

    def test_auto_without_cuda_is_cpu_not_mps(self, monkeypatch):
        # CTranslate2 has no MPS backend, so Apple Silicon must land on CPU.
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        assert pick_device("auto") == ("cpu", "int8")
