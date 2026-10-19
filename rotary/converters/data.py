"""Structured data conversion: JSON, YAML, TOML, XML, CSV, TSV and Excel.

Every format is loaded into plain Python objects and dumped back out, so any
pair converts directly. Table formats (CSV, TSV, XLSX) hold rows of records;
nested data is flattened into dotted column names on the way in.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import json
import math
import re
import tomllib
import xml.etree.ElementTree as ET
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

from .base import ConversionError, Converter, Edge, Options

ALWAYS = ["json", "toml", "xml", "csv", "tsv"]
TABULAR = {"csv", "tsv", "xlsx"}


def _have(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


class DataConverter(Converter):
    name = "data"
    label = "built-in data"
    requirement = "pip install PyYAML openpyxl  (for YAML and Excel)"

    @cached_property
    def formats(self) -> list[str]:
        formats = list(ALWAYS)
        if _have("yaml"):
            formats.append("yaml")
        if _have("openpyxl"):
            formats.append("xlsx")
        return formats

    def edges(self) -> list[Edge]:
        return self._pairs(self.formats, self.formats)

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        try:
            obj = load(src, src_fmt)
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(f"Couldn't parse {src.name} as {src_fmt.upper()}: {exc}") from exc
        try:
            dump(obj, dst, dst_fmt)
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(f"Couldn't write {dst_fmt.upper()}: {exc}") from exc


# --------------------------------------------------------------------------- loading


def load(path: Path, fmt: str) -> Any:
    if fmt == "json":
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    if fmt == "yaml":
        import yaml

        with open(path, encoding="utf-8-sig") as f:
            docs = list(yaml.safe_load_all(f))
        if not docs:
            return None
        return docs[0] if len(docs) == 1 else docs
    if fmt == "toml":
        with open(path, "rb") as f:
            return tomllib.load(f)
    if fmt == "xml":
        root = ET.parse(path).getroot()
        return {_local(root.tag): _element_to_obj(root)}
    if fmt in ("csv", "tsv"):
        return _load_delimited(path, fmt)
    if fmt == "xlsx":
        return _load_xlsx(path)
    raise ConversionError(f"Unsupported data format {fmt}")


def _infer(value: Any) -> Any:
    """Turn text that is unambiguously a number or boolean into one.

    Only exact round-trips count, so ``"007"`` and ``"1.50"`` stay strings.
    """
    if not isinstance(value, str):
        return value
    if value in ("true", "false"):
        return value == "true"
    try:
        as_int = int(value)
        if str(as_int) == value:
            return as_int
    except ValueError:
        pass
    try:
        as_float = float(value)
        if math.isfinite(as_float) and repr(as_float) == value:
            return as_float
    except ValueError:
        pass
    return value


def _load_delimited(path: Path, fmt: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        first_line = f.readline()
        f.seek(0)
        if fmt == "tsv":
            delimiter = "\t"
        else:
            # European spreadsheets often export with semicolons; pick whatever the
            # header row uses most.
            delimiter = max(",;|\t", key=first_line.count) if first_line.strip() else ","
            if first_line.count(delimiter) == 0:
                delimiter = ","
        reader = csv.DictReader(f, delimiter=delimiter, restkey="_extra")
        return [{k: _infer(v) for k, v in row.items()} for row in reader]


def _load_xlsx(path: Path) -> Any:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = {}
        for ws in wb.worksheets:
            rows = [r for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
            if not rows:
                continue
            width = max(max((i + 1 for i, v in enumerate(r) if v is not None), default=0) for r in rows)
            header = _unique_headers(rows[0], width)
            sheets[ws.title] = [
                {header[i]: (row[i] if i < len(row) else None) for i in range(width)}
                for row in rows[1:]
            ]
    finally:
        wb.close()
    if len(sheets) == 1:
        return next(iter(sheets.values()))
    return sheets


def _unique_headers(row: tuple, width: int) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i in range(width):
        value = row[i] if i < len(row) else None
        name = str(value) if value not in (None, "") else f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_to_obj(el: ET.Element) -> Any:
    obj: dict[str, Any] = {f"@{_local(k)}": _infer(v) for k, v in el.attrib.items()}
    groups: dict[str, list] = {}
    for child in el:
        if not isinstance(child.tag, str):  # comments, processing instructions
            continue
        groups.setdefault(_local(child.tag), []).append(_element_to_obj(child))
    for tag, values in groups.items():
        obj[tag] = values[0] if len(values) == 1 else values
    text = (el.text or "").strip()
    if text:
        if not obj:
            return _infer(text)
        obj["#text"] = _infer(text)
    return obj or None


# --------------------------------------------------------------------------- dumping


def dump(obj: Any, path: Path, fmt: str) -> None:
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_normalize(obj), f, indent=2, ensure_ascii=False)
            f.write("\n")
    elif fmt == "yaml":
        import yaml

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(_normalize(obj, for_yaml=True), f, sort_keys=False, allow_unicode=True)
    elif fmt == "toml":
        with open(path, "w", encoding="utf-8") as f:
            f.write(dumps_toml(obj))
    elif fmt == "xml":
        _dump_xml(obj, path)
    elif fmt in ("csv", "tsv"):
        _dump_delimited(obj, path, "\t" if fmt == "tsv" else ",")
    elif fmt == "xlsx":
        _dump_xlsx(obj, path)
    else:
        raise ConversionError(f"Unsupported data format {fmt}")


def _normalize(obj: Any, for_yaml: bool = False) -> Any:
    """Reduce values to what JSON (or YAML, which also has dates and NaN) can represent."""
    if isinstance(obj, dict):
        return {str(k): _normalize(v, for_yaml) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_normalize(v, for_yaml) for v in obj]
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj if for_yaml else obj.isoformat()
    if isinstance(obj, dt.time):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, float) and not math.isfinite(obj) and not for_yaml:
        return None
    return obj


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(_normalize(value), ensure_ascii=False)
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return value


def _flatten(record: dict, prefix: str = "") -> dict:
    out: dict = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(_flatten(value, name + "."))
        else:
            out[name] = value
    return out


def _is_records(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, dict) for v in value)


def to_records(obj: Any) -> list[dict]:
    """Find the table inside arbitrary data."""
    while isinstance(obj, dict):
        nested = [v for v in obj.values() if isinstance(v, (dict, list))]
        if len(nested) != 1:
            break
        # Unwrap containers such as {"root": {"item": [...]}} from XML, and step past
        # scalar metadata (e.g. XML attributes) sitting next to a single table.
        if len(obj) == 1 or (_is_records(nested[0]) and nested[0]):
            obj = nested[0]
        else:
            break
    # A workbook with several sheets: use the first one.
    if isinstance(obj, dict) and obj and all(_is_records(v) for v in obj.values()):
        obj = next(iter(obj.values()))
    if isinstance(obj, list):
        return [_flatten(v) if isinstance(v, dict) else {"value": v} for v in obj]
    if isinstance(obj, dict):
        return [_flatten(obj)]
    return [{"value": obj}]


def _columns(records: list[dict]) -> list[str]:
    columns: dict[str, None] = {}
    for record in records:
        for key in record:
            columns.setdefault(key, None)
    return list(columns)


def _dump_delimited(obj: Any, path: Path, delimiter: str) -> None:
    records = to_records(obj)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_columns(records), delimiter=delimiter)
        writer.writeheader()
        for record in records:
            writer.writerow({k: _cell(v) for k, v in record.items()})


def _dump_xlsx(obj: Any, path: Path) -> None:
    import openpyxl
    from openpyxl.styles import Font

    if isinstance(obj, dict) and obj and all(_is_records(v) for v in obj.values()):
        sheets = {name: [_flatten(r) for r in rows] for name, rows in obj.items()}
    else:
        sheets = {"Sheet1": to_records(obj)}

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for name, records in sheets.items():
        title = re.sub(r"[\[\]:*?/\\]", "_", str(name))[:31] or "Sheet"
        while title in used:
            title = title[:28] + f"_{len(used)}"
        used.add(title)
        ws = wb.create_sheet(title)
        columns = _columns(records)
        ws.append(columns)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for record in records:
            ws.append([_xlsx_cell(record.get(c)) for c in columns])
        for i, column in enumerate(columns, start=1):
            longest = max([len(str(column))] + [len(str(r.get(column) or "")) for r in records[:500]])
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(longest + 2, 60)
        ws.freeze_panes = "A2"
    if not wb.worksheets:
        wb.create_sheet("Sheet1")
    wb.save(path)


def _xlsx_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(_normalize(value), ensure_ascii=False)
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.isoformat()  # Excel has no time zones
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


# --------------------------------------------------------------------------- XML writing

_XML_BAD_CHARS = re.compile(r"[^\w.-]")


def _xml_name(key: Any) -> str:
    name = _XML_BAD_CHARS.sub("_", str(key))
    if not name or not (name[0].isalpha() or name[0] == "_"):
        name = "_" + name
    return name


def _xml_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def _xml_build(parent: ET.Element, tag: str, value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _xml_build(parent, tag, item)
        return
    _xml_fill(ET.SubElement(parent, _xml_name(tag)), value)


def _xml_fill(el: ET.Element, value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key = str(key)
            if key.startswith("@") and len(key) > 1:
                if child is not None:
                    el.set(_xml_name(key[1:]), _xml_text(child))
            elif key == "#text":
                if child is not None:
                    el.text = _xml_text(child)
            else:
                _xml_build(el, key, child)
    elif isinstance(value, list):
        for item in value:
            _xml_build(el, "item", item)
    elif value is not None:
        el.text = _xml_text(value)


def _dump_xml(obj: Any, path: Path) -> None:
    if (
        isinstance(obj, dict)
        and len(obj) == 1
        and not str(next(iter(obj))).startswith(("@", "#"))
        and not isinstance(next(iter(obj.values())), list)
    ):
        (tag, value), = obj.items()
        root = ET.Element(_xml_name(tag))
    else:
        root, value = ET.Element("root"), obj
    _xml_fill(root, value)
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- TOML writing

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def dumps_toml(obj: Any) -> str:
    if not isinstance(obj, dict):
        obj = {"items": obj} if isinstance(obj, list) else {"value": obj}
    lines: list[str] = []
    _toml_table(obj, [], lines, header=None)
    return "\n".join(lines).strip() + "\n"


def _toml_is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _toml_table(table: dict, path: list[str], lines: list[str], header: str | None, force: bool = False) -> None:
    scalars = [(k, v) for k, v in table.items() if v is not None and not isinstance(v, dict) and not _toml_is_table_array(v)]
    tables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    arrays = [(k, v) for k, v in table.items() if _toml_is_table_array(v)]
    if header and (force or scalars or not (tables or arrays)):
        lines += ["", header]
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in tables:
        sub = path + [str(key)]
        _toml_table(value, sub, lines, f"[{_toml_dotted(sub)}]")
    for key, items in arrays:
        sub = path + [str(key)]
        for item in items:
            _toml_table(item, sub, lines, f"[[{_toml_dotted(sub)}]]", force=True)


def _toml_key(key: Any) -> str:
    key = str(key)
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


def _toml_dotted(path: list[str]) -> str:
    return ".".join(_toml_key(p) for p in path)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, Decimal):
        return _toml_value(float(value))
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(_toml_value(v) for v in value if v is not None) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items() if v is not None)
        return "{ " + pairs + " }" if pairs else "{}"
    return _toml_string(str(value))
