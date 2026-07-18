"""
업로드된 매뉴얼 파일(xlsx/docx/pptx/pdf)을 검색용 청크 리스트로 변환한다.
Docling을 사용해 구조(표/섹션)를 보존한 마크다운으로 변환한 뒤,
헤더 단위로 청크를 분리한다. 폐쇄망에서는 Docling 모델 캐시를 사전에
내려받아 컨테이너 이미지에 포함해야 한다 (README 참고).
"""
import os
import re
from dataclasses import dataclass

MAX_CHUNK_CHARS = 1500

_converter = None


def _get_converter():
    """Docling은 무겁고 모델 다운로드가 필요하므로 실제로 문서를 파싱할 때 최초 1회만 로딩한다.
    dev 환경에서 DISABLE_DOCLING=1이면 로딩하지 않고, 파싱 요청 시 명확한 에러를 준다."""
    global _converter
    if os.environ.get("DISABLE_DOCLING") == "1":
        raise RuntimeError(
            "이 개발 환경에서는 Docling이 비활성화되어 있습니다. "
            "docx/pptx/pdf 파싱은 프로덕션 환경에서 확인하세요 (엑셀 업로드/컬럼선택 흐름은 dev에서도 동작)."
        )
    if _converter is None:
        from docling.document_converter import DocumentConverter
        _converter = DocumentConverter()
    return _converter


@dataclass
class ParsedChunk:
    section_title: str | None
    page_no: int | None
    chunk_text: str


def parse_file(filepath: str) -> list[ParsedChunk]:
    """파일을 마크다운으로 변환한 뒤 '#' 헤더 기준으로 섹션을 나누고,
    섹션이 너무 길면 MAX_CHUNK_CHARS 단위로 추가 분할한다."""
    result = _get_converter().convert(filepath)
    markdown = result.document.export_to_markdown()

    sections = _split_by_headers(markdown)
    chunks: list[ParsedChunk] = []
    for title, body in sections:
        body = body.strip()
        if not body:
            continue
        for piece in _split_long_text(body, MAX_CHUNK_CHARS):
            chunks.append(ParsedChunk(section_title=title, page_no=None, chunk_text=piece))
    return chunks


def _split_by_headers(markdown: str) -> list[tuple[str | None, str]]:
    lines = markdown.splitlines()
    sections: list[tuple[str | None, str]] = []
    current_title = None
    current_lines: list[str] = []

    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            if current_lines:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = re.sub(r"^#{1,6}\s+", "", line).strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_title, "\n".join(current_lines)))
    return sections


def _split_long_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    pieces, buf = [], ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 > max_chars and buf:
            pieces.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        pieces.append(buf)
    return pieces
