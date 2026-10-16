import priorauth


def test_package_imports_and_has_version() -> None:
    assert priorauth.__version__ == "0.1.0"
