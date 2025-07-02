from datetime import datetime
import locale


def _get_date_format(format: str = "full") -> str:
    date_format = format
    match format:
        case "full":
            date_format = "%d/%m/%Y"
        case "short":
            date_format = "%d/%m/%y"
        case "full_human":
            date_format = "%d %B %Y"

    return date_format


def _get_lex_date_format(format: str = "full") -> str:
    date_format = format
    match format:
        case "full":
            date_format = "%Y%m%d"
        case "year_month":
            date_format = "%Y%m"
        case "year":
            date_format = "%Y"

    return date_format


def is_date(date: str, format: str = "full", loc="it_IT.UTF-8") -> bool:
    locale.setlocale(locale.LC_TIME, loc)
    date_format = _get_date_format(format)

    try:
        datetime.strptime(date, date_format)
        return True
    except ValueError:
        return False


def date_to_lex(
    date: str, in_format: str = "full", out_format: str = "full", loc="it_IT.UTF-8"
) -> str:
    locale.setlocale(locale.LC_TIME, loc)
    in_date_format = _get_date_format(in_format)
    out_format = _get_lex_date_format(out_format)
    return datetime.strptime(date, in_date_format).strftime(out_format)
