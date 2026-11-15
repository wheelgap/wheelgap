import importlib.resources


def test_py_typed_marker_exists():
    """The package ships a py.typed marker so type checkers treat it as typed."""
    marker = importlib.resources.files("wheelgap") / "py.typed"
    assert marker.is_file()
