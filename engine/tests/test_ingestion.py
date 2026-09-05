"""
Ingestion normalization tests — Agent 1.

Tests the deterministic boundary layer:
  - integer-cents conversion (never float)
  - timezone normalization + confidence
  - ref_id canonicalization
  - memo normalization
  - malformed record handling
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from datetime import timezone
from schema import SourceType, TzConfidence
from ingestion import (
    normalize_amount_to_cents,
    AmountUnreadable,
    normalize_timestamp,
    normalize_ref_id,
    normalize_memo,
    normalize_record,
    normalize_batch,
)


class TestAmountToCents:

    def test_integer_input(self):
        assert normalize_amount_to_cents(100) == 10_000

    def test_float_input_rounds_correctly(self):
        # 9.99 -> 999 cents, NOT 998 (float truncation trap)
        assert normalize_amount_to_cents(9.99) == 999

    def test_string_input_with_commas(self):
        assert normalize_amount_to_cents("1,234.56") == 123_456

    def test_half_cent_rounds_up(self):
        # 0.005 should round to 1 cent (ROUND_HALF_UP)
        assert normalize_amount_to_cents("0.005") == 1

    def test_zero_amount(self):
        assert normalize_amount_to_cents(0) == 0

    def test_large_amount_no_float_drift(self):
        # Float drift trap: 9999.99 in float can misrepresent
        assert normalize_amount_to_cents("9999.99") == 999_999

    def test_returns_int_type(self):
        result = normalize_amount_to_cents(123.45)
        assert isinstance(result, int), f"Expected int, got {type(result)}"


class TestTimestampNormalization:

    def test_utc_timestamp_high_confidence(self):
        ts, confidence = normalize_timestamp("2026-08-20T10:00:00+00:00", SourceType.GATEWAY)
        assert confidence == TzConfidence.HIGH
        assert ts.tzinfo == timezone.utc

    def test_ist_timestamp_explicit_offset(self):
        ts, confidence = normalize_timestamp("2026-08-20T15:30:00+05:30", SourceType.BANK)
        assert confidence == TzConfidence.HIGH
        # IST +5:30 -> UTC: 15:30 - 5:30 = 10:00 UTC
        assert ts.hour == 10
        assert ts.minute == 0

    def test_naive_timestamp_inferred(self):
        # No tz info -> should be INFERRED, not HIGH
        ts, confidence = normalize_timestamp("2026-08-20 10:00:00", SourceType.GATEWAY)
        assert confidence == TzConfidence.INFERRED

    def test_invalid_timestamp_raises(self):
        with pytest.raises(ValueError):
            normalize_timestamp("not-a-date", SourceType.GATEWAY)


class TestRefIdCanonicalization:

    def test_strips_special_chars(self):
        assert normalize_ref_id("RZP-100-001") == "RZP100001"

    def test_uppercases(self):
        assert normalize_ref_id("rzp100") == "RZP100"

    def test_handles_none(self):
        # Should not crash on empty/None ref
        result = normalize_ref_id("")
        assert isinstance(result, str)

    def test_alphanumeric_only(self):
        result = normalize_ref_id("ABC 123!@#")
        assert result == "ABC123"

    def test_truncated_ref_returns_prefix(self):
        # Truncated refs (bank 20-char limit) should be preserved as-is
        # (downstream handles prefix matching)
        long_ref = "RZPAY" * 10  # 50 chars
        result = normalize_ref_id(long_ref)
        assert result.startswith("RZPAY")


class TestMemoNormalization:

    def test_lowercases(self):
        assert normalize_memo("Payment FOR Order") == "payment for order"

    def test_collapses_whitespace(self):
        assert normalize_memo("  multiple   spaces  ") == "multiple spaces"

    def test_handles_empty(self):
        assert normalize_memo("") == ""

    def test_handles_none_like_empty(self):
        # None should be treated like empty string
        result = normalize_memo(None)
        assert result == ""


class TestNormalizeRecord:

    def _make_raw(self, **overrides) -> dict:
        base = {
            "txn_id": "TXN001",
            "ref_id": "RZP123",
            "amount": "500.00",
            "currency": "INR",
            "timestamp": "2026-08-20T10:00:00+05:30",
            "memo": "Payment for Order 123",
        }
        base.update(overrides)
        return base

    def test_basic_record_normalizes(self):
        raw = self._make_raw()
        txn = normalize_record(raw, SourceType.GATEWAY)
        assert txn.source_txn_id == "TXN001"
        assert txn.amount_cents == 50_000
        assert txn.currency == "INR"
        assert txn.timestamp_utc.tzinfo == timezone.utc

    def test_amount_is_integer(self):
        raw = self._make_raw(amount=9.99)
        txn = normalize_record(raw, SourceType.BANK)
        assert isinstance(txn.amount_cents, int)
        assert txn.amount_cents == 999

    def test_extra_fields_stored(self):
        raw = self._make_raw(gateway_fee="12.50", batch_id="B001")
        txn = normalize_record(raw, SourceType.GATEWAY)
        assert "gateway_fee" in txn.extra
        assert txn.extra["gateway_fee"] == "12.50"


class TestBatchNormalize:

    def test_skips_invalid_records(self):
        records = [
            {"txn_id": "OK", "ref_id": "R1", "amount": "100", "currency": "INR", "timestamp": "2026-08-20T10:00:00Z"},
            {"txn_id": "BAD", "ref_id": "R2", "amount": "100", "currency": "INR", "timestamp": "GARBAGE"},
        ]
        result = normalize_batch(records, SourceType.GATEWAY)
        assert len(result) == 1
        assert result[0].source_txn_id == "OK"

    def test_empty_batch(self):
        result = normalize_batch([], SourceType.ERP)
        assert result == []


class TestAmountCleaningRefusesRatherThanGuessing:
    """
    normalize_amount_to_cents decides the integer paise subset-sum matches on,
    and its string pre-processing was "delete everything that is not a digit,
    dot, comma or minus, then drop the commas". Measured against the examples
    in its own docstring, that rule:

        "Rs. 1,234.50"  RAISED     the '.' in "Rs." survived into ".1234.50"
        "1 234,50"      12345000   100x too large, and claimed as supported
        "4,2OO.OO"      4200       OCR damage silently read as a number
        "(1,500.00)"    150000     accounting negative, sign dropped
    """

    def test_its_own_docstring_example_now_works(self):
        assert normalize_amount_to_cents("Rs. 1,234.50") == 123_450

    def test_indian_lakh_grouping(self):
        assert normalize_amount_to_cents("\u20b912,34,567.89") == 123_456_789

    def test_accounting_negative_keeps_its_sign(self):
        # Dropping the parentheses turns a debit into a credit.
        assert normalize_amount_to_cents("(1,500.00)") == -150_000

    @pytest.mark.parametrize("corrupt", ["1 234,50", "4,2OO.OO", "12.34.56", "1.234,56"])
    def test_corrupt_amounts_refuse(self, corrupt):
        with pytest.raises((AmountUnreadable, ValueError)):
            normalize_amount_to_cents(corrupt)

    def test_float_paise_conversion_does_not_lose_a_paisa(self):
        # ROUND_HALF_UP, not truncation: int() would give 469556 here and the
        # exact subset-sum would then never balance.
        assert normalize_amount_to_cents(4695.57) == 469_557
