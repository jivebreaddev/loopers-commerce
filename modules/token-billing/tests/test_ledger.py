from concurrent.futures import ThreadPoolExecutor

import pytest
from token_billing import BudgetExceeded, InvalidUsage, Ledger


def reserve(ledger, request_id="request-1", account="alice", budget=1000, global_budget=1000):
    return ledger.reserve(request_id, account, budget, global_budget, 100, 10, 2, 6)


def test_reserve_settle_is_exact_idempotent_and_persistent(tmp_path):
    path = str(tmp_path / "billing.sqlite3")
    ledger = Ledger(path)
    assert reserve(ledger) == 260
    assert ledger.snapshot("alice", 1000)["reserved"] == 260
    assert ledger.settle("request-1", 20, 5, "primary") == 70
    assert ledger.settle("request-1", 20, 5, "primary") == 70
    snapshot = Ledger(path).snapshot("alice", 1000)
    assert (snapshot["charged"], snapshot["reserved"], snapshot["available"]) == (70, 0, 930)
    with pytest.raises(InvalidUsage):
        ledger.settle("request-1", 21, 5, "primary")


def test_concurrent_reservations_cannot_overspend(tmp_path):
    path = str(tmp_path / "billing.sqlite3")
    Ledger(path)

    def attempt(i):
        try:
            reserve(Ledger(path), str(i), budget=260)
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    assert Ledger(path).snapshot("alice", 260)["available"] == 0


def test_global_budget_applies_across_accounts(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    reserve(ledger, "a", "alice", global_budget=260)
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "b", "bob", global_budget=260)


def test_uncertain_and_pending_survive_restart(tmp_path):
    path = str(tmp_path / "ledger.db")
    ledger = Ledger(path)
    reserve(ledger, "a")
    reserve(ledger, "b")
    ledger.finish_failed("a", uncertain=True)
    restarted = Ledger(path)
    assert restarted.snapshot("alice", 1000)["reserved"] == 520
    assert {row["state"] for row in restarted.recent("alice")} == {"pending", "uncertain"}
    assert restarted.settle("a", 10, 1, "primary") == 26


def test_known_failure_releases_and_cannot_be_charged(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    reserve(ledger)
    ledger.finish_failed("request-1", uncertain=False)
    assert ledger.snapshot("alice", 1000)["available"] == 1000
    with pytest.raises(InvalidUsage):
        ledger.settle("request-1", 10, 2, "primary")


@pytest.mark.parametrize("prompt,completion", [(True, 1), (1, 1.5), (-1, 1), (95, 10), (1, 11)])
def test_untrusted_usage_does_not_release_reservation(tmp_path, prompt, completion):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    reserve(ledger)
    with pytest.raises(InvalidUsage):
        ledger.settle("request-1", prompt, completion, "primary")
    assert ledger.snapshot("alice", 1000)["reserved"] == 260


def test_month_rollover_settles_in_original_period(tmp_path, monkeypatch):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(Ledger, "period", staticmethod(lambda: "2026-01"))
    reserve(ledger)
    monkeypatch.setattr(Ledger, "period", staticmethod(lambda: "2026-02"))
    ledger.settle("request-1", 20, 5, "primary")
    assert ledger.snapshot("alice", 1000)["available"] == 1000
    monkeypatch.setattr(Ledger, "period", staticmethod(lambda: "2026-01"))
    assert ledger.snapshot("alice", 1000)["charged"] == 70
