"""Madrasa / School Management System — Premium Offline Edition (v2).
FastAPI + SQLite + Jinja2. Runs fully offline on one computer."""
from __future__ import annotations

import calendar
import csv
import io
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db as D
from db import (ACTIVE, DONATION, MONTHLY, MONTHS, PRESENT, ABSENT, LEAVE, db, grade_for, pct, safe_int)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = STATIC_DIR / "uploads"
BACKUP_DIR = BASE_DIR / "backups"
for p in (STATIC_DIR, UPLOAD_DIR, BACKUP_DIR):
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Madrasa Premium Offline")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["money"] = lambda v: f"{int(v or 0):,}"
templates.env.filters["pctfmt"] = lambda v: (f"{v:.1f}".rstrip("0").rstrip(".") if v is not None else "0")

PRES = "a.status LIKE 'حاضر%'"          # attendance row alias must be `a`
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMG = 5 * 1024 * 1024


# ------------------------------------------------------------------ plumbing
def render(request: Request, name: str, **ctx):
    with db() as c:
        st = D.get_settings(c)
        school = D.get_school(c)
    ctx.update(request=request, school=school, st=st, MONTHS=MONTHS, today=D.today(),
               cur_month=MONTHS[date.today().month - 1], cur_year=date.today().year,
               ok=request.query_params.get("ok", ""), err=request.query_params.get("err", ""),
               rid=request.query_params.get("rid", ""))
    ctx.setdefault("active", "")
    return templates.TemplateResponse(request, name, ctx)


def go(url: str, ok: str = "", err: str = "", **extra):
    parts = []
    if ok:
        parts.append("ok=" + quote(ok))
    if err:
        parts.append("err=" + quote(err))
    for k, v in extra.items():
        parts.append(f"{k}={quote(str(v))}")
    if parts:
        url += ("&" if "?" in url else "?") + "&".join(parts)
    return RedirectResponse(url=url, status_code=303)


def clean(form, key: str) -> str:
    v = form.get(key)
    return v.strip() if isinstance(v, str) else ""


async def save_image(upload, prefix: str) -> str:
    """Validate and store an uploaded image; returns path relative to /static or ''."""
    if upload is None or not getattr(upload, "filename", ""):
        return ""
    ext = Path(upload.filename).suffix.lower()
    if ext not in IMG_EXT:
        raise ValueError("صرف JPG، PNG یا WEBP تصویر اپلوڈ کریں")
    data = await upload.read()
    if len(data) > MAX_IMG:
        raise ValueError("تصویر 5 MB سے چھوٹی ہونی چاہیے")
    if not data:
        return ""
    name = f"{prefix}_{datetime.now():%Y%m%d%H%M%S%f}{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    return f"uploads/{name}"


def make_backup(tag: str = "manual") -> str:
    BACKUP_DIR.mkdir(exist_ok=True)
    name = f"madrasa_{tag}_{datetime.now():%Y%m%d_%H%M%S}.db"
    src = D.connect()
    dst = sqlite3.connect(str(BACKUP_DIR / name))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return name


def auto_backup() -> None:
    """One automatic backup per day; keeps the newest 14 automatic copies."""
    try:
        stamp = f"madrasa_auto_{date.today():%Y%m%d}"
        if not any(BACKUP_DIR.glob(stamp + "*.db")):
            make_backup("auto")
        autos = sorted(BACKUP_DIR.glob("madrasa_auto_*.db"))
        for old in autos[:-14]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def list_backups(limit: int = 12):
    files = sorted(BACKUP_DIR.glob("*.db"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    return [{"name": f.name, "size_kb": max(1, f.stat().st_size // 1024),
             "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")} for f in files]


def next_roll(c, class_name: str) -> str:
    rows = c.execute("SELECT roll_number FROM students WHERE admission_class=?", (class_name,)).fetchall()
    nums = [safe_int(r[0], 0) for r in rows if (r[0] or "").strip().isdigit()]
    return str(max(nums) + 1) if nums else "1"


def rank_rows(rows, key="percent", group="admission_class"):
    """Adds `position` (ties share a position) inside each class for rows that have marks."""
    by = {}
    for r in rows:
        by.setdefault(r[group], []).append(r)
    for grp in by.values():
        scored = sorted([r for r in grp if r["total"]], key=lambda r: r[key], reverse=True)
        pos, prev, i = 0, None, 0
        for r in scored:
            i += 1
            if r[key] != prev:
                pos, prev = i, r[key]
            r["position"] = pos
    for r in rows:
        r.setdefault("position", None)
    return rows


# ---------------------------------------------------------------------- init
D.ensure_schema()
auto_backup()


# ------------------------------------------------------------------ security
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path == "/login":
        return await call_next(request)
    with db() as c:
        st = D.get_settings(c)
    if st["password_hash"] and request.cookies.get("ms") != D.session_token(st):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    with db() as c:
        st = D.get_settings(c)
    if st["password_hash"] and D.check_password(clean(form, "password"), st["password_hash"]):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("ms", D.session_token(st), httponly=True, samesite="lax", max_age=60 * 60 * 12)
        return resp
    if not st["password_hash"]:
        return RedirectResponse("/", status_code=303)
    return go("/login", err="پاس ورڈ درست نہیں")


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("ms")
    return resp


# ----------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    t = date.today()
    ym, md = t.strftime("%Y-%m"), t.strftime("%m-%d")
    with db() as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        n_students = one(f"SELECT COUNT(*) FROM students s WHERE {ACTIVE}")
        n_teachers = one("SELECT COUNT(*) FROM teachers")
        n_classes = len(D.all_classes(c))
        fee_total = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE payment_type!=?", DONATION)
        hadiya_total = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE payment_type=?", DONATION)
        m_fees = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE payment_type!=? AND substr(paid_date,1,7)=?", DONATION, ym)
        m_don = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE payment_type=? AND substr(paid_date,1,7)=?", DONATION, ym)
        m_exp = one("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE substr(date,1,7)=?", ym) + \
            one("SELECT COALESCE(SUM(amount),0) FROM teacher_salaries WHERE substr(paid_date,1,7)=?", ym)

        att = c.execute("""SELECT s.name, a.status FROM attendance a JOIN students s ON s.id=a.student_id
                           WHERE a.date=? ORDER BY s.name""", (D.today(),)).fetchall()
        present = [r["name"] for r in att if r["status"].startswith("حاضر")]
        absent = [r["name"] for r in att if r["status"].startswith("غیر")]
        leave = [r["name"] for r in att if r["status"].startswith("چھٹی")]
        marked = len(att)
        att_pct = round(len(present) * 100 / marked) if marked else 0

        with_res = [r[0] for r in c.execute(f"""SELECT DISTINCT s.name FROM students s JOIN results r ON r.student_id=s.id
                                                WHERE {ACTIVE} ORDER BY s.name""")]
        without_res = [r[0] for r in c.execute(f"""SELECT s.name FROM students s WHERE {ACTIVE}
                       AND s.id NOT IN (SELECT DISTINCT student_id FROM results) ORDER BY s.name""")]

        series = []
        for y, m in D.month_range(6):
            key = f"{y}-{m:02d}"
            inc = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE substr(paid_date,1,7)=?", key)
            exp = one("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE substr(date,1,7)=?", key) + \
                one("SELECT COALESCE(SUM(amount),0) FROM teacher_salaries WHERE substr(paid_date,1,7)=?", key)
            series.append({"label": MONTHS[m - 1], "income": inc, "expense": exp})

        strength = [{"label": r[0], "value": r[1]} for r in c.execute(
            f"""SELECT COALESCE(NULLIF(TRIM(admission_class),''),'—'), COUNT(*) FROM students s WHERE {ACTIVE}
                GROUP BY 1 ORDER BY 1""")]
        recent_fees = c.execute("""SELECT f.*, s.name student_name FROM fees f LEFT JOIN students s ON s.id=f.student_id
                                   ORDER BY f.id DESC LIMIT 6""").fetchall()
        notices = c.execute("SELECT * FROM notices ORDER BY id DESC LIMIT 4").fetchall()
        birthdays = c.execute(f"""SELECT name, admission_class FROM students s WHERE {ACTIVE}
                                  AND length(birth_date)=10 AND substr(birth_date,6,5)=?""", (md,)).fetchall()
    return render(request, "dashboard.html", active="dashboard", n_students=n_students, n_teachers=n_teachers,
                  n_classes=n_classes, fee_total=fee_total, hadiya_total=hadiya_total, m_fees=m_fees, m_don=m_don,
                  m_exp=m_exp, present=present, absent=absent, leave=leave, marked=marked, att_pct=att_pct,
                  with_res=with_res, without_res=without_res, series=series, strength=strength,
                  recent_fees=recent_fees, notices=notices, birthdays=birthdays)


# -------------------------------------------------------------------- search
@app.get("/search", response_class=HTMLResponse)
async def global_search(request: Request, q: str = ""):
    like = f"%{q.strip()}%"
    with db() as c:
        students = c.execute("""SELECT * FROM students WHERE name LIKE ? OR father_name LIKE ? OR phone LIKE ?
                                OR admission_class LIKE ? OR roll_number LIKE ? ORDER BY name LIMIT 100""",
                             (like,) * 5).fetchall()
        teachers = c.execute("SELECT * FROM teachers WHERE name LIKE ? OR subject LIKE ? OR phone LIKE ? LIMIT 100",
                             (like,) * 3).fetchall()
        fees = c.execute("""SELECT f.*, s.name student_name FROM fees f LEFT JOIN students s ON s.id=f.student_id
                            WHERE s.name LIKE ? OR f.month LIKE ? OR f.payment_type LIKE ? ORDER BY f.id DESC LIMIT 100""",
                         (like,) * 3).fetchall()
    return render(request, "search.html", active="", query=q, students=students, teachers=teachers, fees=fees)


# ------------------------------------------------------------------ students
def _student_form_values(form) -> dict:
    return {
        "name": clean(form, "student_name"), "father_name": clean(form, "father_name"),
        "birth_date": clean(form, "birth_date"), "phone": clean(form, "phone"),
        "previous_class": clean(form, "prev_class"), "guardian_name": clean(form, "guardian_name"),
        "guardian_phone": clean(form, "guardian_phone"), "admission_class": clean(form, "admission_class"),
        "roll_number": clean(form, "roll_number"), "gender": clean(form, "gender"),
        "address": clean(form, "address"), "bform": clean(form, "bform"), "board": clean(form, "board"),
        "residence": clean(form, "residence"), "notes": clean(form, "notes"),
        "monthly_fee": safe_int(form.get("monthly_fee")) if clean(form, "monthly_fee") != "" else None,
    }


@app.get("/students", response_class=HTMLResponse)
async def students_list(request: Request, q: str = "", class_name: str = "", state: str = ""):
    sql, args = "SELECT s.* FROM students s WHERE 1=1", []
    if state == "all":
        pass
    elif state:
        sql += " AND s.status=?"
        args.append(state)
    else:
        sql += f" AND {ACTIVE}"
    if class_name:
        sql += " AND s.admission_class=?"
        args.append(class_name)
    if q.strip():
        sql += " AND (s.name LIKE ? OR s.father_name LIKE ? OR s.phone LIKE ? OR s.roll_number LIKE ?)"
        args += [f"%{q.strip()}%"] * 4
    sql += " ORDER BY s.admission_class, CAST(s.roll_number AS INTEGER), s.name"
    with db() as c:
        rows = c.execute(sql, args).fetchall()
        classes = D.all_classes(c)
    return render(request, "students.html", active="students", students=rows, classes=classes,
                  q=q, class_name=class_name, state=state, states=D.STUDENT_STATE)


@app.get("/students/new", response_class=HTMLResponse)
async def student_new_page(request: Request, class_name: str = ""):
    with db() as c:
        classes = D.all_classes(c)
        roll = next_roll(c, class_name) if class_name else ""
    return render(request, "student_form.html", active="admission", s=None, classes=classes, roll=roll,
                  default_class=class_name, boards=D.BOARDS, residences=D.RESIDENCE, states=D.STUDENT_STATE)


@app.post("/students/new")
async def student_create(request: Request):
    form = await request.form()
    v = _student_form_values(form)
    if not v["name"] or not v["father_name"] or not v["admission_class"]:
        return go("/students/new", err="نام، ولدیت اور درجہ لازمی ہیں")
    try:
        v["photo"] = await save_image(form.get("photo"), "student")
    except ValueError as e:
        return go("/students/new", err=str(e))
    with db() as c:
        if not v["roll_number"]:
            v["roll_number"] = next_roll(c, v["admission_class"])
        v["admission_date"] = D.today()
        v["status"] = "فعال"
        cols = ",".join(v)
        c.execute(f"INSERT INTO students({cols}) VALUES({','.join('?' * len(v))})", list(v.values()))
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return go(f"/students/{sid}", ok="داخلہ محفوظ ہو گیا")


@app.get("/students/export.csv")
async def students_export():
    with db() as c:
        rows = c.execute("""SELECT id, roll_number, name, father_name, admission_class, gender, birth_date, phone,
                            guardian_name, guardian_phone, address, bform, board, residence, admission_date, status
                            FROM students ORDER BY admission_class, CAST(roll_number AS INTEGER), name""").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["آئی ڈی", "رول نمبر", "نام", "ولدیت", "درجہ", "جنس", "تاریخ پیدائش", "فون", "سرپرست", "سرپرست فون",
                "پتہ", "ب فارم", "بورڈ", "رہائش", "تاریخ داخلہ", "حالت"])
    for r in rows:
        w.writerow([("" if x is None else x) for x in tuple(r)])
    data = "\ufeff" + buf.getvalue()
    return Response(content=data.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=students.csv"})


@app.get("/students/promote", response_class=HTMLResponse)
async def promote_page(request: Request, from_class: str = ""):
    with db() as c:
        classes = D.all_classes(c)
        rows = c.execute(f"SELECT * FROM students s WHERE {ACTIVE} AND s.admission_class=? ORDER BY s.name",
                         (from_class,)).fetchall() if from_class else []
    return render(request, "promote.html", active="students", classes=classes, from_class=from_class, students=rows)


@app.post("/students/promote")
async def promote_save(request: Request):
    form = await request.form()
    ids = [safe_int(i) for i in form.getlist("ids")]
    to_class, mode = clean(form, "to_class"), clean(form, "mode")
    if not ids or (mode == "move" and not to_class):
        return go("/students/promote", err="طلبہ اور نیا درجہ منتخب کریں")
    with db() as c:
        for i in ids:
            if mode == "graduate":
                c.execute("UPDATE students SET previous_class=admission_class, status='فارغ التحصیل' WHERE id=?", (i,))
            else:
                c.execute("UPDATE students SET previous_class=admission_class, admission_class=? WHERE id=?",
                          (to_class, i))
    return go("/students", ok=f"{len(ids)} طلبہ کا درجہ اپڈیٹ ہو گیا")


@app.get("/students/cards", response_class=HTMLResponse)
async def id_cards(request: Request, class_name: str = ""):
    with db() as c:
        rows = c.execute(f"SELECT * FROM students s WHERE {ACTIVE} AND (?='' OR s.admission_class=?) "
                         "ORDER BY s.admission_class, CAST(s.roll_number AS INTEGER), s.name", (class_name, class_name)).fetchall()
    return render(request, "id_cards.html", students=rows, class_name=class_name)


@app.get("/students/{student_id}", response_class=HTMLResponse)
async def student_profile(request: Request, student_id: int):
    with db() as c:
        s = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if not s:
            return go("/students", err="طالب علم نہیں ملا")
        fees = c.execute("SELECT * FROM fees WHERE student_id=? ORDER BY id DESC", (student_id,)).fetchall()
        results = c.execute("SELECT * FROM results WHERE student_id=? ORDER BY id DESC", (student_id,)).fetchall()
        exams = [r[0] for r in c.execute("SELECT DISTINCT test_name FROM results WHERE student_id=? ORDER BY test_name",
                                         (student_id,))]
        att = c.execute("SELECT * FROM attendance WHERE student_id=? ORDER BY date DESC", (student_id,)).fetchall()
        pm = D.pass_mark_of(D.get_settings(c))
    n_att = len(att)
    n_p = sum(1 for a in att if a["status"].startswith("حاضر"))
    n_a = sum(1 for a in att if a["status"].startswith("غیر"))
    n_l = n_att - n_p - n_a
    paid = sum(f["amount"] or 0 for f in fees if f["payment_type"] != DONATION)
    results_view = [dict(r, pct=pct(r["obtained_marks"], r["total_marks"]),
                         grade=grade_for(pct(r["obtained_marks"], r["total_marks"]), pm)) for r in results]
    return render(request, "student_profile.html", active="students", s=s, fees=fees, results=results_view,
                  exams=exams, att=att[:15], n_att=n_att, n_p=n_p, n_a=n_a, n_l=n_l,
                  att_pct=round(n_p * 100 / n_att) if n_att else 0, paid=paid)


@app.get("/students/{student_id}/edit", response_class=HTMLResponse)
async def student_edit_page(request: Request, student_id: int):
    with db() as c:
        s = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        classes = D.all_classes(c)
    if not s:
        return go("/students", err="طالب علم نہیں ملا")
    return render(request, "student_form.html", active="students", s=s, classes=classes, roll="",
                  default_class="", boards=D.BOARDS, residences=D.RESIDENCE, states=D.STUDENT_STATE)


@app.post("/students/{student_id}/edit")
async def student_update(request: Request, student_id: int):
    form = await request.form()
    v = _student_form_values(form)
    if not v["name"] or not v["admission_class"]:
        return go(f"/students/{student_id}/edit", err="نام اور درجہ لازمی ہیں")
    v["status"] = clean(form, "status") or "فعال"
    try:
        photo = await save_image(form.get("photo"), "student")
    except ValueError as e:
        return go(f"/students/{student_id}/edit", err=str(e))
    if photo:
        v["photo"] = photo
    with db() as c:
        sets = ",".join(f"{k}=?" for k in v)
        c.execute(f"UPDATE students SET {sets} WHERE id=?", [*v.values(), student_id])
    return go(f"/students/{student_id}", ok="معلومات اپڈیٹ ہو گئیں")


@app.post("/students/{student_id}/delete")
async def student_delete(student_id: int):
    with db() as c:
        c.execute("DELETE FROM attendance WHERE student_id=?", (student_id,))
        c.execute("DELETE FROM results WHERE student_id=?", (student_id,))
        c.execute("DELETE FROM students WHERE id=?", (student_id,))   # fee receipts are kept for the accounts
    return go("/students", ok="طالب علم حذف ہو گیا (فیس کی رسیدیں ریکارڈ میں محفوظ ہیں)")


@app.get("/students/{student_id}/print", response_class=HTMLResponse)
async def student_print(request: Request, student_id: int):
    with db() as c:
        s = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        fees = c.execute("SELECT * FROM fees WHERE student_id=? ORDER BY id DESC", (student_id,)).fetchall()
        res = c.execute("SELECT * FROM results WHERE student_id=? ORDER BY id DESC", (student_id,)).fetchall()
        att = c.execute("SELECT * FROM attendance WHERE student_id=? ORDER BY date DESC", (student_id,)).fetchall()
        pm = D.pass_mark_of(D.get_settings(c))
    n_p = sum(1 for a in att if a["status"].startswith("حاضر"))
    res_view = [dict(r, grade=grade_for(pct(r["obtained_marks"], r["total_marks"]), pm)) for r in res]
    return render(request, "print_student.html", s=s, fees=fees, results=res_view, att=att, n_p=n_p)


@app.get("/students/{student_id}/card", response_class=HTMLResponse)
async def id_card_single(request: Request, student_id: int):
    with db() as c:
        s = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    return render(request, "id_cards.html", students=[s] if s else [], class_name="")


# ------------------------------------------------------------------ teachers
@app.get("/teachers", response_class=HTMLResponse)
async def teachers_page(request: Request):
    with db() as c:
        rows = c.execute("SELECT * FROM teachers ORDER BY name").fetchall()
        total_salary = c.execute("SELECT COALESCE(SUM(salary),0) FROM teachers").fetchone()[0]
    return render(request, "teachers.html", active="teachers", teachers=rows, total_salary=total_salary)


@app.post("/teachers/new")
async def teacher_create(request: Request):
    f = await request.form()
    if not clean(f, "name"):
        return go("/teachers", err="استاد کا نام لازمی ہے")
    with db() as c:
        c.execute("""INSERT INTO teachers(name,subject,phone,salary,qualification,joining_date,address,cnic)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (clean(f, "name"), clean(f, "subject"), clean(f, "phone"), safe_int(f.get("salary")),
                   clean(f, "qualification"), clean(f, "joining_date") or D.today(), clean(f, "address"), clean(f, "cnic")))
    return go("/teachers", ok="استاد شامل ہو گیا")


@app.get("/teachers/{teacher_id}", response_class=HTMLResponse)
async def teacher_profile(request: Request, teacher_id: int):
    ym = D.today()[:7]
    with db() as c:
        t = c.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
        if not t:
            return go("/teachers", err="استاد نہیں ملا")
        sal = c.execute("SELECT * FROM teacher_salaries WHERE teacher_id=? ORDER BY id DESC LIMIT 24", (teacher_id,)).fetchall()
        att = c.execute("SELECT status, COUNT(*) n FROM teacher_attendance WHERE teacher_id=? AND substr(date,1,7)=? GROUP BY status",
                        (teacher_id, ym)).fetchall()
        classes = c.execute("SELECT name FROM classes WHERE teacher_id=? ORDER BY name", (teacher_id,)).fetchall()
    return render(request, "teacher_profile.html", active="teachers", t=t, salaries=sal,
                  att={r["status"]: r["n"] for r in att}, classes=classes)


@app.get("/teachers/{teacher_id}/edit", response_class=HTMLResponse)
async def teacher_edit_page(request: Request, teacher_id: int):
    with db() as c:
        t = c.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not t:
        return go("/teachers", err="استاد نہیں ملا")
    return render(request, "teacher_form.html", active="teachers", t=t)


@app.post("/teachers/{teacher_id}/edit")
async def teacher_update(request: Request, teacher_id: int):
    f = await request.form()
    with db() as c:
        c.execute("""UPDATE teachers SET name=?,subject=?,phone=?,salary=?,qualification=?,joining_date=?,address=?,cnic=?
                     WHERE id=?""",
                  (clean(f, "name"), clean(f, "subject"), clean(f, "phone"), safe_int(f.get("salary")),
                   clean(f, "qualification"), clean(f, "joining_date"), clean(f, "address"), clean(f, "cnic"), teacher_id))
    return go(f"/teachers/{teacher_id}", ok="معلومات اپڈیٹ ہو گئیں")


@app.post("/teachers/{teacher_id}/delete")
async def teacher_delete(teacher_id: int):
    with db() as c:
        c.execute("DELETE FROM teacher_attendance WHERE teacher_id=?", (teacher_id,))
        c.execute("UPDATE classes SET teacher_id=NULL WHERE teacher_id=?", (teacher_id,))
        c.execute("DELETE FROM teachers WHERE id=?", (teacher_id,))
    return go("/teachers", ok="استاد حذف ہو گیا")


@app.post("/teachers/{teacher_id}/salary")
async def teacher_pay_salary(request: Request, teacher_id: int):
    f = await request.form()
    with db() as c:
        t = c.execute("SELECT salary FROM teachers WHERE id=?", (teacher_id,)).fetchone()
        amount = safe_int(f.get("amount"), (t["salary"] if t else 0) or 0)
        c.execute("INSERT INTO teacher_salaries(teacher_id,month,year,amount,paid_date,note) VALUES(?,?,?,?,?,?)",
                  (teacher_id, clean(f, "month"), safe_int(f.get("year"), date.today().year), amount,
                   clean(f, "paid_date") or D.today(), clean(f, "note")))
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return go(f"/teachers/{teacher_id}", ok="تنخواہ درج ہو گئی", rid=sid)


@app.get("/salary/{salary_id}/slip", response_class=HTMLResponse)
async def salary_slip(request: Request, salary_id: int):
    with db() as c:
        row = c.execute("""SELECT ts.*, t.name teacher_name, t.subject, t.phone FROM teacher_salaries ts
                           JOIN teachers t ON t.id=ts.teacher_id WHERE ts.id=?""", (salary_id,)).fetchone()
    if not row:
        return go("/teachers", err="سلپ نہیں ملی")
    return render(request, "salary_slip.html", r=row)


@app.post("/salary/{salary_id}/delete")
async def salary_delete(salary_id: int):
    with db() as c:
        row = c.execute("SELECT teacher_id FROM teacher_salaries WHERE id=?", (salary_id,)).fetchone()
        c.execute("DELETE FROM teacher_salaries WHERE id=?", (salary_id,))
    return go(f"/teachers/{row['teacher_id']}" if row else "/teachers", ok="تنخواہ کا اندراج حذف ہو گیا")


# --------------------------------------------------------------------- fees
def _students_for_select(c):
    return c.execute(f"""SELECT s.id, s.name, s.father_name, s.admission_class,
        COALESCE(s.monthly_fee,(SELECT monthly_fee FROM classes k WHERE k.name=s.admission_class),0) fee
        FROM students s WHERE {ACTIVE} ORDER BY s.admission_class, s.name""").fetchall()


@app.get("/fees", response_class=HTMLResponse)
async def fees_page(request: Request, q: str = "", month: str = "", ptype: str = "", year: str = ""):
    sql = """SELECT f.*, s.name student_name, s.admission_class FROM fees f
             LEFT JOIN students s ON s.id=f.student_id WHERE 1=1"""
    args = []
    if q.strip():
        sql += " AND s.name LIKE ?"
        args.append(f"%{q.strip()}%")
    if month:
        sql += " AND f.month=?"
        args.append(month)
    if ptype:
        sql += " AND f.payment_type=?"
        args.append(ptype)
    if year:
        sql += " AND (f.year=? OR substr(f.paid_date,1,4)=?)"
        args += [safe_int(year), year]
    sql += " ORDER BY f.id DESC LIMIT 500"
    with db() as c:
        rows = c.execute(sql, args).fetchall()
        students = _students_for_select(c)
    total = sum(r["amount"] or 0 for r in rows if r["payment_type"] != DONATION)
    donations = sum(r["amount"] or 0 for r in rows if r["payment_type"] == DONATION)
    return render(request, "fees.html", active="fees", fees=rows, students=students, q=q, month=month,
                  ptype=ptype, year=year, total=total, donations=donations, fee_types=D.FEE_TYPES)


@app.post("/fees/new")
async def fee_create(request: Request):
    f = await request.form()
    sid, amount = safe_int(f.get("student_id")), safe_int(f.get("amount"))
    if not sid or amount <= 0:
        return go("/fees", err="طالب علم اور درست رقم درج کریں")
    with db() as c:
        c.execute("INSERT INTO fees(student_id,amount,month,payment_type,paid_date,year,note) VALUES(?,?,?,?,?,?,?)",
                  (sid, amount, clean(f, "month"), clean(f, "payment_type") or MONTHLY,
                   clean(f, "paid_date") or D.today(), safe_int(f.get("year"), date.today().year), clean(f, "note")))
        fid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return go("/fees", ok="رقم محفوظ ہو گئی", rid=fid)


@app.get("/fees/defaulters", response_class=HTMLResponse)
async def fee_defaulters(request: Request, month: str = "", year: str = "", class_name: str = ""):
    month = month or MONTHS[date.today().month - 1]
    yr = safe_int(year, date.today().year)
    with db() as c:
        classes = D.all_classes(c)
        rows = c.execute(f"""SELECT s.id,s.name,s.father_name,s.admission_class,s.phone,s.guardian_phone,s.roll_number,
            s.monthly_fee own_fee,
            COALESCE(s.monthly_fee,(SELECT monthly_fee FROM classes k WHERE k.name=s.admission_class),0) expected,
            (SELECT COALESCE(SUM(f.amount),0) FROM fees f WHERE f.student_id=s.id AND f.payment_type=? AND f.month=?
               AND (f.year IS NULL OR f.year=?)) paid
            FROM students s WHERE {ACTIVE} AND (?='' OR s.admission_class=?) ORDER BY s.admission_class, s.name""",
                         (MONTHLY, month, yr, class_name, class_name)).fetchall()
    due_rows = [dict(r, due=r["expected"] - r["paid"]) for r in rows if r["expected"] > r["paid"]]
    unset = sum(1 for r in rows if r["expected"] == 0 and r["own_fee"] is None)
    return render(request, "defaulters.html", active="fees", rows=due_rows, month=month, year=yr,
                  class_name=class_name, classes=classes, unset=unset,
                  total_expected=sum(r["expected"] for r in rows), total_paid=sum(r["paid"] for r in rows),
                  total_due=sum(r["due"] for r in due_rows))


@app.get("/fees/{fee_id}/receipt", response_class=HTMLResponse)
async def fee_receipt(request: Request, fee_id: int):
    with db() as c:
        r = c.execute("""SELECT f.*, s.name student_name, s.father_name, s.admission_class, s.roll_number
                         FROM fees f LEFT JOIN students s ON s.id=f.student_id WHERE f.id=?""", (fee_id,)).fetchone()
    if not r:
        return go("/fees", err="رسید نہیں ملی")
    return render(request, "receipt.html", r=r)


@app.get("/fees/{fee_id}/edit", response_class=HTMLResponse)
async def fee_edit_page(request: Request, fee_id: int):
    with db() as c:
        fee = c.execute("SELECT * FROM fees WHERE id=?", (fee_id,)).fetchone()
        students = _students_for_select(c)
    if not fee:
        return go("/fees", err="ریکارڈ نہیں ملا")
    return render(request, "fee_edit.html", active="fees", fee=fee, students=students, fee_types=D.FEE_TYPES)


@app.post("/fees/{fee_id}/edit")
async def fee_update(request: Request, fee_id: int):
    f = await request.form()
    with db() as c:
        c.execute("UPDATE fees SET student_id=?,amount=?,month=?,payment_type=?,paid_date=?,year=?,note=? WHERE id=?",
                  (safe_int(f.get("student_id")), safe_int(f.get("amount")), clean(f, "month"), clean(f, "payment_type"),
                   clean(f, "paid_date"), safe_int(f.get("year"), date.today().year), clean(f, "note"), fee_id))
    return go("/fees", ok="ریکارڈ اپڈیٹ ہو گیا")


@app.post("/fees/{fee_id}/delete")
async def fee_delete(fee_id: int):
    with db() as c:
        c.execute("DELETE FROM fees WHERE id=?", (fee_id,))
    return go("/fees", ok="ریکارڈ حذف ہو گیا")


# ----------------------------------------------------------------- expenses
@app.get("/expenses", response_class=HTMLResponse)
async def expenses_page(request: Request, month: str = ""):
    month = month or D.today()[:7]
    with db() as c:
        rows = c.execute("SELECT * FROM expenses WHERE substr(date,1,7)=? ORDER BY date DESC, id DESC", (month,)).fetchall()
        sal = c.execute("""SELECT ts.*, t.name teacher_name FROM teacher_salaries ts JOIN teachers t ON t.id=ts.teacher_id
                           WHERE substr(ts.paid_date,1,7)=? ORDER BY ts.id DESC""", (month,)).fetchall()
        income = c.execute("SELECT COALESCE(SUM(amount),0) FROM fees WHERE substr(paid_date,1,7)=?", (month,)).fetchone()[0]
    by_cat = {}
    for r in rows:
        by_cat[r["category"] or "متفرق"] = by_cat.get(r["category"] or "متفرق", 0) + (r["amount"] or 0)
    sal_total = sum(r["amount"] or 0 for r in sal)
    if sal_total:
        by_cat["تنخواہیں"] = sal_total
    total = sum(r["amount"] or 0 for r in rows) + sal_total
    return render(request, "expenses.html", active="expenses", rows=rows, month=month, by_cat=by_cat, total=total,
                  income=income, cats=D.EXPENSE_CATS, sal_total=sal_total, sal_count=len(sal))


@app.post("/expenses/new")
async def expense_create(request: Request):
    f = await request.form()
    amount = safe_int(f.get("amount"))
    if amount <= 0:
        return go("/expenses", err="درست رقم درج کریں")
    d = clean(f, "date") or D.today()
    with db() as c:
        c.execute("INSERT INTO expenses(date,category,amount,note) VALUES(?,?,?,?)",
                  (d, clean(f, "category") or "متفرق", amount, clean(f, "note")))
    return go("/expenses", ok="خرچہ درج ہو گیا", month=d[:7])


@app.post("/expenses/{expense_id}/delete")
async def expense_delete(expense_id: int):
    with db() as c:
        c.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    return go("/expenses", ok="خرچہ حذف ہو گیا")


# ------------------------------------------------------------------ results
def build_card(c, student_id: int, exam: str, pm: float):
    s = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not s:
        return None
    rows = c.execute("SELECT * FROM results WHERE student_id=? AND test_name=? ORDER BY id", (student_id, exam)).fetchall()
    lines = []
    for r in rows:
        p = pct(r["obtained_marks"], r["total_marks"])
        lines.append({"subject": r["subject"] or "مجموعی نتیجہ", "total": r["total_marks"] or 0,
                      "obtained": r["obtained_marks"] or 0, "pct": p, "grade": grade_for(p, pm)})
    total, obtained = sum(x["total"] for x in lines), sum(x["obtained"] for x in lines)
    percent = pct(obtained, total)
    mates = c.execute(f"""SELECT s2.id, COALESCE(SUM(r.obtained_marks),0) o, COALESCE(SUM(r.total_marks),0) t
        FROM students s2 JOIN results r ON r.student_id=s2.id AND r.test_name=? WHERE s2.admission_class=? GROUP BY s2.id""",
                      (exam, s["admission_class"])).fetchall()
    scores = [pct(m["o"], m["t"]) for m in mates if m["t"]]
    position = 1 + sum(1 for x in scores if x > percent) if total else None
    att = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN status LIKE 'حاضر%' THEN 1 ELSE 0 END) p FROM attendance WHERE student_id=?",
                    (student_id,)).fetchone()
    return {"s": s, "exam": exam, "lines": lines, "total": total, "obtained": obtained, "pct": percent,
            "grade": grade_for(percent, pm), "passed": total > 0 and percent >= pm, "position": position,
            "out_of": len(scores), "att_total": att["n"] or 0, "att_present": att["p"] or 0,
            "att_pct": round((att["p"] or 0) * 100 / att["n"]) if att["n"] else 0}


@app.get("/results", response_class=HTMLResponse)
async def results_page(request: Request, q: str = "", exam: str = "", class_name: str = ""):
    sql = """SELECT r.*, s.name student_name, s.admission_class FROM results r
             LEFT JOIN students s ON s.id=r.student_id WHERE 1=1"""
    args = []
    if q.strip():
        sql += " AND s.name LIKE ?"
        args.append(f"%{q.strip()}%")
    if exam:
        sql += " AND r.test_name=?"
        args.append(exam)
    if class_name:
        sql += " AND s.admission_class=?"
        args.append(class_name)
    sql += " ORDER BY r.id DESC LIMIT 500"
    with db() as c:
        pm = D.pass_mark_of(D.get_settings(c))
        rows = [dict(r, pct=pct(r["obtained_marks"], r["total_marks"]),
                     grade=grade_for(pct(r["obtained_marks"], r["total_marks"]), pm)) for r in c.execute(sql, args)]
        students = _students_for_select(c)
        exams = [r[0] for r in c.execute("SELECT DISTINCT test_name FROM results WHERE test_name IS NOT NULL ORDER BY test_name")]
        subjects = [r[0] for r in c.execute("SELECT DISTINCT subject FROM results WHERE subject IS NOT NULL AND subject!='' ORDER BY subject")]
        classes = D.all_classes(c)
    return render(request, "results.html", active="results", results=rows, students=students, exams=exams,
                  subjects=subjects, classes=classes, q=q, exam=exam, class_name=class_name)


@app.post("/results/new")
async def result_create(request: Request):
    f = await request.form()
    total, got = safe_int(f.get("total_marks")), safe_int(f.get("obtained_marks"))
    if not safe_int(f.get("student_id")) or not clean(f, "test_name") or total <= 0 or got < 0 or got > total:
        return go("/results", err="معلومات درست درج کریں (حاصل نمبر کل نمبر سے زیادہ نہیں ہو سکتے)")
    with db() as c:
        c.execute("INSERT INTO results(student_id,test_name,subject,total_marks,obtained_marks,exam_date) VALUES(?,?,?,?,?,?)",
                  (safe_int(f.get("student_id")), clean(f, "test_name"), clean(f, "subject"), total, got, D.today()))
    return go("/results", ok="رزلٹ محفوظ ہو گیا")


@app.get("/results/entry", response_class=HTMLResponse)
async def results_entry(request: Request, class_name: str = "", exam: str = "", subject: str = "", total: str = ""):
    with db() as c:
        classes = D.all_classes(c)
        exams = [r[0] for r in c.execute("SELECT DISTINCT test_name FROM results WHERE test_name IS NOT NULL ORDER BY test_name")]
        subjects = [r[0] for r in c.execute("SELECT DISTINCT subject FROM results WHERE subject IS NOT NULL AND subject!='' ORDER BY subject")]
        rows, marks = [], {}
        if class_name:
            rows = c.execute(f"SELECT * FROM students s WHERE {ACTIVE} AND s.admission_class=? "
                             "ORDER BY CAST(s.roll_number AS INTEGER), s.name", (class_name,)).fetchall()
            if exam:
                for r in c.execute("SELECT student_id, obtained_marks, total_marks FROM results WHERE test_name=? AND COALESCE(subject,'')=?",
                                   (exam, subject)):
                    marks[r["student_id"]] = r["obtained_marks"]
                    if not total and r["total_marks"]:
                        total = str(r["total_marks"])
    return render(request, "result_entry.html", active="results", classes=classes, exams=exams, subjects=subjects,
                  students=rows, marks=marks, class_name=class_name, exam=exam, subject=subject, total=total)


@app.post("/results/entry")
async def results_entry_save(request: Request):
    f = await request.form()
    class_name, exam, subject, total = clean(f, "class_name"), clean(f, "exam"), clean(f, "subject"), safe_int(f.get("total"))
    back = f"/results/entry?class_name={quote(class_name)}&exam={quote(exam)}&subject={quote(subject)}&total={total}"
    if not exam or total <= 0:
        return go(back, err="امتحان کا نام اور کل نمبر درج کریں")
    saved = skipped = 0
    with db() as c:
        for key, val in f.items():
            if not key.startswith("marks_") or not isinstance(val, str) or not val.strip():
                continue
            got = safe_int(val, -1)
            if got < 0 or got > total:
                skipped += 1
                continue
            sid = safe_int(key[6:])
            row = c.execute("SELECT id FROM results WHERE student_id=? AND test_name=? AND COALESCE(subject,'')=?",
                            (sid, exam, subject)).fetchone()
            if row:
                c.execute("UPDATE results SET total_marks=?, obtained_marks=? WHERE id=?", (total, got, row["id"]))
            else:
                c.execute("INSERT INTO results(student_id,test_name,subject,total_marks,obtained_marks,exam_date) VALUES(?,?,?,?,?,?)",
                          (sid, exam, subject, total, got, D.today()))
            saved += 1
    msg = f"{saved} طلبہ کے نمبر محفوظ ہوئے"
    return go(back, ok=msg, **({"err": f"{skipped} اندراج غلط نمبروں کی وجہ سے چھوڑ دیے گئے"} if skipped else {}))


@app.get("/results/card", response_class=HTMLResponse)
async def report_card(request: Request, student_id: int = 0, exam: str = ""):
    with db() as c:
        pm = D.pass_mark_of(D.get_settings(c))
        card = build_card(c, student_id, exam, pm)
    if not card:
        return go("/results", err="طالب علم نہیں ملا")
    return render(request, "report_card.html", cards=[card])


@app.get("/results/cards", response_class=HTMLResponse)
async def report_cards_class(request: Request, class_name: str = "", exam: str = ""):
    with db() as c:
        pm = D.pass_mark_of(D.get_settings(c))
        ids = [r[0] for r in c.execute(f"""SELECT s.id FROM students s WHERE {ACTIVE} AND s.admission_class=?
            AND s.id IN (SELECT student_id FROM results WHERE test_name=?) ORDER BY CAST(s.roll_number AS INTEGER), s.name""",
                                       (class_name, exam))]
        cards = [build_card(c, i, exam, pm) for i in ids]
    if not cards:
        return go("/results", err="اس کلاس اور امتحان کا رزلٹ موجود نہیں")
    return render(request, "report_card.html", cards=cards)


@app.get("/results/{result_id}/edit", response_class=HTMLResponse)
async def result_edit_page(request: Request, result_id: int):
    with db() as c:
        r = c.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
        students = _students_for_select(c)
    if not r:
        return go("/results", err="ریکارڈ نہیں ملا")
    return render(request, "result_edit.html", active="results", r=r, students=students)


@app.post("/results/{result_id}/edit")
async def result_update(request: Request, result_id: int):
    f = await request.form()
    total, got = safe_int(f.get("total_marks")), safe_int(f.get("obtained_marks"))
    if total <= 0 or got < 0 or got > total:
        return go(f"/results/{result_id}/edit", err="حاصل نمبر کل نمبر سے زیادہ نہیں ہو سکتے")
    with db() as c:
        c.execute("UPDATE results SET student_id=?,test_name=?,subject=?,total_marks=?,obtained_marks=? WHERE id=?",
                  (safe_int(f.get("student_id")), clean(f, "test_name"), clean(f, "subject"), total, got, result_id))
    return go("/results", ok="رزلٹ اپڈیٹ ہو گیا")


@app.post("/results/{result_id}/delete")
async def result_delete(result_id: int):
    with db() as c:
        c.execute("DELETE FROM results WHERE id=?", (result_id,))
    return go("/results", ok="رزلٹ حذف ہو گیا")


# --------------------------------------------------------------- attendance
@app.get("/attendance", response_class=HTMLResponse)
async def attendance_sheet(request: Request, class_name: str = "", day: str = ""):
    day = day or D.today()
    with db() as c:
        classes = D.all_classes(c)
        class_name = class_name or (classes[0] if classes else "")
        rows = c.execute(f"SELECT * FROM students s WHERE {ACTIVE} AND s.admission_class=? "
                         "ORDER BY CAST(s.roll_number AS INTEGER), s.name", (class_name,)).fetchall()
        existing = {r["student_id"]: r["status"] for r in c.execute(
            "SELECT student_id,status FROM attendance WHERE date=?", (day,))}
    return render(request, "attendance.html", active="attendance", classes=classes, class_name=class_name, day=day,
                  students=rows, existing=existing, statuses=D.STUDENT_STATUSES, saved_count=len(existing))


@app.post("/attendance/save")
async def attendance_save(request: Request):
    f = await request.form()
    class_name, day = clean(f, "class_name"), clean(f, "day") or D.today()
    n = 0
    with db() as c:
        for key, val in f.items():
            if key.startswith("st_") and val in D.STUDENT_STATUSES:
                sid = safe_int(key[3:])
                c.execute("DELETE FROM attendance WHERE student_id=? AND date=?", (sid, day))
                c.execute("INSERT INTO attendance(student_id,date,status) VALUES(?,?,?)", (sid, day, val))
                n += 1
    return go(f"/attendance?class_name={quote(class_name)}&day={day}", ok=f"{n} طلبہ کی حاضری محفوظ ہو گئی")


@app.get("/attendance/monthly", response_class=HTMLResponse)
async def attendance_monthly(request: Request, class_name: str = "", month: str = ""):
    month = month or D.today()[:7]
    try:
        y, m = int(month[:4]), int(month[5:7])
        ndays = calendar.monthrange(y, m)[1]
    except (ValueError, calendar.IllegalMonthError):
        y, m = date.today().year, date.today().month
        month, ndays = D.today()[:7], calendar.monthrange(date.today().year, date.today().month)[1]
    with db() as c:
        classes = D.all_classes(c)
        class_name = class_name or (classes[0] if classes else "")
        students = c.execute(f"SELECT * FROM students s WHERE {ACTIVE} AND s.admission_class=? "
                             "ORDER BY CAST(s.roll_number AS INTEGER), s.name", (class_name,)).fetchall()
        recs = c.execute("""SELECT a.student_id, a.date, a.status FROM attendance a JOIN students s ON s.id=a.student_id
                            WHERE s.admission_class=? AND substr(a.date,1,7)=?""", (class_name, month)).fetchall()
    grid = {}
    for r in recs:
        code = "P" if r["status"].startswith("حاضر") else "A" if r["status"].startswith("غیر") else "L"
        grid.setdefault(r["student_id"], {})[safe_int(r["date"][8:10])] = code
    table = []
    for s in students:
        g = grid.get(s["id"], {})
        p, a, l = (sum(1 for v in g.values() if v == k) for k in "PAL")
        table.append({"s": s, "g": g, "p": p, "a": a, "l": l, "pct": round(p * 100 / (p + a + l)) if (p + a + l) else 0})
    return render(request, "attendance_monthly.html", active="attendance", classes=classes, class_name=class_name,
                  month=month, days=list(range(1, ndays + 1)), table=table,
                  month_name=f"{MONTHS[m - 1]} {y}")


@app.get("/teacher-attendance", response_class=HTMLResponse)
async def teacher_attendance_page(request: Request, day: str = "", month: str = ""):
    day, month = day or D.today(), month or (day or D.today())[:7]
    with db() as c:
        teachers = c.execute("SELECT * FROM teachers ORDER BY name").fetchall()
        existing = {r["teacher_id"]: r["status"] for r in c.execute(
            "SELECT teacher_id,status FROM teacher_attendance WHERE date=?", (day,))}
        summary = c.execute("""SELECT t.id, t.name, t.subject,
            SUM(CASE WHEN ta.status='حاضر' THEN 1 ELSE 0 END) p, SUM(CASE WHEN ta.status='غیر حاضر' THEN 1 ELSE 0 END) a,
            SUM(CASE WHEN ta.status='چھٹی' THEN 1 ELSE 0 END) l, SUM(CASE WHEN ta.status='تاخیر' THEN 1 ELSE 0 END) late
            FROM teachers t LEFT JOIN teacher_attendance ta ON ta.teacher_id=t.id AND substr(ta.date,1,7)=?
            GROUP BY t.id ORDER BY t.name""", (month,)).fetchall()
    return render(request, "teacher_attendance.html", active="teacher_attendance", teachers=teachers, day=day,
                  existing=existing, summary=summary, month=month, statuses=D.TEACHER_STATUSES)


@app.post("/teacher-attendance/save")
async def teacher_attendance_save(request: Request):
    f = await request.form()
    day = clean(f, "day") or D.today()
    n = 0
    with db() as c:
        for key, val in f.items():
            if key.startswith("tt_") and val in D.TEACHER_STATUSES:
                c.execute("""INSERT INTO teacher_attendance(teacher_id,date,status) VALUES(?,?,?)
                             ON CONFLICT(teacher_id,date) DO UPDATE SET status=excluded.status""",
                          (safe_int(key[3:]), day, val))
                n += 1
    return go(f"/teacher-attendance?day={day}", ok=f"{n} اساتذہ کی حاضری محفوظ ہو گئی")


# ------------------------------------------------------------------ classes
@app.get("/classes", response_class=HTMLResponse)
async def classes_page(request: Request):
    with db() as c:
        rows = c.execute(f"""SELECT k.id, k.name, k.description, k.monthly_fee, k.teacher_id, t.name teacher_name,
            (SELECT COUNT(*) FROM students s WHERE s.admission_class=k.name AND {ACTIVE}) n
            FROM classes k LEFT JOIN teachers t ON t.id=k.teacher_id ORDER BY k.name""").fetchall()
        known = {r["name"] for r in rows}
        implicit = [{"name": r[0], "n": r[1]} for r in c.execute(
            f"""SELECT admission_class, COUNT(*) FROM students s WHERE {ACTIVE} AND TRIM(COALESCE(admission_class,''))!=''
                GROUP BY 1 ORDER BY 1""") if r[0] not in known]
        teachers = c.execute("SELECT id,name FROM teachers ORDER BY name").fetchall()
    return render(request, "classes.html", active="classes", classes=rows, implicit=implicit, teachers=teachers)


@app.post("/classes/save")
async def class_save(request: Request):
    f = await request.form()
    name = clean(f, "name")
    if not name:
        return go("/classes", err="کلاس کا نام لازمی ہے")
    with db() as c:
        c.execute("""INSERT INTO classes(name,teacher_id,description,monthly_fee) VALUES(?,?,?,?)
                     ON CONFLICT(name) DO UPDATE SET teacher_id=excluded.teacher_id, description=excluded.description,
                     monthly_fee=excluded.monthly_fee""",
                  (name, safe_int(f.get("teacher_id")) or None, clean(f, "description"), safe_int(f.get("monthly_fee"))))
    return go("/classes", ok="کلاس محفوظ ہو گئی")


@app.post("/classes/{class_id}/delete")
async def class_delete(class_id: int):
    with db() as c:
        c.execute("DELETE FROM classes WHERE id=?", (class_id,))
    return go("/classes", ok="کلاس کی ترتیب حذف ہو گئی (طلبہ کا ریکارڈ محفوظ ہے)")


@app.get("/classes/print", response_class=HTMLResponse)
async def class_print(request: Request, name: str = ""):
    with db() as c:
        rows = c.execute(f"""SELECT s.*,
            (SELECT COALESCE(SUM(obtained_marks),0) FROM results r WHERE r.student_id=s.id) obtained,
            (SELECT COALESCE(SUM(total_marks),0) FROM results r WHERE r.student_id=s.id) total,
            (SELECT COUNT(*) FROM attendance a WHERE a.student_id=s.id AND {PRES}) present,
            (SELECT COUNT(*) FROM attendance a WHERE a.student_id=s.id) days
            FROM students s WHERE s.admission_class=? AND {ACTIVE} ORDER BY CAST(s.roll_number AS INTEGER), s.name""",
                         (name,)).fetchall()
        teacher = c.execute("SELECT t.name FROM classes k JOIN teachers t ON t.id=k.teacher_id WHERE k.name=?", (name,)).fetchone()
    return render(request, "print_class.html", students=rows, class_name=name, teacher=teacher["name"] if teacher else "")


# ------------------------------------------------------------------ reports
def _report_rows(c, class_name: str, month: str, exam: str, pm: float):
    rows = c.execute(f"""SELECT s.id,s.name,s.father_name,s.admission_class,s.roll_number,
      (SELECT COUNT(*) FROM attendance a WHERE a.student_id=s.id AND (?='' OR substr(a.date,1,7)=?)) att_total,
      (SELECT COUNT(*) FROM attendance a WHERE a.student_id=s.id AND {PRES} AND (?='' OR substr(a.date,1,7)=?)) att_present,
      (SELECT COALESCE(SUM(r.obtained_marks),0) FROM results r WHERE r.student_id=s.id AND (?='' OR r.test_name=?)) obtained,
      (SELECT COALESCE(SUM(r.total_marks),0) FROM results r WHERE r.student_id=s.id AND (?='' OR r.test_name=?)) total
      FROM students s WHERE {ACTIVE} AND (?='' OR s.admission_class=?)
      ORDER BY s.admission_class, CAST(s.roll_number AS INTEGER), s.name""",
                     (month, month, month, month, exam, exam, exam, exam, class_name, class_name)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["percent"] = pct(d["obtained"], d["total"])
        d["grade"] = grade_for(d["percent"], pm) if d["total"] else "—"
        d["att_pct"] = round(d["att_present"] * 100 / d["att_total"], 1) if d["att_total"] else 0
        out.append(d)
    return rank_rows(out)


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, class_name: str = "", month: str = "", exam: str = ""):
    with db() as c:
        pm = D.pass_mark_of(D.get_settings(c))
        classes = D.all_classes(c)
        exams = [r[0] for r in c.execute("SELECT DISTINCT test_name FROM results WHERE test_name IS NOT NULL ORDER BY test_name")]
        rows = _report_rows(c, class_name, month, exam, pm)
    toppers = sorted([r for r in rows if r["position"] and r["position"] <= 3],
                     key=lambda r: (r["admission_class"] or "", r["position"]))
    grades = {}
    for r in rows:
        if r["total"]:
            grades[r["grade"]] = grades.get(r["grade"], 0) + 1
    return render(request, "reports.html", active="reports", students=rows, classes=classes, exams=exams,
                  class_name=class_name, month=month, exam=exam, toppers=toppers, grades=grades)


@app.get("/reports/export.csv")
async def reports_export(class_name: str = "", month: str = "", exam: str = ""):
    with db() as c:
        rows = _report_rows(c, class_name, month, exam, D.pass_mark_of(D.get_settings(c)))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["رول نمبر", "نام", "ولدیت", "درجہ", "حاصل نمبر", "کل نمبر", "فیصد", "گریڈ", "پوزیشن", "حاضری %"])
    for r in rows:
        w.writerow([r["roll_number"] or r["id"], r["name"], r["father_name"], r["admission_class"], r["obtained"],
                    r["total"], r["percent"], r["grade"], r["position"] or "", r["att_pct"]])
    return Response(content=("\ufeff" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=report.csv"})


@app.get("/finance", response_class=HTMLResponse)
async def finance_page(request: Request, year: str = ""):
    yr = safe_int(year, date.today().year)
    with db() as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        table = []
        for m in range(1, 13):
            key = f"{yr}-{m:02d}"
            fees = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE payment_type!=? AND substr(paid_date,1,7)=?", DONATION, key)
            don = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE payment_type=? AND substr(paid_date,1,7)=?", DONATION, key)
            exp = one("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE substr(date,1,7)=?", key)
            sal = one("SELECT COALESCE(SUM(amount),0) FROM teacher_salaries WHERE substr(paid_date,1,7)=?", key)
            table.append({"label": MONTHS[m - 1], "fees": fees, "don": don, "exp": exp, "sal": sal,
                          "net": fees + don - exp - sal, "income": fees + don, "expense": exp + sal})
        undated = one("SELECT COALESCE(SUM(amount),0) FROM fees WHERE paid_date IS NULL OR paid_date=''")
    totals = {k: sum(r[k] for r in table) for k in ("fees", "don", "exp", "sal", "net")}
    return render(request, "finance.html", active="finance", table=table, totals=totals, year=yr, undated=undated)


# ------------------------------------------------------------------ notices
@app.get("/notices", response_class=HTMLResponse)
async def notices_page(request: Request):
    with db() as c:
        rows = c.execute("SELECT * FROM notices ORDER BY id DESC").fetchall()
    return render(request, "notices.html", active="notices", notices=rows)


@app.post("/notices/new")
async def notice_create(request: Request):
    f = await request.form()
    if not clean(f, "title"):
        return go("/notices", err="عنوان لازمی ہے")
    with db() as c:
        c.execute("INSERT INTO notices(title,body,created_at) VALUES(?,?,?)",
                  (clean(f, "title"), clean(f, "body"), D.today()))
    return go("/notices", ok="اعلان شائع ہو گیا")


@app.post("/notices/{notice_id}/delete")
async def notice_delete(notice_id: int):
    with db() as c:
        c.execute("DELETE FROM notices WHERE id=?", (notice_id,))
    return go("/notices", ok="اعلان حذف ہو گیا")


# ----------------------------------------------------------------- settings
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return render(request, "settings.html", active="settings", backups=list_backups())


@app.post("/settings")
async def settings_save(request: Request):
    f = await request.form()
    name = clean(f, "name")
    if not name:
        return go("/settings", err="ادارے کا نام لازمی ہے")
    pm = min(max(safe_int(f.get("pass_mark"), 50), 1), 100)
    with db() as c:
        c.execute("UPDATE schools SET name=?,address=?,phone=? WHERE id=1", (name, clean(f, "address"), clean(f, "phone")))
        D.set_setting(c, "institution_type", clean(f, "institution_type") or "مدرسہ")
        D.set_setting(c, "principal", clean(f, "principal"))
        D.set_setting(c, "pass_mark", str(pm))
        D.set_setting(c, "receipt_note", clean(f, "receipt_note"))
    return go("/settings", ok="سیٹنگ محفوظ ہو گئی")


@app.post("/settings/logo")
async def settings_logo(request: Request):
    f = await request.form()
    try:
        path = await save_image(f.get("logo"), "logo")
    except ValueError as e:
        return go("/settings", err=str(e))
    if not path:
        return go("/settings", err="تصویر منتخب کریں")
    with db() as c:
        c.execute("UPDATE schools SET logo=? WHERE id=1", (path,))
    return go("/settings", ok="لوگو اپڈیٹ ہو گیا")


@app.post("/settings/password")
async def settings_password(request: Request):
    f = await request.form()
    new, confirm, current = clean(f, "new_password"), clean(f, "confirm_password"), clean(f, "current_password")
    with db() as c:
        st = D.get_settings(c)
        if st["password_hash"] and not D.check_password(current, st["password_hash"]):
            return go("/settings", err="موجودہ پاس ورڈ درست نہیں")
        if new != confirm:
            return go("/settings", err="نیا پاس ورڈ اور تصدیق مختلف ہیں")
        if new and len(new) < 4:
            return go("/settings", err="پاس ورڈ کم از کم 4 حروف کا ہو")
        D.set_setting(c, "password_hash", D.hash_password(new) if new else "")
        st = D.get_settings(c)
    resp = go("/settings", ok="پاس ورڈ لگا دیا گیا" if new else "پاس ورڈ ہٹا دیا گیا")
    if new:
        resp.set_cookie("ms", D.session_token(st), httponly=True, samesite="lax", max_age=60 * 60 * 12)
    else:
        resp.delete_cookie("ms")
    return resp


@app.post("/backup/create")
async def backup_create():
    make_backup("manual")
    return go("/settings", ok="بیک اپ بن گیا (backups فولڈر میں محفوظ)")


@app.get("/backup/download")
async def backup_download(name: str = ""):
    if not name:
        name = make_backup("download")
    path = BACKUP_DIR / Path(name).name
    if not path.exists() or path.suffix != ".db":
        return go("/settings", err="بیک اپ فائل نہیں ملی")
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")


@app.post("/backup/restore")
async def backup_restore(request: Request):
    f = await request.form()
    up = f.get("backup")
    if up is None or not getattr(up, "filename", ""):
        return go("/settings", err="بیک اپ فائل منتخب کریں")
    tmp = BACKUP_DIR / "_restore_tmp.db"
    tmp.write_bytes(await up.read())
    try:
        chk = sqlite3.connect(str(tmp))
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        has = chk.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='students'").fetchone()
        chk.close()
        if not ok or not has:
            raise ValueError("یہ درست بیک اپ فائل نہیں")
        make_backup("before_restore")
        src, dst = sqlite3.connect(str(tmp)), D.connect()
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        D.ensure_schema()
    except (sqlite3.DatabaseError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        return go("/settings", err=str(e) if isinstance(e, ValueError) else "فائل خراب ہے")
    tmp.unlink(missing_ok=True)
    return go("/settings", ok="ڈیٹا بحال ہو گیا (پرانا ڈیٹا بیک اپ میں محفوظ ہے)")
