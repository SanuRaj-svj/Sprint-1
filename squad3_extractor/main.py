import json

from pdf_reader import extract_pdf_text
from extractor import extract_clinical_facts


def process_document(pdf_path: str) -> dict:

    # Step 1: Read PDF
    document_text = extract_pdf_text(pdf_path)

    # Step 2: Extract clinical facts
    result = extract_clinical_facts(document_text)

    # Step 3: Convert validated output to JSON-compatible data
    output = result.model_dump(mode="json")

    return output


if __name__ == "__main__":
    pdf_path = "sample_docs/discharge_summary.pdf"

    try:
        result = process_document(pdf_path)

        print(json.dumps(result, indent=2))

        with open(
            "extraction_output.json",
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(result, file, indent=2)

        print("Extraction completed.")

    except Exception as error:
        print(f"Extraction failed: {error}")