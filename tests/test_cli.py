import json

from rotary.cli import main


def test_shortcut_converts(tmp_path, capsys, rgba_png):
    assert main([str(rgba_png), "-t", "webp"]) == 0
    assert (tmp_path / "logo.webp").exists()
    assert "logo.webp" in capsys.readouterr().out


def test_output_extension_picks_the_format(tmp_path):
    src = tmp_path / "d.json"
    src.write_text('{"a": 1}')
    assert main(["convert", str(src), "-o", str(tmp_path / "out" / "x.yaml")]) == 0
    assert (tmp_path / "out" / "x.yaml").read_text() == "a: 1\n"


def test_folder_scan_skips_what_cannot_convert(tmp_path, capsys, rgba_png):
    (tmp_path / "notes.json").write_text("{}")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.json").write_text("[]")
    assert main([str(tmp_path), "-t", "yaml", "-d", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "1 converted, 1 skipped" in out
    assert (tmp_path / "out" / "notes.yaml").exists()
    assert main([str(tmp_path), "-r", "-t", "yaml", "-d", str(tmp_path / "out2")]) == 0
    assert (tmp_path / "out2" / "deep.yaml").exists()


def test_errors_set_exit_code(tmp_path, capsys, rgba_png):
    assert main([str(tmp_path / "missing.png"), "-t", "jpg"]) == 1
    assert main([str(rgba_png), "-t", "yaml"]) == 1
    err = capsys.readouterr().err
    assert "no such file" in err and "No way to convert PNG image to YAML" in err
    assert main([str(rgba_png), "-t", "notaformat"]) == 2


def test_refuses_to_overwrite(tmp_path, capsys, rgba_png):
    assert main([str(rgba_png), "-t", "jpg"]) == 0
    assert main([str(rgba_png), "-t", "jpg"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main([str(rgba_png), "-t", "jpg", "--overwrite"]) == 0


def test_dry_run_writes_nothing(tmp_path, capsys):
    src = tmp_path / "d.json"
    src.write_text(json.dumps({"a": 1}))
    assert main([str(src), "-t", "toml", "-n"]) == 0
    assert "TOML" in capsys.readouterr().out
    assert not (tmp_path / "d.toml").exists()


def test_info_commands(capsys):
    assert main(["formats"]) == 0
    assert "image" in capsys.readouterr().out
    assert main(["formats", "json"]) == 0
    assert "yaml" in capsys.readouterr().out
    assert main(["route", "photo.png", "jpg"]) == 0
    assert "PNG -> JPG" in capsys.readouterr().out
    assert main(["backends"]) == 0
    assert "Pillow" in capsys.readouterr().out
