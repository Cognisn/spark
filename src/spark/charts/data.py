"""A chart's data from a CSV or Excel file of the conversation.

A definition can name a file instead of giving its rows: ``{"file_id": 12, "sheet":
"Sales", "columns": ["Region", "Total"]}``. :func:`load_file_table` reads it for the
turn's sender with the same scoping as the document preview: a file of this
conversation (whoever in it stored it) or one of the sender's own files. Anyone
else's file answers as not found, exactly as a file that does not exist.

CSV files are read with the standard library's reader (the delimiter sniffed among
comma, semicolon, tab and bar) and Excel workbooks with openpyxl, the reader SPARK's
document tools use. The first row holds the column names. A file with more rows than a
chart takes is refused rather than cut short, so a chart never silently shows part of
its data.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any, BinaryIO, Final

from spark import files as files_pkg
from spark.charts.schema import LIMITS
from spark.charts.validate import Cell, ChartRefused, FileData, Table
from spark.database import files as files_db
from spark.db.engine import Row
from spark.files.area import BridgeLike
from spark.files.titles import clean_text
from spark.preview.kinds import base_type

__all__ = ["MAX_FILE_BYTES", "find_file", "load_file_table", "read_table"]

MAX_FILE_BYTES: Final = 20 * 1024 * 1024
_CSV_TYPES: Final = frozenset({"text/csv", "application/csv", "text/comma-separated-values"})
_XLSX_TYPES: Final = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    }
)


def _cell(value: Any) -> Cell:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _header(values: Sequence[Any]) -> tuple[str, ...]:
    names: list[str] = []
    for index, value in enumerate(values, 1):
        name = clean_text(value, limit=LIMITS.label) or f"Column {index}"
        base, n = name, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        names.append(name)
    return tuple(names)


def _table(rows: Iterable[Sequence[Any]], what: str) -> Table:
    header: tuple[str, ...] | None = None
    body: list[tuple[Cell, ...]] = []
    for raw in rows:
        values = [_cell(v) for v in raw]
        if header is None:
            if not any(v not in (None, "") for v in values):
                continue
            while values and values[-1] in (None, ""):
                values.pop()
            header = _header(values)
            if len(header) > LIMITS.columns:
                raise ChartRefused(
                    f"{what} has {len(header)} columns; name the ones to chart in data.columns."
                )
            continue
        if not any(v not in (None, "") for v in values):
            continue
        cells = (values + [None] * len(header))[: len(header)]
        body.append(tuple(cells))
        if len(body) > LIMITS.rows:
            raise ChartRefused(
                f"{what} has more than {LIMITS.rows:,} rows; a chart takes at most "
                f"{LIMITS.rows:,}. Summarise it first (for example with a smaller sheet)."
            )
    if header is None or not body:
        raise ChartRefused(f"{what} has no rows of data under a row of column names.")
    return Table(header, tuple(body))


def _csv(raw: bytes, what: str) -> Table:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect: Any = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return _table(csv.reader(io.StringIO(text), dialect), what)


def _excel(raw: bytes, sheet: str | None, what: str) -> Table:
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable workbook is refused alike
        raise ChartRefused(f"{what} could not be read as an Excel workbook.") from exc
    try:
        if sheet is None:
            target = book.worksheets[0]
        elif sheet in book.sheetnames:
            target = book[sheet]
        else:
            names = ", ".join(clean_text(n, limit=LIMITS.label) for n in book.sheetnames)
            raise ChartRefused(
                f"{what} has no sheet {clean_text(sheet, limit=60)}; its sheets are: {names}."
            )
        return _table(target.iter_rows(values_only=True), what)
    finally:
        book.close()


def read_table(handle: BinaryIO, *, name: str, mime: str | None, sheet: str | None = None) -> Table:
    """The table in a CSV or Excel file; raises :class:`ChartRefused` for anything else."""
    what = f"The file {clean_text(name, limit=LIMITS.label)}"
    raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ChartRefused(f"{what} is too large to chart; summarise it first.")
    media = base_type(mime)
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if media in _CSV_TYPES or suffix in ("csv", "tsv", "txt"):
        if sheet is not None:
            raise ChartRefused("data.sheet applies to Excel workbooks only; this file is CSV.")
        return _csv(raw, what)
    if media in _XLSX_TYPES or suffix in ("xlsx", "xlsm"):
        return _excel(raw, sheet, what)
    if suffix == "xls" or media == "application/vnd.ms-excel":
        raise ChartRefused(f"{what} is an old Excel file (.xls); save it as .xlsx or CSV first.")
    raise ChartRefused(f"{what} is not a CSV or Excel file; a chart reads data from those only.")


def find_file(db: BridgeLike, user: str, conversation_id: int | None, file_id: int) -> Row:
    """A file the sender may chart: one of this conversation's, or one of their own."""
    row: Row | None = None
    if conversation_id is not None:
        row = db.call(files_db.get_conversation_file, user, conversation_id, file_id)
    if row is None:
        row = db.call(files_db.get_user_file, user, file_id)
    if row is None:
        raise ChartRefused(f"File {file_id} is not one of this conversation's files or yours.")
    return row


def load_file_table(
    db: BridgeLike, user: str, conversation_id: int | None, data: FileData
) -> Table:
    """The table a definition's file holds, with only ``data.columns`` when it names them."""
    row = find_file(db, user, conversation_id, data.file_id)
    try:
        with files_pkg.file_store().open(str(row["user_guid"]), str(row["storage_key"])) as handle:
            table = read_table(
                handle, name=str(row["filename"]), mime=row["mime_type"], sheet=data.sheet
            )
    except (FileNotFoundError, OSError) as exc:
        raise ChartRefused(f"File {data.file_id} is no longer stored.") from exc
    if data.columns is None:
        return table
    missing = [c for c in data.columns if c not in table.columns]
    if missing:
        raise ChartRefused(
            f"The file has no column {', '.join(clean_text(m, limit=LIMITS.label) for m in missing)}. "
            f"Its columns are: {', '.join(table.columns)}."
        )
    keep = [table.columns.index(c) for c in data.columns]
    return Table(data.columns, tuple(tuple(r[i] for i in keep) for r in table.rows))
