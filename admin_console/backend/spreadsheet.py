"""
표 형식 파일(.xlsx/.xls/.csv) 읽기 공통 유틸.

여러 탭(매뉴얼·커맨드 등)이 같은 방식으로 헤더/샘플을 미리 보고, 선택한 열을 정제해
가져올 수 있도록 파싱을 한 곳에 모은다. 실제 정제(clean_text)는 호출부에서 수행한다.

CSV는 엑셀과 완전히 같은 "열 매핑" 흐름을 타도록 여기서 흡수한다(호출부는 확장자를 몰라도 된다).
사내에서 만든 CSV는 UTF-8(BOM 포함)이거나 CP949(엑셀 한글 기본)인 경우가 많아 둘 다 시도하고,
구분자도 쉼표/탭/세미콜론/파이프를 자동 판별한다.
"""
import csv
import os

import openpyxl

CSV_EXTS = {".csv"}
EXCEL_EXTS = {".xlsx", ".xls"}
TABLE_EXTS = EXCEL_EXTS | CSV_EXTS

_CSV_ENCODINGS = ("utf-8-sig", "cp949", "utf-8", "latin-1")


def _read_csv_all(path: str) -> list[list[str]]:
    """CSV 전체를 문자열 2차원 리스트로 읽는다. 인코딩/구분자를 자동으로 맞춘다."""
    last_err: Exception | None = None
    for enc in _CSV_ENCODINGS:
        try:
            with open(path, "r", newline="", encoding=enc) as f:
                sample = f.read(8192)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel        # 한 열짜리 파일 등 판별 실패 시 기본(쉼표)
                return [row for row in csv.reader(f, dialect)]
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise ValueError(
        f"CSV 인코딩을 인식할 수 없습니다(UTF-8 또는 CP949로 저장해 주세요): {last_err}")


def _is_csv(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in CSV_EXTS


def _header_of(row) -> list[str]:
    return [str(v).strip() if v is not None and str(v).strip() else f"column_{i}"
            for i, v in enumerate(row)]


def read_table_meta(path: str, sample_size: int = 5):
    """(sheet, header, sample_rows, total_rows)를 반환한다. 첫 행을 헤더로 본다.
    빈 파일이면 header가 빈 리스트다. CSV면 sheet는 "CSV"."""
    if _is_csv(path):
        rows = _read_csv_all(path)
        rows = [r for r in rows if any((v or "").strip() for v in r)]
        if not rows:
            return None, [], [], 0
        header = _header_of(rows[0])
        body = rows[1:]
        sample = [[("" if v is None else str(v)) for v in r] for r in body[:sample_size]]
        return "CSV", header, sample, len(body)

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        try:
            header_row = next(it)
        except StopIteration:
            return None, [], [], 0
        header = _header_of(header_row)
        sample, total = [], 0
        for i, row in enumerate(it):
            total += 1
            if i < sample_size:
                sample.append(["" if v is None else str(v) for v in row])
        return ws.title, header, sample, total
    finally:
        wb.close()


def load_table_rows(path: str):
    """(header, col_idx, rows)를 반환한다. 완전히 빈 행은 제외한다.
    col_idx는 열 이름 -> 인덱스 매핑이다."""
    if _is_csv(path):
        rows = _read_csv_all(path)
        rows = [r for r in rows if any((v or "").strip() for v in r)]
        if not rows:
            return [], {}, []
        header = _header_of(rows[0])
        col_idx = {name: i for i, name in enumerate(header)}
        # 헤더보다 열이 적은 행이 있어도 인덱스 접근이 터지지 않도록 길이를 맞춘다.
        body = [r + [None] * (len(header) - len(r)) if len(r) < len(header) else r
                for r in rows[1:]]
        return header, col_idx, body

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        try:
            header_row = next(it)
        except StopIteration:
            return [], {}, []
        header = _header_of(header_row)
        col_idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in it if not all(v is None for v in row)]
        return header, col_idx, rows
    finally:
        wb.close()


# 이전 이름 유지(호출부 점진 이행용).
read_excel_meta = read_table_meta
load_excel_rows = load_table_rows
