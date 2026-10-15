"""Database layer for Madrasa Premium Offline.
Pure sqlite3 (no web framework) so it can be tested and reused on its own.
All schema changes are additive: existing records are never dropped."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "madrasa.db"

MONTHS = ["جنوری", "فروری", "مارچ", "اپریل", "مئی", "جون",
          "جولائی", "اگست", "ستمبر", "اکتوبر", "نومبر", "دسمبر"]

PRESENT, ABSENT, LEAVE = "حاضر (Present)", "غیر حاضر (Absent)", "چھٹی (Leave)"
STUDENT_STATUSES = [PRESENT, ABSENT, LEAVE]
TEACHER_STATUSES = ["حاضر", "غیر حاضر", "چھٹی", "تاخیر"]

DONATION = "عطیہ / فنڈ"
MONTHLY = "ماہانہ فیس"
FEE_TYPES = [MONTHLY, "داخلہ فیس", "امتحانی فیس", "کتب / یونیفارم", DONATION]
EXPENSE_CATS = ["بجلی / گیس / پانی", "کرایہ", "کتب و سٹیشنری", "مرمت و دیکھ بھال",
                "راشن / کھانا", "تقریبات", "متفرق"]
STUDENT_STATE = ["فعال", "فارغ التحصیل", "خارج"]
BOARDS = ["", "وفاقی", "غیر وفاقی", "سرکاری / نجی اسکول"]
RESIDENCE = ["", "غیر مقیم (ڈے سکالر)", "مقیم (ہاسٹل)"]

ACTIVE = "(s.status IS NULL OR s.status='' OR s.status='فعال')"

DEFAULT_SETTINGS = {
    "institution_type": "مدرسہ",
    "principal": "",
    "pass_mark": "50",
    "currency": "روپے",
    "receipt_note": "شکریہ — یہ رسید ادارے کے ریکارڈ کا حصہ ہے۔",
    "password_hash": "",
    "secret": "",
}


# ---------------------------------------------------------------- connection
def connect() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def db():
    c = connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


# -------------------------------------------------------------------- schema
def ensure_schema() -> None:
    c = connect()
    cur = c.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, father_name TEXT, phone TEXT,
        previous_class TEXT, guardian_name TEXT, guardian_phone TEXT, admission_class TEXT);
    CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, subject TEXT, phone TEXT, salary INTEGER);
    CREATE TABLE IF NOT EXISTS fees (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, amount INTEGER, month TEXT, payment_type TEXT);
    CREATE TABLE IF NOT EXISTS results (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, test_name TEXT, total_marks INTEGER, obtained_marks INTEGER);
    CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, date TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS schools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, address TEXT DEFAULT '', phone TEXT DEFAULT '', logo TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS teacher_attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'حاضر', UNIQUE(teacher_id,date));
    CREATE TABLE IF NOT EXISTS classes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, teacher_id INTEGER, description TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, category TEXT, amount INTEGER NOT NULL DEFAULT 0, note TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS teacher_salaries (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL, month TEXT, year INTEGER, amount INTEGER NOT NULL DEFAULT 0, paid_date TEXT, note TEXT DEFAULT '');
    """)

    def add(table, col, decl):
        have = [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
        if col not in have:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    for col, decl in [("birth_date", "TEXT"), ("photo", "TEXT"), ("roll_number", "TEXT"), ("gender", "TEXT"),
                      ("admission_date", "TEXT"), ("school_id", "INTEGER DEFAULT 1"), ("address", "TEXT"),
                      ("bform", "TEXT"), ("board", "TEXT"), ("residence", "TEXT"), ("status", "TEXT DEFAULT 'فعال'"),
                      ("monthly_fee", "INTEGER"), ("notes", "TEXT")]:
        add("students", col, decl)
    for col, decl in [("qualification", "TEXT"), ("joining_date", "TEXT"), ("school_id", "INTEGER DEFAULT 1"),
                      ("address", "TEXT"), ("cnic", "TEXT")]:
        add("teachers", col, decl)
    for col, decl in [("school_id", "INTEGER DEFAULT 1"), ("paid_date", "TEXT"), ("year", "INTEGER"), ("note", "TEXT")]:
        add("fees", col, decl)
    for col, decl in [("school_id", "INTEGER DEFAULT 1"), ("grade", "TEXT"), ("exam_date", "TEXT"), ("subject", "TEXT")]:
        add("results", col, decl)
    add("attendance", "school_id", "INTEGER DEFAULT 1")
    add("classes", "monthly_fee", "INTEGER DEFAULT 0")

    cur.execute("INSERT OR IGNORE INTO schools(id,name,address,phone) VALUES(1,'میرا ادارہ','','')")
    for k, v in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", (k, v))
    if not cur.execute("SELECT value FROM app_settings WHERE key='secret'").fetchone()[0]:
        cur.execute("UPDATE app_settings SET value=? WHERE key='secret'", (secrets.token_hex(32),))
    cur.execute("CREATE INDEX IF NOT EXISTS ix_att_student_date ON attendance(student_id,date)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_fees_student ON fees(student_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_results_student ON results(student_id)")
    c.commit()
    c.close()


# ------------------------------------------------------------------ settings
def get_settings(c) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in c.execute("SELECT key,value FROM app_settings")})
    return s


def set_setting(c, key: str, value: str) -> None:
    c.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, value))


def get_school(c) -> sqlite3.Row:
    return c.execute("SELECT * FROM schools WHERE id=1").fetchone()


# ------------------------------------------------------------------ security
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(test, h)


def session_token(settings: dict) -> str:
    return hmac.new(settings["secret"].encode(), b"madrasa-session:" + settings["password_hash"].encode(),
                    hashlib.sha256).hexdigest()


# ------------------------------------------------------------------- helpers
def grade_for(pct_value: float, pass_mark: float = 50) -> str:
    if pct_value < pass_mark:
        return "F"
    for limit, g in ((90, "A+"), (80, "A"), (70, "B"), (60, "C")):
        if pct_value >= limit:
            return g
    return "D"


def pct(obtained, total) -> float:
    return round((obtained or 0) * 100 / total, 1) if total else 0.0


def pass_mark_of(settings: dict) -> float:
    try:
        return float(settings.get("pass_mark") or 50)
    except ValueError:
        return 50.0


def all_classes(c) -> list[str]:
    rows = c.execute("""SELECT name FROM classes
        UNION SELECT DISTINCT admission_class FROM students WHERE admission_class IS NOT NULL AND TRIM(admission_class)!=''
        ORDER BY 1""").fetchall()
    return [r[0] for r in rows]


def today() -> str:
    return date.today().isoformat()


def safe_int(v, default: int = 0) -> int:
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


def month_range(n: int = 6):
    """Last n (year, month) pairs ending with the current month."""
    t = date.today()
    y, m = t.year, t.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))
