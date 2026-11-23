import json

from app.i18n import Translator


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_translates_in_requested_locale(tmp_path):
    _write(tmp_path / "hu.json", {"hello": "Szia"})
    _write(tmp_path / "en.json", {"hello": "Hello"})
    translator = Translator(tmp_path, "hu")

    assert translator.translate("hello", "en") == "Hello"
    assert translator.translate("hello") == "Szia"


def test_falls_back_to_default_locale_then_key(tmp_path):
    _write(tmp_path / "hu.json", {"only_hu": "csak magyar"})
    _write(tmp_path / "en.json", {})
    translator = Translator(tmp_path, "hu")

    assert translator.translate("only_hu", "en") == "csak magyar"
    assert translator.translate("missing", "en") == "missing"


def test_formats_parameters(tmp_path):
    _write(tmp_path / "hu.json", {"count": "{n} számla"})

    assert Translator(tmp_path, "hu").translate("count", n=3) == "3 számla"


def test_lists_available_locales(tmp_path):
    _write(tmp_path / "hu.json", {})
    _write(tmp_path / "en.json", {})

    assert sorted(Translator(tmp_path, "hu").locales) == ["en", "hu"]
