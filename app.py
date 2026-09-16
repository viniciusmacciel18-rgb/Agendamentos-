```python
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    Response,
    jsonify
)

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import IntegrityError

from datetime import datetime, date, timedelta

import os
from functools import wraps


# ==========================================================
# APLICAÇÃO FLASK
# ==========================================================

app = Flask(__name__)

app.secret_key = "troque-esta-chave-em-producao"


# ==========================================================
# CONFIGURAÇÕES
# ==========================================================

ADMIN_USERNAME = "Rayssa"

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD"
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL"
)


# ==========================================================
# SERVIÇOS
# ==========================================================

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

    def execute(
        self,
        sql,
        params=None
    ):

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

    # ======================================================
    # TABELA DE AGENDAMENTOS
    # ======================================================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS appointments (

            id SERIAL PRIMARY KEY,

            name TEXT NOT NULL,

            phone TEXT NOT NULL,

            service TEXT NOT NULL,

            appointment_date TEXT NOT NULL,

            appointment_time TEXT NOT NULL,

            notes TEXT,

            created_at TEXT NOT NULL,

            status TEXT NOT NULL
            DEFAULT 'Confirmado'
        )
        """
    )

    # Garante que a coluna status exista
    con.execute(
        """
        ALTER TABLE appointments
        ADD COLUMN IF NOT EXISTS
        status TEXT NOT NULL
        DEFAULT 'Confirmado'
        """
    )

    # Remove constraint antiga
    con.execute(
        """
        ALTER TABLE appointments
        DROP CONSTRAINT IF EXISTS
        appointments_appointment_date_appointment_time_key
        """
    )

    # Impede dois agendamentos ativos no mesmo horário
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        unique_active_appointment_slot
        ON appointments (
            appointment_date,
            appointment_time
        )
        WHERE status <> 'Cancelado'
        """
    )

    # ======================================================
    # TABELA DE HORÁRIOS BLOQUEADOS
    # ======================================================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS blocked_slots (

            id SERIAL PRIMARY KEY,

            blocked_date TEXT NOT NULL,

            blocked_time TEXT NOT NULL,

            reason TEXT,

            created_at TEXT NOT NULL
        )
        """
    )

    # Impede o mesmo horário de ser bloqueado duas vezes
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        unique_blocked_slot
        ON blocked_slots (
            blocked_date,
            blocked_time
        )
        """
    )

    con.commit()

    con.close()


# ==========================================================
# FUNÇÕES AUXILIARES
# ==========================================================

def service_duration(service):

    for name, minutes, price in SERVICES:

        if name == service:

            return minutes

    return 30


# ==========================================================
# VERIFICAR CONFLITO DE AGENDAMENTO
# ==========================================================

def appointment_conflict(
    con,
    day,
    time,
    duration,
    ignore_id=None
):

    appointments = con.execute(
        """
        SELECT
            id,
            appointment_time,
            service

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

    new_end = (
        new_start
        + timedelta(minutes=duration)
    )

    for appointment in appointments:

        if (
            ignore_id
            and
            appointment["id"] == ignore_id
        ):

            continue

        existing_start = datetime.strptime(
            f"{day} {appointment['appointment_time']}",
            "%Y-%m-%d %H:%M"
        )

        existing_duration = service_duration(
            appointment["service"]
        )

        existing_end = (
            existing_start
            + timedelta(
                minutes=existing_duration
            )
        )

        if (
            new_start < existing_end
            and
            new_end > existing_start
        ):

            return True

    return False


# ==========================================================
# VERIFICAR HORÁRIO BLOQUEADO
# ==========================================================

def blocked_slot_conflict(
    con,
    day,
    start_time,
    duration
):

    blocked = con.execute(
        """
        SELECT
            blocked_time

        FROM blocked_slots

        WHERE blocked_date = %s
        """,
        (day,)
    ).fetchall()

    new_start = datetime.strptime(
        f"{day} {start_time}",
        "%Y-%m-%d %H:%M"
    )

    new_end = (
        new_start
        + timedelta(minutes=duration)
    )

    for item in blocked:

        blocked_start = datetime.strptime(
            f"{day} {item['blocked_time']}",
            "%Y-%m-%d %H:%M"
        )

        # Cada bloqueio representa 30 minutos
        blocked_end = (
            blocked_start
            + timedelta(minutes=30)
        )

        if (
            new_start < blocked_end
            and
            new_end > blocked_start
        ):

            return True

    return False


# ==========================================================
# HORÁRIOS DISPONÍVEIS
# ==========================================================

def slots_for(
    day,
    service=None
):

    d = datetime.strptime(
        day,
        "%Y-%m-%d"
    ).date()

    # Domingo fechado
    if d.weekday() == 6:

        return []

    # Duração do serviço
    duration = 30

    if service:

        duration = service_duration(
            service
        )

    slots = []

    # ======================================================
    # SEGUNDA A SEXTA
    # ======================================================

    if d.weekday() <= 4:

        # Quarta-feira
        if d.weekday() == 2:

            periods = [
                (7, 30, 11, 0),
                (13, 30, 15, 0)
            ]

        # Segunda, terça, quinta e sexta
        else:

            periods = [
                (7, 30, 11, 0),
                (13, 30, 20, 0)
            ]

    # ======================================================
    # SÁBADO
    # ======================================================

    else:

        periods = [
            (9, 0, 16, 0)
        ]

    # ======================================================
    # GERAR HORÁRIOS
    # ======================================================

    for (
        start_hour,
        start_minute,
        end_hour,
        end_minute
    ) in periods:

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

        allowed_end = (
            period_end
            + timedelta(minutes=30)
        )

        while current <= period_end:

            appointment_end = (
                current
                + timedelta(
                    minutes=duration
                )
            )

            if appointment_end <= allowed_end:

                slots.append(
                    current.strftime("%H:%M")
                )

            current += timedelta(
                minutes=30
            )

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
# HORÁRIOS DISPONÍVEIS PARA CLIENTE
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

    try:

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

        blocked = con.execute(
            """
            SELECT
                blocked_time

            FROM blocked_slots

            WHERE blocked_date = %s
            """,
            (day,)
        ).fetchall()

    finally:

        con.close()

    duration = service_duration(
        service
    )

    available = []

    # Lista de horários bloqueados
    blocked_times = {
        item["blocked_time"]
        for item in blocked
    }

    for slot in slots_for(
        day,
        service
    ):

        slot_start = datetime.strptime(
            f"{day} {slot}",
            "%Y-%m-%d %H:%M"
        )

        slot_end = (
            slot_start
            + timedelta(
                minutes=duration
            )
        )

        conflict = False

        # ==================================================
        # VERIFICAR AGENDAMENTOS
        # ==================================================

        for appointment in appointments:

            existing_start = datetime.strptime(
                f"{day} {appointment['appointment_time']}",
                "%Y-%m-%d %H:%M"
            )

            existing_duration = service_duration(
                appointment["service"]
            )

            existing_end = (
                existing_start
                + timedelta(
                    minutes=existing_duration
                )
            )

            if (
                slot_start < existing_end
                and
                slot_end > existing_start
            ):

                conflict = True

                break

        if conflict:

            continue

        # ==================================================
        # VERIFICAR BLOQUEIOS
        # ==================================================

        for blocked_time in blocked_times:

            blocked_start = datetime.strptime(
                f"{day} {blocked_time}",
                "%Y-%m-%d %H:%M"
            )

            blocked_end = (
                blocked_start
                + timedelta(minutes=30)
            )

            if (
                slot_start < blocked_end
                and
                slot_end > blocked_start
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

        # Verifica novamente os bloqueios
        # antes de gravar.
        duration = service_duration(
            service
        )

        if blocked_slot_conflict(
            con,
            day,
            time,
            duration
        ):

            con.close()

            flash(
                "Esse horário está bloqueado.",
                "error"
            )

            return redirect(
                url_for("index")
            )

        # Insere o agendamento
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

            VALUES
            (
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
            "Esse horário acabou de ser reservado. "
            "Escolha outro.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    con.close()

    return render_template(
        "confirmacao.html",
        name=name,
        phone=phone,
        service=service,
        day=day,
        time=time
    )


# ==========================================================
# MEUS AGENDAMENTOS
# ==========================================================

@app.route(
    "/meus-agendamentos"
)
def meus_agendamentos():

    return render_template(
        "meus_agendamentos.html"
    )


# ==========================================================
# API - MEUS AGENDAMENTOS
# ==========================================================

@app.route(
    "/api/meus-agendamentos"
)
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

            "date": appointment[
                "appointment_date"
            ],

            "time": appointment[
                "appointment_time"
            ],

            "notes": appointment[
                "notes"
            ] or "",

            "status": appointment[
                "status"
            ]

        })

    return {
        "success": True,
        "appointments": result
    }


# ==========================================================
# PROTEÇÃO ADMIN
# ==========================================================

def proteger_admin(func):

    @wraps(func)
    def wrapper(
        *args,
        **kwargs
    ):

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
# ADMIN
# ==========================================================

@app.route("/admin")
@proteger_admin
def admin():

    con = db()

    try:

        appointments = con.execute(
            """
            SELECT *
            FROM appointments
            ORDER BY
                appointment_date,
                appointment_time
            """
        ).fetchall()

        blocked_slots = con.execute(
            """
            SELECT *
            FROM blocked_slots
            ORDER BY
                blocked_date,
                blocked_time
            """
        ).fetchall()

    finally:

        con.close()

    return render_template(
        "admin.html",
        appointments=appointments,
        blocked_slots=blocked_slots
    )


# ==========================================================
# CANCELAR AGENDAMENTO
# ==========================================================

@app.post(
    "/admin/cancelar/<int:appointment_id>"
)
@proteger_admin
def cancelar(
    appointment_id
):

    con = db()

    try:

        con.execute(
            """
            UPDATE appointments

            SET status = 'Cancelado'

            WHERE id = %s

            AND status <> 'Cancelado'
            """,
            (appointment_id,)
        )

        con.commit()

    finally:

        con.close()

    flash(
        "Agendamento cancelado.",
        "ok"
    )

    return redirect(
        url_for("admin")
    )


# ==========================================================
# APAGAR AGENDAMENTO CANCELADO DO HISTÓRICO
# ==========================================================

@app.post(
    "/admin/apagar/<int:appointment_id>"
)
@proteger_admin
def apagar_historico(
    appointment_id
):

    con = db()

    try:

        con.execute(
            """
            DELETE FROM appointments

            WHERE id = %s

            AND status = 'Cancelado'
            """,
            (appointment_id,)
        )

        con.commit()

    finally:

        con.close()

    flash(
        "Agendamento apagado do histórico.",
        "ok"
    )

    return redirect(
        url_for("admin")
    )


# ==========================================================
# BLOQUEAR HORÁRIO
# ==========================================================

@app.post(
    "/admin/bloquear"
)
@proteger_admin
def bloquear_horario():

    day = request.form.get(
        "blocked_date",
        ""
    ).strip()

    time = request.form.get(
        "blocked_time",
        ""
    ).strip()

    reason = request.form.get(
        "reason",
        ""
    ).strip()

    # ======================================================
    # VALIDAR DATA
    # ======================================================

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
            url_for("admin")
        )

    # Não permite domingo
    if chosen.weekday() == 6:

        flash(
            "Domingo está fechado.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    # ======================================================
    # VALIDAR HORÁRIO
    # ======================================================

    if time not in slots_for(
        day
    ):

        flash(
            "Horário inválido para esse dia.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    con = db()

    try:

        # ==================================================
        # VERIFICAR SE JÁ EXISTE AGENDAMENTO
        # ==================================================

        if appointment_conflict(
            con,
            day,
            time,
            30
        ):

            flash(
                "Não é possível bloquear esse horário "
                "porque existe um agendamento ativo nele.",
                "error"
            )

            con.close()

            return redirect(
                url_for("admin")
            )

        # ==================================================
        # INSERIR BLOQUEIO
        # ==================================================

        con.execute(
            """
            INSERT INTO blocked_slots
            (
                blocked_date,
                blocked_time,
                reason,
                created_at
            )

            VALUES
            (
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                day,
                time,
                reason,
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )
        )

        con.commit()

    except IntegrityError:

        con.rollback()

        flash(
            "Esse horário já está bloqueado.",
            "error"
        )

        con.close()

        return redirect(
            url_for("admin")
        )

    con.close()

    flash(
        "Horário bloqueado com sucesso.",
        "ok"
    )

    return redirect(
        url_for("admin")
    )


# ==========================================================
# DESBLOQUEAR HORÁRIO
# ==========================================================

@app.post(
    "/admin/desbloquear/<int:block_id>"
)
@proteger_admin
def desbloquear_horario(
    block_id
):

    con = db()

    try:

        con.execute(
            """
            DELETE FROM blocked_slots

            WHERE id = %s
            """,
            (block_id,)
        )

        con.commit()

    finally:

        con.close()

    flash(
        "Horário desbloqueado.",
        "ok"
    )

    return redirect(
        url_for("admin")
    )


# ==========================================================
# API - HORÁRIOS DO ADMIN
# ==========================================================

@app.route(
    "/admin/horarios-bloqueio"
)
@proteger_admin
def admin_horarios_bloqueio():

    day = request.args.get(
        "date",
        ""
    ).strip()

    if not day:

        return jsonify({
            "success": False,
            "message": "Data não informada."
        }), 400

    try:

        datetime.strptime(
            day,
            "%Y-%m-%d"
        )

    except ValueError:

        return jsonify({
            "success": False,
            "message": "Data inválida."
        }), 400

    # Horários de trabalho
    work_slots = slots_for(day)

    con = db()

    try:

        blocked = con.execute(
            """
            SELECT
                id,
                blocked_time,
                reason

            FROM blocked_slots

            WHERE blocked_date = %s

            ORDER BY blocked_time
            """,
            (day,)
        ).fetchall()

        appointments = con.execute(
            """
            SELECT
                appointment_time,
                service,
                name

            FROM appointments

            WHERE appointment_date = %s

            AND status <> 'Cancelado'

            ORDER BY appointment_time
            """,
            (day,)
        ).fetchall()

    finally:

        con.close()

    return jsonify({

        "success": True,

        "slots": work_slots,

        "blocked": [
            {
                "id": item["id"],
                "time": item["blocked_time"],
                "reason": item["reason"] or ""
            }
            for item in blocked
        ],

        "appointments": [
            {
                "time": item["appointment_time"],
                "service": item["service"],
                "name": item["name"]
            }
            for item in appointments
        ]

    })


# ==========================================================
# INICIALIZAÇÃO DO BANCO
# ==========================================================

init_db()


# ==========================================================
# EXECUTAR APLICAÇÃO
# ==========================================================

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
```
