from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
import os

app = Flask(__name__)
app.secret_key = "troque-esta-chave-em-producao"
DB = Path(__file__).with_name("agendamentos.db")

SERVICES = [
    ("Manicure", 30, 35.00),
    ("Pedicure", 45, 45.00),
    ("Manicure + Pedicure", 75, 75.00),
    ("Alongamento de unhas", 120, 120.00),
]

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            service TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(appointment_date, appointment_time)
        )
    """)
    con.commit()
    con.close()

def slots_for(day):
    # Segunda a sábado, 09:00–18:00, intervalo de 30 min.
    d = datetime.strptime(day, "%Y-%m-%d").date()
    if d.weekday() == 6:
        return []
    start = datetime.combine(d, datetime.min.time()).replace(hour=9)
    end = datetime.combine(d, datetime.min.time()).replace(hour=18)
    slots = []
    cur = start
    while cur < end:
        slots.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=30)
    return slots

@app.context_processor
def inject_today():
    return {"now": date.today().isoformat()}

@app.route("/")
def index():
    return render_template("index.html", services=SERVICES)

@app.route("/horarios")
def horarios():
    day = request.args.get("date", "")
    if not day:
        return {"slots": []}
    con = db()
    taken = {r["appointment_time"] for r in con.execute(
        "SELECT appointment_time FROM appointments WHERE appointment_date = ?", (day,)
    )}
    con.close()
    available = [s for s in slots_for(day) if s not in taken]
    return {"slots": available}

@app.route("/agendar", methods=["POST"])
def agendar():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    service = request.form.get("service", "").strip()
    day = request.form.get("date", "").strip()
    time = request.form.get("time", "").strip()
    notes = request.form.get("notes", "").strip()

    valid_services = {s[0] for s in SERVICES}
    if not all([name, phone, service, day, time]) or service not in valid_services:
        flash("Preencha todos os campos obrigatórios.", "error")
        return redirect(url_for("index"))

    try:
        chosen = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        flash("Data inválida.", "error")
        return redirect(url_for("index"))

    if chosen < date.today() or time not in slots_for(day):
        flash("Data ou horário inválido.", "error")
        return redirect(url_for("index"))

    con = db()
    try:
        con.execute("""
            INSERT INTO appointments
            (name, phone, service, appointment_date, appointment_time, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, phone, service, day, time, notes, datetime.now().isoformat(timespec="seconds")))
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        flash("Esse horário acabou de ser reservado. Escolha outro.", "error")
        return redirect(url_for("index"))
    con.close()

    return render_template("confirmacao.html", name=name, service=service, day=day, time=time)

@app.route("/admin")
def admin():
    con = db()
    appointments = con.execute("""
        SELECT * FROM appointments
        ORDER BY appointment_date, appointment_time
    """).fetchall()
    con.close()
    return render_template("admin.html", appointments=appointments)

@app.post("/admin/cancelar/<int:appointment_id>")
def cancelar(appointment_id):
    con = db()
    con.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
    con.commit()
    con.close()
    flash("Agendamento cancelado.", "ok")
    return redirect(url_for("admin"))

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
