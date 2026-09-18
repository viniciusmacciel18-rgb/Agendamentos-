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
    ("Plano mensal", 90, 150.00)
]


# ==========================================================
# CONEXÃO COM BANCO
# ==========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL não foi configurada."
        )

    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


# ==========================================================
# INICIALIZAÇÃO DO BANCO
# ==========================================================

def init_db():

    con = get_db()
    cur = con.cursor()

    try:

        # --------------------------------------------------
        # TABELA DE AGENDAMENTOS
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
        # TABELA DE BLOQUEIOS
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
        # EVITA DUPLICIDADE DE BLOQUEIO
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

    finally:

        cur.close()
        con.close()


# ==========================================================
# SERVIÇOS AUXILIARES
# ==========================================================

def service_duration(service_name):

    for name, duration, price in SERVICES:

        if name == service_name:
            return duration

    return 30


def service_price(service_name):

    for name, duration, price in SERVICES:

        if name == service_name:
            return price

    return 0.00


# ==========================================================
# GERA HORÁRIOS DISPONÍVEIS DO DIA
# ==========================================================

def slots_for(day, service=None):

    # ------------------------------------------------------
    # DOMINGO FECHADO
    # ------------------------------------------------------

    if day.weekday() == 6:
        return []


    # ------------------------------------------------------
    # DEFINE OS HORÁRIOS DE FUNCIONAMENTO
    # ------------------------------------------------------

    if day.weekday() in [0, 1, 3, 4]:

        periods = [
            ("07:30", "11:00"),
            ("13:30", "20:00")
        ]

    elif day.weekday() == 2:

        periods = [
            ("07:30", "11:00"),
            ("13:30", "15:00")
        ]

    elif day.weekday() == 5:

        periods = [
            ("09:00", "16:00")
        ]

    else:

        return []


    slots = []


    # ------------------------------------------------------
    # GERA CADA HORÁRIO DE 30 EM 30 MINUTOS
    # ------------------------------------------------------

    for start, end in periods:

        current = datetime.strptime(
            start,
            "%H:%M"
        )

        period_end = datetime.strptime(
            end,
            "%H:%M"
        )


        # Permite que o último atendimento
        # comece até 30 minutos depois do
        # horário final definido.

        allowed_end = (
            period_end
            + timedelta(minutes=30)
        )


        while current < allowed_end:

            # --------------------------------------------------
            # VERIFICA SE O HORÁRIO JÁ PASSOU HOJE
            # --------------------------------------------------

            if day == date.today():

                now = datetime.now()

                slot_datetime = datetime.combine(
                    day,
                    current.time()
                )

                if slot_datetime <= now:

                    current += timedelta(
                        minutes=30
                    )

                    continue


            # --------------------------------------------------
            # VERIFICA A DURAÇÃO DO SERVIÇO
            # --------------------------------------------------

            if service:

                duration = service_duration(
                    service
                )

                service_end = (
                    current
                    + timedelta(
                        minutes=duration
                    )
                )


                # O serviço precisa terminar
                # dentro do período permitido.

                if service_end > allowed_end:

                    current += timedelta(
                        minutes=30
                    )

                    continue


            # --------------------------------------------------
            # ADICIONA O HORÁRIO
            # --------------------------------------------------

            slots.append(
                current.strftime("%H:%M")
            )


            current += timedelta(
                minutes=30
            )


    return slots


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

    duration = service_duration(service)

    new_start = datetime.strptime(
        f"{appointment_date} {appointment_time}",
        "%Y-%m-%d %H:%M"
    )

    new_end = new_start + timedelta(
        minutes=duration
    )

    cur = con.cursor(
        cursor_factory=RealDictCursor
    )

    query = """
        SELECT *
        FROM appointments
        WHERE date = %s
        AND status = 'active'
    """

    params = [appointment_date]

    cur.execute(
        query,
        params
    )

    appointments = cur.fetchall()

    cur.close()

    for appt in appointments:

        if ignore_id is not None:
            if appt["id"] == ignore_id:
                continue

        appt_start = datetime.strptime(
            f"{appt['date']} {appt['time']}",
            "%Y-%m-%d %H:%M"
        )

        appt_duration = service_duration(
            appt["service"]
        )

        appt_end = appt_start + timedelta(
            minutes=appt_duration
        )

        # Verifica sobreposição
        if (
            new_start < appt_end
            and new_end > appt_start
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

    duration = service_duration(service)

    start = datetime.strptime(
        f"{appointment_date} {appointment_time}",
        "%Y-%m-%d %H:%M"
    )

    end = start + timedelta(
        minutes=duration
    )

    cur = con.cursor()

    cur.execute("""
        SELECT blocked_time
        FROM blocked_slots
        WHERE blocked_date = %s
    """, (
        appointment_date,
    ))

    blocked = cur.fetchall()

    cur.close()

    for row in blocked:

        blocked_start = datetime.strptime(
            f"{appointment_date} {row[0]}",
            "%Y-%m-%d %H:%M"
        )

        blocked_end = blocked_start + timedelta(
            minutes=30
        )

        if (
            start < blocked_end
            and end > blocked_start
        ):
            return True

    return False


# ==========================================================
# HORÁRIOS PÚBLICOS
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
    # VALIDA DATA
    # ------------------------------------------------------

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
    # GERA TODOS OS HORÁRIOS NORMAIS
    # ------------------------------------------------------

    slots = slots_for(
        selected_date,
        service
    )

    # ------------------------------------------------------
    # BUSCA BANCO
    # ------------------------------------------------------

    con = get_db()

    cur = con.cursor(
        cursor_factory=RealDictCursor
    )

    try:

        # AGENDAMENTOS
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

        # BLOQUEIOS
        cur.execute("""
            SELECT
                blocked_time
            FROM blocked_slots
            WHERE blocked_date = %s
        """, (
            date_str,
        ))

        blocked_rows = cur.fetchall()

    finally:

        cur.close()
        con.close()

    # ------------------------------------------------------
    # HORÁRIOS BLOQUEADOS
    # ------------------------------------------------------

    blocked_times = {
        row["blocked_time"]
        for row in blocked_rows
    }

    # ------------------------------------------------------
    # FILTRA OS HORÁRIOS
    # ------------------------------------------------------

    available = []

    requested_duration = service_duration(
        service
    )

    for slot in slots:

        slot_start = datetime.strptime(
            f"{date_str} {slot}",
            "%Y-%m-%d %H:%M"
        )

        slot_end = slot_start + timedelta(
            minutes=requested_duration
        )

        conflict = False

        # ----------------------------------------------
        # VERIFICA AGENDAMENTOS
        # ----------------------------------------------

        for appt in appointments:

            appt_start = datetime.strptime(
                f"{date_str} {appt['time']}",
                "%Y-%m-%d %H:%M"
            )

            appt_end = appt_start + timedelta(
                minutes=service_duration(
                    appt["service"]
                )
            )

            if (
                slot_start < appt_end
                and slot_end > appt_start
            ):

                conflict = True
                break

        if conflict:
            continue

        # ----------------------------------------------
        # VERIFICA BLOQUEIOS
        # ----------------------------------------------

        for blocked_time in blocked_times:

            blocked_start = datetime.strptime(
                f"{date_str} {blocked_time}",
                "%Y-%m-%d %H:%M"
            )

            blocked_end = blocked_start + timedelta(
                minutes=30
            )

            if (
                slot_start < blocked_end
                and slot_end > blocked_start
            ):

                conflict = True
                break

        if not conflict:

            available.append(
                slot
            )

    # ------------------------------------------------------
    # DEVOLVE NO FORMATO PADRÃO
    # ------------------------------------------------------

    return jsonify({
        "slots": available
    })
    
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
# AGENDAMENTO
# ==========================================================

@app.route("/agendar", methods=["POST"])
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
    # VALIDA SERVIÇO
    # ------------------------------------------------------

    valid_service = any(
        item[0] == service
        for item in SERVICES
    )

    if not valid_service:

        flash(
            "Serviço inválido.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    # ------------------------------------------------------
    # VALIDA DATA
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
    # VALIDA HORÁRIO
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
    # NÃO PERMITE DATA PASSADA
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
    # NÃO PERMITE HORÁRIO PASSADO HOJE
    # ------------------------------------------------------

    if selected_date == date.today():

        selected_datetime = datetime.strptime(
            f"{appointment_date} {appointment_time}",
            "%Y-%m-%d %H:%M"
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
    # VERIFICA SE O HORÁRIO EXISTE
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

    con = get_db()

    try:

        # --------------------------------------------------
        # VERIFICA BLOQUEIO
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
        # VERIFICA OUTRO AGENDAMENTO
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
        # SALVA AGENDAMENTO
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

    except IntegrityError:

        con.rollback()

        flash(
            "Não foi possível realizar o agendamento.",
            "error"
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
    cur = con.cursor(
        cursor_factory=RealDictCursor
    )

    try:

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

    finally:

        cur.close()
        con.close()

    return jsonify(
        appointments
    )


# ==========================================================
# LOGIN ADMIN
# ==========================================================

@app.route("/admin/login", methods=["GET", "POST"])
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
# PROTEÇÃO ADMIN
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
# LOGOUT ADMIN
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
    cur = con.cursor(
        cursor_factory=RealDictCursor
    )

    try:

        # --------------------------------------------------
        # AGENDAMENTOS
        # --------------------------------------------------

        cur.execute("""
    SELECT *
    FROM appointments
    WHERE status <> 'Cancelado'
    ORDER BY appointment_date, appointment_time
""")

        appointments = cur.fetchall()

        # --------------------------------------------------
        # HISTÓRICO DE BLOQUEIOS
        # --------------------------------------------------

        cur.execute("""
            SELECT *
            FROM blocked_slots
            ORDER BY blocked_date, blocked_time
        """)

        blocked_rows = cur.fetchall()

    finally:

        cur.close()
        con.close()

    # ======================================================
    # AGRUPAR BLOQUEIOS CONTÍGUOS
    # ======================================================

    grouped_blocks = []

    # Agrupa por:
    # - data
    # - motivo
    groups = {}

    for block in blocked_rows:

        key = (
            block["blocked_date"],
            block.get("reason") or ""
        )

        if key not in groups:
            groups[key] = []

        groups[key].append(block)

    # ======================================================
    # PROCESSA CADA GRUPO
    # ======================================================

    for (
        group_date,
        reason
    ), blocks in groups.items():

        # Ordena por horário
        blocks.sort(
            key=lambda x: x["blocked_time"]
        )

        # --------------------------------------------------
        # HORÁRIOS NORMAIS DO DIA
        # --------------------------------------------------

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
        # VERIFICA SE O DIA INTEIRO ESTÁ BLOQUEADO
        # --------------------------------------------------

        is_full_day = (
            len(normal_slots) > 0
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
                "end_display": normal_slots[-1],
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

            previous_time = datetime.strptime(
                current_group[-1]["blocked_time"],
                "%H:%M"
            )

            current_time = datetime.strptime(
                block["blocked_time"],
                "%H:%M"
            )

            difference = (
                current_time
                - previous_time
            )

            # Se diferença for exatamente 30 minutos,
            # faz parte do mesmo período.
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
                    "end_display": last["blocked_time"],
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
        # SALVA O ÚLTIMO GRUPO
        # --------------------------------------------------

        if current_group:

            first = current_group[0]
            last = current_group[-1]

            grouped_blocks.append({
                "id": first["id"],
                "date": group_date,
                "start": first["blocked_time"],
                "end": last["blocked_time"],
                "end_display": last["blocked_time"],
                "reason": reason,
                "day_full": False,
                "block_ids": [
                    item["id"]
                    for item in current_group
                ]
            })

    # ======================================================
    # ORDENA HISTÓRICO
    # ======================================================

    grouped_blocks.sort(
        key=lambda x: (
            x["date"],
            x["start"]
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
    cur = con.cursor()

    try:

        cur.execute("""
            UPDATE appointments
            SET status = 'Cancelado'
            WHERE id = %s
        """, (
            appointment_id,
        ))

        con.commit()

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

        cur.close()
        con.close()

    return redirect(
        url_for("admin")
    )


# ==========================================================
# EXCLUIR AGENDAMENTO DO HISTÓRICO
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
    cur = con.cursor()

    try:

        cur.execute("""
            DELETE FROM appointments
            WHERE id = %s
        """, (
            appointment_id,
        ))

        con.commit()

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

        cur.close()
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

    block_type = request.form.get(
        "block_type",
        "specific"
    )

    blocked_date = request.form.get(
        "date",
        ""
    ).strip()

    blocked_time = request.form.get(
        "time",
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

    con = get_db()

    try:

        # ==================================================
        # MONTA LISTA DE HORÁRIOS A BLOQUEAR
        # ==================================================

        times_to_block = []

        # --------------------------------------------------
        # BLOQUEIO DE UM HORÁRIO
        # --------------------------------------------------

        if block_type == "specific":

            if not blocked_time:

                flash(
                    "Selecione um horário.",
                    "error"
                )

                return redirect(
                    url_for("admin")
                )

            times_to_block = [
                blocked_time
            ]

        # --------------------------------------------------
        # BLOQUEIO DE PERÍODO
        # --------------------------------------------------

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

            while current < finish:

                time_string = current.strftime(
                    "%H:%M"
                )

                # Só adiciona horários que fazem
                # parte da agenda normal.
                if time_string in slots_for(
                    selected_date
                ):

                    times_to_block.append(
                        time_string
                    )

                current += timedelta(
                    minutes=30
                )

        # --------------------------------------------------
        # BLOQUEIO DO DIA INTEIRO
        # --------------------------------------------------

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

        # --------------------------------------------------
        # VERIFICA SE EXISTE ALGUM AGENDAMENTO
        # --------------------------------------------------

        cur = con.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT *
            FROM appointments
            WHERE date = %s
            AND status = 'active'
        """, (
            blocked_date,
        ))

        appointments = cur.fetchall()

        cur.close()

        # --------------------------------------------------
        # NÃO PERMITE BLOQUEAR HORÁRIO JÁ AGENDADO
        # --------------------------------------------------

        for slot in times_to_block:

            slot_start = datetime.strptime(
                f"{blocked_date} {slot}",
                "%Y-%m-%d %H:%M"
            )

            slot_end = slot_start + timedelta(
                minutes=30
            )

            for appt in appointments:

                appt_start = datetime.strptime(
                    f"{appt['date']} {appt['time']}",
                    "%Y-%m-%d %H:%M"
                )

                appt_end = appt_start + timedelta(
                    minutes=service_duration(
                        appt["service"]
                    )
                )

                if (
                    slot_start < appt_end
                    and slot_end > appt_start
                ):

                    flash(
                        f"O horário {slot} já possui um agendamento.",
                        "error"
                    )

                    return redirect(
                        url_for("admin")
                    )

        # ==================================================
        # INSERE OS BLOQUEIOS
        # ==================================================

        cur = con.cursor()

        for slot in times_to_block:

            try:

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

            except Exception as e:

                print(
                    "ERRO AO INSERIR BLOQUEIO:",
                    e
                )

        con.commit()

        cur.close()

        if not times_to_block:

            flash(
                "Nenhum horário válido foi encontrado para bloquear.",
                "error"
            )

        else:

            flash(
                "Horário bloqueado com sucesso.",
                "success"
            )

    except Exception as e:

        con.rollback()

        print(
            "ERRO AO BLOQUEAR:",
            e
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
# HORÁRIOS DE BLOQUEIO DO ADMIN
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

    # ------------------------------------------------------
    # VALIDA DATA
    # ------------------------------------------------------

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
    # GERA TODOS OS HORÁRIOS DA DATA
    # ------------------------------------------------------

    slots = slots_for(
        selected_date
    )

    # ------------------------------------------------------
    # BANCO
    # ------------------------------------------------------

    con = get_db()

    cur = con.cursor(
        cursor_factory=RealDictCursor
    )

    try:

        # ----------------------------------------------
        # BLOQUEIOS EXISTENTES
        # ----------------------------------------------

        cur.execute("""
            SELECT
                blocked_time
            FROM blocked_slots
            WHERE blocked_date = %s
        """, (
            date_str,
        ))

        blocked_rows = cur.fetchall()

        # ----------------------------------------------
        # AGENDAMENTOS EXISTENTES
        # ----------------------------------------------

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

    finally:

        cur.close()
        con.close()

    # ------------------------------------------------------
    # TRANSFORMA EM CONJUNTOS
    # ------------------------------------------------------

    blocked_times = {
        row["blocked_time"]
        for row in blocked_rows
    }

    # ------------------------------------------------------
    # MONTA LISTA
    # ------------------------------------------------------

    slots_result = []

    for slot in slots:

        slot_start = datetime.strptime(
            f"{date_str} {slot}",
            "%Y-%m-%d %H:%M"
        )

        slot_end = slot_start + timedelta(
            minutes=30
        )

        appointment_conflict = False

        # ----------------------------------------------
        # VERIFICA AGENDAMENTO
        # ----------------------------------------------

        for appt in appointments:

            appt_start = datetime.strptime(
                f"{date_str} {appt['time']}",
                "%Y-%m-%d %H:%M"
            )

            appt_end = appt_start + timedelta(
                minutes=service_duration(
                    appt["service"]
                )
            )

            if (
                slot_start < appt_end
                and slot_end > appt_start
            ):

                appointment_conflict = True
                break

        # ----------------------------------------------
        # ADICIONA HORÁRIO
        # ----------------------------------------------

        slots_result.append({
            "time": slot,
            "blocked": slot in blocked_times,
            "appointment": appointment_conflict
        })

    return jsonify({
        "slots": slots_result
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
        # LOCALIZA O BLOQUEIO CLICADO
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

        clicked_time = clicked[
            "blocked_time"
        ]

        clicked_reason = (
            clicked.get("reason")
            or ""
        )

        # --------------------------------------------------
        # PEGA TODOS OS BLOQUEIOS DO MESMO DIA
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

        # ==================================================
        # VERIFICA SE O DIA INTEIRO ESTÁ BLOQUEADO
        # ==================================================

        try:

            day_obj = datetime.strptime(
                blocked_date,
                "%Y-%m-%d"
            ).date()

        except ValueError:

            day_obj = None

        if day_obj:

            normal_slots = slots_for(
                day_obj
            )

            blocked_times = {
                block["blocked_time"]
                for block in blocks
            }

            is_full_day = (
                len(normal_slots) > 0
                and all(
                    slot in blocked_times
                    for slot in normal_slots
                )
            )

        else:

            is_full_day = False

        # ==================================================
        # DIA INTEIRO
        # ==================================================

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

        # ==================================================
        # ENCONTRA O PERÍODO CONTÍGUO
        # ==================================================

        same_reason_blocks = [
            block
            for block in blocks
            if (
                (block.get("reason") or "")
                == clicked_reason
            )
        ]

        same_reason_blocks.sort(
            key=lambda x: x["blocked_time"]
        )

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

            current_time = datetime.strptime(
                same_reason_blocks[
                    start_index
                ]["blocked_time"],
                "%H:%M"
            )

            previous_time = datetime.strptime(
                same_reason_blocks[
                    start_index - 1
                ]["blocked_time"],
                "%H:%M"
            )

            difference = (
                current_time
                - previous_time
            )

            if difference == timedelta(
                minutes=30
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

            current_time = datetime.strptime(
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

            difference = (
                next_time
                - current_time
            )

            if difference == timedelta(
                minutes=30
            ):

                end_index += 1

            else:

                break

        # ==================================================
        # PEGA OS IDs DO PERÍODO
        # ==================================================

        period_blocks = (
            same_reason_blocks[
                start_index:end_index + 1
            ]
        )

        ids_to_delete = [
            block["id"]
            for block in period_blocks
        ]

        # ==================================================
        # EXCLUI TODO O PERÍODO
        # ==================================================

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
# INICIALIZA BANCO
# ==========================================================

try:

    init_db()

except Exception as e:

    print(
        "ERRO AO INICIALIZAR BANCO:",
        e
    )


# ==========================================================
# EXECUÇÃO
# ==========================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )
