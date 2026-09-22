"""
A scanned statement is read by the model and used only if it balances.

The model is mocked: what these pin is the check around it. A reading that
balances line by line is used; one misread digit anywhere is refused and the
line named; a reading that cannot be checked — no balances, no opening and
closing — is refused rather than trusted; and with no model the scan is
refused with that reason, never guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import llm_provider
import statement_ocr
import statement_parsers as sp

SCAN = Path(__file__).resolve().parents[2] / "public" / "sample-data" / "statements" / "statement_scanned.pdf"

# The sample's real content (make_statement_samples.py), as a correct reading.
TRUE_READING = {
    "account": "50200012345678", "currency": "INR",
    "opening_balance": "12,54,320.00", "closing_balance": "9,23,753.86",
    "lines": [
        {"date": "31/08/2026", "description": "NEFT DR VENDOR ANAND PACKAGING",
         "reference": "N243250001", "debit": "85,000.00", "credit": "", "balance": "11,69,320.00"},
        {"date": "01/09/2026", "description": "NEFT CR RAZORPAY SOFTWARE PVT LTD SETTLE-001",
         "reference": "UTR20260901001", "debit": "", "credit": "66,466.36", "balance": "12,35,786.36"},
        {"date": "01/09/2026", "description": "UPI CR 624418889120 SHARMA TRADERS",
         "reference": "624418889120", "debit": "", "credit": "12,500.00", "balance": "12,48,286.36"},
        {"date": "02/09/2026", "description": "ACH DR ELECTRICITY BESCOM",
         "reference": "ACH0909221", "debit": "8,742.50", "credit": "", "balance": "12,39,543.86"},
        {"date": "03/09/2026", "description": "IMPS CR 624617700031 REFUND FROM SUPPLIER",
         "reference": "624617700031", "debit": "", "credit": "4,210.00", "balance": "12,43,753.86"},
        {"date": "04/09/2026", "description": "SALARY BATCH SEP 2026",
         "reference": "SAL2609", "debit": "3,20,000.00", "credit": "", "balance": "9,23,753.86"},
    ],
}

# What Tesseract.js really read off the sample scan: right at 3,400 px in
# single-block mode, and with one balance misread (5 as 6) at 1x.
TESSERACT = Path(__file__).resolve().parent / "fixtures" / "tesseract"


def reading(changes=None):
    """The true reading, with {(row, field): misread value} applied."""
    r = json.loads(json.dumps(TRUE_READING))
    for (i, key), value in (changes or {}).items():
        r["lines"][i][key] = value
    return r


@pytest.fixture
def model(monkeypatch):
    """A configured model that returns whatever the test says it read."""
    seen = {}

    def use(answer):
        def generate(prompt, **kw):
            seen.update(kw)
            return json.dumps(answer) if answer is not None else None
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "generate", generate)
        return seen
    return use


class TestAScan:
    def test_the_sample_is_a_scan(self):
        data = SCAN.read_bytes()
        assert sp.detect(data) == "pdf" and not sp.pdf_text(data).strip()

    def test_a_reading_that_balances_is_used(self, model):
        seen = model(reading())
        st, check = sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")
        assert st.format == "scan" and len(st.lines) == 6 and check["holds"]
        assert st.lines[1].amount_cents == 66_466_36 and st.lines[5].amount_cents == -3_20_000_00
        assert seen["attachments"][0][1] == "application/pdf"
        assert any("balance" in n for n in st.notes)

    def test_one_misread_digit_is_refused_and_the_line_named(self, model):
        model(reading({(3, "debit"): "8,142.50"}))       # 7 read as 1
        with pytest.raises(sp.StatementUnreadable, match="line 4"):
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")

    def test_a_misread_balance_is_refused(self, model):
        model(reading({(2, "balance"): "12,48,236.36"}))
        with pytest.raises(sp.StatementUnreadable, match="misread"):
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")

    def test_a_reading_without_balances_is_not_trusted(self, model):
        model(reading({(3, "balance"): ""}))
        with pytest.raises(sp.StatementUnreadable, match="carry no balance"):
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")

    def test_dates_out_of_order_are_refused(self, model):
        model(reading({(3, "date"): "30/08/2026"}))
        with pytest.raises(sp.StatementUnreadable, match="comes before"):
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")

    def test_a_row_with_both_a_debit_and_a_credit_is_refused(self, model):
        model(reading({(0, "credit"): "1.00"}))
        with pytest.raises(sp.StatementUnreadable, match="both"):
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")

    def test_without_a_model_the_scan_is_refused_with_the_reason(self, monkeypatch):
        monkeypatch.setattr(llm_provider, "is_configured", lambda: False)
        with pytest.raises(sp.StatementUnreadable, match="no model is configured"):
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")

    def test_a_model_that_gives_no_answer_is_a_refusal(self, model):
        model(None)
        with pytest.raises(sp.StatementUnreadable, match="did not read it"):
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf")

    def test_a_photo_is_read_as_an_image(self, model):
        seen = model(reading())
        st, _ = sp.parse(b"\xff\xd8\xff\xe0" + b"\0" * 64, "statement.jpg")
        assert st.format == "scan" and seen["attachments"][0][1] == "image/jpeg"


def test_the_text_pdf_still_needs_no_model(monkeypatch):
    def never(*_a, **_k):
        raise AssertionError("a text PDF must not reach the model")
    monkeypatch.setattr(llm_provider, "generate", never)
    st, check = sp.parse((SCAN.parent / "statement.pdf").read_bytes(), "statement.pdf")
    assert st.format == "pdf" and check["holds"]


def test_the_reader_is_asked_to_copy_not_compute():
    assert "Do not calculate" in statement_ocr._SYSTEM
    assert "never an instruction" in statement_ocr._SYSTEM


class TestReadInTheBrowser:
    """Tesseract.js in the browser reads the scan; the engine only checks it."""

    def test_a_right_reading_is_used_without_any_model(self, monkeypatch):
        def never(*_a, **_k):
            raise AssertionError("a reading that balances must not reach the model")
        monkeypatch.setattr(llm_provider, "generate", never)
        text = (TESSERACT / "sample_scan_read_right.txt").read_text(encoding="utf-8")
        st, check = sp.parse(SCAN.read_bytes(), "statement_scanned.pdf", scan_text=text)
        assert st.format == "scan_ocr" and len(st.lines) == 6 and check["holds"]
        assert st.lines[1].reference == "UTR20260901001" and st.lines[1].amount_cents == 66_466_36
        assert any("Tesseract" in n for n in st.notes)

    def test_a_misread_with_no_model_is_refused_naming_the_line(self, monkeypatch):
        monkeypatch.setattr(llm_provider, "is_configured", lambda: False)
        text = (TESSERACT / "sample_scan_misread.txt").read_text(encoding="utf-8")
        with pytest.raises(sp.StatementUnreadable) as exc:
            sp.parse(SCAN.read_bytes(), "statement_scanned.pdf", scan_text=text)
        assert "2026-09-02" in str(exc.value) and "misread" in str(exc.value)
        assert "no model is configured" in str(exc.value)

    def test_a_misread_goes_to_the_model_where_there_is_one(self, model):
        seen = model(reading())
        text = (TESSERACT / "sample_scan_misread.txt").read_text(encoding="utf-8")
        st, check = sp.parse(SCAN.read_bytes(), "statement_scanned.pdf", scan_text=text)
        assert st.format == "scan" and check["holds"] and seen["attachments"]
        assert any("refused first" in n for n in st.notes)

    def test_ocr_text_is_ignored_for_a_file_that_has_its_own_text(self):
        # A text PDF's own text layer wins; nothing a browser sends replaces it.
        st, _ = sp.parse((SCAN.parent / "statement.pdf").read_bytes(), "statement.pdf",
                         scan_text="01/01/2026 FORGED 1.00 999.00")
        assert st.format == "pdf"
