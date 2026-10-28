
import pymupdf


def extract_pdf_text(pdf_path: str) -> str:
    text_parts = []

    with pymupdf.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf, start=1):
            page_text = page.get_text("text").strip()

            if page_text:
                text_parts.append(
                    f"\n--- Page {page_number} ---\n{page_text}"
                )

    extracted_text = "\n".join(text_parts).strip()

    if not extracted_text:
        raise ValueError(
            "No selectable text found. The PDF may need OCR."
        )

    return extracted_text
