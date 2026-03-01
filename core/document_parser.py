import os
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


def parse_docx(file_path: str) -> str:
    with zipfile.ZipFile(file_path) as z:
        xml_content = z.read('word/document.xml')
        tree = ET.fromstring(xml_content)

        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        paragraphs = tree.findall('.//w:p', ns)

        text_parts = []
        for p in paragraphs:
            texts = p.findall('.//w:t', ns)
            para_text = ''.join(t.text for t in texts if t.text)
            if para_text.strip():
                text_parts.append(para_text.strip())

        return '\n\n'.join(text_parts)


def parse_pdf(file_path: str) -> str:
    if fitz is None:
        raise ImportError("PyMuPDF (fitz) is required to parse PDF files")

    doc = fitz.open(file_path)
    text_parts = []

    for page in doc:
        text = page.get_text()
        if text.strip():
            text_parts.append(text.strip())

    doc.close()
    return '\n\n'.join(text_parts)


def parse_txt(file_path: str) -> str:
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()


def parse_document(file_path: str) -> str:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {file_path}")

    suffix = path.suffix.lower()

    parsers = {
        '.docx': parse_docx,
        '.pdf': parse_pdf,
        '.txt': parse_txt,
    }

    parser = parsers.get(suffix)
    if parser is None:
        raise ValueError(f"Unsupported file format: {suffix}")

    return parser(file_path)


def get_document_name(file_path: str) -> str:
    return Path(file_path).stem
