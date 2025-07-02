from collections import deque
import pathlib
import pdfplumber

from .date import is_date


def _parse_upper_left(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = 0
    top = 0
    x1 = width / 2
    bottom = height / 4

    region = (x0, top, x1, bottom)
    text = page.within_bbox(region).extract_text()
    text_lower = text.lower()
    text_split = text_lower.split()
    return text_split


def get_report_date(path: pathlib.Path) -> str | None:
    with pdfplumber.open(path) as pdf:
        texts = _parse_upper_left(pdf)

        date_ita_month_deque = deque(
            (i for i, s in enumerate(texts) if is_date(s, format="%B")), maxlen=1
        )
        date_ita_month_index = (
            date_ita_month_deque[0] if len(date_ita_month_deque) > 0 else -1
        )

        if date_ita_month_index < 0:
            return None

        date_ita = " ".join(texts[date_ita_month_index - 1 : date_ita_month_index + 2])
        return date_ita
