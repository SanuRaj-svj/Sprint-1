from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class ClinicalFact(BaseModel):
    name: str = Field(
        description="Diagnosis or procedure name as documented"
    )

    event_date: Optional[date] = Field(
        default=None,
        description="Explicitly documented date of this fact"
    )

    source_text: str = Field(
        description="Exact supporting text from the document"
    )


class ClinicalExtraction(BaseModel):
    diagnoses: list[ClinicalFact] = Field(default_factory=list)

    procedures: list[ClinicalFact] = Field(default_factory=list)

    admission_date: Optional[date] = None

    discharge_date: Optional[date] = None