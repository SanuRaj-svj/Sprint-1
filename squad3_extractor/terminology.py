"""Small, reviewable terminology normalization for extracted clinical terms.

This module performs string normalization only. It does not assign ICD, SNOMED CT,
LOINC, or other clinical terminology codes.
"""

import re


DIAGNOSIS_ALIASES = {
  
    "community acquired pneumonia": "community-acquired pneumonia",

 
    "right eye mature cataract": "RIGHT EYE Mature Cataract",
    "left eye senile cataract with pseudoexfoliation":
        "LEFT EYE Senile Cataract with Pseudoexfoliation",
    "left eye senile cataract with pseudoextoliation":
        "LEFT EYE Senile Cataract with Pseudoexfoliation",    
    "bilateral age-related cataract (right eye operated)":
        "Bilateral Age-related Cataract (Right Eye operated)",
    "right eye soft cataract with high myopia":
        "RIGHT EYE Soft Cataract with High Myopia",
    "left eye hypermature cataract": "LEFT EYE Hypermature Cataract",
}

PROCEDURE_ALIASES = {
    # Existing aliases
    "chest xray": "chest X-ray",
    "chest x-ray": "chest X-ray",
    "chest x ray": "chest X-ray",
    "complete blood count": "complete blood count",
    "cbc": "complete blood count (CBC) laboratory test",

    #input 20 docs data
    "right eye phacoemulsification with foldable iol + ta":
        "RIGHT EYE Phacoemulsification with Foldable IOL + TA",
    "left eye phacoemulsification with pciol under local anaesthesia":
        "LEFT EYE Phacoemulsification with PCIOL under Local Anaesthesia",
    "right eye phaco with foldable hydrophobic iol":
        "RIGHT EYE Phaco with Foldable Hydrophobic IOL",
    "right eye phaco with foldable hydrophobic ol":
        "RIGHT EYE Phaco with Foldable Hydrophobic IOL",
    "right eye phacoemulsification + foldable iol (clear corneal)":
        "RIGHT EYE Phacoemulsification + Foldable IOL (Clear Corneal)",
    "left eye phaco + anterior vitrectomy + foldable iol":
        "LEFT EYE Phaco + Anterior Vitrectomy + Foldable IOL",
}


def normalize_text(text: str) -> str:
    """Collapse repeated whitespace and trim leading/trailing whitespace."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_with_aliases(text: str, aliases: dict[str, str]) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return normalized
    return aliases.get(normalized.casefold(), normalized)


def normalize_diagnosis(text: str) -> str:
    """Normalize known diagnosis aliases; preserve unknown terms."""
    return _normalize_with_aliases(text, DIAGNOSIS_ALIASES)


def normalize_procedure(text: str) -> str:
    """Normalize known procedure aliases; preserve unknown terms."""
    return _normalize_with_aliases(text, PROCEDURE_ALIASES)
