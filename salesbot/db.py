"""SQLite storage: products, stock items (auto-delivered goods), orders."""

import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    content TEXT NOT NULL,
    order_id INTEGER REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    username TEXT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | cancelled
    created_at TEXT NOT NULL,
    paid_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined_at TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0  -- 1 when the user blocked the bot
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_available ON stock(product_id, order_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        # Customers who ordered before the users table existed still get announcements.
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, joined_at)"
            " SELECT user_id, MAX(username), MIN(created_at) FROM orders GROUP BY user_id"
        )

    # ---------- settings ----------
    def get_setting(self, key: str, default: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_setting(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ---------- users ----------
    def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        self.conn.execute(
            "INSERT INTO users (id, username, first_name, joined_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET username = excluded.username,"
            " first_name = excluded.first_name, blocked = 0",
            (user_id, username, first_name, _now()),
        )

    def active_user_ids(self) -> list[int]:
        rows = self.conn.execute("SELECT id FROM users WHERE blocked = 0 ORDER BY id").fetchall()
        return [r["id"] for r in rows]

    def set_user_blocked(self, user_id: int) -> None:
        self.conn.execute("UPDATE users SET blocked = 1 WHERE id = ?", (user_id,))

    # ---------- products ----------
    def add_product(self, name: str, price: int, description: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO products (name, price, description) VALUES (?, ?, ?)",
            (name, price, description),
        )
        return cur.lastrowid

    def set_product_active(self, product_id: int, active: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE products SET active = ? WHERE id = ?", (int(active), product_id)
        )
        return cur.rowcount > 0

    def set_product_price(self, product_id: int, price: int) -> bool:
        cur = self.conn.execute(
            "UPDATE products SET price = ? WHERE id = ?", (price, product_id)
        )
        return cur.rowcount > 0

    def get_product(self, product_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            f"SELECT p.*, ({self._available_sql()}) AS stock FROM products p WHERE id = ?",
            (product_id,),
        ).fetchone()

    def list_products(self, only_active: bool = True) -> list[sqlite3.Row]:
        where = "WHERE active = 1" if only_active else ""
        return self.conn.execute(
            f"SELECT p.*, ({self._available_sql()}) AS stock FROM products p {where} ORDER BY id"
        ).fetchall()

    @staticmethod
    def _available_sql() -> str:
        return "SELECT COUNT(*) FROM stock s WHERE s.product_id = p.id AND s.order_id IS NULL"

    # ---------- stock ----------
    def add_stock(self, product_id: int, items: list[str]) -> int:
        self.conn.executemany(
            "INSERT INTO stock (product_id, content) VALUES (?, ?)",
            [(product_id, item) for item in items],
        )
        return len(items)

    def reserved_quantity(self, product_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM orders WHERE product_id = ? AND status = 'pending'",
            (product_id,),
        ).fetchone()
        return row[0]

    def sellable_quantity(self, product_id: int) -> int:
        """Stock not yet delivered and not held by a pending order."""
        product = self.get_product(product_id)
        if product is None:
            return 0
        return max(product["stock"] - self.reserved_quantity(product_id), 0)

    # ---------- orders ----------
    def create_order(
        self, prefix: str, user_id: int, username: str | None, product_id: int, quantity: int
    ) -> sqlite3.Row | None:
        """Create a pending order, or return None when not enough stock."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            product = self.get_product(product_id)
            if product is None or not product["active"]:
                self.conn.execute("ROLLBACK")
                return None
            if self.sellable_quantity(product_id) < quantity:
                self.conn.execute("ROLLBACK")
                return None
            code = self._new_code(prefix)
            cur = self.conn.execute(
                "INSERT INTO orders (code, user_id, username, product_id, quantity, amount, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, user_id, username, product_id, quantity, product["price"] * quantity, _now()),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get_order(cur.lastrowid)

    def _new_code(self, prefix: str) -> str:
        alphabet = string.ascii_uppercase + string.digits
        while True:
            code = prefix + "".join(secrets.choice(alphabet) for _ in range(6))
            if not self.conn.execute("SELECT 1 FROM orders WHERE code = ?", (code,)).fetchone():
                return code

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id = o.product_id"
            " WHERE o.id = ?",
            (order_id,),
        ).fetchone()

    def get_order_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id = o.product_id"
            " WHERE o.code = ?",
            (code.upper(),),
        ).fetchone()

    def list_user_orders(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id = o.product_id"
            " WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    def list_orders(self, status: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        sql = "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id = o.product_id"
        params: tuple = ()
        if status:
            sql += " WHERE o.status = ?"
            params = (status,)
        return self.conn.execute(sql + " ORDER BY o.id DESC LIMIT ?", (*params, limit)).fetchall()

    def mark_paid(self, order_id: int) -> list[str] | None:
        """Mark a pending order as paid and allocate stock to it.

        Returns the delivered items, or None if the order is not pending
        (already paid / cancelled) or stock ran out.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            order = self.get_order(order_id)
            if order is None or order["status"] != "pending":
                self.conn.execute("ROLLBACK")
                return None
            rows = self.conn.execute(
                "SELECT id, content FROM stock WHERE product_id = ? AND order_id IS NULL"
                " ORDER BY id LIMIT ?",
                (order["product_id"], order["quantity"]),
            ).fetchall()
            if len(rows) < order["quantity"]:
                self.conn.execute("ROLLBACK")
                return None
            self.conn.executemany(
                "UPDATE stock SET order_id = ? WHERE id = ?", [(order_id, r["id"]) for r in rows]
            )
            self.conn.execute(
                "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?", (_now(), order_id)
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return [r["content"] for r in rows]

    def delivered_items(self, order_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT content FROM stock WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
        return [r["content"] for r in rows]

    def cancel_order(self, order_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (order_id,),
        )
        return cur.rowcount > 0

    def expire_orders(self, minutes: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
            timespec="seconds"
        )
        expired = self.conn.execute(
            "SELECT * FROM orders WHERE status = 'pending' AND created_at < ?", (cutoff,)
        ).fetchall()
        for order in expired:
            self.cancel_order(order["id"])
        return expired

    # ---------- admin website ----------
    def update_product(self, product_id: int, name: str, price: int, description: str) -> bool:
        cur = self.conn.execute(
            "UPDATE products SET name = ?, price = ?, description = ? WHERE id = ?",
            (name, price, description, product_id),
        )
        return cur.rowcount > 0

    def list_stock(self, product_id: int, available: bool, limit: int = 500) -> list[sqlite3.Row]:
        cond = "order_id IS NULL" if available else "order_id IS NOT NULL"
        return self.conn.execute(
            f"SELECT s.id, s.content, s.order_id, o.code FROM stock s"
            f" LEFT JOIN orders o ON o.id = s.order_id"
            f" WHERE s.product_id = ? AND s.{cond} ORDER BY s.id DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()

    def stock_contents(self, product_id: int) -> set[str]:
        rows = self.conn.execute("SELECT content FROM stock WHERE product_id = ?", (product_id,))
        return {r["content"] for r in rows}

    def delete_stock_item(self, stock_id: int) -> int | None:
        """Delete an undelivered stock item; returns its product id."""
        row = self.conn.execute(
            "SELECT product_id FROM stock WHERE id = ? AND order_id IS NULL", (stock_id,)
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("DELETE FROM stock WHERE id = ?", (stock_id,))
        return row["product_id"]

    def revenue_summary(self) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT"
            " COALESCE(SUM(amount) FILTER (WHERE date(paid_at, 'localtime') = date('now', 'localtime')), 0) AS today,"
            " COUNT(*) FILTER (WHERE date(paid_at, 'localtime') = date('now', 'localtime')) AS today_orders,"
            " COALESCE(SUM(amount) FILTER (WHERE date(paid_at, 'localtime') >= date('now', 'localtime', '-6 days')), 0) AS week,"
            " COALESCE(SUM(amount) FILTER (WHERE strftime('%Y-%m', paid_at, 'localtime')"
            "   = strftime('%Y-%m', 'now', 'localtime')), 0) AS month,"
            " COALESCE(SUM(amount), 0) AS total,"
            " COUNT(*) AS total_orders"
            " FROM orders WHERE status = 'paid'"
        ).fetchone()

    def revenue_by_day(self, days: int) -> dict[str, tuple[int, int]]:
        """{'YYYY-MM-DD': (revenue, orders)} for paid orders in the last ``days`` local days."""
        rows = self.conn.execute(
            "SELECT date(paid_at, 'localtime') AS day, SUM(amount) AS revenue, COUNT(*) AS orders"
            " FROM orders WHERE status = 'paid'"
            " AND date(paid_at, 'localtime') >= date('now', 'localtime', ?)"
            " GROUP BY day",
            (f"-{days - 1} days",),
        ).fetchall()
        return {r["day"]: (r["revenue"], r["orders"]) for r in rows}

    def top_products(self, days: int, limit: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.name, SUM(o.quantity) AS sold, SUM(o.amount) AS revenue"
            " FROM orders o JOIN products p ON p.id = o.product_id"
            " WHERE o.status = 'paid' AND date(o.paid_at, 'localtime') >= date('now', 'localtime', ?)"
            " GROUP BY p.id ORDER BY revenue DESC LIMIT ?",
            (f"-{days - 1} days", limit),
        ).fetchall()

    def search_orders(
        self,
        status: str | None = None,
        query: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        where, params = [], []
        if status:
            where.append("o.status = ?")
            params.append(status)
        if query:
            where.append("(o.code LIKE ? OR o.username LIKE ? OR CAST(o.user_id AS TEXT) = ? OR p.name LIKE ?)")
            like = f"%{query.strip().lstrip('@')}%"
            params += [like, like, query.strip(), like]
        if date_from:
            where.append("date(o.created_at, 'localtime') >= ?")
            params.append(date_from)
        if date_to:
            where.append("date(o.created_at, 'localtime') <= ?")
            params.append(date_to)
        base = " FROM orders o JOIN products p ON p.id = o.product_id"
        if where:
            base += " WHERE " + " AND ".join(where)
        total = self.conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        sql = "SELECT o.*, p.name AS product_name" + base + " ORDER BY o.id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        return self.conn.execute(sql, params).fetchall(), total

    def count_users(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users WHERE blocked = 0").fetchone()[0]

    def stats(self) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT"
            " COUNT(*) FILTER (WHERE status = 'paid') AS paid_orders,"
            " COALESCE(SUM(amount) FILTER (WHERE status = 'paid'), 0) AS revenue,"
            " COUNT(*) FILTER (WHERE status = 'pending') AS pending_orders,"
            " COUNT(DISTINCT user_id) AS customers,"
            " (SELECT COUNT(*) FROM users WHERE blocked = 0) AS users"
            " FROM orders"
        ).fetchone()
