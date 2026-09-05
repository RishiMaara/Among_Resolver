"""
Tests for Agent 0 — File Understanding Agent

Verifies header mapping, fuzzy matching, data scrubbing, and parsing.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import llm_header_mapper
from file_agent import (
    TARGET_FIELDS,
    FileRejected,
    _clean_amount,
    AmountUnreadable,
    resolve_day_order,
    normalize_date,
    parse_settlements,
    _map_headers,
    map_headers_with_report,
    parse_csv,
    parse_file_content,
    parse_json,
    parse_settlements,
    resolve_headers,
)


class TestDataCleaning:

    def test_clean_amount_removes_symbols(self):
        assert _clean_amount("$100.50") == 100.5
        assert _clean_amount("Rs. 1,000") == 1000.0
        assert _clean_amount(" -500 ") == -500.0

    def test_clean_amount_handles_numbers(self):
        assert _clean_amount(100) == 100.0
        assert _clean_amount(10.5) == 10.5

    def test_clean_amount_blank_means_absent(self):
        # A blank cell is a fact, not a defect: a debit row on a bank
        # statement carries no credit amount.
        assert _clean_amount("") == 0.0
        assert _clean_amount(None) == 0.0
        assert _clean_amount("   ") == 0.0

    def test_clean_amount_keeps_real_world_decoration(self):
        assert _clean_amount("1,234.56 CR") == 1234.56
        assert _clean_amount("₹2,50,000.00") == 250000.0   # Indian lakh grouping
        assert _clean_amount("INR 900") == 900.0

    def test_clean_amount_reads_accounting_negatives(self):
        # (1,500.00) is how ledgers write -1500. Dropping the parentheses
        # turns a debit into a credit, which on a settlement feed is the
        # worst single thing this parser could do.
        assert _clean_amount("(1,500.00)") == -1500.0
        assert _clean_amount("(250)") == -250.0

    @pytest.mark.parametrize(
        "corrupt,old_result",
        [
            ("4,2OO.OO", 42.0),        # OCR read 0 as O -- off by 100x
            ("1.234,56", 1.234),       # European format -- off by 1000x
            ("12 34 567.89", 12.0),    # spaced grouping -- off by 100000x
            ("invalid", 0.0),
        ],
    )
    def test_clean_amount_refuses_rather_than_guessing(self, corrupt, old_result):
        """
        Each of these previously returned a confident, plausible, WRONG number.
        The first matters most: this engine accepts scanned statements and
        O-for-0 is the classic scan error, so a settlement of 4,200 became
        42.00 -- a wrong reconciliation that still ties out.
        """
        with pytest.raises(AmountUnreadable):
            _clean_amount(corrupt)


class TestHeaderMapping:

    def test_exact_mapping(self):
        headers = ["txn_id", "amount", "memo"]
        mapping = _map_headers(headers)
        assert mapping["txn_id"] == "txn_id"
        assert mapping["amount"] == "amount"
        assert mapping["memo"] == "memo"

    def test_fuzzy_synonym_mapping(self):
        headers = ["Transaction Date", "Net Amt", "Bank Ref Num", "Notes"]
        mapping = _map_headers(headers)
        assert mapping["Transaction Date"] == "timestamp"
        assert mapping["Net Amt"] == "amount"
        assert mapping["Bank Ref Num"] == "txn_id"
        assert mapping["Notes"] == "memo"

    def test_ignores_unknown_columns(self):
        headers = ["amount", "IP Address", "Shipping Zone"]
        mapping = _map_headers(headers)
        assert "amount" in mapping.values()
        assert "IP Address" not in mapping
        assert "Shipping Zone" not in mapping

    def test_customer_name_is_the_payer(self):
        """
        "Customer Name" used to be this test suite's example of a column
        worth ignoring. It is the opposite: the customer is the payer, and
        the payer is what sanctions screening screens and what the
        structuring, velocity and concentration rules group by. While it went
        unmapped those rules had nothing to work with.
        """
        mapping = _map_headers(["amount", "Customer Name", "Beneficiary"])
        assert mapping["Customer Name"] == "payer_id"
        assert mapping["Beneficiary"] == "payee_id"


class TestFileParsing:

    def test_parse_csv_basic(self):
        csv_content = (
            "Payment ID,Order ID,Amount (INR),Date\n"
            'pay_123,order_456,"1,500.00",2026-08-20T10:00:00Z\n'
        )
        results = parse_csv(csv_content)
        assert len(results) == 1
        row = results[0]
        assert row["txn_id"] == "pay_123"
        assert row["ref_id"] == "order_456"
        assert row["amount"] == 1500.0  # 1,500.00 -> 1500.0
        assert row["timestamp"] == "2026-08-20T10:00:00Z"

    def test_parse_csv_missing_amount_raises(self):
        csv_content = (
            "Payment ID,Order ID,Date\n"
            "pay_123,order_456,2026-08-20T10:00:00Z\n"
        )
        with pytest.raises(ValueError, match="amount"):
            parse_csv(csv_content)

    def test_parse_json_basic(self):
        json_content = '''[
            {
                "id": "txn_999",
                "reference": "ref_888",
                "credit": "$250.00",
                "created_at": "2026-08-21T12:00:00Z",
                "remarks": "Refund"
            }
        ]'''
        results = parse_json(json_content)
        assert len(results) == 1
        row = results[0]
        assert row["txn_id"] == "txn_999"
        assert row["ref_id"] == "ref_888"
        assert row["amount"] == 250.0
        assert row["timestamp"] == "2026-08-21T12:00:00Z"
        assert row["memo"] == "Refund"

    def test_mirrors_ids_if_one_missing(self):
        csv_content = (
            "amount,Date,Order ID\n"
            "100.00,2026-08-20,ref_123\n"
        )
        results = parse_csv(csv_content)
        row = results[0]
        # txn_id wasn't in CSV, so it should mirror the ref_id
        assert row["ref_id"] == "ref_123"
        assert row["txn_id"] == "ref_123"

    def test_value_date_column_maps_to_timestamp_not_amount(self):
        """
        Regression test for a real bug found via the 50K stress dataset:
        fuzz.token_set_ratio scores any token-subset match as a perfect
        100, so "Value Date" (tokens {value, date}) was matching amount's
        old "value" synonym at 100 -- tied with timestamp's "date" synonym
        also at 100 -- with amount winning the tie purely because it's
        checked earlier in TARGET_FIELDS order. This silently corrupted
        the amount field with a date fragment (e.g. "2026" parsed out of
        "2026-08-14") and left timestamp empty, which caused every
        affected row to be dropped downstream (empty timestamp fails to
        parse in ingestion.normalize_batch). Confirmed on the real 50K
        stress test: 100% of the bank_statement.csv source (15,000 rows)
        silently vanished until this was fixed.
        """
        headers = ["Bank Ref", "Debit", "Credit", "Value Date", "Description", "Counterparty"]
        mapping = _map_headers(headers)
        assert mapping["Value Date"] == "timestamp"
        assert mapping.get("Value Date") != "amount"

    def test_debit_credit_blank_does_not_overwrite_populated_amount(self):
        """
        Regression test: a bank statement with separate Debit/Credit
        columns populates only one per row. Both columns map to the
        "amount" target (both are exact synonym matches). The row-mapping
        loop must not let a later-processed BLANK column erase a value
        already set by an earlier-processed POPULATED column -- that was
        only "correct" before by accident of the columns' left-to-right
        order in this specific file, not by design.
        """
        csv_content = (
            "Bank Ref,Debit,Credit,Value Date,Description\n"
            "UTR_1,,58371.62,2026-08-14,Deposit\n"
            "UTR_2,1200.00,,2026-08-15,Withdrawal\n"
        )
        results = parse_csv(csv_content)
        assert results[0]["amount"] == 58371.62  # Credit populated, Debit blank
        assert results[0]["timestamp"] == "2026-08-14"
        assert results[1]["amount"] == 1200.0    # Debit populated, Credit blank
        assert results[1]["timestamp"] == "2026-08-15"


class TestHeaderMappingDisambiguation:
    """
    Regression tests from the ReconRiver evaluation (a third-party dataset).

    The old mapper let several columns claim the same target and left the
    winner to whichever was processed last. It produced correct output on that
    dataset by luck, which is the most dangerous kind of passing.
    """

    PROCESSOR = [
        "processor_transaction_id", "merchant_order_id", "processor_event_type",
        "processor_event_time", "gross_amount", "fee_amount", "net_amount",
        "currency", "settlement_batch_id", "processor_status",
    ]

    def _cols_for(self, headers, target):
        return [c for c, t in _map_headers(headers).items() if t == target]

    def test_fee_column_can_never_be_the_amount(self):
        """
        A fee is a deduction, not the value of the payment. Reconciling
        against one yields a confidently wrong answer that still looks
        arithmetically tidy. gross/fee/net all scored a perfect 100 here, so
        header order alone decided it.
        """
        assert "fee_amount" not in self._cols_for(self.PROCESSOR, "amount")

    def test_single_valued_targets_get_exactly_one_column(self):
        for target in ("amount", "timestamp", "txn_id", "currency"):
            cols = self._cols_for(self.PROCESSOR, target)
            assert len(cols) <= 1, f"{target} claimed {cols}"

    def test_settlement_reference_survives_as_ref_id(self):
        """
        settlement_batch_id is the linkage anchor — the single most valuable
        signal the engine has. The generic "id" synonym was letting txn_id
        claim it, after which it was dropped entirely and linkage had nothing
        to anchor on.
        """
        refs = self._cols_for(self.PROCESSOR, "ref_id")
        assert "settlement_batch_id" in refs

    def test_specific_synonym_beats_generic_one(self):
        """"order id" is a more specific match than "id", so merchant_order_id
        belongs to ref_id rather than txn_id."""
        m = _map_headers(self.PROCESSOR)
        assert m.get("processor_transaction_id") == "txn_id"
        assert m.get("merchant_order_id") == "ref_id"

    def test_debit_and_credit_both_map_to_amount(self):
        """
        Complementary, not competing: a statement row populates one and leaves
        the other blank. Mapping only one silently zeroes every row of the
        other sign — half the file on a real statement.
        """
        headers = ["Bank Ref", "Debit", "Credit", "Value Date", "Description"]
        cols = self._cols_for(headers, "amount")
        assert set(cols) == {"Debit", "Credit"}

    def test_report_surfaces_the_competition(self):
        r = map_headers_with_report(self.PROCESSOR)
        assert "fee_amount" in r.disqualified
        assert any("amount" in w for w in r.warnings)

    def test_unmapped_amount_or_timestamp_is_warned_about(self):
        r = map_headers_with_report(["foo", "bar", "baz"])
        joined = " ".join(r.warnings)
        assert "amount" in joined and "timestamp" in joined


class TestAgent0RejectsUnusableFiles:
    """
    Agent 0 is a gatekeeper, not just a guesser.

    A missing required field does not produce a bad answer, it produces a
    silent absence: rows with no timestamp are discarded during normalisation,
    so the run reports "nothing matched" when the real fault was ingestion.
    On the ReconRiver dataset that lost 107 of 207 records without an error.
    Refusing the file with a stated reason is strictly better.
    """

    def _reject(self, raw: bytes, name: str = "bank_statement.csv"):
        with pytest.raises(FileRejected) as ei:
            parse_file_content(raw, name)
        return ei.value

    def test_rejects_when_no_amount_column(self):
        e = self._reject(b"Bank Ref,Value Date,Description\nUTR1,2026-08-14,Deposit\n")
        assert any("amount" in p for p in e.problems)

    def test_rejects_when_no_date_column(self):
        e = self._reject(b"Bank Ref,Credit,Description\nUTR1,58371.62,Deposit\n")
        assert any("timestamp" in p for p in e.problems)

    def test_a_fee_column_alone_does_not_satisfy_amount(self):
        """Reconciling against fees would be confidently wrong, so a file whose
        only monetary column is a fee must be refused rather than used."""
        e = self._reject(b"Bank Ref,Value Date,Fee Amount\nUTR1,2026-08-14,12.50\n")
        assert any("amount" in p for p in e.problems)

    def test_rejects_when_no_identifier_of_any_kind(self):
        e = self._reject(b"Credit,Value Date\n58371.62,2026-08-14\n")
        assert any("identifier" in p for p in e.problems)

    def test_rejects_when_dates_map_but_are_all_corrupt(self):
        """Schema validation alone is not enough — a correctly identified date
        column full of '2026-99-99' maps cleanly and still yields nothing."""
        e = self._reject(b"Bank Ref,Credit,Value Date\nUTR1,58371.62,2026-99-99T99:99:99Z\n")
        assert any("parseable date" in p for p in e.problems)

    def test_rejects_when_every_amount_is_zero(self):
        e = self._reject(b"Bank Ref,Credit,Value Date\nUTR1,0.00,2026-08-14\n")
        assert any("non-zero amount" in p for p in e.problems)

    def test_rejects_a_file_with_headers_but_no_rows(self):
        e = self._reject(b"Bank Ref,Credit,Value Date\n")
        assert any("no data rows" in p for p in e.problems)

    def test_accepts_a_complete_file(self):
        rows = parse_file_content(
            b"Bank Ref,Credit,Value Date\nUTR1,58371.62,2026-08-14\n",
            "bank_statement.csv",
        )
        assert len(rows) == 1
        assert rows[0]["amount"] == 58371.62

    def test_rejection_names_the_columns_and_how_to_fix_it(self):
        """The message has to be actionable — a reviewer should not need the
        source to work out which column to rename."""
        e = self._reject(b"Bank Ref,Credit,Description\nUTR1,58371.62,Deposit\n")
        assert "Bank Ref" in e.headers
        assert e.hint and "HEADER_SYNONYMS" in e.hint
        assert e.to_dict()["rejected"] is True


class TestLLMAssistedMapping:
    """
    The model PROPOSES; the deterministic layer DECIDES.

    These tests pin the safety property rather than the LLM's answers: a
    suggestion must not be able to talk its way past a rule that exists to
    prevent an expensive mistake.
    """

    def test_disabled_without_credentials(self, monkeypatch):
        """Sending sample cell values to an external service must never happen
        as a silent side effect of running the pipeline."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert llm_header_mapper.is_enabled() is False

    def test_explicit_opt_out_wins_over_credentials(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("LLM_HEADER_MAPPING", "0")
        assert llm_header_mapper.is_enabled() is False

    def test_not_consulted_when_rules_already_succeed(self, monkeypatch):
        """A working rule-based mapping is free, instant and reproducible.
        Never spend a call — or accept the variance — to second-guess it."""
        called = False

        def _spy(*a, **k):
            nonlocal called
            called = True
            return None

        monkeypatch.setattr(llm_header_mapper, "propose_mapping", _spy)
        rows = [{"Bank Ref": "U1", "Credit": "10.00", "Value Date": "2026-08-14"}]
        resolve_headers(["Bank Ref", "Credit", "Value Date"], rows, "ok.csv")
        assert called is False

    def test_proposal_cannot_make_a_fee_the_amount(self, monkeypatch):
        monkeypatch.setattr(
            llm_header_mapper, "propose_mapping",
            lambda *a, **k: ({"Fee Amount": "amount", "When": "timestamp",
                              "Ref": "ref_id"}, ["reasoning"]),
        )
        rows = [{"Ref": "R1", "Fee Amount": "1.50", "When": "2026-08-14"}]
        report = resolve_headers(["Ref", "Fee Amount", "When"], rows, "x.csv")
        assert report.mapping.get("Fee Amount") != "amount"

    def test_proposal_cannot_give_two_columns_to_one_target(self, monkeypatch):
        monkeypatch.setattr(
            llm_header_mapper, "propose_mapping",
            lambda *a, **k: ({"A": "amount", "B": "amount", "When": "timestamp",
                              "Ref": "ref_id"}, []),
        )
        rows = [{"Ref": "R1", "A": "1.00", "B": "2.00", "When": "2026-08-14"}]
        report = resolve_headers(["Ref", "A", "B", "When"], rows, "x.csv")
        assert sum(1 for t in report.mapping.values() if t == "amount") == 1

    def test_unknown_field_names_are_dropped(self, monkeypatch):
        monkeypatch.setattr(
            llm_header_mapper, "propose_mapping",
            lambda *a, **k: ({"Ref": "ref_id", "Val": "amount",
                              "When": "timestamp", "X": "not_a_real_field"}, []),
        )
        rows = [{"Ref": "R1", "Val": "1.00", "When": "2026-08-14", "X": "?"}]
        report = resolve_headers(["Ref", "Val", "When", "X"], rows, "x.csv")
        assert "X" not in report.mapping

    def test_llm_result_is_flagged_for_the_audit_trail(self, monkeypatch):
        """
        The rows here are deliberately empty of values.

        Value inference now runs BEFORE the model, and on populated rows it
        would resolve this mapping itself — which is the point of it, and why
        this test had to change. The model is reached only where inspecting
        the values cannot help, so that is the case exercised here.
        """
        monkeypatch.setattr(
            llm_header_mapper, "propose_mapping",
            lambda *a, **k: ({"Ref": "ref_id", "Val": "amount",
                              "Booked": "timestamp"}, ["Booked -> timestamp"]),
        )
        rows = [{"Ref": "R1", "Val": "", "Booked": ""}]
        report = resolve_headers(["Ref", "Val", "Booked"], rows, "x.csv")
        assert report.llm_assisted is True
        assert any("LLM assistance" in w for w in report.warnings)

    def test_a_failed_proposal_still_ends_in_rejection(self, monkeypatch):
        """The gate is the same either way — the model cannot rescue a file
        that genuinely lacks a required field."""
        monkeypatch.setattr(
            llm_header_mapper, "propose_mapping",
            lambda *a, **k: ({"Ref": "ref_id"}, []),
        )
        with pytest.raises(FileRejected):
            parse_file_content(b"Ref,Notes\nR1,hello\n", "x.csv")

    def test_api_failure_degrades_to_rules_rather_than_crashing(self, monkeypatch):
        monkeypatch.setattr(
            llm_header_mapper, "propose_mapping",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")),
        )
        rows = [{"Bank Ref": "U1", "Credit": "10.00", "Value Date": "2026-08-14"}]
        # Rules already suffice here, so the mapper is never consulted and the
        # raising stub proves it: a working file must not depend on the network.
        report = resolve_headers(["Bank Ref", "Credit", "Value Date"], rows, "ok.csv")
        assert "amount" in report.mapping.values()


class TestUnfamiliarExportsAreNotRefused:
    """
    A rejection is the worst outcome Agent 0 can produce.

    The alternative to an uncertain mapping is not a slightly worse
    reconciliation — it is no reconciliation at all, and a user staring at an
    error for a file that is perfectly reconcilable. Measured against eight
    plausible export formats, name matching alone refused four of them.

    Value inference closes that: a column of parseable dates is a timestamp
    whatever it is called, a column of decimals is a candidate amount, a
    column of opaque high-cardinality strings is an identifier. The structural
    rules still apply on top — a fee column is still disqualified from
    becoming `amount`.
    """

    SCHEMES = {
        "stripe-ish": ["id", "charge", "gross", "curr", "created", "statement_descriptor"],
        "tally": ["Voucher No", "Party Name", "Value", "Dated", "Narration"],
        "sap-ish": ["BELNR", "WRBTR", "WAERS", "BUDAT", "SGTXT"],
        "generic ledger": ["Sr No", "Particulars", "Amount (INR)", "Date", "Remarks"],
        "opaque names": ["col_a", "col_b", "col_c", "col_d"],
    }

    def _csv(self, cols):
        import csv as _csv, io
        amountish = ("amount", "amt", "value", "gross", "net", "debit",
                     "credit", "wrbtr", "col_b")
        dateish = ("date", "created", "dated", "time", "budat", "col_c")
        rows = []
        for i in range(4):
            rows.append({
                c: (f"2026-08-2{i}" if any(k in c.lower() for k in dateish)
                    else f"{1200 + i}.50" if any(k in c.lower() for k in amountish)
                    else "INR" if any(k in c.lower() for k in ("curr", "waers"))
                    else f"TXN0012{i}")
                for c in cols
            })
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode()

    @pytest.mark.parametrize("name", list(SCHEMES))
    def test_format_is_accepted(self, name):
        rows = parse_file_content(self._csv(self.SCHEMES[name]), f"{name}.csv")
        assert len(rows) == 4
        assert rows[0]["amount"], f"{name}: no amount mapped"
        assert rows[0]["timestamp"], f"{name}: no timestamp mapped"

    def test_a_fee_column_is_still_never_the_amount(self):
        """Inference must not become a way around the disqualifiers."""
        import csv as _csv, io
        cols = ["ref", "fee_amount", "when"]
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows([{"ref": f"R{i}", "fee_amount": f"{i}.50",
                      "when": f"2026-08-2{i}"} for i in range(4)])
        with pytest.raises(FileRejected):
            parse_file_content(buf.getvalue().encode(), "fees_only.csv")


class TestBankStatementsOfAnyShape:
    """
    Every bank formats differently, and nobody knows what a user will upload.

    This was found the hard way: the settlements reader was tuned to one real
    SBI statement, and against five bank layouts it accepted exactly the one
    it had been tuned to. Each synonym added matches the sample in hand and
    misses the next file — an unbounded chase.

    Two things make a statement harder than a transaction feed, and both are
    asserted here:

      A statement has debit AND credit columns. A settlement is money
      arriving, so picking "the numeric column" is wrong half the time.

      A statement has a running balance: numeric, positive, present on every
      row, and never the amount. Sparseness is what separates them.
    """

    LAYOUTS = {
        "sbi": ["Txn Date", "Description", "Debit", "Credit", "Balance"],
        "hdfc": ["Date", "Narration", "Chq/Ref No", "Withdrawal Amt",
                 "Deposit Amt", "Closing Balance"],
        "icici": ["Value Date", "Transaction Remarks", "Withdrawal (Dr)",
                  "Deposit (Cr)", "Balance"],
        "axis": ["Tran Date", "PARTICULARS", "DR", "CR", "BAL"],
        "no_identifier": ["Date", "Amount Credited", "Running Balance"],
        "opaque": ["c1", "c2", "c3"],
    }

    def _statement(self, cols):
        import csv as _csv, io
        rows = []
        for i in range(6):
            r = {}
            for c in cols:
                lc = c.lower()
                if "date" in lc or c == "c1":
                    r[c] = f"2026-04-0{i + 1}"
                elif ("credit" in lc or "deposit" in lc or lc == "cr") and "bal" not in lc:
                    r[c] = f"{500 + i * 100}.00" if i % 2 == 0 else ""
                elif ("debit" in lc or "withdraw" in lc or lc == "dr") and "bal" not in lc:
                    r[c] = "" if i % 2 == 0 else f"{80 + i}.00"
                elif "bal" in lc:
                    r[c] = f"{9000 + i * 13}.00"
                elif c == "c2":
                    r[c] = f"{500 + i * 100}.00" if i % 2 == 0 else ""
                elif c == "c3":
                    r[c] = f"{9000 + i * 13}.00"
                else:
                    r[c] = f"UPI/CR/6094695252{i}/SOMEONE"
            rows.append(r)
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode()

    @pytest.mark.parametrize("bank", list(LAYOUTS))
    def test_layout_is_read(self, bank):
        out = parse_settlements(self._statement(self.LAYOUTS[bank]), f"{bank}.csv")
        assert out, f"{bank}: no settlements read"
        amounts = {float(str(r["net_amount"]).replace(",", "")) for r in out}
        assert 500.0 in amounts, (
            f"{bank}: read {sorted(amounts)} — the credit column was not the "
            f"one picked. A balance or a debit was taken for the amount."
        )

    def test_the_running_balance_is_never_the_amount(self):
        """The balance is the most amount-looking column and never the amount."""
        out = parse_settlements(self._statement(self.LAYOUTS["sbi"]), "sbi.csv")
        amounts = {float(str(r["net_amount"]).replace(",", "")) for r in out}
        assert not any(a > 8000 for a in amounts), (
            f"Picked the running balance: {sorted(amounts)}"
        )

    def test_a_missing_identifier_is_derived_and_marked(self):
        """
        Most statements have no settlement id, and requiring one refused the
        commonest file a finance team has. A derived id must be visibly
        derived so nobody mistakes it for one the bank assigned.
        """
        out = parse_settlements(
            self._statement(self.LAYOUTS["no_identifier"]), "x.csv")
        assert out
        assert all(r["batch_id"].startswith("CREDIT-") for r in out)
        assert all(r.get("_synthetic_id") for r in out)


class TestDateOrder:
    """
    A day-first statement was reporting a month it did not contain.

    settled_at left the parser as the file's raw string and the browser read
    it with Date(), which assumes month-first: 09/03/2026 (9 March) became
    September 3rd, and 15/03/2026 became Invalid Date so the field silently
    never filled. Both are resolved here, where the whole column is visible.
    """

    def test_one_decisive_row_settles_the_whole_column(self):
        order, proven = resolve_day_order(["09/03/2026", "15/03/2026", "02/03/2026"])
        assert (order, proven) == ("day", True)      # 15 cannot be a month

    def test_month_first_is_detected_too(self):
        order, proven = resolve_day_order(["03/15/2026", "03/09/2026"])
        assert (order, proven) == ("month", True)

    def test_all_ambiguous_is_reported_as_unproven(self):
        # Every value <= 12/12. Day-first is assumed, and `proven` is False so
        # the caller must present it as an assumption rather than a finding.
        order, proven = resolve_day_order(["09/03/2026", "02/09/2026"])
        assert (order, proven) == ("day", False)

    def test_march_statement_never_reports_september(self):
        stmt = (
            b"Txn Date,Narration,Withdrawal,Deposit,Balance\n"
            b"09/03/2026,NEFT SETTL A,,48250.00,148250.00\n"
            b"15/03/2026,ATM WDL,2000.00,,146250.00\n"
            b"31/03/2026,NEFT SETTL B,,127400.50,273650.50\n"
        )
        report: dict = {}
        rows = parse_settlements(stmt, "s.csv", report=report)
        dates = [r["settled_at"] for r in rows]
        assert dates == ["2026-03-09", "2026-03-31"]
        assert all(d.startswith("2026-03") for d in dates)
        assert report["date_order_proven"] is True

    def test_day_thirty_one_no_longer_vanishes(self):
        # Date("31/03/2026") is Invalid Date in a browser, so this row used to
        # fill nothing at all.
        assert normalize_date("31/03/2026", "day") == "2026-03-31"
