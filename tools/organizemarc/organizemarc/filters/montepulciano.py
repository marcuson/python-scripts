import pathlib
from typing import Literal

from .date import date_to_lex


def _montepulciano_imposta_soggiorno_date_lex(
    month_start: str, month_end: str, year: str
) -> str:
    date_ita_start = "01 " + month_start + " " + year
    date_ita_end = "01 " + month_end + " " + year
    date_lex = (
        date_to_lex(date_ita_start, in_format="full_human", out_format="year_month")
        + "-"
        + date_to_lex(date_ita_end, in_format="full_human", out_format="year_month")
    )
    return date_lex


def montepulciano_imposta_soggiorno_avviso(path: pathlib.Path, month_start: str, month_end: str, year: str) -> dict[str, str] | Literal[False]:  # type: ignore[misc], year=year)) -> dict[str, str] | Literal[False]:
    date_lex = _montepulciano_imposta_soggiorno_date_lex(month_start, month_end, year)
    return {"date_lex": date_lex}


def montepulciano_imposta_soggiorno_ricevuta(path: pathlib.Path, month_start: str, month_end: str, year: str) -> dict[str, str] | Literal[False]:  # type: ignore[misc], year=year)) -> dict[str, str] | Literal[False]:
    date_lex = _montepulciano_imposta_soggiorno_date_lex(month_start, month_end, year)
    return {"date_lex": date_lex}
