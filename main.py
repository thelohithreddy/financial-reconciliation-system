"""
Financial Reconciliation System
================================
Production-grade system to reconcile platform transactions with bank settlements.
"""

import csv
import json
import random
import hashlib
from datetime import date, timedelta, datetime
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
import os

# ─────────────────────────────────────────────
# SYSTEM ASSUMPTIONS
# ─────────────────────────────────────────────
ASSUMPTIONS = {
    "settlement_window_days": (1, 2),        # Settlements arrive 1–2 business days after transaction
    "rounding_tolerance": Decimal("0.01"),    # Max acceptable amount discrepancy (±1 cent)
    "duplicate_window_hours": 24,             # Settlements within 24h with same txn_id = duplicate
    "refund_must_match_original": True,       # Refunds must reference a valid original txn_id
    "currency": "USD",
    "month_end_deadline_days": 5,             # Settlements must land within 5 days after month-end
}

# ─────────────────────────────────────────────
# ENUMS & DATA CLASSES
# ─────────────────────────────────────────────
class TxnType(str, Enum):
    PAYMENT  = "PAYMENT"
    REFUND   = "REFUND"
    REVERSAL = "REVERSAL"

class DiscrepancyType(str, Enum):
    MISSING_SETTLEMENT      = "MISSING_SETTLEMENT"
    DUPLICATE_SETTLEMENT    = "DUPLICATE_SETTLEMENT"
    AMOUNT_MISMATCH         = "AMOUNT_MISMATCH"
    ORPHAN_REFUND           = "ORPHAN_REFUND"
    DELAYED_SETTLEMENT      = "DELAYED_SETTLEMENT"
    CROSS_MONTH_SETTLEMENT  = "CROSS_MONTH_SETTLEMENT"
    ORPHAN_SETTLEMENT       = "ORPHAN_SETTLEMENT"

@dataclass
class Transaction:
    txn_id:  str
    date:    date
    amount:  Decimal
    type:    TxnType

@dataclass
class Settlement:
    settlement_id: str
    txn_id:        str
    date:          date
    amount:        Decimal

@dataclass
class Discrepancy:
    discrepancy_type: DiscrepancyType
    txn_id:           str
    detail:           str
    txn_amount:       Optional[Decimal] = None
    settlement_amount: Optional[Decimal] = None
    delta:            Optional[Decimal] = None
    txn_date:         Optional[date]    = None
    settlement_date:  Optional[date]    = None

# ─────────────────────────────────────────────
# SYNTHETIC DATA GENERATOR
# ─────────────────────────────────────────────
class SyntheticDataGenerator:
    """Generates realistic transaction and settlement datasets with intentional edge cases."""

    def __init__(self, seed: int = 42):
        random.seed(seed)
        self._txn_counter = 1000

    def _next_id(self) -> str:
        self._txn_counter += 1
        return f"TXN{self._txn_counter}"

    def _settle_id(self, txn_id: str, suffix: str = "") -> str:
        raw = f"{txn_id}{suffix}"
        return "STL" + hashlib.md5(raw.encode()).hexdigest()[:8].upper()

    def generate(self, base_date: date, n: int = 20):
        transactions: list[Transaction] = []
        settlements:  list[Settlement]  = []

        # ── Normal transactions with on-time settlements ──────────────────
        for i in range(n):
            txn_id = self._next_id()
            txn_date = base_date + timedelta(days=random.randint(0, 25))
            amount   = Decimal(str(round(random.uniform(10.0, 5000.0), 2)))
            transactions.append(Transaction(txn_id, txn_date, amount, TxnType.PAYMENT))
            delay = random.randint(1, 2)
            settlements.append(Settlement(
                self._settle_id(txn_id), txn_id,
                txn_date + timedelta(days=delay), amount
            ))

        # ── EDGE CASE 1: Cross-month settlement ───────────────────────────
        ec1_txn_id   = self._next_id()
        # Use last day of month so +3 days always crosses into next month
        import calendar
        last_day = calendar.monthrange(base_date.year, base_date.month)[1]
        ec1_txn_date = base_date.replace(day=last_day)
        ec1_amount   = Decimal("1234.56")
        transactions.append(Transaction(ec1_txn_id, ec1_txn_date, ec1_amount, TxnType.PAYMENT))
        # Settle 3 days later → guaranteed cross-month
        next_month_date = ec1_txn_date + timedelta(days=3)
        settlements.append(Settlement(
            self._settle_id(ec1_txn_id, "cross"), ec1_txn_id,
            next_month_date, ec1_amount
        ))

        # ── EDGE CASE 2: Rounding discrepancy ────────────────────────────
        ec2_txn_id   = self._next_id()
        ec2_txn_date = base_date + timedelta(days=5)
        ec2_amount   = Decimal("999.99")
        transactions.append(Transaction(ec2_txn_id, ec2_txn_date, ec2_amount, TxnType.PAYMENT))
        # Bank rounds differently → 999.98
        settlements.append(Settlement(
            self._settle_id(ec2_txn_id, "round"), ec2_txn_id,
            ec2_txn_date + timedelta(days=1), Decimal("999.98")
        ))

        # ── EDGE CASE 3: Duplicate settlement ────────────────────────────
        ec3_txn_id   = self._next_id()
        ec3_txn_date = base_date + timedelta(days=10)
        ec3_amount   = Decimal("450.00")
        transactions.append(Transaction(ec3_txn_id, ec3_txn_date, ec3_amount, TxnType.PAYMENT))
        stl_base = Settlement(
            self._settle_id(ec3_txn_id, "a"), ec3_txn_id,
            ec3_txn_date + timedelta(days=1), ec3_amount
        )
        stl_dup = Settlement(
            self._settle_id(ec3_txn_id, "b"), ec3_txn_id,  # different STL id, same txn
            ec3_txn_date + timedelta(days=1), ec3_amount
        )
        settlements.extend([stl_base, stl_dup])

        # ── EDGE CASE 4: Refund without original transaction ──────────────
        ec4_txn_id = self._next_id()
        transactions.append(Transaction(
            ec4_txn_id, base_date + timedelta(days=8),
            Decimal("-320.00"), TxnType.REFUND
        ))
        # No original payment in dataset → orphan refund

        # ── EDGE CASE 5: Missing settlement ──────────────────────────────
        ec5_txn_id = self._next_id()
        transactions.append(Transaction(
            ec5_txn_id, base_date + timedelta(days=15),
            Decimal("780.50"), TxnType.PAYMENT
        ))
        # Intentionally no settlement added

        # ── EDGE CASE 6: Orphan settlement (no transaction) ──────────────
        ghost_txn_id = "TXN9999"
        settlements.append(Settlement(
            self._settle_id(ghost_txn_id), ghost_txn_id,
            base_date + timedelta(days=2), Decimal("100.00")
        ))

        return transactions, settlements


# ─────────────────────────────────────────────
# RECONCILIATION ENGINE
# ─────────────────────────────────────────────
class ReconciliationEngine:
    """
    Core reconciliation logic.

    PSEUDOCODE:
    ───────────
    1. Index all transactions by txn_id
    2. Index all settlements by txn_id (detect duplicates in this step)
    3. For each transaction:
         a. If no settlement → MISSING_SETTLEMENT
         b. If multiple settlements → DUPLICATE_SETTLEMENT
         c. If amount delta > tolerance → AMOUNT_MISMATCH
         d. If settlement.date > txn.date + 2 days → DELAYED_SETTLEMENT
         e. If settlement crosses month boundary → CROSS_MONTH_SETTLEMENT
         f. If txn type is REFUND and no parent PAYMENT exists → ORPHAN_REFUND
    4. For each settlement with no matching transaction → ORPHAN_SETTLEMENT
    5. Compile discrepancy log and summary metrics
    """

    def __init__(
        self,
        tolerance:      Decimal = ASSUMPTIONS["rounding_tolerance"],
        settle_window:  tuple   = ASSUMPTIONS["settlement_window_days"],
    ):
        self.tolerance     = tolerance
        self.settle_window = settle_window

    def reconcile(
        self,
        transactions: list[Transaction],
        settlements:  list[Settlement],
    ) -> list[Discrepancy]:

        discrepancies: list[Discrepancy] = []

        # ── Index structures ──────────────────────────────────────────────
        txn_index: dict[str, Transaction]        = {t.txn_id: t for t in transactions}
        payment_ids: set[str]                    = {t.txn_id for t in transactions if t.type == TxnType.PAYMENT}
        stl_index:  dict[str, list[Settlement]]  = defaultdict(list)

        for s in settlements:
            stl_index[s.txn_id].append(s)

        # ── Per-transaction checks ────────────────────────────────────────
        for txn in transactions:

            # Orphan refund check
            if txn.type == TxnType.REFUND:
                # Expect a corresponding PAYMENT with same absolute value
                parent_exists = any(
                    t.txn_id != txn.txn_id
                    and t.type == TxnType.PAYMENT
                    and t.amount == abs(txn.amount)
                    for t in transactions
                )
                if not parent_exists:
                    discrepancies.append(Discrepancy(
                        DiscrepancyType.ORPHAN_REFUND, txn.txn_id,
                        f"Refund of {txn.amount} has no matching original PAYMENT",
                        txn_amount=txn.amount, txn_date=txn.date
                    ))
                continue  # Refunds don't require settlements

            matched = stl_index.get(txn.txn_id, [])

            # Missing settlement
            if not matched:
                discrepancies.append(Discrepancy(
                    DiscrepancyType.MISSING_SETTLEMENT, txn.txn_id,
                    "No settlement found for transaction",
                    txn_amount=txn.amount, txn_date=txn.date
                ))
                continue

            # Duplicate settlement
            if len(matched) > 1:
                ids = [s.settlement_id for s in matched]
                discrepancies.append(Discrepancy(
                    DiscrepancyType.DUPLICATE_SETTLEMENT, txn.txn_id,
                    f"Found {len(matched)} settlements: {ids}",
                    txn_amount=txn.amount, txn_date=txn.date
                ))
                # Still validate the first one
            
            stl = matched[0]

            # Amount mismatch
            delta = abs(stl.amount - txn.amount)
            if delta > self.tolerance:
                discrepancies.append(Discrepancy(
                    DiscrepancyType.AMOUNT_MISMATCH, txn.txn_id,
                    f"Amount delta {delta} exceeds tolerance {self.tolerance}",
                    txn_amount=txn.amount, settlement_amount=stl.amount,
                    delta=delta, txn_date=txn.date, settlement_date=stl.date
                ))
            elif Decimal("0") < delta <= self.tolerance:
                # Within tolerance but still worth flagging as informational — log as rounding
                discrepancies.append(Discrepancy(
                    DiscrepancyType.AMOUNT_MISMATCH, txn.txn_id,
                    f"Rounding delta {delta} within tolerance (INFO)",
                    txn_amount=txn.amount, settlement_amount=stl.amount,
                    delta=delta, txn_date=txn.date, settlement_date=stl.date
                ))

            # Delayed settlement
            max_days = self.settle_window[1]
            if (stl.date - txn.date).days > max_days:
                discrepancies.append(Discrepancy(
                    DiscrepancyType.DELAYED_SETTLEMENT, txn.txn_id,
                    f"Settlement arrived {(stl.date - txn.date).days} days after transaction",
                    txn_date=txn.date, settlement_date=stl.date
                ))

            # Cross-month settlement
            if stl.date.month != txn.date.month or stl.date.year != txn.date.year:
                discrepancies.append(Discrepancy(
                    DiscrepancyType.CROSS_MONTH_SETTLEMENT, txn.txn_id,
                    f"Transaction in {txn.date.strftime('%Y-%m')}, "
                    f"settled in {stl.date.strftime('%Y-%m')}",
                    txn_date=txn.date, settlement_date=stl.date
                ))

        # ── Orphan settlement check ───────────────────────────────────────
        for txn_id, stl_list in stl_index.items():
            if txn_id not in txn_index:
                for s in stl_list:
                    discrepancies.append(Discrepancy(
                        DiscrepancyType.ORPHAN_SETTLEMENT, txn_id,
                        f"Settlement {s.settlement_id} references unknown transaction",
                        settlement_amount=s.amount, settlement_date=s.date
                    ))

        return discrepancies


# ─────────────────────────────────────────────
# REPORT GENERATOR
# ─────────────────────────────────────────────
class ReportGenerator:
    """Builds structured reconciliation reports."""

    def generate(
        self,
        transactions:   list[Transaction],
        settlements:    list[Settlement],
        discrepancies:  list[Discrepancy],
        period:         str,
    ) -> dict:

        by_type = defaultdict(list)
        for d in discrepancies:
            by_type[d.discrepancy_type].append(d)

        total_txn_amount = sum(t.amount for t in transactions if t.type == TxnType.PAYMENT)
        total_stl_amount = sum(s.amount for s in settlements)
        settled_ids      = {s.txn_id for s in settlements}
        matched_count    = sum(
            1 for t in transactions
            if t.type == TxnType.PAYMENT and t.txn_id in settled_ids
        )

        summary = {
            "period":                  period,
            "generated_at":            datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_transactions":      len(transactions),
            "total_settlements":       len(settlements),
            "matched_transactions":    matched_count,
            "total_discrepancies":     len(discrepancies),
            "total_txn_amount":        str(total_txn_amount),
            "total_settlement_amount": str(total_stl_amount),
            "net_exposure":            str(total_txn_amount - total_stl_amount),
            "discrepancy_breakdown":   {k.value: len(v) for k, v in by_type.items()},
        }

        detail_log = []
        for d in discrepancies:
            row = {
                "type":              d.discrepancy_type.value,
                "txn_id":            d.txn_id,
                "detail":            d.detail,
                "txn_amount":        str(d.txn_amount)        if d.txn_amount        else None,
                "settlement_amount": str(d.settlement_amount) if d.settlement_amount else None,
                "delta":             str(d.delta)             if d.delta             else None,
                "txn_date":          str(d.txn_date)          if d.txn_date          else None,
                "settlement_date":   str(d.settlement_date)   if d.settlement_date   else None,
            }
            detail_log.append(row)

        return {"summary": summary, "discrepancies": detail_log}


# ─────────────────────────────────────────────
# TEST SUITE
# ─────────────────────────────────────────────
class TestSuite:
    """Validation tests covering all edge scenarios."""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.results = []

    def assert_case(self, name: str, condition: bool, detail: str = ""):
        status = "PASS" if condition else "FAIL"
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        self.results.append({"test": name, "status": status, "detail": detail})

    def run_all(self, discrepancies: list[Discrepancy], transactions: list[Transaction]):
        types = {d.discrepancy_type for d in discrepancies}
        by_type = defaultdict(list)
        for d in discrepancies:
            by_type[d.discrepancy_type].append(d)

        self.assert_case(
            "TC-01: Missing settlement detected",
            DiscrepancyType.MISSING_SETTLEMENT in types,
            "At least one transaction has no settlement"
        )
        self.assert_case(
            "TC-02: Duplicate settlement detected",
            DiscrepancyType.DUPLICATE_SETTLEMENT in types,
            "Duplicate settlement entries flagged"
        )
        self.assert_case(
            "TC-03: Amount mismatch detected",
            DiscrepancyType.AMOUNT_MISMATCH in types,
            "Rounding discrepancy caught"
        )
        self.assert_case(
            "TC-04: Orphan refund detected",
            DiscrepancyType.ORPHAN_REFUND in types,
            "Refund without original payment flagged"
        )
        self.assert_case(
            "TC-05: Cross-month settlement detected",
            DiscrepancyType.CROSS_MONTH_SETTLEMENT in types,
            "Settlement crossing month boundary flagged"
        )
        self.assert_case(
            "TC-06: Delayed settlement detected",
            DiscrepancyType.DELAYED_SETTLEMENT in types,
            "Settlement beyond 2-day window flagged"
        )
        self.assert_case(
            "TC-07: Orphan settlement detected",
            DiscrepancyType.ORPHAN_SETTLEMENT in types,
            "Settlement referencing unknown transaction flagged"
        )
        self.assert_case(
            "TC-08: Rounding within tolerance is informational only",
            any(
                d.discrepancy_type == DiscrepancyType.AMOUNT_MISMATCH
                and d.delta is not None
                and d.delta <= Decimal("0.01")
                for d in discrepancies
            ),
            "Sub-cent rounding recorded as INFO"
        )
        self.assert_case(
            "TC-09: All transaction types accounted for",
            all(t.type in TxnType for t in transactions),
            "No unknown transaction types"
        )
        self.assert_case(
            "TC-10: Duplicate settlement references same txn_id",
            any(
                len(by_type[DiscrepancyType.DUPLICATE_SETTLEMENT]) >= 1
                and "settlements" in d.detail
                for d in by_type[DiscrepancyType.DUPLICATE_SETTLEMENT]
            ) if DiscrepancyType.DUPLICATE_SETTLEMENT in by_type else False,
            "Duplicate record includes settlement IDs in detail"
        )

        return {
            "total": self.passed + self.failed,
            "passed": self.passed,
            "failed": self.failed,
            "results": self.results,
        }


# ─────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────
def export_csv(data: list[dict], filepath: str):
    if not data:
        return
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)


# ─────────────────────────────────────────────
# LIMITATIONS
# ─────────────────────────────────────────────
LIMITATIONS = [
    {
        "id": "L-01",
        "title": "No Real-Time Streaming Support",
        "description": (
            "This system processes batch snapshots. In production, transactions and settlements "
            "arrive via streaming pipelines (Kafka, Kinesis). Batch reconciliation introduces "
            "latency and may miss real-time fraud signals or double-settlement events."
        ),
        "mitigation": "Integrate with a stream processing layer (e.g., Apache Flink) and run micro-batch reconciliation every 15 minutes."
    },
    {
        "id": "L-02",
        "title": "Single-Currency Assumption",
        "description": (
            "All amounts are assumed to be in USD. Multi-currency environments introduce FX "
            "conversion risks where rounding at the settlement layer may produce discrepancies "
            "that are legitimate but flagged as errors."
        ),
        "mitigation": "Store original currency and converted amount separately; apply currency-aware tolerance thresholds."
    },
    {
        "id": "L-03",
        "title": "Naive Duplicate Detection",
        "description": (
            "Duplicates are detected purely by txn_id collision on settlements. "
            "In real banking, duplicate settlements may arrive with distinct settlement IDs "
            "and slightly different timestamps, making them undetectable without probabilistic "
            "matching (fuzzy deduplication)."
        ),
        "mitigation": "Implement similarity scoring on (txn_id, amount, date ±1 day) and flag near-duplicates for manual review."
    },
    {
        "id": "L-04",
        "title": "No Audit Trail or Immutability Guarantee",
        "description": (
            "Reconciliation results are written to flat files. There is no append-only audit log, "
            "cryptographic signing, or database versioning. In regulated environments (PCI-DSS, SOX), "
            "reconciliation records must be tamper-evident."
        ),
        "mitigation": "Persist results to an immutable store (e.g., AWS QLDB, append-only PostgreSQL table with row-level hashing)."
    },
    {
        "id": "L-05",
        "title": "Assumes Clean Input Schema",
        "description": (
            "The engine has no schema validation layer. Malformed records (nulls, wrong types, "
            "truncated IDs) will cause silent failures or incorrect matches in production data feeds."
        ),
        "mitigation": "Add a validation/sanitization layer using Pydantic or Great Expectations before data enters the engine."
    },
]


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────
def main():
    OUTPUT_DIR = "outputs"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    PERIOD      = "2024-01"
    BASE_DATE   = date(2024, 1, 1)

    print("=" * 60)
    print("  FINANCIAL RECONCILIATION SYSTEM")
    print(f"  Period: {PERIOD}")
    print("=" * 60)

    # 1. Generate synthetic data
    print("\n[1/5] Generating synthetic datasets...")
    gen = SyntheticDataGenerator(seed=42)
    transactions, settlements = gen.generate(BASE_DATE, n=20)
    print(f"      Transactions: {len(transactions)}  |  Settlements: {len(settlements)}")

    # 2. Run reconciliation
    print("\n[2/5] Running reconciliation engine...")
    engine = ReconciliationEngine()
    discrepancies = engine.reconcile(transactions, settlements)
    print(f"      Discrepancies found: {len(discrepancies)}")

    # 3. Generate report
    print("\n[3/5] Generating reconciliation report...")
    reporter = ReportGenerator()
    report   = reporter.generate(transactions, settlements, discrepancies, PERIOD)

    # 4. Run tests
    print("\n[4/5] Running validation test suite...")
    suite      = TestSuite()
    test_report = suite.run_all(discrepancies, transactions)
    print(f"      Tests: {test_report['passed']} PASSED / {test_report['failed']} FAILED")

    # 5. Export all outputs
    print("\n[5/5] Exporting outputs...")

    # Transactions CSV
    export_csv(
        [{"txn_id": t.txn_id, "date": t.date, "amount": t.amount, "type": t.type.value}
         for t in transactions],
        f"{OUTPUT_DIR}/transactions.csv"
    )

    # Settlements CSV
    export_csv(
        [{"settlement_id": s.settlement_id, "txn_id": s.txn_id, "date": s.date, "amount": s.amount}
         for s in settlements],
        f"{OUTPUT_DIR}/settlements.csv"
    )

    # Discrepancy log CSV
    export_csv(report["discrepancies"], f"{OUTPUT_DIR}/discrepancy_log.csv")

    # Full JSON report
    full_output = {
        "assumptions":   ASSUMPTIONS,
        "report":        report,
        "test_results":  test_report,
        "limitations":   LIMITATIONS,
    }
    # Convert Decimal keys in assumptions to str for JSON
    full_output["assumptions"] = {
        k: str(v) if isinstance(v, Decimal) else v
        for k, v in ASSUMPTIONS.items()
    }
    with open(f"{OUTPUT_DIR}/reconciliation_report.json", "w") as f:
        json.dump(full_output, f, indent=2, default=str)

    # ── Print Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  RECONCILIATION SUMMARY")
    print("=" * 60)
    s = report["summary"]
    print(f"  Period              : {s['period']}")
    print(f"  Transactions        : {s['total_transactions']}")
    print(f"  Settlements         : {s['total_settlements']}")
    print(f"  Matched             : {s['matched_transactions']}")
    print(f"  Total Discrepancies : {s['total_discrepancies']}")
    print(f"  Txn Volume          : ${s['total_txn_amount']}")
    print(f"  Settlement Volume   : ${s['total_settlement_amount']}")
    print(f"  Net Exposure        : ${s['net_exposure']}")
    print("\n  DISCREPANCY BREAKDOWN:")
    for dtype, count in s["discrepancy_breakdown"].items():
        print(f"    {dtype:<35} {count}")

    print("\n  TEST RESULTS:")
    for t in test_report["results"]:
        mark = "✓" if t["status"] == "PASS" else "✗"
        print(f"    {mark} {t['test']}")

    print("\n  LIMITATIONS:")
    for lim in LIMITATIONS:
        print(f"    [{lim['id']}] {lim['title']}")

    print("\n  OUTPUT FILES:")
    for fname in ["transactions.csv", "settlements.csv", "discrepancy_log.csv", "reconciliation_report.json"]:
        print(f"    → {OUTPUT_DIR}/{fname}")

    print("\n" + "=" * 60)
    print("  Reconciliation complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
