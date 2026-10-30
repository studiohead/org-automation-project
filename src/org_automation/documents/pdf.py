"""PLAIN ENGLISH: The PDF page-reader.

Turns an uploaded PDF into a list of page texts (so we can later say "this value is on page 3").
It only reads PDFs that already contain real text. Scanned images need OCR (a separate tool) —
we say so clearly instead of silently returning nothing.
"""
import io

from pypdf import PdfReader

MAX_BYTES = 10 * 1024 * 1024  # refuse giant uploads
MAX_PAGES = 100


class PdfError(ValueError):
    pass


def pdf_to_pages(data: bytes) -> list[str]:
    if len(data) > MAX_BYTES:
        raise PdfError("PDF is too large.")
    if not data.startswith(b"%PDF"):
        raise PdfError("File does not look like a PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PAGES:
            raise PdfError("PDF has too many pages.")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except PdfError:
        raise
    except Exception as exc:  # corrupt or encrypted files
        raise PdfError(f"Could not read PDF: {type(exc).__name__}") from exc
    if not any(p.strip() for p in pages):
        raise PdfError("PDF has no text layer (looks scanned). OCR is not implemented in this scaffold.")
    return pages
