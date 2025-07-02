import pathlib
from typing import Literal
import pdfplumber

from .date import date_to_lex, is_date


def _clean_text(s: str) -> str:
    return s.strip().strip(",")


def _ade_parse_upper_left(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = 0
    top = 0
    x1 = width / 10
    bottom = height / 5

    region = (x0, top, x1, bottom)
    text = page.within_bbox(region).extract_text()
    text_lower = text.lower()
    text_split = text_lower.split()
    return text_split


def ade_soggiorno(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        upper_left_texts = _ade_parse_upper_left(pdf)

        date_ita = next(
            (
                s_clean
                for s_clean in (_clean_text(s) for s in upper_left_texts)
                if is_date(s_clean, format="short")
            ),
            None,
        )
        date_lex = (
            date_to_lex(date_ita, in_format="short")
            if date_ita is not None
            else "NO_DATE"
        )

        return {"date_lex": date_lex}
