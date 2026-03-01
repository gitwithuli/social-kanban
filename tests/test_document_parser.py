import pytest
import tempfile
import os

from core.document_parser import parse_document, parse_txt, get_document_name


@pytest.fixture
def sample_txt_file():
    content = """Trading Wisdom

The market is not your enemy. Your lack of discipline is.

Every trade should have a reason. If you don't know why you're entering, you don't know when to exit.

Risk management is not optional. It is the foundation of longevity in the markets.
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(content)
        f.flush()
        yield f.name

    os.unlink(f.name)


def test_parse_txt(sample_txt_file):
    content = parse_txt(sample_txt_file)
    assert "Trading Wisdom" in content
    assert "discipline" in content


def test_parse_document_txt(sample_txt_file):
    content = parse_document(sample_txt_file)
    assert "discipline" in content


def test_get_document_name(sample_txt_file):
    name = get_document_name(sample_txt_file)
    assert name.startswith("tmp")


def test_parse_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        parse_document("/nonexistent/path/file.txt")


def test_unsupported_format():
    with tempfile.NamedTemporaryFile(suffix='.xyz', delete=False) as f:
        f.write(b"test content")
        f.flush()
        try:
            with pytest.raises(ValueError):
                parse_document(f.name)
        finally:
            os.unlink(f.name)
