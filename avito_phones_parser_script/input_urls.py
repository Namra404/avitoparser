import re
from pathlib import Path
from urllib.parse import urljoin
import pandas as pd

def read_urls_from_excel_or_csv(
    path: Path,
    sheet: str | int | None = None,
    url_column: str | None = None
) -> list[str]:
    url_re = re.compile(r'https?://(?:www\.)?avito\.ru/[^\s"]+')
    urls: list[str] = []

    if path.suffix.lower() in {".xlsx", ".xls"}:
        xls = pd.ExcelFile(path)
        sheets = [sheet] if sheet is not None else xls.sheet_names
        for sh in sheets:
            df = xls.parse(sh, dtype=str)
            if url_column and url_column in df.columns:
                col = df[url_column].dropna().astype(str)
                urls.extend(col.tolist())
            else:
                for col in df.columns:
                    s = df[col].dropna().astype(str)
                    for val in s:
                        urls.extend(url_re.findall(val))
    elif path.suffix.lower() in {".csv", ".txt"}:
        df = pd.read_csv(path, dtype=str, sep=None, engine="python")
        if url_column and url_column in df.columns:
            col = df[url_column].dropna().astype(str)
            urls.extend(col.tolist())
        else:
            for col in df.columns:
                s = df[col].dropna().astype(str)
                for val in s:
                    urls.extend(url_re.findall(val))
    else:
        raise ValueError("Поддерживаются .xlsx/.xls/.csv/.txt")

    cleaned = []
    seen = set()
    for u in urls:
        u = u.strip()
        if not u.startswith("http"):
            u = urljoin("https://www.avito.ru", u)
        u = u.split("#", 1)[0]
        u = u.split("?", 1)[0]
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
    return cleaned
