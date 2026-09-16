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

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "troque-esta-chave-em-producao"
)


# ==========================================================
# CONFIGURAÇÕES
# ==========================================================

ADMIN_USERNAME = "Rayssa"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

DATABASE_URL = os.environ.get("DATABASE_URL")


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
    ("Plano mensal", 90, 150.00)
]


# ==========================================================
# BANCO DE DADOS
# ==========================================================

class Database:

    def __init__(self):

        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL não configurada no ambiente."
            )

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


# ==========================================================
# INICIALIZAÇÃO DO BANCO
# ==========================================================

def init_db():

    if not DATABASE_URL:

        print(
            "DATABASE_URL ausente. "
            "Pulo na inicialização do DB."
        )

        return


    con = db()

    try:

        # --------------------------------------------------
        # TABELA DE AGENDAMENTOS
        # --------------------------------------------------

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


        # --------------------------------------------------
        # GARANTE COLUNA STATUS
        # --------------------------------------------------

        con.execute(
            """
            ALTER TABLE appointments
            ADD COLUMN IF NOT EXISTS
            status TEXT NOT NULL
            DEFAULT 'Confirmado'
            """
        )


        # --------------------------------------------------
        # REMOVE CONSTRAINT ANTIGA
        # --------------------------------------------------

        con.execute(
            """
            ALTER TABLE appointments
            DROP CONSTRAINT IF EXISTS
            appointments_appointment_date_appointment_time_key
            """
        )


        # --------------------------------------------------
        # IMPEDE DOIS AGENDAMENTOS ATIVOS
        # NO MESMO HORÁRIO
        # --------------------------------------------------

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


        # --------------------------------------------------
        # TABELA DE HORÁRIOS BLOQUEADOS
        # --------------------------------------------------

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


        # --------------------------------------------------
        # IMPEDE MESMO HORÁRIO DE SER
        # BLOQUEADO DUAS VEZES
        # --------------------------------------------------

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


    except Exception as e:

        con.rollback()

        print(
            f"Erro ao inicializar o banco de dados: {e}"
        )


    finally:

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
# CONFLITO COM AGENDAMENTO
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
        new_start +
        timedelta(minutes=duration)
    )


    for appointment in appointments:

        if (
            ignore_id is not None
            and appointment["id"] == ignore_id
        ):
            continue


        existing_start = datetime.strptime(
            f"{day} "
            f"{appointment['appointment_time']}",
            "%Y-%m-%d %H:%M"
        )


        existing_duration = service_duration(
            appointment["service"]
        )


        existing_end = (
            existing_start +
            timedelta(minutes=existing_duration)
        )


        if (
            new_start < existing_end
            and new_end > existing_start
        ):

            return True


    return False


# ==========================================================
# CONFLITO COM HORÁRIO BLOQUEADO
# ==========================================================

def blocked_slot_conflict(
    con,
    day,
    start_time,
    duration
):

    blocked = con.execute(
        """
        SELECT blocked_time

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
        new_start +
        timedelta(minutes=duration)
    )


    for item in blocked:

        blocked_start = datetime.strptime(
            f"{day} "
            f"{item['blocked_time']}",
            "%Y-%m-%d %H:%M"
        )


        # Cada bloqueio representa 30 minutos
        blocked_end = (
            blocked_start +
            timedelta(minutes=30)
        )


        if (
            new_start < blocked_end
            and new_end > blocked_start
        ):

            return True


    return False


# ==========================================================
# GERA HORÁRIOS DISPONÍVEIS
# ==========================================================

def slots_for(day, service=None):

    d = datetime.strptime(
        day,
        "%Y-%m-%d"
    ).date()


    # Domingo fechado
    if d.weekday() == 6:
        return []


    duration = (
        service_duration(service)
        if service
        else 30
    )


    slots = []


    # ------------------------------------------------------
    # SEGUNDA A SEXTA
    # ------------------------------------------------------

    if d.weekday() <= 4:

        # Quarta-feira
        if d.weekday() == 2:

            periods = [
                (7, 30, 11, 0),
                (13, 30, 15, 0)
            ]


        # Segunda, Terça, Quinta e Sexta
        else:

            periods = [
                (7, 30, 11, 0),
                (13, 30, 20, 0)
            ]


    # ------------------------------------------------------
    # SÁBADO
    # ------------------------------------------------------

    else:

        periods = [
            (9, 0, 16, 0)
        ]


    # ------------------------------------------------------
    # CRIA OS HORÁRIOS DE 30 EM 30 MINUTOS
    # ------------------------------------------------------

    for (
        start_hour,
        start_minute,
        end_hour,
        end_minute
    ) in periods:


        period_start = (
            datetime.combine(
                d,
                datetime.min.time()
            )
            .replace(
                hour=start_hour,
                minute=start_minute
            )
        )


        period_end = (
            datetime.combine(
                d,
                datetime.min.time()
            )
            .replace(
                hour=end_hour,
                minute=end_minute
            )
        )


        current = period_start


        allowed_end = (
            period_end +
            timedelta(minutes=30)
        )


        while current <= period_end:

            appointment_end = (
                current +
                timedelta(minutes=duration)
            )


            if appointment_end <= allowed_end:

                slots.append(
                    current.strftime("%H:%M")
                )


            current += timedelta(minutes=30)


    return slots


# ==========================================================
# CONTEXTO GLOBAL
# ==========================================================

@app.context_processor
def inject_today():

    return {
        "now": date.today().isoformat()
    }


# ==========================================================
# ROTA PRINCIPAL
# ==========================================================

@app.route("/")
def index():

    return render_template(
        "index.html",
        services=SERVICES
    )


# ==========================================================
# HORÁRIOS PARA CLIENTE
# ==========================================================

@app.route("/horarios")
def horarios():

    day = request.args.get(
        "date",
        ""
    ).strip()


    service = request.args.get(
        "service",
        ""
    ).strip()


    if not day:

        return {
            "slots": []
        }


    try:

        datetime.strptime(
            day,
            "%Y-%m-%d"
        )

    except ValueError:

        return {
            "slots": []
        }


    con = db()


    try:

        # --------------------------------------------------
        # AGENDAMENTOS
        # --------------------------------------------------

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


        # --------------------------------------------------
        # BLOQUEIOS
        # --------------------------------------------------

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


    blocked_times = {
        item["blocked_time"]
        for item in blocked
    }


    # ------------------------------------------------------
    # ANALISA CADA HORÁRIO
    # ------------------------------------------------------

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


        # --------------------------------------------------
        # VERIFICA AGENDAMENTOS
        # --------------------------------------------------

        for appointment in appointments:

            existing_start = datetime.strptime(
                f"{day} "
                f"{appointment['appointment_time']}",
                "%Y-%m-%d %H:%M"
            )


            existing_duration = service_duration(
                appointment["service"]
            )


            existing_end = (
                existing_start +
                timedelta(
                    minutes=existing_duration
                )
            )


            if (
                slot_start < existing_end
                and slot_end > existing_start
            ):

                conflict = True

                break


        if conflict:
            continue


        # --------------------------------------------------
        # VERIFICA BLOQUEIOS
        # --------------------------------------------------

        for blocked_time in blocked_times:

            blocked_start = datetime.strptime(
                f"{day} {blocked_time}",
                "%Y-%m-%d %H:%M"
            )


            blocked_end = (
                blocked_start +
                timedelta(minutes=30)
            )


            if (
                slot_start < blocked_end
                and slot_end > blocked_start
            ):

                conflict = True

                break


        if not conflict:

            available.append(slot)


    return {
        "slots": available
    }


# ==========================================================
# AGENDAR
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


    # ------------------------------------------------------
    # CAMPOS OBRIGATÓRIOS
    # ------------------------------------------------------

    if (
        not all([
            name,
            phone,
            service,
            day,
            time
        ])
        or service not in valid_services
    ):

        flash(
            "Preencha todos os campos obrigatórios.",
            "error"
        )

        return redirect(
            url_for("index")
        )


    # ------------------------------------------------------
    # VALIDA DATA
    # ------------------------------------------------------

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


    # ------------------------------------------------------
    # NÃO PERMITE DATA PASSADA
    # ------------------------------------------------------

    if chosen < date.today():

        flash(
            "Não é possível agendar para uma data passada.",
            "error"
        )

        return redirect(
            url_for("index")
        )


    # ------------------------------------------------------
    # VALIDA HORÁRIO
    # ------------------------------------------------------

    if time not in slots_for(
        day,
        service
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

        duration = service_duration(
            service
        )


        # --------------------------------------------------
        # VERIFICA BLOQUEIO
        # --------------------------------------------------

        if blocked_slot_conflict(
            con,
            day,
            time,
            duration
        ):

            flash(
                "Esse horário está bloqueado.",
                "error"
            )

            return redirect(
                url_for("index")
            )


        # --------------------------------------------------
        # VERIFICA CONFLITO
        # --------------------------------------------------

        if appointment_conflict(
            con,
            day,
            time,
            duration
        ):

            flash(
                "Esse horário acabou de ser reservado. "
                "Escolha outro.",
                "error"
            )

            return redirect(
                url_for("index")
            )


        # --------------------------------------------------
        # SALVA AGENDAMENTO
        # --------------------------------------------------

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

        flash(
            "Esse horário acabou de ser reservado. "
            "Escolha outro.",
            "error"
        )

        return redirect(
            url_for("index")
        )


    except Exception as e:

        con.rollback()

        print(
            f"Erro ao realizar agendamento: {e}"
        )

        flash(
            "Ocorreu um erro ao realizar o agendamento.",
            "error"
        )

        return redirect(
            url_for("index")
        )


    finally:

        con.close()


    # ------------------------------------------------------
    # CONFIRMAÇÃO
    # ------------------------------------------------------

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

            WHERE regexp_replace(
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


    result = [

        {
            "id": appt["id"],

            "name": appt["name"],

            "service": appt["service"],

            "date": appt["appointment_date"],

            "time": appt["appointment_time"],

            "notes": appt["notes"] or "",

            "status": appt["status"]
        }

        for appt in appointments

    ]


    return {
        "success": True,
        "appointments": result
    }


# ==========================================================
# PROTEÇÃO ADMINISTRATIVA
# ==========================================================

def proteger_admin(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        auth = request.authorization


        if (
            not auth
            or auth.username != ADMIN_USERNAME
            or auth.password != ADMIN_PASSWORD
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
# PAINEL ADMINISTRATIVO
# ==========================================================

@app.route("/admin")
@proteger_admin
def admin():

    con = db()


    try:

        # --------------------------------------------------
        # TODOS OS AGENDAMENTOS
        # --------------------------------------------------

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
                created_at,
                status

            FROM appointments

            ORDER BY
                appointment_date DESC,
                appointment_time DESC
            """
        ).fetchall()


        # --------------------------------------------------
        # HORÁRIOS BLOQUEADOS
        # --------------------------------------------------

        blocked_slots = con.execute(
            """
            SELECT
                id,
                blocked_date,
                blocked_time,
                reason,
                created_at

            FROM blocked_slots

            ORDER BY
                blocked_date ASC,
                blocked_time ASC
            """
        ).fetchall()


    finally:

        con.close()


    return render_template(
        "admin.html",
        appointments=appointments,
        blocked_slots=blocked_slots,
        now=date.today().isoformat()
    )


# ==========================================================
# CANCELAR AGENDAMENTO
# ==========================================================

@app.route(
    "/admin/cancelar/<int:appointment_id>",
    methods=["POST"]
)
@proteger_admin
def cancelar(appointment_id):

    con = db()


    try:

        appointment = con.execute(
            """
            SELECT
                id,
                status

            FROM appointments

            WHERE id = %s
            """,
            (appointment_id,)
        ).fetchone()


        if not appointment:

            flash(
                "Agendamento não encontrado.",
                "error"
            )

            return redirect(
                url_for("admin")
            )


        # --------------------------------------------------
        # JÁ ESTÁ CANCELADO
        # --------------------------------------------------

        if appointment["status"] == "Cancelado":

            flash(
                "Esse agendamento já está cancelado.",
                "error"
            )

            return redirect(
                url_for("admin")
            )


        # --------------------------------------------------
        # CANCELA
        # --------------------------------------------------

        con.execute(
            """
            UPDATE appointments

            SET status = 'Cancelado'

            WHERE id = %s
            """,
            (appointment_id,)
        )


        con.commit()


        flash(
            "Agendamento cancelado com sucesso.",
            "success"
        )


    except Exception as e:

        con.rollback()

        print(
            f"Erro ao cancelar agendamento: {e}"
        )

        flash(
            "Não foi possível cancelar o agendamento.",
            "error"
        )


    finally:

        con.close()


    return redirect(
        url_for("admin")
    )


# ==========================================================
# APAGAR AGENDAMENTO CANCELADO
# DO HISTÓRICO
# ==========================================================

@app.route(
    "/admin/apagar-historico/<int:appointment_id>",
    methods=["POST"]
)
@proteger_admin
def apagar_historico(appointment_id):

    con = db()


    try:

        appointment = con.execute(
            """
            SELECT
                id,
                status

            FROM appointments

            WHERE id = %s
            """,
            (appointment_id,)
        ).fetchone()


        if not appointment:

            flash(
                "Agendamento não encontrado.",
                "error"
            )

            return redirect(
                url_for("admin")
            )


        # --------------------------------------------------
        # SÓ PERMITE APAGAR CANCELADOS
        # --------------------------------------------------

        if appointment["status"] != "Cancelado":

            flash(
                "Somente agendamentos cancelados "
                "podem ser apagados do histórico.",
                "error"
            )

            return redirect(
                url_for("admin")
            )


        # --------------------------------------------------
        # APAGA
        # --------------------------------------------------

        con.execute(
            """
            DELETE FROM appointments

            WHERE id = %s

            AND status = 'Cancelado'
            """,
            (appointment_id,)
        )


        con.commit()


        flash(
            "Agendamento apagado do histórico.",
            "success"
        )


    except Exception as e:

        con.rollback()

        print(
            f"Erro ao apagar histórico: {e}"
        )

        flash(
            "Não foi possível apagar o agendamento.",
            "error"
        )


    finally:

        con.close()


    return redirect(
        url_for("admin")
    )


# ==========================================================
# BLOQUEAR HORÁRIO
# ==========================================================

@app.route(
    "/admin/bloquear-horario",
    methods=["POST"]
)
@proteger_admin
def bloquear_horario():

    blocked_date = request.form.get(
        "blocked_date",
        ""
    ).strip()


    blocked_time = request.form.get(
        "blocked_time",
        ""
    ).strip()


    reason = request.form.get(
        "reason",
        ""
    ).strip()


    # ------------------------------------------------------
    # VALIDA DATA
    # ------------------------------------------------------

    try:

        selected_date = datetime.strptime(
            blocked_date,
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


    # ------------------------------------------------------
    # NÃO PERMITE DATA PASSADA
    # ------------------------------------------------------

    if selected_date < date.today():

        flash(
            "Não é possível bloquear uma data passada.",
            "error"
        )

        return redirect(
            url_for("admin")
        )


    # ------------------------------------------------------
    # VALIDA HORÁRIO
    # ------------------------------------------------------

    available_slots = slots_for(
        blocked_date
    )


    if blocked_time not in available_slots:

        flash(
            "Horário inválido para essa data.",
            "error"
        )

        return redirect(
            url_for("admin")
        )


    con = db()


    try:

        # --------------------------------------------------
        # VERIFICA SE JÁ EXISTE AGENDAMENTO
        # --------------------------------------------------

        appointment = con.execute(
            """
            SELECT
                id,
                name,
                service

            FROM appointments

            WHERE appointment_date = %s

            AND appointment_time = %s

            AND status <> 'Cancelado'

            LIMIT 1
            """,
            (
                blocked_date,
                blocked_time
            )
        ).fetchone()


        if appointment:

            flash(
                "Esse horário já possui um agendamento "
                "e não pode ser bloqueado.",
                "error"
            )

            return redirect(
                url_for("admin")
            )


        # --------------------------------------------------
        # VERIFICA CONFLITO COM OUTROS AGENDAMENTOS
        # --------------------------------------------------

        if appointment_conflict(
            con,
            blocked_date,
            blocked_time,
            30
        ):

            flash(
                "Esse horário está dentro do período "
                "de um agendamento existente.",
                "error"
            )

            return redirect(
                url_for("admin")
            )


        # --------------------------------------------------
        # INSERE BLOQUEIO
        # --------------------------------------------------

        con.execute(
            """
            INSERT INTO blocked_slots
            (
                blocked_date,
                blocked_time,
                reason,
                created_at
            )

            VALUES (
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                blocked_date,
                blocked_time,
                reason,
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )
        )


        con.commit()


        flash(
            "Horário bloqueado com sucesso.",
            "success"
        )


    except IntegrityError:

        con.rollback()

        flash(
            "Esse horário já está bloqueado.",
            "error"
        )


    except Exception as e:

        con.rollback()

        print(
            f"Erro ao bloquear horário: {e}"
        )

        flash(
            "Não foi possível bloquear o horário.",
            "error"
        )


    finally:

        con.close()


    return redirect(
        url_for("admin")
    )


# ==========================================================
# DESBLOQUEAR HORÁRIO
# ==========================================================

@app.route(
    "/admin/desbloquear-horario/<int:block_id>",
    methods=["POST"]
)
@proteger_admin
def desbloquear_horario(block_id):

    con = db()


    try:

        block = con.execute(
            """
            SELECT
                id,
                blocked_date,
                blocked_time

            FROM blocked_slots

            WHERE id = %s
            """,
            (block_id,)
        ).fetchone()


        if not block:

            flash(
                "Horário bloqueado não encontrado.",
                "error"
            )

            return redirect(
                url_for("admin")
            )


        # --------------------------------------------------
        # REMOVE BLOQUEIO
        # --------------------------------------------------

        con.execute(
            """
            DELETE FROM blocked_slots

            WHERE id = %s
            """,
            (block_id,)
        )


        con.commit()


        flash(
            "Horário desbloqueado com sucesso.",
            "success"
        )


    except Exception as e:

        con.rollback()

        print(
            f"Erro ao desbloquear horário: {e}"
        )

        flash(
            "Não foi possível desbloquear o horário.",
            "error"
        )


    finally:

        con.close()


    return redirect(
        url_for("admin")
    )


# ==========================================================
# API DO CALENDÁRIO DE BLOQUEIO
# ==========================================================

@app.route(
    "/admin/horarios-bloqueio"
)
@proteger_admin
def horarios_bloqueio():

    day = request.args.get(
        "date",
        ""
    ).strip()


    # ------------------------------------------------------
    # VALIDA DATA
    # ------------------------------------------------------

    try:

        datetime.strptime(
            day,
            "%Y-%m-%d"
        )


    except ValueError:

        return jsonify({
            "success": False,
            "message": "Data inválida.",
            "slots": [],
            "blocked": [],
            "appointments": []
        }), 400


    con = db()


    try:

        # --------------------------------------------------
        # HORÁRIOS BLOQUEADOS
        # --------------------------------------------------

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


        # --------------------------------------------------
        # AGENDAMENTOS
        # --------------------------------------------------

        appointments = con.execute(
            """
            SELECT
                id,
                appointment_time,
                name,
                service,
                status

            FROM appointments

            WHERE appointment_date = %s

            AND status <> 'Cancelado'

            ORDER BY appointment_time
            """,
            (day,)
        ).fetchall()


    finally:

        con.close()


    # ------------------------------------------------------
    # HORÁRIOS NORMAIS
    # ------------------------------------------------------

    slots = slots_for(day)


    # ------------------------------------------------------
    # FORMATA BLOQUEIOS
    # ------------------------------------------------------

    blocked_result = [

        {
            "id": item["id"],
            "time": item["blocked_time"],
            "reason": item["reason"] or ""
        }

        for item in blocked

    ]


    # ------------------------------------------------------
    # FORMATA AGENDAMENTOS
    # ------------------------------------------------------

    appointments_result = [

        {
            "id": item["id"],
            "time": item["appointment_time"],
            "name": item["name"],
            "service": item["service"],
            "status": item["status"]
        }

        for item in appointments

    ]


    return jsonify({

        "success": True,

        "slots": slots,

        "blocked": blocked_result,

        "appointments": appointments_result

    })


# ==========================================================
# INICIALIZAÇÃO
# ==========================================================

with app.app_context():

    try:

        init_db()

    except Exception as e:

        print(
            "Falha ao rodar init_db "
            f"na inicialização: {e}"
        )


# ==========================================================
# EXECUÇÃO
# ==========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        )
    )
