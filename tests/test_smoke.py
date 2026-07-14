import open_allocator


def test_package_importable() -> None:
    assert open_allocator.__version__
