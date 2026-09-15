from flask import Flask, render_template, request, redirect, url_for, flash, Response
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import IntegrityError
from datetime import datetime, date, timedelta
import os
from functools import wraps


app = Flask(__name__)

app.secret_key = "troque-esta-chave-em-producao"


# ==========================================================
# CONFIGURAÇÕES
# ==========================================================

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


# ==========================================================
# BANCO DE DADOS
# ==========================================================

class Database:

    def __init__(self):

        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL não configurada."
            )

        self.con = psycopg2.connect(
            DATABASE_URL
        )

    def execute(self, sql, params=None):

        cursor = self.con.cursor(
            cursor_factory=RealDictCursor
        )

        cursor.execute(
            sql,
            params
        )

        return cursor

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()

    def close(self):
        self.con.close()


def db():
    return Database()


# ==========================================================
# INICIALIZAÇÃO DO BANCO
# ==========================================================

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

    # Caso a tabela antiga não tenha a coluna status
    con.execute("""
        ALTER TABLE appointments
        ADD COLUMN IF NOT EXISTS
        status TEXT NOT NULL DEFAULT 'Confirmado'
    """)

    # Remove a antiga restrição de horário único
    con.execute("""
        ALTER TABLE appointments
        DROP CONSTRAINT IF EXISTS
        appointments_appointment_date_appointment_time_key
    """)

    # Impede dois agendamentos ativos no mesmo horário.
    # Cancelados não bloqueiam o horário.
    con.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        unique_active_appointment_slot
        ON appointments (
            appointment_date,
            appointment_time
        )
        WHERE status <> 'Cancelado'
    """)

    con.commit()
    con.close()


# ==========================================================
# HORÁRIOS DISPONÍVEIS
# ==========================================================

def slots_for(day, service=None):

    d = datetime.strptime(
        day,
        "%Y-%m-%d"
    ).date()

    # Domingo fechado
    if d.weekday() == 6:
        return []

    # Duração padrão
    duration = 30

    # Descobre duração do serviço
    if service:

        for name, minutes, price in SERVICES:

            if name == service:
                duration = minutes
                break

    slots = []

    # Segunda a sexta
    if d.weekday() <= 4:

        periods = [
            (7, 30, 11, 0),
            (13, 30, 20, 0)
        ]

    # Sábado
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

        current = period_start

        while current <= period_end:

            appointment_end = (
                current +
                timedelta(minutes=duration)
            )

            # Permite ultrapassar o fechamento
            # em no máximo 30 minutos.
            allowed_end = (
                period_end +
                timedelta(minutes=30)
            )

            if appointment_end <= allowed_end:

                slots.append(
                    current.strftime("%H:%M")
                )

            current += timedelta(minutes=30)

    return slots


# ==========================================================
# DATA ATUAL PARA O SITE
# ==========================================================

@app.context_processor
def inject_today():

    return {
        "now": date.today().isoformat()
    }


# ==========================================================
# PÁGINA PRINCIPAL
# ==========================================================

@app.route("/")
def index():

    return render_template(
        "index.html",
        services=SERVICES
    )


# ==========================================================
# HORÁRIOS DISPONÍVEIS
# ==========================================================

@app.route("/horarios")
def horarios():

    day = request.args.get(
        "date",
        ""
    )

    service = request.args.get(
        "service",
        ""
    )

    if not day:
        return {
            "slots": []
        }

    con = db()

    appointments = con.execute(
        """
        SELECT
            appointment_time,
            service
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

    for slot in slots_for(
        day,
        service
    ):

        slot_start = datetime.strptime(
            f"{day} {slot}",
            "%Y-%m-%d %H:%M"
        )

        slot_end = (
            slot_start +
            timedelta(minutes=duration)
        )

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

            existing_end = (
                existing_start +
                timedelta(
                    minutes=existing_duration
                )
            )

            # Verifica sobreposição
            if (
                slot_start < existing_end
                and
                slot_end > existing_start
            ):

                conflict = True
                break

        if not conflict:

            available.append(slot)

    return {
        "slots": available
    }


# ==========================================================
# FAZER AGENDAMENTO
# ==========================================================

@app.route(
    "/agendar",
    methods=["POST"]
)
def agendar():

    name = request.form.get(
        "name",
        ""
    ).strip()

    phone = request.form.get(
        "phone",
        ""
    ).strip()

    service = request.form.get(
        "service",
        ""
    ).strip()

    day = request.form.get(
        "date",
        ""
    ).strip()

    time = request.form.get(
        "time",
        ""
    ).strip()

    notes = request.form.get(
        "notes",
        ""
    ).strip()

    valid_services = {
        s[0]
        for s in SERVICES
    }

    if (
        not all([
            name,
            phone,
            service,
            day,
            time
        ])
        or
        service not in valid_services
    ):

        flash(
            "Preencha todos os campos obrigatórios.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    try:

        chosen = datetime.strptime(
            day,
            "%Y-%m-%d"
        ).date()

    except ValueError:

        flash(
            "Data inválida.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    if (
        chosen < date.today()
        or
        time not in slots_for(
            day,
            service
        )
    ):

        flash(
            "Data ou horário inválido.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    con = db()

    try:

        con.execute(
            """
            INSERT INTO appointments
            (
                name,
                phone,
                service,
                appointment_date,
                appointment_time,
                notes,
                created_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                name,
                phone,
                service,
                day,
                time,
                notes,
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )
        )

        con.commit()

    except IntegrityError:

        con.rollback()
        con.close()

        flash(
            "Esse horário acabou de ser reservado. Escolha outro.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    con.close()

    return render_template(
        "confirmacao.html",
        name=name,
        service=service,
        day=day,
        time=time
    )


# ==========================================================
# MEUS AGENDAMENTOS - CLIENTE
# ==========================================================

@app.route("/meus-agendamentos")
def meus_agendamentos():

    return render_template(
        "meus_agendamentos.html"
    )


@app.route("/api/meus-agendamentos")
def api_meus_agendamentos():

    phone = request.args.get(
        "phone",
        ""
    ).strip()

    if not phone:

        return {
            "success": False,
            "appointments": [],
            "message": "Informe o WhatsApp."
        }, 400

    con = db()

    try:

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
            WHERE
                regexp_replace(
                    phone,
                    '[^0-9]',
                    '',
                    'g'
                )
                =
                regexp_replace(
                    %s,
                    '[^0-9]',
                    '',
                    'g'
                )
            ORDER BY
                appointment_date,
                appointment_time
            """,
            (phone,)
        ).fetchall()

    finally:

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


# ==========================================================
# PROTEÇÃO DA ÁREA ADMINISTRATIVA
# ==========================================================

def proteger_admin(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        auth = request.authorization

        if (
            not auth
            or
            auth.username != ADMIN_USERNAME
            or
            auth.password != ADMIN_PASSWORD
        ):

            return Response(
                "Acesso restrito. Digite a senha correta.",
                401,
                {
                    "WWW-Authenticate":
                    'Basic realm="Área administrativa"'
                }
            )

        return func(
            *args,
            **kwargs
        )

    return wrapper


# ==========================================================
# ÁREA ADMINISTRATIVA
# ==========================================================

@app.route("/admin")
@proteger_admin
def admin():

    con = db()

    appointments = con.execute(
        """
        SELECT *
        FROM appointments
        ORDER BY
            appointment_date,
            appointment_time
        """
    ).fetchall()

    con.close()

    return render_template(
        "admin.html",
        appointments=appointments
    )


# ==========================================================
# CANCELAR AGENDAMENTO PELO ADMIN
# ==========================================================

@app.post(
    "/admin/cancelar/<int:appointment_id>"
)
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

    flash(
        "Agendamento cancelado.",
        "ok"
    )

    return redirect(
        url_for("admin")
    )


# ==========================================================
# INICIALIZAÇÃO
# ==========================================================

init_db()


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
