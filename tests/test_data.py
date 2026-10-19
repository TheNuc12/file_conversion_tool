import json
import tomllib

import pytest

from rotary.converters.data import dumps_toml, to_records

SAMPLE = {
    "people": [
        {"name": "Ada", "age": 36, "zip": "007", "tags": ["math", "engines"], "address": {"city": "London"}},
        {"name": "Grace", "age": 85, "active": True},
    ]
}


def convert(engine, tmp_path, name, content, to):
    src = tmp_path / name
    src.write_text(content, encoding="utf-8")
    return engine.convert(src, to).output


def test_json_yaml_round_trip(engine, tmp_path):
    out = convert(engine, tmp_path, "s.json", json.dumps(SAMPLE), "yaml")
    back = engine.convert(out, "json", out_dir=tmp_path / "back").output
    assert json.loads(back.read_text()) == SAMPLE


def test_json_to_csv_flattens_and_csv_keeps_leading_zeros(engine, tmp_path):
    out = convert(engine, tmp_path, "s.json", json.dumps(SAMPLE), "csv")
    lines = out.read_text().splitlines()
    assert lines[0] == "name,age,zip,tags,address.city,active"
    assert lines[1] == 'Ada,36,007,"[""math"", ""engines""]",London,'
    back = json.loads(engine.convert(out, "json", out_dir=tmp_path / "back").output.read_text())
    assert back[0]["zip"] == "007" and back[0]["age"] == 36 and back[1]["active"] is True


def test_semicolon_csv_is_detected(engine, tmp_path):
    out = convert(engine, tmp_path, "eu.csv", "name;price\nTea;2.5\n", "json")
    assert json.loads(out.read_text()) == [{"name": "Tea", "price": 2.5}]


def test_xml_round_trip(engine, tmp_path):
    xml = '<shop id="7"><item><name>Tea</name><qty>2</qty></item><item><name>Jam</name><qty>1</qty></item></shop>'
    out = convert(engine, tmp_path, "shop.xml", xml, "json")
    data = json.loads(out.read_text())
    assert data == {"shop": {"@id": 7, "item": [{"name": "Tea", "qty": 2}, {"name": "Jam", "qty": 1}]}}
    back = engine.convert(out, "xml", out_dir=tmp_path / "back").output
    assert json.loads(engine.convert(back, "json", out_dir=tmp_path / "again").output.read_text()) == data
    rows = engine.convert(out, "csv", out_dir=tmp_path / "csv").output.read_text().splitlines()
    assert rows == ["name,qty", "Tea,2", "Jam,1"]


def test_toml_writer_output_parses():
    data = {
        "title": 'Quote " and \\ and \x7f',
        "server": {"ports": [80, 443], "opts": {"tls": True}, "ratio": 0.5},
        "users": [{"name": "a", "meta": {"admin": True}}, {"name": "b", "none": None}],
        "weird key": 1,
        "mixed": [1, {"x": 2}],
    }
    parsed = tomllib.loads(dumps_toml(data))
    assert parsed["title"] == data["title"]
    assert parsed["server"] == data["server"]
    assert parsed["users"] == [{"name": "a", "meta": {"admin": True}}, {"name": "b"}]
    assert parsed["weird key"] == 1 and parsed["mixed"] == [1, {"x": 2}]
    assert tomllib.loads(dumps_toml([1, 2])) == {"items": [1, 2]}


def test_xlsx_round_trip_with_several_sheets(engine, tmp_path):
    pytest.importorskip("openpyxl")
    books = {"Fruit": [{"name": "apple", "n": 3}], "Veg": [{"name": "kale", "n": 1}]}
    out = convert(engine, tmp_path, "book.json", json.dumps(books), "xlsx")
    back = json.loads(engine.convert(out, "json", out_dir=tmp_path / "back").output.read_text())
    assert back == books
    # A table format can only hold one sheet: the first one wins.
    first = engine.convert(out, "csv", out_dir=tmp_path / "csv").output.read_text().splitlines()
    assert first == ["name,n", "apple,3"]


def test_to_records_finds_the_table():
    assert to_records({"root": {"row": [{"a": 1}, {"a": 2}]}}) == [{"a": 1}, {"a": 2}]
    assert to_records([1, 2]) == [{"value": 1}, {"value": 2}]
    assert to_records({"a": {"b": 1}, "c": 2}) == [{"a.b": 1, "c": 2}]


def test_bad_input_is_a_readable_error(engine, tmp_path):
    from rotary.converters import ConversionError

    with pytest.raises(ConversionError, match="Couldn't parse broken.json as JSON"):
        convert(engine, tmp_path, "broken.json", "{nope", "yaml")
