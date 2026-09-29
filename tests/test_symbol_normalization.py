import pytest

from src.ingestion.security_sync import normalize_a_share_symbol


@pytest.mark.parametrize(
    ("raw", "canonical", "exchange", "yfinance"),
    [
        ("600036", "600036.SH", "SH", "600036.SS"),
        ("  sh600036 ", "600036.SH", "SH", "600036.SS"),
        ("600036.ss", "600036.SH", "SH", "600036.SS"),
        ("SZ000001", "000001.SZ", "SZ", "000001.SZ"),
        ("300750.SZ", "300750.SZ", "SZ", "300750.SZ"),
        ("bj430047", "430047.BJ", "BJ", "430047.BJ"),
    ],
)
def test_normalize_a_share_symbol_accepts_supported_code_formats(
    raw, canonical, exchange, yfinance
):
    result = normalize_a_share_symbol(raw)

    assert result.code == canonical[:6]
    assert result.exchange == exchange
    assert result.canonical == canonical
    assert result.yfinance == yfinance


def test_normalize_a_share_symbol_resolves_chinese_name_through_lookup():
    result = normalize_a_share_symbol(
        "招商银行",
        lookup_by_name=lambda name: "600036" if name == "招商银行" else None,
    )

    assert result.canonical == "600036.SH"


@pytest.mark.parametrize("raw", ["600036.SZ", "000001.SH", "12345", "688001.BJ"])
def test_normalize_a_share_symbol_rejects_invalid_or_mismatched_exchange(raw):
    with pytest.raises(ValueError):
        normalize_a_share_symbol(raw)
