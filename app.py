from flask import Flask, render_template, request, redirect, url_for, flash, Response
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = "troque-esta-chave-em-producao"
ADMIN_USERNAME = "Rayssa"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
DB = Path(__file__).with_name("agendamentos.db")

SERVICES = [
    ("Esmaltação simples", 30, 30.00),
    ("Esmaltação em Gel", 90, 45.00),
    ("Blindagem de unhas", 60, 30.00),
    ("Pedicure", 45, 30.00),
    ("Manicure + Pedicure", 60, 50.00),
    ("Alongamento de unhas", 120, 60.00),
    ("Plano mensao", 90, 150.00)
    
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
    # Segunda a sexta:
    # Manhã: 07:30 às 11:00
    # Tarde: 13:30 às 20:00
    #
    # Sábado:
    # 09:00 às 16:00
    #
    # Domingo: fechado
    # Horários de 30 em 30 minutos.

    d = datetime.strptime(day, "%Y-%m-%d").date()

    # Domingo fechado
    if d.weekday() == 6:
        return []

    slots = []

    # Segunda a sexta
    if d.weekday() <= 4:

        # Manhã
        cur = datetime.combine(d, datetime.min.time()).replace(
            hour=7, minute=30
        )

        end_morning = datetime.combine(d, datetime.min.time()).replace(
            hour=11, minute=0
        )

        while cur <= end_morning:
            slots.append(cur.strftime("%H:%M"))
            cur += timedelta(minutes=30)

        # Tarde
        cur = datetime.combine(d, datetime.min.time()).replace(
            hour=13, minute=30
        )

        end_afternoon = datetime.combine(d, datetime.min.time()).replace(
            hour=20, minute=0
        )

        while cur <= end_afternoon:
            slots.append(cur.strftime("%H:%M"))
            cur += timedelta(minutes=30)

    # Sábado
    else:

        cur = datetime.combine(d, datetime.min.time()).replace(
            hour=9, minute=0
        )

        end_saturday = datetime.combine(d, datetime.min.time()).replace(
            hour=16, minute=0
        )

        while cur <= end_saturday:
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
def proteger_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.authorization

        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return Response(
                "Acesso restrito. Digite a senha correta.",
                401,
                {"WWW-Authenticate": 'Basic realm="Área administrativa"'}
            )

        return func(*args, **kwargs)

    return wrapper
@app.route("/admin")
@proteger_admin
def admin():
    con = db()
    appointments = con.execute("""
        SELECT * FROM appointments
        ORDER BY appointment_date, appointment_time
    """).fetchall()
    con.close()
    return render_template("admin.html", appointments=appointments)

@app.post("/admin/cancelar/<int:appointment_id>")
@proteger_admin
def cancelar(appointment_id):
    con = db()
    con.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
    con.commit()
    con.close()
    flash("Agendamento cancelado.", "ok")
    return redirect(url_for("admin"))

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
