"""Represent OOXML formula objects as meaningful text for value-oriented exports."""

from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula


def describe_special_formula(value) -> str:
    if isinstance(value, ArrayFormula) and value.text:
        return f"{value.text} [배열 범위 {value.ref}]"
    if isinstance(value, DataTableFormula):
        settings = ", ".join(f"{key}={item}" for key, item in value)
        return f"데이터 표 [{settings}]"
    raise ValueError("본문이 없거나 지원하지 않는 Excel 수식 구조입니다.")
