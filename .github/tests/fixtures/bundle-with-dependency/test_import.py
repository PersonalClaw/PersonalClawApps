from apps_test_dependency import SENTINEL


def test_manifest_dependency_is_importable():
    assert SENTINEL == "installed-from-app-json"
