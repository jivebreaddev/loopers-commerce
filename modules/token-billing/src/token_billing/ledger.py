import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class BudgetExceeded(Exception):
    pass


class InvalidUsage(Exception):
    pass


class Ledger:
    """Persistent monthly token charges, in integer micro-USD (1 USD = 1e6).

    Pending/uncertain reservations continue to consume budget across restarts.
    BEGIN IMMEDIATE serializes budget checks and writes across connections.
    """

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS token_ledger (
                    request_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    period TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('pending', 'settled', 'released', 'uncertain')),
                    reserved INTEGER NOT NULL CHECK(reserved >= 0),
                    charged INTEGER NOT NULL DEFAULT 0 CHECK(charged >= 0),
                    prompt_cap INTEGER NOT NULL,
                    completion_cap INTEGER NOT NULL,
                    input_price INTEGER NOT NULL,
                    output_price INTEGER NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    backend TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ledger_period_account
                    ON token_ledger(period, account);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(token_ledger)")}
            for name in ("department", "model"):
                if name not in columns:
                    db.execute(f"ALTER TABLE token_ledger ADD COLUMN {name} TEXT")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def period() -> str:
        return datetime.now(UTC).strftime("%Y-%m")

    @staticmethod
    def _used(db, period, account=None, department=None) -> int:
        query = """SELECT COALESCE(SUM(CASE
            WHEN state = 'settled' THEN charged
            WHEN state IN ('pending', 'uncertain') THEN reserved ELSE 0 END), 0)
            FROM token_ledger WHERE period = ?"""
        params = [period]
        if account is not None:
            query += " AND account = ?"
            params.append(account)
        if department is not None:
            query += " AND department = ?"
            params.append(department)
        return db.execute(query, params).fetchone()[0]

    def reserve(
        self,
        request_id: str,
        account: str,
        budget: int,
        global_budget: int,
        context_limit: int,
        max_tokens: int,
        input_price: int,
        output_price: int,
        department: str | None = None,
        department_budget: int | None = None,
    ) -> int:
        if min(budget, global_budget, input_price, output_price) < 0:
            raise ValueError("Budget and prices must be non-negative")
        if not 0 < max_tokens < context_limit:
            raise ValueError("Invalid token limits")
        # Conservative prompt bound, independent of tokenizer/chat-template differences.
        reserved = context_limit * input_price + max_tokens * output_price
        period = self.period()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if reserved > 0 and (
                self._used(db, period, account) + reserved > budget
                or self._used(db, period) + reserved > global_budget
                or (
                    department is not None
                    and department_budget is not None
                    and self._used(db, period, department=department) + reserved > department_budget
                )
            ):
                raise BudgetExceeded
            db.execute(
                """INSERT INTO token_ledger
                (request_id, account, period, state, reserved, prompt_cap, completion_cap,
                 input_price, output_price, created_at, department)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    account,
                    period,
                    reserved,
                    context_limit,
                    max_tokens,
                    input_price,
                    output_price,
                    datetime.now(UTC).isoformat(),
                    department,
                ),
            )
        return reserved

    def settle(
        self,
        request_id: str,
        prompt: int,
        completion: int,
        backend: str,
        *,
        input_price: int | None = None,
        output_price: int | None = None,
        model: str | None = None,
    ) -> int:
        if type(prompt) is not int or type(completion) is not int:
            raise InvalidUsage("Token counts must be integers")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM token_ledger WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise InvalidUsage("Reservation missing")
            rate_in = row["input_price"] if input_price is None else input_price
            rate_out = row["output_price"] if output_price is None else output_price
            if not (0 <= rate_in <= row["input_price"] and 0 <= rate_out <= row["output_price"]):
                raise InvalidUsage("Settlement tariff exceeds reservation")
            if row["state"] == "settled":
                if (
                    row["prompt_tokens"] != prompt
                    or row["completion_tokens"] != completion
                    or row["charged"] != prompt * rate_in + completion * rate_out
                ):
                    raise InvalidUsage("Settlement conflicts with existing usage")
                return row["charged"]
            if row["state"] not in ("pending", "uncertain"):
                raise InvalidUsage("Reservation already released")
            if (
                min(prompt, completion) < 0
                or prompt + completion > row["prompt_cap"]
                or completion > row["completion_cap"]
            ):
                raise InvalidUsage("Usage exceeds reservation limits")
            charged = prompt * rate_in + completion * rate_out
            db.execute(
                """UPDATE token_ledger SET state = 'settled', charged = ?,
                prompt_tokens = ?, completion_tokens = ?, backend = ?, model = ?,
                input_price = ?, output_price = ? WHERE request_id = ?""",
                (charged, prompt, completion, backend, model, rate_in, rate_out, request_id),
            )
            return charged

    def finish_failed(self, request_id: str, uncertain: bool):
        with self._connection() as db:
            db.execute(
                """UPDATE token_ledger SET state = ?
                WHERE request_id = ? AND state = 'pending'""",
                ("uncertain" if uncertain else "released", request_id),
            )

    def snapshot(self, account: str, budget: int) -> dict:
        period = self.period()
        with self._connection() as db:
            row = db.execute(
                """SELECT
                COALESCE(SUM(CASE WHEN state = 'settled' THEN charged ELSE 0 END), 0) charged,
                COALESCE(SUM(CASE WHEN state IN ('pending', 'uncertain')
                    THEN reserved ELSE 0 END), 0) reserved,
                COALESCE(SUM(prompt_tokens), 0) prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) completion_tokens
                FROM token_ledger WHERE account = ? AND period = ?""",
                (account, period),
            ).fetchone()
            return {
                "account": account,
                "period": period,
                "currency": "USD",
                "unit": "micro_usd",
                "budget": budget,
                **dict(row),
                "available": max(0, budget - row["charged"] - row["reserved"]),
            }

    def recent(self, account: str) -> list[dict]:
        with self._connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT request_id, period, state, reserved, charged, prompt_tokens,
                completion_tokens, backend, model, department, created_at FROM token_ledger
                WHERE account = ? ORDER BY created_at DESC LIMIT 20""",
                    (account,),
                )
            ]

    def global_snapshot(self, budget: int) -> dict:
        with self._connection() as db:
            used = self._used(db, self.period())
        return {"committed": used, "available": max(0, budget - used), "budget": budget}

    def department_snapshot(self, department: str, budget: int) -> dict:
        with self._connection() as db:
            used = self._used(db, self.period(), department=department)
        return {
            "department": department,
            "period": self.period(),
            "committed": used,
            "budget": budget,
            "available": max(0, budget - used),
        }
