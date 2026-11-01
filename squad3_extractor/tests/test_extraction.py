from types import SimpleNamespace

import pytest

import extractor


class FakeModels:
    def __init__(self, response_text):
        self.response_text = response_text
        self.request = None

    def generate_content(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(text=self.response_text)


class FakeClient:
    def __init__(self, response_text):
        self.models = FakeModels(response_text)


def test_extract_normalizes_facts_and_preserves_source_text(monkeypatch):
    fake_client = FakeClient(
        '{"diagnoses":[{"name":"community acquired pneumonia",'
        '"source_text":"Diagnosis: community acquired pneumonia"}],'
        '"procedures":[{"name":"CHEST XRAY",'
        '"source_text":"Chest X-ray performed"}]}'
    )
    monkeypatch.setattr(extractor, "client", fake_client)

    result = extractor.extract_clinical_facts("Clinical document")

    assert result.diagnoses[0].name == "community-acquired pneumonia"
    assert result.procedures[0].name == "chest X-ray"
    assert result.diagnoses[0].source_text == (
        "Diagnosis: community acquired pneumonia"
    )
    assert fake_client.models.request["model"] == "gemini-3.5-flash"


def test_missing_api_key_is_reported_when_extraction_runs(monkeypatch):
    monkeypatch.setattr(extractor, "client", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="GEMINI_API_KEY is not set"):
        extractor.extract_clinical_facts("Clinical document")


def test_invalid_document_text_is_rejected(monkeypatch):
    monkeypatch.setattr(extractor, "client", None)

    with pytest.raises(ValueError, match="document text is empty"):
        extractor.extract_clinical_facts("  ")


def test_invalid_facts_are_rejected(monkeypatch):
    monkeypatch.setattr(
        extractor,
        "client",
        FakeClient(
            '{"diagnoses":[{"name":"pneumonia","source_text":"  "}]}'
        ),
    )

    with pytest.raises(
        ValueError, match="Diagnosis 1 has no supporting source text"
    ):
        extractor.extract_clinical_facts("Clinical document")
