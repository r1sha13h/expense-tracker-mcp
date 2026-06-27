from fastmcp import FastMCP
import os
import json
import csv
import io
from datetime import datetime
import libsql_experimental as libsql

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

mcp = FastMCP("ExpenseTracker")

def get_conn():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

def _load_categories():
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# Loaded once at module import; categories.json is static and bundled.
CATEGORIES = _load_categories()

# ---- Validation helpers -------------------------------------------------
# Each returns an error string if invalid, or None if valid.

def _validate_date(date):
    try:
        datetime.strptime(date, "%Y-%m-%d")
        return None
    except (ValueError, TypeError):
        return f"Invalid date '{date}'. Expected format YYYY-MM-DD (e.g., 2026-06-27)."

def _validate_amount(amount):
    try:
        value = float(amount)
    except (ValueError, TypeError):
        return f"Invalid amount '{amount}'. Must be a number."
    if value <= 0:
        return f"Invalid amount {amount}. Must be greater than 0."
    return None

def _validate_category(category, subcategory):
    if category not in CATEGORIES:
        return (f"Invalid category '{category}'. Valid categories: "
                f"{', '.join(sorted(CATEGORIES))}.")
    if subcategory:
        valid_subs = CATEGORIES[category]
        if subcategory not in valid_subs:
            return (f"Invalid subcategory '{subcategory}' for category '{category}'. "
                    f"Valid subcategories: {', '.join(valid_subs)}.")
    return None

def init_db():
    try:
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        print("Database initialized successfully")
    except Exception as e:
        print(f"Database initialization error: {e}")
        raise

init_db()

@mcp.tool()
def add_expense(date, amount, category, subcategory="", note=""):
    '''Add a new expense entry. Validates date (YYYY-MM-DD), amount (>0),
    and category/subcategory against the taxonomy before inserting.'''
    err = (_validate_date(date)
           or _validate_amount(amount)
           or _validate_category(category, subcategory))
    if err:
        return {"status": "error", "message": err}
    try:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note, created_at) "
            "VALUES (?,?,?,?,?, datetime('now'))",
            (date, float(amount), category, subcategory, note)
        )
        conn.commit()
        return {"status": "success", "id": cur.lastrowid, "message": "Expense added successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
def list_expenses(start_date, end_date, category=None, subcategory=None,
                  min_amount=None, max_amount=None, note_contains=None, limit=None):
    '''List expenses within an inclusive date range, with optional filters:
    category, subcategory, min_amount/max_amount, note_contains (substring
    search), and limit (max rows returned).'''
    try:
        conn = get_conn()
        query = """
            SELECT id, date, amount, category, subcategory, note, created_at
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """
        params = [start_date, end_date]
        if category:
            query += " AND category = ?"
            params.append(category)
        if subcategory:
            query += " AND subcategory = ?"
            params.append(subcategory)
        if min_amount is not None:
            query += " AND amount >= ?"
            params.append(min_amount)
        if max_amount is not None:
            query += " AND amount <= ?"
            params.append(max_amount)
        if note_contains:
            query += " AND note LIKE ?"
            params.append(f"%{note_contains}%")
        query += " ORDER BY date DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cur = conn.execute(query, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error listing expenses: {str(e)}"}

@mcp.tool()
def get_expense(id):
    '''Fetch a single expense by its id.'''
    try:
        conn = get_conn()
        cur = conn.execute(
            "SELECT id, date, amount, category, subcategory, note, created_at "
            "FROM expenses WHERE id = ?",
            (id,)
        )
        row = cur.fetchone()
        if row is None:
            return {"status": "error", "message": f"No expense found with id {id}."}
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    except Exception as e:
        return {"status": "error", "message": f"Error fetching expense: {str(e)}"}

@mcp.tool()
def update_expense(id, date=None, amount=None, category=None, subcategory=None, note=None):
    '''Update an existing expense by id. Only the fields you pass are changed;
    omit a field to leave it unchanged. Validates any changed field the same
    way add_expense does.'''
    try:
        conn = get_conn()
        cur = conn.execute(
            "SELECT id, date, amount, category, subcategory, note FROM expenses WHERE id = ?",
            (id,)
        )
        row = cur.fetchone()
        if row is None:
            return {"status": "error", "message": f"No expense found with id {id}."}
        current = dict(zip([d[0] for d in cur.description], row))

        new_date = date if date is not None else current["date"]
        new_amount = amount if amount is not None else current["amount"]
        new_category = category if category is not None else current["category"]
        new_subcategory = subcategory if subcategory is not None else current["subcategory"]
        new_note = note if note is not None else current["note"]

        # Validate only what's relevant to the requested change.
        err = None
        if date is not None:
            err = _validate_date(new_date)
        if not err and amount is not None:
            err = _validate_amount(new_amount)
        if not err and (category is not None or subcategory is not None):
            err = _validate_category(new_category, new_subcategory)
        if err:
            return {"status": "error", "message": err}

        conn.execute(
            "UPDATE expenses SET date=?, amount=?, category=?, subcategory=?, note=? WHERE id=?",
            (new_date, float(new_amount), new_category, new_subcategory, new_note, id)
        )
        conn.commit()
        return {"status": "success", "id": id, "message": "Expense updated successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Error updating expense: {str(e)}"}

@mcp.tool()
def delete_expense(id):
    '''Delete an expense by its id. Returns an error if no such id exists.'''
    try:
        conn = get_conn()
        cur = conn.execute("SELECT id FROM expenses WHERE id = ?", (id,))
        if cur.fetchone() is None:
            return {"status": "error", "message": f"No expense found with id {id}."}
        conn.execute("DELETE FROM expenses WHERE id = ?", (id,))
        conn.commit()
        return {"status": "success", "id": id, "message": "Expense deleted successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Error deleting expense: {str(e)}"}

@mcp.tool()
def summarize(start_date, end_date, category=None):
    '''Summarize expenses by category within an inclusive date range.'''
    try:
        conn = get_conn()
        query = """
            SELECT category, SUM(amount) AS total_amount, COUNT(*) as count
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """
        params = [start_date, end_date]
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " GROUP BY category ORDER BY total_amount DESC"
        cur = conn.execute(query, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error summarizing expenses: {str(e)}"}

@mcp.tool()
def export_expenses(start_date, end_date, format="csv"):
    '''Export expenses within an inclusive date range as a backup.
    format: "csv" (default) or "json". Returns the serialized content as text.'''
    fmt = (format or "csv").lower()
    if fmt not in ("csv", "json"):
        return {"status": "error", "message": f"Invalid format '{format}'. Use 'csv' or 'json'."}
    try:
        conn = get_conn()
        cur = conn.execute(
            """
            SELECT id, date, amount, category, subcategory, note, created_at
            FROM expenses
            WHERE date BETWEEN ? AND ?
            ORDER BY date ASC, id ASC
            """,
            (start_date, end_date)
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()

        if fmt == "json":
            data = [dict(zip(cols, r)) for r in rows]
            content = json.dumps(data, indent=2, ensure_ascii=False)
        else:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(cols)
            writer.writerows(rows)
            content = buf.getvalue()

        return {"status": "success", "format": fmt, "count": len(rows), "content": content}
    except Exception as e:
        return {"status": "error", "message": f"Error exporting expenses: {str(e)}"}

@mcp.tool()
def get_schema():
    '''Returns the database schema and all valid expense categories with subcategories.'''
    schema = {
        "table": "expenses",
        "columns": {
            "id": "INTEGER, primary key, auto-incremented",
            "date": "TEXT, required, format YYYY-MM-DD",
            "amount": "REAL, required, numeric value in INR, must be > 0",
            "category": "TEXT, required, must be one of the valid categories",
            "subcategory": "TEXT, optional, must be valid for the chosen category",
            "note": "TEXT, optional, free-form description",
            "created_at": "TEXT, auto-set by the server (UTC), insertion timestamp; do not pass this in"
        }
    }
    if CATEGORIES:
        schema["categories"] = CATEGORIES
    else:
        schema["categories_error"] = "Could not load categories.json"
    return schema

@mcp.resource("expense:///categories", mime_type="application/json")
def categories():
    try:
        default_categories = {
            "categories": [
                "Food & Dining", "Transportation", "Shopping", "Entertainment",
                "Bills & Utilities", "Healthcare", "Travel", "Education", "Business", "Other"
            ]
        }
        try:
            with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return json.dumps(default_categories, indent=2)
    except Exception as e:
        return f'{{"error": "Could not load categories: {str(e)}"}}'

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
