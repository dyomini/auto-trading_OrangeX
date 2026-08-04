"""
xlsx(제까깟_마틴게이 REALFINALBOSS.xlsx)에서 가중치와 골든 테스트 데이터를 추출한다.

openpyxl 등 서드파티 패키지 없이 표준 라이브러리(zipfile + xml.etree)만 사용한다.
xlsx의 각 수식 셀에는 엑셀이 마지막으로 계산해 캐시해 둔 값이 <v> 태그로 그대로 들어있으므로,
엑셀을 다시 계산할 필요 없이 그 캐시값을 golden 데이터로 그대로 추출한다.
"""
from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
XLSX_PATH = ROOT / "제까깟_마틴게이 REALFINALBOSS.xlsx"

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

# 시트명 -> golden csv 파일명
SHEET_TO_SLUG = {
    "비트_롱계산기": "btc_long",
    "비트_숏계산기": "btc_short",
    "이더_롱계산기": "eth_long",
    "이더_숏계산기": "eth_short",
}

# B..Q 열, 헤더는 시트 XML에서 직접 읽는다 (표준 라이브러리 등가물)
COLUMNS = list("BCDEFGHIJKLMNOPQ")
HEADER_ROW = 18
DATA_START_ROW = 19
DATA_END_ROW = 118


def load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    with zf.open("xl/sharedStrings.xml") as f:
        tree = ET.parse(f)
    root = tree.getroot()
    strings = []
    for si in root.findall("main:si", NS):
        # 서식이 섞인 rich text(<r><t>..)와 단순 <t> 둘 다 처리
        text_parts = [t.text or "" for t in si.findall(".//main:t", NS)]
        strings.append("".join(text_parts))
    return strings


def load_sheet_name_to_path(zf: zipfile.ZipFile) -> dict[str, str]:
    with zf.open("xl/workbook.xml") as f:
        wb_tree = ET.parse(f)
    with zf.open("xl/_rels/workbook.xml.rels") as f:
        rels_tree = ET.parse(f)

    rid_to_target = {}
    for rel in rels_tree.getroot():
        rid_to_target[rel.attrib["Id"]] = rel.attrib["Target"]

    name_to_path = {}
    for sheet in wb_tree.getroot().find("main:sheets", NS):
        name = sheet.attrib["name"]
        rid = sheet.attrib[f'{{{NS["r"]}}}id']
        target = rid_to_target[rid]
        name_to_path[name] = f"xl/{target}"
    return name_to_path


CELL_REF_RE = re.compile(r"([A-Z]+)(\d+)")


def parse_sheet_cells(zf: zipfile.ZipFile, sheet_path: str, shared_strings: list[str]) -> dict[tuple[str, int], str]:
    """{(col_letter, row_num): cell_value_as_string} 형태로 반환. 숫자는 문자열 그대로(정밀도 보존)."""
    with zf.open(sheet_path) as f:
        tree = ET.parse(f)
    root = tree.getroot()
    sheet_data = root.find("main:sheetData", NS)

    cells: dict[tuple[str, int], str] = {}
    for row in sheet_data.findall("main:row", NS):
        for c in row.findall("main:c", NS):
            ref = c.attrib["r"]
            m = CELL_REF_RE.match(ref)
            col, row_num = m.group(1), int(m.group(2))
            cell_type = c.attrib.get("t")
            v_elem = c.find("main:v", NS)
            if v_elem is None:
                continue
            value = v_elem.text
            if cell_type == "s":
                value = shared_strings[int(value)]
            cells[(col, row_num)] = value
    return cells


def main() -> None:
    config_dir = ROOT / "config"
    golden_dir = ROOT / "tests" / "golden"
    config_dir.mkdir(parents=True, exist_ok=True)
    golden_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(XLSX_PATH) as zf:
        shared_strings = load_shared_strings(zf)
        name_to_path = load_sheet_name_to_path(zf)

        # 헤더 확인용 (참고 출력만, 실제 컬럼 매핑은 이미 사전 검증 완료)
        first_sheet_path = name_to_path["비트_롱계산기"]
        cells = parse_sheet_cells(zf, first_sheet_path, shared_strings)
        headers = {col: cells.get((col, HEADER_ROW), "") for col in COLUMNS}
        print("헤더 확인:", headers)

        # 1) weights.csv 추출 (E열, 비트_롱계산기 기준 — 4시트 동일값 사전 확인됨)
        weights = [cells[("E", r)] for r in range(DATA_START_ROW, DATA_END_ROW + 1)]
        weights_sum = sum(float(w) for w in weights)
        print(f"weights count={len(weights)} sum={weights_sum}")
        assert len(weights) == 100
        assert abs(weights_sum - 17130) < 1e-9, f"weights sum mismatch: {weights_sum}"

        with open(config_dir / "weights.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["step_index", "weight"])
            for i, w in enumerate(weights):
                writer.writerow([i, w])

        # 2) 4개 시트 golden CSV 추출
        golden_header = [
            "major_tier", "sub_step", "entry_price", "weight", "step_qty", "step_margin",
            "cum_qty", "cum_margin", "avg_price", "available_balance", "liq_price",
            "liq_pct", "target_roe", "target_tp_price", "required_bounce", "sl_price",
        ]
        for sheet_name, slug in SHEET_TO_SLUG.items():
            sheet_path = name_to_path[sheet_name]
            sheet_cells = parse_sheet_cells(zf, sheet_path, shared_strings)
            out_path = golden_dir / f"{slug}.csv"
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(golden_header)
                for r in range(DATA_START_ROW, DATA_END_ROW + 1):
                    row_values = [sheet_cells.get((col, r), "") for col in COLUMNS]
                    writer.writerow(row_values)
            print(f"{sheet_name} -> {out_path} ({DATA_END_ROW - DATA_START_ROW + 1} rows)")

    print("DONE")


if __name__ == "__main__":
    main()
