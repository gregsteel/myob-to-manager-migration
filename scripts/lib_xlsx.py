"""Minimal stdlib .xlsx reader — no pandas/openpyxl (project golden rule).

Reads MYOB's exported .xlsx (or .csv) reports into rows of strings, handling
shared strings and numeric cells. Good enough for MYOB's simple flat report
exports; not a general spreadsheet library.
"""
from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
COL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _col_index(cell_ref: str) -> int:
    m = COL_RE.match(cell_ref)
    if not m:
        raise ValueError(f"bad cell ref {cell_ref!r}")
    col = m.group(1)
    idx = 0
    for ch in col:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        data = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    out = []
    for si in root.findall(f"{NS}si"):
        # concatenate all <t> under this <si> (handles rich text runs <r><t>)
        text = "".join(t.text or "" for t in si.iter(f"{NS}t"))
        out.append(text)
    return out


def _sheet_path(z: zipfile.ZipFile, sheet_index: int) -> str:
    names = [n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    names.sort(key=lambda n: int(re.search(r"\d+", n).group()))
    return names[sheet_index]


def read_xlsx_rows(path: str | Path, sheet_index: int = 0) -> list[list[str]]:
    """Read a sheet into a list of rows, each a list of cell strings.

    Rows are padded to the widest row seen (no ragged rows). Numbers are
    returned as their raw string form (e.g. "102911.69").
    """
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        sheet_xml = z.read(_sheet_path(z, sheet_index))

    root = ET.fromstring(sheet_xml)
    sheet_data = root.find(f"{NS}sheetData")
    rows: list[list[str]] = []
    max_col = 0
    raw_rows: list[dict[int, str]] = []
    for row_el in sheet_data.findall(f"{NS}row"):
        row: dict[int, str] = {}
        for c in row_el.findall(f"{NS}c"):
            ref = c.get("r")
            col = _col_index(ref) if ref else len(row)
            ctype = c.get("t")
            v_el = c.find(f"{NS}v")
            is_el = c.find(f"{NS}is")
            if is_el is not None:
                value = "".join(t.text or "" for t in is_el.iter(f"{NS}t"))
            elif v_el is None:
                value = ""
            elif ctype == "s":
                value = shared[int(v_el.text)]
            else:
                value = v_el.text or ""
            row[col] = value
            max_col = max(max_col, col)
        raw_rows.append(row)

    for row in raw_rows:
        rows.append([row.get(i, "") for i in range(max_col + 1)])
    return rows


def read_rows(path: str | Path, sheet_index: int = 0) -> list[list[str]]:
    """Read .xlsx or .csv transparently."""
    p = Path(path)
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8-sig") as f:
            return [row for row in csv.reader(f)]
    return read_xlsx_rows(p, sheet_index=sheet_index)
