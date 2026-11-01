from terminology import (
    normalize_text,
    normalize_diagnosis,
    normalize_procedure,
)


def test_normalize_text():
    assert normalize_text("  chest   X-ray  ") == "chest X-ray"


def test_normalize_diagnosis_alias():
    assert normalize_diagnosis(
        "community acquired pneumonia"
    ) == "community-acquired pneumonia"


def test_unknown_diagnosis_is_preserved():
    assert normalize_diagnosis("unknown clinical term") == "unknown clinical term"


def test_normalize_procedure_alias():
    assert normalize_procedure("CHEST XRAY") == "chest X-ray"


def test_unknown_procedure_is_preserved():
    assert normalize_procedure("unknown procedure") == "unknown procedure"


def test_candidate_diagnosis_case_is_canonicalized():
    assert normalize_diagnosis(
        "right eye mature cataract"
    ) == "RIGHT EYE Mature Cataract"


def test_candidate_procedure_case_is_canonicalized():
    assert normalize_procedure(
        "LEFT EYE PHACOEMULSIFICATION WITH PCIOL UNDER LOCAL ANAESTHESIA"
    ) == "LEFT EYE Phacoemulsification with PCIOL under Local Anaesthesia"


def test_obvious_ocr_variant_ol_is_normalized_to_iol():
    assert normalize_procedure(
        "RIGHT EYE Phaco with Foldable Hydrophobic OL"
    ) == "RIGHT EYE Phaco with Foldable Hydrophobic IOL"


def test_unknown_terms_are_not_rewritten():
    term = "left eye procedure not in inventory"
    assert normalize_procedure(term) == term

    
def test_pseudoextoliation_variant_is_normalized():
    assert normalize_diagnosis(
        "LEFT EYE Senile Cataract with Pseudoextoliation"
    ) == "LEFT EYE Senile Cataract with Pseudoexfoliation"