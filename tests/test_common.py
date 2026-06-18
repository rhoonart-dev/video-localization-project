"""engine/common.py — 경로/JSON/.env 헬퍼."""
import os
import pathlib
import tempfile

from engine import common


def test_resolve_path_absolute():
    assert str(common.resolve_path("/tmp/x")) == "/tmp/x"


def test_resolve_path_relative():
    assert common.resolve_path("config/x.yaml") == common.PROJECT_ROOT / "config" / "x.yaml"


def test_ensure_dir_creates():
    with tempfile.TemporaryDirectory() as d:
        p = common.ensure_dir(pathlib.Path(d) / "a" / "b")
        assert p.is_dir()


def test_write_read_json_roundtrip_unicode():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "sub" / "x.json"
        common.write_json({"k": "값", "n": 3}, p)
        assert common.read_json(p) == {"k": "값", "n": 3}


def test_load_env_manual_parse():
    with tempfile.TemporaryDirectory() as d:
        envp = pathlib.Path(d) / ".env"
        envp.write_text('LOOPY_TEST_KEY="bar123"\n# comment\nEMPTY\n', encoding="utf-8")
        os.environ.pop("LOOPY_TEST_KEY", None)
        common.load_env(envp)
        assert os.environ.get("LOOPY_TEST_KEY") == "bar123"
        os.environ.pop("LOOPY_TEST_KEY", None)


def test_get_secret_fallback_and_optional():
    os.environ.pop("PRIMARY_K", None)
    os.environ["FALLBACK_K"] = "v9"
    try:
        assert common.get_secret("PRIMARY_K", "FALLBACK_K") == "v9"
        assert common.get_secret("DOES_NOT_EXIST_K") is None
    finally:
        os.environ.pop("FALLBACK_K", None)
