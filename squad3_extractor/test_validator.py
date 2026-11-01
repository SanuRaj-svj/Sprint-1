from datetime import date

import pytest

from schemas import ClinicalExtraction, ClinicalFact
from validator import validate_extraction


def test_valid_extraction_has_no_errors():
    facts = ClinicalExtraction(
        diagnoses=[
            ClinicalFact(
                name="pneumonia",
                event_date=date(2026, 10, 20),
                source_text="Diagnosis: pneumonia",
            )
        ],
        procedures=[
            ClinicalFact(
                name="chest X-ray",
                event_date=date(2026, 10, 21),
                source_text="Chest X-ray performed",
            )
        ],
        admission_date=date(2026, 10, 20),
        discharge_date=date(2026, 10, 23),
    )

    assert validate_extraction(facts) == []


def test_discharge_before_admission_is_reported():
    facts = ClinicalExtraction(
        admission_date=date(2026, 10, 23),
        discharge_date=date(2026, 10, 20),
    )

    errors = validate_extraction(facts)

    assert any(
        "Discharge date cannot be earlier than admission date."
        in error
        for error in errors
    )


def test_empty_diagnosis_name_is_reported():
    facts = ClinicalExtraction(
        diagnoses=[
            ClinicalFact(
                name="   ",
                source_text="Diagnosis documented",
            )
        ]
    )

    errors = validate_extraction(facts)

    assert any("empty name" in error for error in errors)


def test_missing_procedure_source_text_is_reported():
    facts = ClinicalExtraction(
        procedures=[
            ClinicalFact(
                name="chest X-ray",
                source_text="   ",
            )
        ]
    )

    errors = validate_extraction(facts)

    assert any(
        "no supporting source text" in error
        for error in errors
    )


def test_empty_procedure_name_is_reported():
    facts = ClinicalExtraction(
        procedures=[
            ClinicalFact(
                name="   ",
                source_text="Procedure documented",
            )
        ]
    )

    errors = validate_extraction(facts)

    assert any("empty name" in error for error in errors)


def test_missing_diagnosis_source_text_is_reported():
    facts = ClinicalExtraction(
        diagnoses=[
            ClinicalFact(
                name="pneumonia",
                source_text="   ",
            )
        ]
    )

    errors = validate_extraction(facts)

    assert any(
        "no supporting source text" in error
        for error in errors
    )


def test_non_clinical_extraction_input_raises_type_error():
    with pytest.raises(TypeError, match="ClinicalExtraction object"):
        validate_extraction({"diagnoses": []})
