import pathlib
import pdfplumber

from .date import is_date


def _parse_lower_left(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = 0
    top = height / 2
    x1 = width / 5
    bottom = height / 2 + height / 4

    region = (x0, top, x1, bottom)
    text = page.within_bbox(region).extract_text()
    text_lower = text.lower()
    text_split = text_lower.split()
    return text_split


def get_due_date(path: pathlib.Path) -> str | None:
    with pdfplumber.open(path) as pdf:
        lower_left_texts = _parse_lower_left(pdf)

        date_ita = next(
            (
                s_clean
                for s_clean in (s.strip().strip(":") for s in lower_left_texts)
                if is_date(s_clean)
            ),
            None,
        )

        return date_ita
