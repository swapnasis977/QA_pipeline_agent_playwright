"""Suite-wide test setup.

The model preflight (utils.llm_client.preflight) makes a real request before the
first model-powered stage. Tests build pipelines out of stages with real stage
names but fake functions, so it must not fire for them. Tests that cover the
preflight itself remove this variable explicitly.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_model_preflight(monkeypatch):
    monkeypatch.setenv("QA_SKIP_MODEL_PREFLIGHT", "1")
