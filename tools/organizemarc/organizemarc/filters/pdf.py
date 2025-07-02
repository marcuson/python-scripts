import pdfplumber
import pdfplumber.page


def extract_texts(
    pdfPage: pdfplumber.page.Page, x0: float, top: float, x1: float, bottom: float
) -> list[str]:
    region = (x0, top, x1, bottom)
    text = pdfPage.within_bbox(region).extract_text()
    text_lower = text.lower()
    text_split = text_lower.split()
    return text_split
