from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify
)

import os
from functools import wraps
from datetime import datetime, date, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor


# ==========================================================
# APLICAÇÃO
# ==========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "troque-esta-chave"
)


# ==========================================================
# CONFIGURAÇÕES
# ==========================================================

ADMIN_USERNAME = "Rayssa"

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "123456"
)

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
    ("Plano mensal", 90, 150.00),
]


# ==========================================================
# BANCO DE DADOS
# ==========================================================

def get_db():
    """
    Abre uma conexão com o PostgreSQL.
    """

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL não foi configurada."
        )

    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


def init_db():
    """
    Garante que as tabelas necessárias existam.

    IMPORTANTE:
    IF NOT EXISTS não apaga os dados existentes.
    """

    con = get_db()

    try:
        cur = con.cursor()

        # --------------------------------------------------
        # AGENDAMENTOS
        # --------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'active'
            )
        """)

        # --------------------------------------------------
        # BLOQUEIOS
        # --------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS blocked_slots (
                id SERIAL PRIMARY KEY,
                blocked_date TEXT NOT NULL,
                blocked_time TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # --------------------------------------------------
        # EVITA DUPLICIDADE DE BLOQUEIOS
        # --------------------------------------------------

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            blocked_slots_date_time_unique
            ON blocked_slots (
                blocked_date,
                blocked_time
            )
        """)

        con.commit()

        cur.close()

    finally:
        con.close()


# ==========================================================
# SERVIÇOS AUXILIARES
# ==========================================================

def service_duration(service_name):
    """
    Retorna a duração do serviço em minutos.
    """

    for name, duration, price in SERVICES:
        if name == service_name:
            return duration

    return 30


def service_price(service_name):
    """
    Retorna o preço do serviço.
    """

    for name, duration, price in SERVICES:
        if name == service_name:
            return price

    return 0.00


def valid_service(service_name):
    """
    Verifica se o serviço existe na lista oficial.
    """

    return any(
        name == service_name
        for name, duration, price in SERVICES
    )


# ==========================================================
# HORÁRIOS DE FUNCIONAMENTO
# ==========================================================

def business_periods(day):
    """
    Retorna os períodos de funcionamento de determinado dia.

    Segunda, terça, quinta e sexta:
        07:30 - 11:00
        13:30 - 20:00

    Quarta:
        07:30 - 11:00
        13:30 - 15:00

    Sábado:
        09:00 - 16:00

    Domingo:
        fechado
    """

    weekday = day.weekday()

    # Segunda, terça, quinta e sexta
    if weekday in [0, 1, 3, 4]:
        return [
            ("07:30", "11:00"),
            ("13:30", "20:00")
        ]

    # Quarta-feira
    if weekday == 2:
        return [
            ("07:30", "11:00"),
            ("13:30", "15:00")
        ]

    # Sábado
    if weekday == 5:
        return [
            ("09:00", "16:00")
        ]

    # Domingo
    return []


# ==========================================================
# GERA HORÁRIOS POSSÍVEIS
# ==========================================================

def slots_for(day, service=None):
    """
    Gera os horários disponíveis estruturalmente
    para determinado dia.

    Os horários são gerados de 30 em 30 minutos.

    Um serviço precisa conseguir terminar dentro
    do período de funcionamento.
    """

    periods = business_periods(day)

    if not periods:
        return []

    slots = []

    # Duração do serviço escolhido
    duration = service_duration(service) if service else 30

    for start, end in periods:

        current = datetime.strptime(
            start,
            "%H:%M"
        )

        period_end = datetime.strptime(
            end,
            "%H:%M"
        )

        # O atendimento pode terminar até
        # 30 minutos depois do final configurado.
        allowed_end = (
            period_end +
            timedelta(minutes=30)
        )

        while current < allowed_end:

            # ----------------------------------------------
            # SE FOR HOJE, ESCONDE HORÁRIOS PASSADOS
            # ----------------------------------------------

            if day == date.today():

                now = datetime.now()

                slot_datetime = datetime.combine(
                    day,
                    current.time()
                )

                if slot_datetime <= now:
                    current += timedelta(minutes=30)
                    continue

            # ----------------------------------------------
            # VERIFICA SE O SERVIÇO CABE NO PERÍODO
            # ----------------------------------------------

            service_end = (
                current +
                timedelta(minutes=duration)
            )

            if service_end > allowed_end:
                current += timedelta(minutes=30)
                continue

            slots.append(
                current.strftime("%H:%M")
            )

            current += timedelta(minutes=30)

    return slots


# ==========================================================
# CONVERTE DATA + HORA PARA DATETIME
# ==========================================================

def make_datetime(appointment_date, appointment_time):
    """
    Converte:
        2026-09-20 + 13:30

    para um objeto datetime.
    """

    return datetime.strptime(
        f"{appointment_date} {appointment_time}",
        "%Y-%m-%d %H:%M"
    )


# ==========================================================
# VERIFICA CONFLITO COM AGENDAMENTOS
# ==========================================================

def appointment_conflict(
    con,
    appointment_date,
    appointment_time,
    service,
    ignore_id=None
):
    """
    Verifica se o novo atendimento sobrepõe
    algum atendimento existente.
    """

    new_start = make_datetime(
        appointment_date,
        appointment_time
    )

    new_end = (
        new_start +
        timedelta(
            minutes=service_duration(service)
        )
    )

    cur = con.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute("""
        SELECT
            id,
            date,
            time,
            service
        FROM appointments
        WHERE date = %s
        AND status = 'active'
    """, (
        appointment_date,
    ))

    appointments = cur.fetchall()

    cur.close()

    for appointment in appointments:

        # Ignora o próprio agendamento quando
        # necessário em futuras alterações.
        if (
            ignore_id is not None
            and appointment["id"] == ignore_id
        ):
            continue

        existing_start = make_datetime(
            appointment["date"],
            appointment["time"]
        )

        existing_end = (
            existing_start +
            timedelta(
                minutes=service_duration(
                    appointment["service"]
                )
            )
        )

        # Existe sobreposição?
        if (
            new_start < existing_end
            and new_end > existing_start
        ):
            return True

    return False


# ==========================================================
# VERIFICA CONFLITO COM BLOQUEIOS
# ==========================================================

def blocked_slot_conflict(
    con,
    appointment_date,
    appointment_time,
    service
):
    """
    Verifica se o atendimento passa por algum
    horário bloqueado.
    """

    start = make_datetime(
        appointment_date,
        appointment_time
    )

    end = (
        start +
        timedelta(
            minutes=service_duration(service)
        )
    )

    cur = con.cursor()

    cur.execute("""
        SELECT blocked_time
        FROM blocked_slots
        WHERE blocked_date = %s
    """, (
        appointment_date,
    ))

    blocked_rows = cur.fetchall()

    cur.close()

    for row in blocked_rows:

        blocked_start = make_datetime(
            appointment_date,
            row[0]
        )

        blocked_end = (
            blocked_start +
            timedelta(minutes=30)
        )

        if (
            start < blocked_end
            and end > blocked_start
        ):
            return True

    return False


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
# API DE HORÁRIOS
# ==========================================================

@app.route("/horarios")
def horarios():

    date_str = request.args.get(
        "date",
        ""
    ).strip()

    service = request.args.get(
        "service",
        ""
    ).strip()

    # ------------------------------------------------------
    # SEM DATA
    # ------------------------------------------------------

    if not date_str:
        return jsonify({
            "slots": []
        })

    # ------------------------------------------------------
    # VALIDA DATA
    # ------------------------------------------------------

    try:

        selected_date = datetime.strptime(
            date_str,
            "%Y-%m-%d"
        ).date()

    except ValueError:

        return jsonify({
            "slots": []
        })

    # ------------------------------------------------------
    # SERVIÇO INVÁLIDO
    # ------------------------------------------------------

    if service and not valid_service(service):

        return jsonify({
            "slots": []
        })

    # ------------------------------------------------------
    # DATA PASSADA
    # ------------------------------------------------------

    if selected_date < date.today():

        return jsonify({
            "slots": []
        })

    # ------------------------------------------------------
    # GERA HORÁRIOS
    # ------------------------------------------------------

    slots = slots_for(
        selected_date,
        service
    )

    # ------------------------------------------------------
    # BANCO
    # ------------------------------------------------------

    con = get_db()

    try:

        cur = con.cursor(
            cursor_factory=RealDictCursor
        )

        # Agendamentos
        cur.execute("""
            SELECT
                date,
                time,
                service
            FROM appointments
            WHERE date = %s
            AND status = 'active'
        """, (
            date_str,
        ))

        appointments = cur.fetchall()

        # Bloqueios
        cur.execute("""
            SELECT
                blocked_time
            FROM blocked_slots
            WHERE blocked_date = %s
        """, (
            date_str,
        ))

        blocked_rows = cur.fetchall()

        cur.close()

    finally:
        con.close()

    blocked_times = {
        row["blocked_time"]
        for row in blocked_rows
    }

    available = []

    requested_duration = service_duration(
        service
    )

    # ======================================================
    # ANALISA CADA HORÁRIO
    # ======================================================

    for slot in slots:

        slot_start = make_datetime(
            date_str,
            slot
        )

        slot_end = (
            slot_start +
            timedelta(
                minutes=requested_duration
            )
        )

        conflict = False

        # --------------------------------------------------
        # AGENDAMENTOS
        # --------------------------------------------------

        for appointment in appointments:

            appointment_start = make_datetime(
                date_str,
                appointment["time"]
            )

            appointment_end = (
                appointment_start +
                timedelta(
                    minutes=service_duration(
                        appointment["service"]
                    )
                )
            )

            if (
                slot_start < appointment_end
                and slot_end > appointment_start
            ):
                conflict = True
                break

        if conflict:
            continue

        # --------------------------------------------------
        # BLOQUEIOS
        # --------------------------------------------------

        for blocked_time in blocked_times:

            blocked_start = make_datetime(
                date_str,
                blocked_time
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

    return jsonify({
        "slots": available
    })


# ==========================================================
# REALIZA AGENDAMENTO
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

    appointment_date = request.form.get(
        "date",
        ""
    ).strip()

    appointment_time = request.form.get(
        "time",
        ""
    ).strip()

    # ------------------------------------------------------
    # CAMPOS OBRIGATÓRIOS
    # ------------------------------------------------------

    if not all([
        name,
        phone,
        service,
        appointment_date,
        appointment_time
    ]):

        flash(
            "Preencha todos os campos.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    # ------------------------------------------------------
    # SERVIÇO
    # ------------------------------------------------------

    if not valid_service(service):

        flash(
            "Serviço inválido.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    # ------------------------------------------------------
    # DATA
    # ------------------------------------------------------

    try:

        selected_date = datetime.strptime(
            appointment_date,
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
    # DATA PASSADA
    # ------------------------------------------------------

    if selected_date < date.today():

        flash(
            "Não é possível agendar em uma data passada.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    # ------------------------------------------------------
    # HORÁRIO
    # ------------------------------------------------------

    try:

        datetime.strptime(
            appointment_time,
            "%H:%M"
        )

    except ValueError:

        flash(
            "Horário inválido.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    # ------------------------------------------------------
    # HORÁRIO PASSADO HOJE
    # ------------------------------------------------------

    if selected_date == date.today():

        selected_datetime = make_datetime(
            appointment_date,
            appointment_time
        )

        if selected_datetime <= datetime.now():

            flash(
                "Esse horário já passou.",
                "error"
            )

            return redirect(
                url_for("index")
            )

    # ------------------------------------------------------
    # HORÁRIO PRECISA SER VÁLIDO
    # ------------------------------------------------------

    valid_slots = slots_for(
        selected_date,
        service
    )

    if appointment_time not in valid_slots:

        flash(
            "Esse horário não está disponível.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    # ------------------------------------------------------
    # BANCO
    # ------------------------------------------------------

    con = get_db()

    try:

        # --------------------------------------------------
        # BLOQUEIO
        # --------------------------------------------------

        if blocked_slot_conflict(
            con,
            appointment_date,
            appointment_time,
            service
        ):

            flash(
                "Esse horário está bloqueado.",
                "error"
            )

            return redirect(
                url_for("index")
            )

        # --------------------------------------------------
        # OUTRO AGENDAMENTO
        # --------------------------------------------------

        if appointment_conflict(
            con,
            appointment_date,
            appointment_time,
            service
        ):

            flash(
                "Esse horário já está ocupado.",
                "error"
            )

            return redirect(
                url_for("index")
            )

        # --------------------------------------------------
        # SALVA
        # --------------------------------------------------

        cur = con.cursor()

        cur.execute("""
            INSERT INTO appointments (
                name,
                phone,
                service,
                date,
                time,
                created_at,
                status
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                'active'
            )
        """, (
            name,
            phone,
            service,
            appointment_date,
            appointment_time,
            datetime.now().isoformat()
        ))

        con.commit()

        cur.close()

        flash(
            "Agendamento realizado com sucesso!",
            "success"
        )

    except Exception as e:

        con.rollback()

        print(
            "ERRO AO AGENDAR:",
            e
        )

        flash(
            "Ocorreu um erro ao realizar o agendamento.",
            "error"
        )

    finally:

        con.close()

    return redirect(
        url_for("index")
    )


# ==========================================================
# MEUS AGENDAMENTOS
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
        return jsonify([])

    con = get_db()

    try:

        cur = con.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT
                id,
                name,
                phone,
                service,
                date,
                time,
                status
            FROM appointments
            WHERE phone = %s
            ORDER BY date, time
        """, (
            phone,
        ))

        appointments = cur.fetchall()

        cur.close()

    finally:

        con.close()

    return jsonify(
        appointments
    )


# ==========================================================
# LOGIN ADMINISTRATIVO
# ==========================================================

@app.route(
    "/admin/login",
    methods=["GET", "POST"]
)
def admin_login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if (
            username == ADMIN_USERNAME
            and password == ADMIN_PASSWORD
        ):

            response = redirect(
                url_for("admin")
            )

            response.set_cookie(
                "admin_logged",
                "1",
                httponly=True,
                samesite="Lax"
            )

            return response

        flash(
            "Usuário ou senha incorretos.",
            "error"
        )

    return render_template(
        "admin_login.html"
    )


# ==========================================================
# PROTEÇÃO DO ADMIN
# ==========================================================

def proteger_admin(view):

    @wraps(view)
    def wrapped_view(*args, **kwargs):

        if request.cookies.get(
            "admin_logged"
        ) != "1":

            return redirect(
                url_for("admin_login")
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped_view


# ==========================================================
# LOGOUT
# ==========================================================

@app.route("/admin/logout")
def admin_logout():

    response = redirect(
        url_for("admin_login")
    )

    response.delete_cookie(
        "admin_logged"
    )

    return response


# ==========================================================
# PAINEL ADMIN
# ==========================================================

@app.route("/admin")
@proteger_admin
def admin():

    con = get_db()

    try:

        cur = con.cursor(
            cursor_factory=RealDictCursor
        )

        # --------------------------------------------------
        # AGENDAMENTOS ATIVOS
        # --------------------------------------------------

        cur.execute("""
            SELECT
                id,
                name,
                phone,
                service,
                date,
                time,
                created_at,
                status
            FROM appointments
            WHERE status = 'active'
            ORDER BY date, time
        """)

        appointments = cur.fetchall()

        # --------------------------------------------------
        # BLOQUEIOS
        # --------------------------------------------------

        cur.execute("""
            SELECT
                id,
                blocked_date,
                blocked_time,
                reason,
                created_at
            FROM blocked_slots
            ORDER BY blocked_date, blocked_time
        """)

        blocked_rows = cur.fetchall()

        cur.close()

    finally:

        con.close()

    # ======================================================
    # AGRUPA BLOQUEIOS
    # ======================================================

    grouped_blocks = []

    groups = {}

    for block in blocked_rows:

        key = (
            block["blocked_date"],
            block.get("reason") or ""
        )

        groups.setdefault(
            key,
            []
        ).append(block)

    # ======================================================
    # PROCESSA GRUPOS
    # ======================================================

    for (
        group_date,
        reason
    ), blocks in groups.items():

        blocks.sort(
            key=lambda item: item["blocked_time"]
        )

        try:

            day_obj = datetime.strptime(
                group_date,
                "%Y-%m-%d"
            ).date()

        except ValueError:

            continue

        normal_slots = slots_for(
            day_obj
        )

        blocked_times = {
            block["blocked_time"]
            for block in blocks
        }

        # --------------------------------------------------
        # DIA INTEIRO
        # --------------------------------------------------

        is_full_day = (
            bool(normal_slots)
            and all(
                slot in blocked_times
                for slot in normal_slots
            )
        )

        if is_full_day:

            grouped_blocks.append({
                "id": blocks[0]["id"],
                "date": group_date,
                "start": normal_slots[0],
                "end": normal_slots[-1],
                "reason": reason,
                "day_full": True,
                "block_ids": [
                    block["id"]
                    for block in blocks
                ]
            })

            continue

        # --------------------------------------------------
        # AGRUPA HORÁRIOS CONTÍGUOS
        # --------------------------------------------------

        current_group = []

        for block in blocks:

            if not current_group:

                current_group = [
                    block
                ]

                continue

            previous = datetime.strptime(
                current_group[-1]["blocked_time"],
                "%H:%M"
            )

            current = datetime.strptime(
                block["blocked_time"],
                "%H:%M"
            )

            difference = (
                current - previous
            )

            if difference == timedelta(
                minutes=30
            ):

                current_group.append(
                    block
                )

            else:

                first = current_group[0]
                last = current_group[-1]

                grouped_blocks.append({
                    "id": first["id"],
                    "date": group_date,
                    "start": first["blocked_time"],
                    "end": last["blocked_time"],
                    "reason": reason,
                    "day_full": False,
                    "block_ids": [
                        item["id"]
                        for item in current_group
                    ]
                })

                current_group = [
                    block
                ]

        # --------------------------------------------------
        # ÚLTIMO GRUPO
        # --------------------------------------------------

        if current_group:

            first = current_group[0]
            last = current_group[-1]

            grouped_blocks.append({
                "id": first["id"],
                "date": group_date,
                "start": first["blocked_time"],
                "end": last["blocked_time"],
                "reason": reason,
                "day_full": False,
                "block_ids": [
                    item["id"]
                    for item in current_group
                ]
            })

    # ======================================================
    # ORDENA
    # ======================================================

    grouped_blocks.sort(
        key=lambda item: (
            item["date"],
            item["start"]
        )
    )

    return render_template(
        "admin.html",
        appointments=appointments,
        blocked_slots=grouped_blocks,
        services=SERVICES
    )


# ==========================================================
# CANCELAR AGENDAMENTO
# ==========================================================

@app.route(
    "/admin/cancelar/<int:appointment_id>",
    methods=["POST"]
)
@proteger_admin
def cancelar_agendamento(
    appointment_id
):

    con = get_db()

    try:

        cur = con.cursor()

        cur.execute("""
            UPDATE appointments
            SET status = 'Cancelado'
            WHERE id = %s
        """, (
            appointment_id,
        ))

        con.commit()

        cur.close()

        flash(
            "Agendamento cancelado.",
            "success"
        )

    except Exception as e:

        con.rollback()

        print(
            "ERRO AO CANCELAR:",
            e
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
# EXCLUIR AGENDAMENTO
# ==========================================================

@app.route(
    "/admin/excluir/<int:appointment_id>",
    methods=["POST"]
)
@proteger_admin
def excluir_agendamento(
    appointment_id
):

    con = get_db()

    try:

        cur = con.cursor()

        cur.execute("""
            DELETE FROM appointments
            WHERE id = %s
        """, (
            appointment_id,
        ))

        con.commit()

        cur.close()

        flash(
            "Agendamento excluído.",
            "success"
        )

    except Exception as e:

        con.rollback()

        print(
            "ERRO AO EXCLUIR:",
            e
        )

        flash(
            "Não foi possível excluir o agendamento.",
            "error"
        )

    finally:

        con.close()

    return redirect(
        url_for("admin")
    )


# ==========================================================
# BLOQUEAR HORÁRIO / PERÍODO / DIA
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

    block_type = request.form.get(
        "blocked_type",
        "specific"
    ).strip()

    blocked_time = request.form.get(
        "blocked_time",
        ""
    ).strip()

    start_time = request.form.get(
        "start_time",
        ""
    ).strip()

    end_time = request.form.get(
        "end_time",
        ""
    ).strip()

    reason = request.form.get(
        "reason",
        ""
    ).strip()

    # ------------------------------------------------------
    # DATA
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

    if selected_date < date.today():

        flash(
            "Não é possível bloquear uma data passada.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    # ------------------------------------------------------
    # GERA HORÁRIOS A BLOQUEAR
    # ------------------------------------------------------

    times_to_block = []

    # ------------------------------------------------------
    # HORÁRIO ESPECÍFICO
    # ------------------------------------------------------

    if block_type == "specific":

        if not blocked_time:

            flash(
                "Selecione um horário.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        if blocked_time not in slots_for(
            selected_date
        ):

            flash(
                "Esse horário não pertence à agenda.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        times_to_block = [
            blocked_time
        ]

    # ------------------------------------------------------
    # PERÍODO
    # ------------------------------------------------------

    elif block_type == "period":

        if not start_time or not end_time:

            flash(
                "Informe o horário inicial e final.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        try:

            current = datetime.strptime(
                start_time,
                "%H:%M"
            )

            finish = datetime.strptime(
                end_time,
                "%H:%M"
            )

        except ValueError:

            flash(
                "Horário inválido.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        if current >= finish:

            flash(
                "O horário inicial deve ser menor que o horário final.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        normal_slots = set(
            slots_for(selected_date)
        )

        while current < finish:

            current_string = current.strftime(
                "%H:%M"
            )

            if current_string in normal_slots:

                times_to_block.append(
                    current_string
                )

            current += timedelta(
                minutes=30
            )

    # ------------------------------------------------------
    # DIA INTEIRO
    # ------------------------------------------------------

    elif block_type == "day":

        times_to_block = slots_for(
            selected_date
        )

    else:

        flash(
            "Tipo de bloqueio inválido.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    # ------------------------------------------------------
    # NENHUM HORÁRIO
    # ------------------------------------------------------

    if not times_to_block:

        flash(
            "Nenhum horário válido foi encontrado.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    con = get_db()

    try:

        cur = con.cursor(
            cursor_factory=RealDictCursor
        )

        # --------------------------------------------------
        # AGENDAMENTOS EXISTENTES
        # --------------------------------------------------

        cur.execute("""
            SELECT
                id,
                date,
                time,
                service
            FROM appointments
            WHERE date = %s
            AND status = 'active'
        """, (
            blocked_date,
        ))

        appointments = cur.fetchall()

        # --------------------------------------------------
        # VERIFICA CONFLITOS
        # --------------------------------------------------

        for slot in times_to_block:

            slot_start = make_datetime(
                blocked_date,
                slot
            )

            slot_end = (
                slot_start +
                timedelta(minutes=30)
            )

            for appointment in appointments:

                appointment_start = make_datetime(
                    appointment["date"],
                    appointment["time"]
                )

                appointment_end = (
                    appointment_start +
                    timedelta(
                        minutes=service_duration(
                            appointment["service"]
                        )
                    )
                )

                if (
                    slot_start < appointment_end
                    and slot_end > appointment_start
                ):

                    flash(
                        f"O horário {slot} já possui um agendamento.",
                        "error"
                    )

                    cur.close()
                    con.close()

                    return redirect(
                        url_for("admin")
                    )

        # --------------------------------------------------
        # INSERE BLOQUEIOS
        # --------------------------------------------------

        for slot in times_to_block:

            cur.execute("""
                INSERT INTO blocked_slots (
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
                ON CONFLICT (
                    blocked_date,
                    blocked_time
                )
                DO NOTHING
            """, (
                blocked_date,
                slot,
                reason,
                datetime.now().isoformat()
            ))

        con.commit()

        cur.close()

        flash(
            "Bloqueio realizado com sucesso.",
            "success"
        )

    except Exception as e:

        con.rollback()

        print(
            "ERRO AO BLOQUEAR:",
            e
        )

        flash(
            "Não foi possível realizar o bloqueio.",
            "error"
        )

    finally:

        con.close()

    return redirect(
        url_for("admin")
    )


# ==========================================================
# HORÁRIOS PARA O ADMIN
# ==========================================================

@app.route(
    "/admin/horarios-bloqueio"
)
@proteger_admin
def horarios_bloqueio():

    date_str = request.args.get(
        "date",
        ""
    ).strip()

    if not date_str:

        return jsonify({
            "slots": []
        })

    try:

        selected_date = datetime.strptime(
            date_str,
            "%Y-%m-%d"
        ).date()

    except ValueError:

        return jsonify({
            "slots": []
        })

    # ------------------------------------------------------
    # HORÁRIOS NORMAIS
    # ------------------------------------------------------

    slots = slots_for(
        selected_date
    )

    con = get_db()

    try:

        cur = con.cursor(
            cursor_factory=RealDictCursor
        )

        # Bloqueios
        cur.execute("""
            SELECT
                blocked_time
            FROM blocked_slots
            WHERE blocked_date = %s
        """, (
            date_str,
        ))

        blocked_rows = cur.fetchall()

        # Agendamentos
        cur.execute("""
            SELECT
                date,
                time,
                service
            FROM appointments
            WHERE date = %s
            AND status = 'active'
        """, (
            date_str,
        ))

        appointments = cur.fetchall()

        cur.close()

    finally:

        con.close()

    blocked_times = {
        row["blocked_time"]
        for row in blocked_rows
    }

    result = []

    for slot in slots:

        slot_start = make_datetime(
            date_str,
            slot
        )

        slot_end = (
            slot_start +
            timedelta(minutes=30)
        )

        has_appointment = False

        # --------------------------------------------------
        # VERIFICA AGENDAMENTO
        # --------------------------------------------------

        for appointment in appointments:

            appointment_start = make_datetime(
                date_str,
                appointment["time"]
            )

            appointment_end = (
                appointment_start +
                timedelta(
                    minutes=service_duration(
                        appointment["service"]
                    )
                )
            )

            if (
                slot_start < appointment_end
                and slot_end > appointment_start
            ):

                has_appointment = True
                break

        result.append({
            "time": slot,
            "blocked": slot in blocked_times,
            "appointment": has_appointment
        })

    return jsonify({
        "slots": result
    })


# ==========================================================
# DESBLOQUEAR HORÁRIO / PERÍODO
# ==========================================================

@app.route(
    "/admin/desbloquear/<int:block_id>",
    methods=["POST"]
)
@proteger_admin
def desbloquear_horario(
    block_id
):

    con = get_db()

    try:

        cur = con.cursor(
            cursor_factory=RealDictCursor
        )

        # --------------------------------------------------
        # BLOQUEIO SELECIONADO
        # --------------------------------------------------

        cur.execute("""
            SELECT *
            FROM blocked_slots
            WHERE id = %s
        """, (
            block_id,
        ))

        clicked = cur.fetchone()

        if not clicked:

            cur.close()

            flash(
                "Bloqueio não encontrado.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        blocked_date = clicked[
            "blocked_date"
        ]

        clicked_reason = (
            clicked.get("reason")
            or ""
        )

        # --------------------------------------------------
        # TODOS OS BLOQUEIOS DO DIA
        # --------------------------------------------------

        cur.execute("""
            SELECT *
            FROM blocked_slots
            WHERE blocked_date = %s
            ORDER BY blocked_time
        """, (
            blocked_date,
        ))

        blocks = cur.fetchall()

        cur.close()

        # --------------------------------------------------
        # VERIFICA DIA INTEIRO
        # --------------------------------------------------

        try:

            day_obj = datetime.strptime(
                blocked_date,
                "%Y-%m-%d"
            ).date()

        except ValueError:

            day_obj = None

        is_full_day = False

        if day_obj:

            normal_slots = slots_for(
                day_obj
            )

            blocked_times = {
                block["blocked_time"]
                for block in blocks
            }

            is_full_day = (
                bool(normal_slots)
                and all(
                    slot in blocked_times
                    for slot in normal_slots
                )
            )

        # --------------------------------------------------
        # DESBLOQUEIA DIA INTEIRO
        # --------------------------------------------------

        if is_full_day:

            cur = con.cursor()

            cur.execute("""
                DELETE FROM blocked_slots
                WHERE blocked_date = %s
            """, (
                blocked_date,
            ))

            con.commit()
            cur.close()

            flash(
                "O dia inteiro foi desbloqueado.",
                "success"
            )

            return redirect(
                url_for("admin")
            )

        # --------------------------------------------------
        # FILTRA PELO MESMO MOTIVO
        # --------------------------------------------------

        same_reason_blocks = [
            block
            for block in blocks
            if (
                (block.get("reason") or "")
                == clicked_reason
            )
        ]

        same_reason_blocks.sort(
            key=lambda item: item["blocked_time"]
        )

        # --------------------------------------------------
        # LOCALIZA O BLOQUEIO CLICADO
        # --------------------------------------------------

        clicked_index = None

        for index, block in enumerate(
            same_reason_blocks
        ):

            if block["id"] == block_id:

                clicked_index = index
                break

        if clicked_index is None:

            flash(
                "Bloqueio não encontrado.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        # --------------------------------------------------
        # EXPANDE PARA TRÁS
        # --------------------------------------------------

        start_index = clicked_index

        while start_index > 0:

            current = datetime.strptime(
                same_reason_blocks[
                    start_index
                ]["blocked_time"],
                "%H:%M"
            )

            previous = datetime.strptime(
                same_reason_blocks[
                    start_index - 1
                ]["blocked_time"],
                "%H:%M"
            )

            if (
                current - previous
                == timedelta(minutes=30)
            ):

                start_index -= 1

            else:

                break

        # --------------------------------------------------
        # EXPANDE PARA FRENTE
        # --------------------------------------------------

        end_index = clicked_index

        while (
            end_index
            < len(same_reason_blocks) - 1
        ):

            current = datetime.strptime(
                same_reason_blocks[
                    end_index
                ]["blocked_time"],
                "%H:%M"
            )

            next_time = datetime.strptime(
                same_reason_blocks[
                    end_index + 1
                ]["blocked_time"],
                "%H:%M"
            )

            if (
                next_time - current
                == timedelta(minutes=30)
            ):

                end_index += 1

            else:

                break

        # --------------------------------------------------
        # PEGA IDs DO PERÍODO
        # --------------------------------------------------

        period_blocks = same_reason_blocks[
            start_index:end_index + 1
        ]

        ids_to_delete = [
            block["id"]
            for block in period_blocks
        ]

        # --------------------------------------------------
        # EXCLUI
        # --------------------------------------------------

        cur = con.cursor()

        cur.execute("""
            DELETE FROM blocked_slots
            WHERE id = ANY(%s)
        """, (
            ids_to_delete,
        ))

        con.commit()

        cur.close()

        flash(
            "Período desbloqueado com sucesso.",
            "success"
        )

    except Exception as e:

        con.rollback()

        print(
            "ERRO AO DESBLOQUEAR:",
            e
        )

        flash(
            "Não foi possível desbloquear o período.",
            "error"
        )

    finally:

        con.close()

    return redirect(
        url_for("admin")
    )


# ==========================================================
# INICIALIZAÇÃO
# ==========================================================

try:

    init_db()

except Exception as e:

    print(
        "ERRO AO INICIALIZAR BANCO:",
        e
    )


# ==========================================================
# EXECUÇÃO LOCAL
# ==========================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )
