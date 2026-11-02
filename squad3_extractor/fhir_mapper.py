# fhir_mapper.py
# Converts validated clinical extraction data into basic FHIR resources.


def create_condition(diagnosis):
    """Create a FHIR Condition resource from a diagnosis."""

    if not diagnosis:
        return None

    # Support both a diagnosis string and a dictionary.
    if isinstance(diagnosis, dict):
        diagnosis_name = diagnosis.get("name")
        source_text = diagnosis.get("source_text")
        event_date = diagnosis.get("event_date")
    else:
        diagnosis_name = diagnosis
        source_text = None
        event_date = None

    if not diagnosis_name or not diagnosis_name.strip():
        return None

    condition = {
        "resourceType": "Condition",
        "code": {
            "text": diagnosis_name
        }
    }

    if event_date:
        condition["onsetDateTime"] = event_date

    return condition


def create_procedure(procedure_name, procedure_date=None):
    """Create a FHIR Procedure resource."""

    if not procedure_name:
        return None

    # Support both a procedure string and a dictionary.
    if isinstance(procedure_name, dict):
        procedure_date = procedure_name.get(
            "event_date", procedure_date
        )
        procedure_name = procedure_name.get("name")

    if not procedure_name or not procedure_name.strip():
        return None

    procedure = {
        "resourceType": "Procedure",
        "status": "completed",
        "code": {
            "text": procedure_name
        }
    }

    if procedure_date:
        procedure["performedDateTime"] = procedure_date

    return procedure


def create_encounter(admission_date=None, discharge_date=None):
    """Create a FHIR Encounter resource."""

    encounter = {
        "resourceType": "Encounter",
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "IMP",
            "display": "inpatient encounter"
        }
    }

    period = {}

    if admission_date:
        period["start"] = admission_date

    if discharge_date:
        period["end"] = discharge_date

    if period:
        encounter["period"] = period

    return encounter


def map_to_fhir(facts):
    """
    Convert extracted clinical facts into a FHIR Bundle.

    Expected input:
        A dictionary produced by ClinicalExtraction.model_dump(
            mode="json"
        ).

    Supports multiple diagnoses and procedures.
    """

    if not isinstance(facts, dict):
        raise TypeError("facts must be a dictionary")

    fhir_bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": []
    }

    # Handle multiple diagnoses.
    diagnoses = facts.get("diagnoses", [])

    # Backward compatibility with the old single-diagnosis format.
    if not diagnoses and facts.get("diagnosis"):
        diagnoses = [facts["diagnosis"]]

    for diagnosis in diagnoses:
        condition = create_condition(diagnosis)

        if condition:
            fhir_bundle["entry"].append({
                "resource": condition
            })

    # Handle multiple procedures.
    procedures = facts.get("procedures", [])

    # Backward compatibility with the old single-procedure format.
    if not procedures and facts.get("procedure"):
        procedures = [{
            "name": facts["procedure"],
            "event_date": facts.get("procedure_date")
        }]

    for procedure in procedures:
        procedure_resource = create_procedure(procedure)

        if procedure_resource:
            fhir_bundle["entry"].append({
                "resource": procedure_resource
            })

    # Create Encounter when at least one encounter date exists.
    admission_date = facts.get("admission_date")
    discharge_date = facts.get("discharge_date")

    if admission_date or discharge_date:
        encounter = create_encounter(
            admission_date,
            discharge_date
        )

        fhir_bundle["entry"].append({
            "resource": encounter
        })

    return fhir_bundle