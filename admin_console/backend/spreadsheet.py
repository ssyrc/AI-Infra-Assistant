"""
엑셀(.xlsx/.xls) 읽기 공통 유틸.

여러 탭(매뉴얼·커맨드 등)이 같은 방식으로 헤더/샘플을 미리 보고, 선택한 열을 정제해
가져올 수 있도록 파싱을 한 곳에 모은다. 실제 정제(clean_text)는 호출부에서 수행한다.
"""
import openpyxl


def read_excel_meta(path: str, sample_size: int = 5):
    """(sheet, header, sample_rows, total_rows)를 반환한다. 첫 행을 헤더로 본다.
    빈 파일이면 header가 빈 리스트다."""
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        try:
            header_row = next(it)
        except StopIteration:
            return None, [], [], 0
        header = [
            str(v).strip() if v is not None else f"column_{i}"
            for i, v in enumerate(header_row)
        ]
        sample, total = [], 0
        for i, row in enumerate(it):
            total += 1
            if i < sample_size:
                sample.append(["" if v is None else str(v) for v in row])
        return ws.title, header, sample, total
    finally:
        wb.close()


def load_excel_rows(path: str):
    """(header, col_idx, rows)를 반환한다. 완전히 빈 행은 제외한다.
    col_idx는 열 이름 -> 인덱스 매핑이다."""
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        try:
            header_row = next(it)
        except StopIteration:
            return [], {}, []
        header = [
            str(v).strip() if v is not None else f"column_{i}"
            for i, v in enumerate(header_row)
        ]
        col_idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in it if not all(v is None for v in row)]
        return header, col_idx, rows
    finally:
        wb.close()
