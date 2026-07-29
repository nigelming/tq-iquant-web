import re

STOCK_CODE_PATTERN = re.compile(r"^\d{6}\.(SZ|SH)$")


def validate_stock_code(code):
    return bool(STOCK_CODE_PATTERN.match(code))
