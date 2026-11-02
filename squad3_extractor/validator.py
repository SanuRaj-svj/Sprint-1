from schemas import ClinicalExtraction


def validate_extraction(facts: ClinicalExtraction) -> list[str]:
    """
    Validate basic consistency of extracted clinical facts.

    Returns an empty list if no errors are found.
    Otherwise, returns a list of validation errors.
    """

    if not isinstance(facts, ClinicalExtraction):
        raise TypeError(
            "facts must be a ClinicalExtraction object."
        )

    errors = []

    
    for index, diagnosis in enumerate(facts.diagnoses):
        if not diagnosis.name or not diagnosis.name.strip():
            errors.append(
                f"Diagnosis {index + 1} has an empty name."
            )

        if not diagnosis.source_text or not diagnosis.source_text.strip():
            errors.append(
                f"Diagnosis {index + 1} has no supporting source text."
            )

    
    for index, procedure in enumerate(facts.procedures):
        if not procedure.name or not procedure.name.strip():
            errors.append(
                f"Procedure {index + 1} has an empty name."
            )

        if not procedure.source_text or not procedure.source_text.strip():
            errors.append(
                f"Procedure {index + 1} has no supporting source text."
            )

   
    if (
        facts.admission_date is not None
        and facts.discharge_date is not None
        and facts.discharge_date < facts.admission_date
    ):
        errors.append(
            "Discharge date cannot be earlier than admission date."
        )

    return errors
