from flask import Flask, render_template, request, redirect, url_for, flash, Response
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import IntegrityError
from datetime import datetime, date, timedelta
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = "troque-esta-chave-em-producao"

ADMIN_USERNAME = "Rayssa"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
DATABASE_URL = os.environ.get("DATABASE_URL")

SERVICES = [
    ("Esmaltação simples", 30, 30.00),
    ("Esmaltação em Gel", 90, 45.00),
    ("Blindagem de unhas", 60, 30.00),
    ("Pedicure", 45, 30.00),
    ("Manicure + Pedicure", 60, 50.00),
    ("Alongamento de unhas", 120, 60.00),
    ("Plano mensao", 90, 150.00)
]


class Database:
    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL não configurada.")

        self.con = psycopg2.connect(DATABASE_URL)

    def execute(self, sql, params=None):
        cursor = self.con.cursor(
            cursor_factory=RealDictCursor
        )
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()

    def close(self):
        self.con.close()


def db():
    return Database()


def init_db():
    con = db()

    con.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            service TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Confirmado'
        )
    """)

    # Adiciona a coluna status caso a tabela antiga já exista
    con.execute("""
        ALTER TABLE appointments
        ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'Confirmado'
    """)

    # Remove a antiga restrição de horário único, se existir
    con.execute("""
        ALTER TABLE appointments
        DROP CONSTRAINT IF EXISTS appointments_appointment_date_appointment_time_key
    """)

    # Impede dois agendamentos ativos no mesmo horário,
    # mas permite manter registros cancelados.
    con.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        unique_active_appointment_slot
        ON appointments (appointment_date, appointment_time)
        WHERE status <> 'Cancelado'
    """)

    con.commit()
    con.close()


def slots_for(day, service=None):
    # Segunda a sexta:
    # 07:30 às 11:00
    # 13:30 às 20:00
    #
    # Sábado:
    # 09:00 às 16:00
    #
    # Domingo: fechado
    #
    # O último atendimento pode ultrapassar o fechamento
    # em no máximo 30 minutos.

    d = datetime.strptime(day, "%Y-%m-%d").date()

    # Domingo fechado
    if d.weekday() == 6:
        return []

    # Descobre a duração do serviço
    duration = 30

    if service:
        for name, minutes, price in SERVICES:
            if name == service:
                duration = minutes
                break

    slots = []

    # Define os períodos de atendimento
    if d.weekday() <= 4:
        periods = [
            (7, 30, 11, 0),
            (13, 30, 20, 0)
        ]
    else:
        periods = [
            (9, 0, 16, 0)
        ]

    for start_hour, start_minute, end_hour, end_minute in periods:

        period_start = datetime.combine(
            d,
            datetime.min.time()
        ).replace(
            hour=start_hour,
            minute=start_minute
        )

        period_end = datetime.combine(
            d,
            datetime.min.time()
        ).replace(
            hour=end_hour,
            minute=end_minute
        )

        cur = period_start

        while cur <= period_end:

            appointment_end = cur + timedelta(minutes=duration)

            # Permite ultrapassar somente o fechamento
            # em no máximo 30 minutos.
            allowed_end = period_end + timedelta(minutes=30)

            if appointment_end <= allowed_end:
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
    service = request.args.get("service", "")

    if not day:
        return {"slots": []}

    con = db()

    appointments = con.execute(
    """
    SELECT appointment_time, service
    FROM appointments
    WHERE appointment_date = %s
      AND status <> 'Cancelado'
    """,
    (day,)
).fetchall()

    con.close()

    # Duração do serviço escolhido
    duration = 30

    for name, minutes, price in SERVICES:
        if name == service:
            duration = minutes
            break

    available = []

    for slot in slots_for(day, service):

        slot_start = datetime.strptime(
            f"{day} {slot}",
            "%Y-%m-%d %H:%M"
        )

        slot_end = slot_start + timedelta(minutes=duration)

        conflict = False

        for appointment in appointments:

            existing_start = datetime.strptime(
                f"{day} {appointment['appointment_time']}",
                "%Y-%m-%d %H:%M"
            )

            existing_duration = 30

            for name, minutes, price in SERVICES:
                if name == appointment["service"]:
                    existing_duration = minutes
                    break

            existing_end = existing_start + timedelta(
                minutes=existing_duration
            )

            # Verifica se os horários se sobrepõem
            if slot_start < existing_end and slot_end > existing_start:
                conflict = True
                break

        if not conflict:
            available.append(slot)

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

    if chosen < date.today() or time not in slots_for(day, service):
        flash("Data ou horário inválido.", "error")
        return redirect(url_for("index"))

    con = db()

    try:
        con.execute("""
            INSERT INTO appointments
            (name, phone, service, appointment_date, appointment_time, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            name,
            phone,
            service,
            day,
            time,
            notes,
            datetime.now().isoformat(timespec="seconds")
        ))

        con.commit()

    except IntegrityError:
        con.rollback()
        con.close()

        flash("Esse horário acabou de ser reservado. Escolha outro.", "error")
        return redirect(url_for("index"))

    con.close()

    return render_template(
        "confirmacao.html",
        name=name,
        service=service,
        day=day,
        time=time
    )


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
        SELECT *
        FROM appointments
        ORDER BY appointment_date, appointment_time
    """).fetchall()

    con.close()

    return render_template(
        "admin.html",
        appointments=appointments
    )
# ==========================================================
# MEUS AGENDAMENTOS - CLIENTE
# ==========================================================

def service_duration(service):
    for name, minutes, price in SERVICES:
        if name == service:
            return minutes
    return 30


def appointment_conflict(con, day, time, duration, ignore_id=None):
    appointments = con.execute(
        """
        SELECT id, appointment_time, service
        FROM appointments
        WHERE appointment_date = %s
          AND status <> 'Cancelado'
        """,
        (day,)
    ).fetchall()

    new_start = datetime.strptime(
        f"{day} {time}",
        "%Y-%m-%d %H:%M"
    )

    new_end = new_start + timedelta(minutes=duration)

    for appointment in appointments:

        if ignore_id and appointment["id"] == ignore_id:
            continue

        existing_start = datetime.strptime(
            f"{day} {appointment['appointment_time']}",
            "%Y-%m-%d %H:%M"
        )

        existing_duration = service_duration(
            appointment["service"]
        )

        existing_end = existing_start + timedelta(
            minutes=existing_duration
        )

        if new_start < existing_end and new_end > existing_start:
            return True

    return False


@app.get("/meus-agendamentos")
def meus_agendamentos():
    return render_template("meus_agendamentos.html")


@app.get("/api/meus-agendamentos")
def api_meus_agendamentos():

    phone = request.args.get("phone", "").strip()

    if not phone:
        return {
            "success": False,
            "appointments": []
        }

    con = db()

    appointments = con.execute(
        """
        SELECT
            id,
            name,
            phone,
            service,
            appointment_date,
            appointment_time,
            notes,
            status
        FROM appointments
        WHERE regexp_replace(phone, '[^0-9]', '', 'g')
              =
              regexp_replace(%s, '[^0-9]', '', 'g')
        ORDER BY appointment_date, appointment_time
        """,
        (phone,)
    ).fetchall()

    con.close()

    result = []

    for appointment in appointments:

        result.append({
            "id": appointment["id"],
            "name": appointment["name"],
            "service": appointment["service"],
            "date": appointment["appointment_date"],
            "time": appointment["appointment_time"],
            "notes": appointment["notes"] or "",
            "status": appointment["status"]
        })

    return {
        "success": True,
        "appointments": result
    }

    con = db()

    appointments = con.execute(
        """
        SELECT
            id,
            name,
            phone,
            service,
            appointment_date,
            appointment_time,
            notes,
            status
        FROM appointments
        WHERE regexp_replace(phone, '[^0-9]', '', 'g')
              =
              regexp_replace(%s, '[^0-9]', '', 'g')
        ORDER BY appointment_date, appointment_time
        """,
        (phone,)
    ).fetchall()

    con.close()

    result = []

    for appointment in appointments:

        result.append({
            "id": appointment["id"],
            "name": appointment["name"],
            "service": appointment["service"],
            "date": appointment["appointment_date"],
            "time": appointment["appointment_time"],
            "notes": appointment["notes"] or "",
            "status": appointment["status"]
        })

    return {
        "success": True,
        "appointments": result
    }


@app.post("/meus-agendamentos/<int:appointment_id>/alterar-servico")
def alterar_servico_cliente(appointment_id):

    data = request.get_json() or {}

    phone = data.get("phone", "").strip()
    service = data.get("service", "").strip()

    valid_services = {s[0] for s in SERVICES}

    if not phone or service not in valid_services:
        return {
            "success": False,
            "message": "Dados inválidos."
        }, 400

    con = db()

    appointment = con.execute(
        """
        SELECT *
        FROM appointments
        WHERE id = %s
          AND regexp_replace(phone, '[^0-9]', '', 'g')
              =
              regexp_replace(%s, '[^0-9]', '', 'g')
          AND status <> 'Cancelado'
        """,
        (appointment_id, phone)
    ).fetchone()

    if not appointment:
        con.close()

        return {
            "success": False,
            "message": "Agendamento não encontrado."
        }, 404

    duration = service_duration(service)

    if appointment_conflict(
        con,
        appointment["appointment_date"],
        appointment["appointment_time"],
        duration,
        appointment_id
    ):
        con.close()

        return {
            "success": False,
            "message": "Esse horário não comporta o novo serviço."
        }, 409

    con.execute(
        """
        UPDATE appointments
        SET service = %s
        WHERE id = %s
        """,
        (service, appointment_id)
    )

    con.commit()
    con.close()

    return {
        "success": True,
        "message": "Serviço alterado com sucesso."
    }


@app.post("/meus-agendamentos/<int:appointment_id>/reagendar")
def reagendar_cliente(appointment_id):

    data = request.get_json() or {}

    phone = data.get("phone", "").strip()
    new_date = data.get("date", "").strip()
    new_time = data.get("time", "").strip()

    if not phone or not new_date or not new_time:
        return {
            "success": False,
            "message": "Informe a nova data e horário."
        }, 400

    try:
        chosen_date = datetime.strptime(
            new_date,
            "%Y-%m-%d"
        ).date()
    except ValueError:
        return {
            "success": False,
            "message": "Data inválida."
        }, 400

    if chosen_date < date.today():
        return {
            "success": False,
            "message": "Não é possível reagendar para uma data passada."
        }, 400

    con = db()

    appointment = con.execute(
        """
        SELECT *
        FROM appointments
        WHERE id = %s
          AND regexp_replace(phone, '[^0-9]', '', 'g')
              =
              regexp_replace(%s, '[^0-9]', '', 'g')
          AND status <> 'Cancelado'
        """,
        (appointment_id, phone)
    ).fetchone()

    if not appointment:
        con.close()

        return {
            "success": False,
            "message": "Agendamento não encontrado."
        }, 404

    service = appointment["service"]

    if new_time not in slots_for(new_date, service):
        con.close()

        return {
            "success": False,
            "message": "Esse horário não está disponível."
        }, 409

    duration = service_duration(service)

    if appointment_conflict(
        con,
        new_date,
        new_time,
        duration,
        appointment_id
    ):
        con.close()

        return {
            "success": False,
            "message": "Esse horário já está ocupado."
        }, 409

    con.execute(
        """
        UPDATE appointments
        SET appointment_date = %s,
            appointment_time = %s
        WHERE id = %s
        """,
        (
            new_date,
            new_time,
            appointment_id
        )
    )

    con.commit()
    con.close()

    return {
        "success": True,
        "message": "Agendamento reagendado com sucesso."
    }


@app.post("/meus-agendamentos/<int:appointment_id>/cancelar")
def cancelar_cliente(appointment_id):

    data = request.get_json() or {}

    phone = data.get("phone", "").strip()

    if not phone:
        return {
            "success": False,
            "message": "WhatsApp não informado."
        }, 400

    con = db()

    appointment = con.execute(
        """
        SELECT id
        FROM appointments
        WHERE id = %s
          AND regexp_replace(phone, '[^0-9]', '', 'g')
              =
              regexp_replace(%s, '[^0-9]', '', 'g')
          AND status <> 'Cancelado'
        """,
        (appointment_id, phone)
    ).fetchone()

    if not appointment:
        con.close()

        return {
            "success": False,
            "message": "Agendamento não encontrado."
        }, 404

    con.execute(
        """
        UPDATE appointments
        SET status = 'Cancelado'
        WHERE id = %s
        """,
        (appointment_id,)
    )

    con.commit()
    con.close()

    return {
        "success": True,
        "message": "Agendamento cancelado com sucesso."
    }

@app.post("/admin/cancelar/<int:appointment_id>")
@proteger_admin
def cancelar(appointment_id):
    con = db()

    con.execute(
    """
    UPDATE appointments
    SET status = 'Cancelado'
    WHERE id = %s
    """,
    (appointment_id,)
)

    con.commit()
    con.close()

    flash("Agendamento cancelado.", "ok")

    return redirect(url_for("admin"))


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
