import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.database.connection import SessionLocal, engine, get_db
from app.database.models import Feriado, HoraExtra, ReglaHora, TipoContratacion, Usuario, UsuarioRol
from app.services.ad_auth import ActiveDirectoryAuthError, authenticate_ad_user, list_ad_group_users
from app.services.access_catalog import CONVENIOS_DISPONIBLES, PERFILES_DISPONIBLES, ROLES_DISPONIBLES
from app.services.calculo_horas import (
    CalculoHorasError,
    calcular_resultado_horas_extra,
    calcular_resultado_otra_carga,
    calcular_resultado_reintegro,
    dividir_carga_en_fechas,
)
from app.services.local_auth import (
    authenticate_local_user,
    find_user,
    hash_password,
    initialize_seed_admin,
    is_local_username,
    local_usernames,
    normalize_username,
    session_user,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_seed_admin(SessionLocal)
    yield


app = FastAPI(title="Horas extras", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

PUBLIC_PATHS = {"/login", "/health", "/health/db", "/health/database"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in PUBLIC_PATHS:
        return await call_next(request)
    session_user_data = request.session.get("user")
    if session_user_data is not None:
        active_role = str(session_user_data.get("role") or "").upper()
        users_path = path.startswith("/configuracion/usuarios")
        if path.startswith("/configuracion") and not (
            active_role == "ADMIN" or (users_path and active_role == "RRHH")
        ):
            return HTMLResponse("Acceso denegado: se requiere el rol ADMIN.", status_code=403)
        return await call_next(request)
    return RedirectResponse(f"/login?next={quote(path, safe='/')}", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "rrhh-dev-session-key-change-me"),
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"},
)


def parse_optional_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="El horario ingresado no es válido.") from error


def rule_form_data(
    convenio: str,
    tipo_dia: str,
    tipo_hora: str,
    hora_nocturna_desde: str | None,
    hora_nocturna_hasta: str | None,
    permite_reintegro: str | None,
    observaciones: str | None,
) -> dict:
    convenio = convenio.strip().upper()
    tipo_dia = tipo_dia.strip()
    tipo_hora = tipo_hora.strip()
    if not convenio or not tipo_dia or not tipo_hora:
        raise HTTPException(status_code=422, detail="Completá los campos obligatorios.")
    if convenio not in CONVENIOS_DISPONIBLES:
        raise HTTPException(status_code=422, detail="El convenio seleccionado no es válido.")
    return {
        "convenio": convenio,
        "tipo_dia": tipo_dia,
        "tipo_hora": tipo_hora,
        "hora_nocturna_desde": parse_optional_time(hora_nocturna_desde),
        "hora_nocturna_hasta": parse_optional_time(hora_nocturna_hasta),
        "permite_reintegro": permite_reintegro == "on",
        "observaciones": observaciones.strip() if observaciones else None,
    }


def clean_optional(value: str | None) -> str | None:
    value = value.strip() if value else ""
    return value or None


def safe_next_url(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def usuarios_habilitados_para_carga(
    request: Request,
    db: Session,
    destino: str | None = None,
) -> tuple[UsuarioRol, list[Usuario]]:
    session_data = request.session.get("user") or {}
    assignment = db.scalar(
        select(UsuarioRol).where(
            UsuarioRol.id_rol == session_data.get("active_assignment_id"),
            UsuarioRol.usuario_id == session_data.get("id"),
        )
    )
    if assignment is None:
        raise HTTPException(status_code=403, detail="Seleccioná un rol y perfil activo para cargar horas.")

    current_user = db.get(Usuario, assignment.usuario_id)
    if current_user is None or not current_user.status:
        raise HTTPException(status_code=403, detail="El usuario activo no está habilitado.")

    if assignment.perfil.upper() != "JEFE":
        return assignment, [current_user]

    if destino != "otro":
        return assignment, []

    usuarios = list(db.scalars(
        select(Usuario)
        .join(Usuario.roles)
        .where(
            Usuario.status.is_(True),
            Usuario.id != current_user.id,
            func.upper(UsuarioRol.rol) == assignment.rol.upper(),
            func.upper(UsuarioRol.perfil) == "USUARIO",
        )
        .distinct()
        .order_by(Usuario.apellido, Usuario.nombre)
    ))
    return assignment, usuarios


def ids_habilitados_para_confirmar(request: Request, db: Session) -> set[int]:
    assignment, propios = usuarios_habilitados_para_carga(request, db)
    permitidos = {item.id for item in propios}
    if assignment.perfil.upper() == "JEFE":
        _, terceros = usuarios_habilitados_para_carga(request, db, "otro")
        permitidos.update(item.id for item in terceros)
    return permitidos


def tipos_otras_cargas(db: Session) -> list[tuple[str, str]]:
    return list(db.execute(
        select(ReglaHora.convenio, ReglaHora.tipo_hora)
        .where(
            func.upper(ReglaHora.tipo_dia) == "TODOS",
            func.upper(ReglaHora.tipo_hora).notin_(("COMIDA", "MERIENDA")),
        )
        .distinct()
        .order_by(ReglaHora.convenio, ReglaHora.tipo_hora)
    ).tuples())


def encode_carga(
    usuario_id: int,
    fecha: date,
    hora_inicio: time | None,
    hora_fin: time | None,
    observaciones: str | None,
    tipo_registro: str = "HORAS",
    marcar_como_franco: bool = False,
    tipo_hora: str | None = None,
    cantidad: Decimal | None = None,
) -> str:
    return "|".join((
        str(usuario_id),
        fecha.isoformat(),
        hora_inicio.strftime("%H:%M") if hora_inicio else "",
        hora_fin.strftime("%H:%M") if hora_fin else "",
        quote(clean_optional(observaciones) or "", safe=""),
        tipo_registro,
        "SI" if marcar_como_franco else "NO",
        quote(clean_optional(tipo_hora) or "", safe=""),
        str(cantidad) if cantidad is not None else "",
    ))


def decode_carga(value: str) -> tuple[int, date, time | None, time | None, str | None, str, bool, str | None, Decimal | None]:
    try:
        partes = value.split("|", maxsplit=8)
        if len(partes) == 5:
            partes.append("HORAS")
        if len(partes) == 6:
            partes.append("NO")
        while len(partes) < 9:
            partes.append("")
        usuario_id, fecha, hora_inicio, hora_fin, observaciones, tipo_registro, marcar_franco, tipo_hora, cantidad = partes
        tipo_registro = tipo_registro.strip().upper()
        marcar_franco = marcar_franco.strip().upper()
        if tipo_registro not in {"HORAS", "REINTEGRO", "OTRAS"} or marcar_franco not in {"SI", "NO"}:
            raise ValueError
        return (
            int(usuario_id),
            date.fromisoformat(fecha),
            time.fromisoformat(hora_inicio) if hora_inicio else None,
            time.fromisoformat(hora_fin) if hora_fin else None,
            clean_optional(unquote(observaciones)),
            tipo_registro,
            marcar_franco == "SI",
            clean_optional(unquote(tipo_hora)),
            Decimal(cantidad) if cantidad else None,
        )
    except (TypeError, ValueError) as error:
        raise CalculoHorasError("La solicitud contiene una carga con formato inválido.") from error


def calcular_carga_codificada(datos, db: Session):
    usuario_id, fecha, hora_inicio, hora_fin, _, tipo_registro, marcar_como_franco, tipo_hora, cantidad = datos
    if tipo_registro == "REINTEGRO":
        return calcular_resultado_reintegro(usuario_id, fecha, db)
    if tipo_registro == "OTRAS":
        if tipo_hora is None or cantidad is None:
            raise CalculoHorasError("La otra carga no contiene un tipo y una cantidad válidos.")
        return calcular_resultado_otra_carga(usuario_id, fecha, tipo_hora, cantidad, db)
    if hora_inicio is None or hora_fin is None:
        raise CalculoHorasError("La carga de horas no contiene un horario válido.")
    return calcular_resultado_horas_extra(
        usuario_id, fecha, hora_inicio, hora_fin, db,
        marcar_como_franco=marcar_como_franco,
    )


def periodo_corte(fecha_referencia: date) -> tuple[date, date]:
    corte_actual = date(fecha_referencia.year, fecha_referencia.month, 20)
    if fecha_referencia.day > 20:
        desde = corte_actual
        if fecha_referencia.month == 12:
            hasta = date(fecha_referencia.year + 1, 1, 20)
        else:
            hasta = date(fecha_referencia.year, fecha_referencia.month + 1, 20)
    else:
        hasta = corte_actual
        if fecha_referencia.month == 1:
            desde = date(fecha_referencia.year - 1, 12, 20)
        else:
            desde = date(fecha_referencia.year, fecha_referencia.month - 1, 20)
    return desde, hasta


def obtener_autorizador(request: Request, db: Session) -> tuple[Usuario, UsuarioRol, bool]:
    session_data = request.session.get("user") or {}
    asignacion = db.scalar(
        select(UsuarioRol).where(
            UsuarioRol.id_rol == session_data.get("active_assignment_id"),
            UsuarioRol.usuario_id == session_data.get("id"),
        )
    )
    usuario = db.scalar(
        select(Usuario)
        .options(selectinload(Usuario.roles))
        .where(Usuario.id == session_data.get("id"))
    )
    es_admin = asignacion is not None and asignacion.rol.upper() == "ADMIN"
    if usuario is None or asignacion is None or not usuario.status or (
        not es_admin and asignacion.perfil.upper() != "JEFE"
    ):
        raise HTTPException(
            status_code=403,
            detail="No tenés permisos para acceder a las autorizaciones.",
        )
    return usuario, asignacion, es_admin


def validar_hora_para_autorizador(
    hora: HoraExtra | None,
    asignacion: UsuarioRol,
    es_admin: bool,
    requiere_pendiente: bool = True,
) -> HoraExtra:
    if hora is None:
        raise HTTPException(status_code=404, detail="La carga de horas no existe.")
    roles_usuario = {item.rol.upper() for item in hora.usuario.roles}
    if not es_admin and asignacion.rol.upper() not in roles_usuario:
        raise HTTPException(
            status_code=403,
            detail="No podés administrar horas de usuarios con otro rol.",
        )
    if requiere_pendiente and hora.estado != "PENDIENTE":
        raise HTTPException(
            status_code=409,
            detail="La carga ya no está pendiente de autorización.",
        )
    return hora


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, error: str | None = None):
    if request.session.get("user"):
        return RedirectResponse(safe_next_url(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "active_page": "login",
        "next_url": safe_next_url(next),
        "error": error,
    })


@app.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next_url: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    redirect_to = safe_next_url(next_url)
    normalized_username = normalize_username(username)
    if is_local_username(normalized_username):
        local_user = authenticate_local_user(db, normalized_username, password)
        if local_user is None:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "active_page": "login",
                    "next_url": redirect_to,
                    "error": "Usuario o contraseña incorrectos.",
                    "username": normalized_username,
                },
                status_code=401,
            )
        request.session["user"] = session_user(local_user, "LOCAL")
        return RedirectResponse(redirect_to, status_code=303)

    try:
        authenticate_ad_user(normalized_username, password)
    except ActiveDirectoryAuthError as error:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "active_page": "login",
                "next_url": redirect_to,
                "error": str(error),
                "username": normalized_username,
            },
            status_code=401,
        )

    database_user = find_user(db, normalized_username)
    if database_user is None or not database_user.status:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "active_page": "login",
                "next_url": redirect_to,
                "error": "El usuario no está habilitado en la aplicación.",
                "username": normalized_username,
            },
            status_code=403,
        )
    request.session["user"] = session_user(database_user, "AD")
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/sesion/asignacion")
def cambiar_asignacion(
    request: Request,
    asignacion_id: Annotated[int, Form()],
    db: Session = Depends(get_db),
):
    session_data = request.session.get("user")
    if not session_data:
        return RedirectResponse("/login", status_code=303)
    assignment = db.scalar(
        select(UsuarioRol).where(
            UsuarioRol.id_rol == asignacion_id,
            UsuarioRol.usuario_id == session_data.get("id"),
        )
    )
    if assignment is None:
        raise HTTPException(status_code=403, detail="La asignación no pertenece al usuario activo.")
    session_data["active_assignment_id"] = assignment.id_rol
    session_data["role"] = assignment.rol
    session_data["profile"] = assignment.perfil
    request.session["user"] = session_data
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request, db: Session = Depends(get_db)):
    assignment, _ = usuarios_habilitados_para_carga(request, db)
    pendientes = db.scalar(
        select(func.count(HoraExtra.id)).where(
            HoraExtra.usuario_id == assignment.usuario_id,
            HoraExtra.estado == "PENDIENTE",
        )
    ) or 0
    es_admin = assignment.rol.upper() == "ADMIN"
    es_jefe = assignment.perfil.upper() == "JEFE"
    autorizaciones_pendientes = 0
    if es_admin:
        autorizaciones_pendientes = db.scalar(
            select(func.count(HoraExtra.id)).where(HoraExtra.estado == "PENDIENTE")
        ) or 0
    elif es_jefe:
        autorizaciones_pendientes = db.scalar(
            select(func.count(HoraExtra.id))
            .join(HoraExtra.usuario)
            .where(
                HoraExtra.estado == "PENDIENTE",
                Usuario.id != assignment.usuario_id,
                Usuario.roles.any(
                    func.upper(UsuarioRol.rol) == assignment.rol.upper()
                ),
            )
        ) or 0
    return templates.TemplateResponse(request, "inicio.html", {
        "active_page": "inicio",
        "es_jefe": es_jefe,
        "es_admin": es_admin,
        "rol_activo": assignment.rol,
        "cargas_pendientes": pendientes,
        "autorizaciones_pendientes": autorizaciones_pendientes,
    })


@app.get("/horas", response_class=HTMLResponse)
def home(
    request: Request,
    estado: str | None = None,
    guardado: int | None = None,
    db: Session = Depends(get_db),
):
    horas: list[HoraExtra] = []
    session_user_id = (request.session.get("user") or {}).get("id")
    if not session_user_id:
        raise HTTPException(status_code=403, detail="La sesión no contiene un usuario válido.")
    error = None
    try:
        consulta = (
            select(HoraExtra)
            .options(selectinload(HoraExtra.usuario), selectinload(HoraExtra.aprobador))
            .where(HoraExtra.usuario_id == session_user_id)
            .order_by(HoraExtra.fecha.desc(), HoraExtra.hora_inicio.desc())
        )
        if estado:
            consulta = consulta.where(HoraExtra.estado == estado)
        horas = list(db.scalars(consulta))
    except SQLAlchemyError:
        error = "No se pudieron cargar las horas extras. Verificá la conexión con la base."
    return templates.TemplateResponse(request, "dashboard.html", {
        "active_page": "horas",
        "horas": horas,
        "estado_filtro": estado or "",
        "guardado": guardado,
        "total_horas": sum(
            (hora.horas_totales or Decimal("0.00") for hora in horas),
            start=Decimal("0.00"),
        ),
        "cantidad_pendientes": sum(
            1 for hora in horas if hora.estado == "PENDIENTE"
        ),
        "cantidad_autorizadas": sum(
            1 for hora in horas if hora.estado == "APROBADA"
        ),
        "error": error,
    })


@app.get("/solicitudes/nueva", response_class=HTMLResponse)
def nueva_solicitud(
    request: Request,
    destino: str = "propio",
    db: Session = Depends(get_db),
):
    usuarios: list[Usuario] = []
    error = None
    try:
        assignment, usuarios = usuarios_habilitados_para_carga(request, db, destino)
        if assignment.perfil.upper() != "JEFE":
            destino = "propio"
    except SQLAlchemyError:
        error = "No se pudieron cargar los usuarios activos. Verificá la conexión con la base."
    return templates.TemplateResponse(request, "home.html", {
        "active_page": "horas",
        "usuarios": usuarios,
        "usuario_fijo": usuarios[0] if destino == "propio" and usuarios else None,
        "destino": destino,
        "fecha_hoy": date.today().isoformat(),
        "tipos_otras_cargas": tipos_otras_cargas(db),
        "error": error,
    })


@app.post("/solicitudes/procesar", response_class=HTMLResponse)
def procesar_solicitud(
    request: Request,
    usuario_id: Annotated[int, Form()],
    fecha: Annotated[date, Form()],
    tipo_registro: Annotated[str, Form()] = "HORAS",
    hora_inicio: Annotated[time | None, Form()] = None,
    hora_fin: Annotated[time | None, Form()] = None,
    observaciones: Annotated[str | None, Form()] = None,
    destino: Annotated[str, Form()] = "propio",
    cargas: Annotated[list[str] | None, Form()] = None,
    reintegros: Annotated[list[str] | None, Form()] = None,
    marcar_como_franco: Annotated[str | None, Form()] = None,
    solicita_reintegro_dia: Annotated[str | None, Form()] = None,
    tipo_otra_carga: Annotated[str | None, Form()] = None,
    cantidad: Annotated[Decimal | None, Form()] = None,
    db: Session = Depends(get_db),
):
    resultados = []
    observaciones_resultados: list[str | None] = []
    cargas_codificadas = list(cargas or [])
    selecciones_reintegro = [
        value if value in {"SI", "NO"} else "NO"
        for value in (reintegros or [])
    ]
    if len(selecciones_reintegro) < len(cargas_codificadas):
        selecciones_reintegro.extend(
            ["NO"] * (len(cargas_codificadas) - len(selecciones_reintegro))
        )
    selecciones_reintegro = selecciones_reintegro[:len(cargas_codificadas)]
    error = None
    try:
        _, usuarios_habilitados = usuarios_habilitados_para_carga(request, db, destino)
        if usuario_id not in {item.id for item in usuarios_habilitados}:
            raise CalculoHorasError("No tenés permiso para cargar horas al usuario seleccionado.")
        for carga in cargas_codificadas:
            datos = decode_carga(carga)
            if datos[0] not in ids_habilitados_para_confirmar(request, db):
                raise CalculoHorasError("Una de las cargas contiene un usuario no autorizado.")
            resultados.append(calcular_carga_codificada(datos, db))
            observaciones_resultados.append(datos[4])
        tipo_registro = tipo_registro.strip().upper()
        if tipo_registro == "REINTEGRO":
            nuevos_tramos = [(fecha, None, None)]
            nuevos_resultados = [calcular_resultado_reintegro(usuario_id, fecha, db)]
        elif tipo_registro == "OTRAS":
            if not tipo_otra_carga or cantidad is None:
                raise CalculoHorasError("Seleccioná un tipo de carga e ingresá la cantidad.")
            nuevos_tramos = [(fecha, None, None)]
            nuevos_resultados = [
                calcular_resultado_otra_carga(
                    usuario_id, fecha, tipo_otra_carga, cantidad, db,
                )
            ]
        else:
            if tipo_registro != "HORAS" or hora_inicio is None or hora_fin is None:
                raise CalculoHorasError("Ingresá la hora de inicio y finalización.")
            nuevos_tramos = dividir_carga_en_fechas(fecha, hora_inicio, hora_fin)
            nuevos_resultados = [
                calcular_resultado_horas_extra(
                    usuario_id, fecha_tramo, inicio_tramo, fin_tramo, db,
                    marcar_como_franco=(marcar_como_franco == "on" and fecha_tramo == fecha),
                )
                for fecha_tramo, inicio_tramo, fin_tramo in nuevos_tramos
            ]
        if (
            tipo_registro == "HORAS"
            and solicita_reintegro_dia == "on"
            and not nuevos_resultados[0].permite_reintegro
        ):
            raise CalculoHorasError(
                "La regla correspondiente a la fecha no permite pedir reintegro de día."
            )
        resultados.extend(nuevos_resultados)
        observaciones_resultados.extend(
            [clean_optional(observaciones)] * len(nuevos_tramos)
        )
        cargas_codificadas.extend(
            encode_carga(
                usuario_id, fecha_tramo, inicio_tramo, fin_tramo, observaciones,
                tipo_registro,
                marcar_como_franco=(marcar_como_franco == "on" and fecha_tramo == fecha),
                tipo_hora=resultado.tipo_hora if tipo_registro == "OTRAS" else None,
                cantidad=resultado.cantidad if tipo_registro == "OTRAS" else None,
            )
            for (fecha_tramo, inicio_tramo, fin_tramo), resultado in zip(nuevos_tramos, nuevos_resultados)
        )
        selecciones_reintegro.extend([
            "SI"
            if tipo_registro == "REINTEGRO" or (
                solicita_reintegro_dia == "on"
                and fecha_tramo == fecha
                and resultado.permite_reintegro
            )
            else "NO"
            for (fecha_tramo, _, _), resultado in zip(nuevos_tramos, nuevos_resultados)
        ])
    except CalculoHorasError as exc:
        error = str(exc)

    _, usuarios = usuarios_habilitados_para_carga(request, db, destino)
    return templates.TemplateResponse(
        request,
        "solicitud.html",
        {
            "active_page": "horas",
            "resultados": resultados,
            "observaciones_resultados": observaciones_resultados,
            "cargas": cargas_codificadas,
            "reintegros": selecciones_reintegro,
            "usuarios": usuarios,
            "usuario_fijo": usuarios[0] if destino == "propio" and usuarios else None,
            "destino": destino,
            "fecha_hoy": date.today().isoformat(),
            "tipos_otras_cargas": tipos_otras_cargas(db),
            "total_horas": sum((item.horas_totales or Decimal("0.00") for item in resultados), start=Decimal("0.00")),
            "total_nocturnas": sum((item.horas_nocturnas or Decimal("0.00") for item in resultados), start=Decimal("0.00")),
            "error": error,
        },
        status_code=422 if error and not resultados else 200,
    )


@app.post("/solicitudes/confirmar")
def confirmar_solicitud(
    request: Request,
    cargas: Annotated[list[str] | None, Form()] = None,
    reintegros: Annotated[list[str] | None, Form()] = None,
    db: Session = Depends(get_db),
):
    cargas = list(cargas or [])
    if not cargas:
        return RedirectResponse("/solicitudes/nueva", status_code=303)
    selecciones = list(reintegros or [])
    if len(selecciones) != len(cargas):
        raise HTTPException(status_code=422, detail="La selección de reintegros es inconsistente.")

    try:
        ids_permitidos = ids_habilitados_para_confirmar(request, db)
        for indice, carga in enumerate(cargas):
            datos = decode_carga(carga)
            if datos[0] not in ids_permitidos:
                raise CalculoHorasError("Una de las cargas contiene un usuario no autorizado.")
            resultado = calcular_carga_codificada(datos, db)
            solicita_reintegro = (
                resultado.tipo_registro == "REINTEGRO"
                or (resultado.permite_reintegro and selecciones[indice] == "SI")
            )
            db.add(HoraExtra(
                usuario_id=resultado.usuario_id,
                fecha=resultado.fecha,
                hora_inicio=resultado.hora_inicio,
                hora_fin=resultado.hora_fin,
                horas_totales=resultado.horas_totales,
                cantidad=resultado.cantidad,
                tipo_dia=resultado.tipo_dia,
                tipo_hora=resultado.tipo_hora,
                horas_nocturnas=resultado.horas_nocturnas,
                solicita_reintegro=solicita_reintegro,
                observaciones=datos[4],
                estado="PENDIENTE",
                aprobado_por=None,
                fecha_carga=datetime.now(),
                tipo_registro=resultado.tipo_registro,
            ))
        db.commit()
    except (CalculoHorasError, SQLAlchemyError) as error:
        db.rollback()
        mensaje = str(error) if isinstance(error, CalculoHorasError) else "No se pudo guardar la solicitud en la base de datos."
        raise HTTPException(status_code=422, detail=mensaje) from error
    return RedirectResponse(f"/horas?guardado={len(cargas)}", status_code=303)


@app.get("/configuracion/parametros", response_class=HTMLResponse)
def parametros(
    request: Request,
    editar: int | None = None,
    guardado: str | None = None,
    db: Session = Depends(get_db),
):
    error = None
    reglas: list[ReglaHora] = []
    regla_edicion = None
    try:
        reglas = list(db.scalars(select(ReglaHora).order_by(ReglaHora.convenio, ReglaHora.tipo_dia, ReglaHora.id)))
        if editar is not None:
            regla_edicion = db.get(ReglaHora, editar)
            if regla_edicion is None:
                error = "La regla seleccionada no existe."
    except SQLAlchemyError:
        error = "No se pudieron cargar las reglas. Verificá la conexión con la base de datos."

    return templates.TemplateResponse(
        request,
        "parametros.html",
        {
            "active_page": "parametros",
            "reglas": reglas,
            "regla_edicion": regla_edicion,
            "guardado": guardado,
            "error": error,
            "roles_disponibles": ROLES_DISPONIBLES,
            "perfiles_disponibles": PERFILES_DISPONIBLES,
            "convenios_disponibles": CONVENIOS_DISPONIBLES,
        },
    )


@app.post("/configuracion/parametros/reglas")
def crear_regla(
    convenio: Annotated[str, Form()],
    tipo_dia: Annotated[str, Form()],
    tipo_hora: Annotated[str, Form()],
    hora_nocturna_desde: Annotated[str | None, Form()] = None,
    hora_nocturna_hasta: Annotated[str | None, Form()] = None,
    permite_reintegro: Annotated[str | None, Form()] = None,
    observaciones: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    nueva_regla = ReglaHora(**rule_form_data(
        convenio, tipo_dia, tipo_hora, hora_nocturna_desde,
        hora_nocturna_hasta, permite_reintegro, observaciones,
    ))
    try:
        db.add(nueva_regla)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo guardar la regla.") from error
    return RedirectResponse("/configuracion/parametros?guardado=creada", status_code=303)


@app.post("/configuracion/parametros/reglas/{regla_id}")
def actualizar_regla(
    regla_id: int,
    convenio: Annotated[str, Form()],
    tipo_dia: Annotated[str, Form()],
    tipo_hora: Annotated[str, Form()],
    hora_nocturna_desde: Annotated[str | None, Form()] = None,
    hora_nocturna_hasta: Annotated[str | None, Form()] = None,
    permite_reintegro: Annotated[str | None, Form()] = None,
    observaciones: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    regla = db.get(ReglaHora, regla_id)
    if regla is None:
        raise HTTPException(status_code=404, detail="La regla no existe.")
    for field, value in rule_form_data(
        convenio, tipo_dia, tipo_hora, hora_nocturna_desde,
        hora_nocturna_hasta, permite_reintegro, observaciones,
    ).items():
        setattr(regla, field, value)
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo actualizar la regla.") from error
    return RedirectResponse("/configuracion/parametros?guardado=actualizada", status_code=303)


@app.get("/configuracion/usuarios", response_class=HTMLResponse)
def usuarios_y_roles(
    request: Request,
    editar_usuario: int | None = None,
    consultar_ad: bool = False,
    guardado: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    usuarios: list[Usuario] = []
    tipos_contratacion: list[TipoContratacion] = []
    convenios: list[str] = list(CONVENIOS_DISPONIBLES)
    usuario_edicion = None
    usuarios_ad = None
    error_ad = None
    try:
        tipos_contratacion = list(db.scalars(
            select(TipoContratacion).order_by(TipoContratacion.id_tipo_contratacion)
        ))
        usuarios = list(db.scalars(
            select(Usuario)
            .options(selectinload(Usuario.roles), selectinload(Usuario.tipo_contratacion))
            .order_by(Usuario.apellido, Usuario.nombre)
        ))
        if editar_usuario is not None:
            usuario_edicion = db.get(Usuario, editar_usuario)
            if usuario_edicion is None:
                error = "El usuario seleccionado no existe."
    except SQLAlchemyError:
        error = "No se pudieron cargar usuarios y roles. Verificá la conexión con la base."

    if consultar_ad:
        try:
            usuarios_ad = list_ad_group_users()
        except ActiveDirectoryAuthError as ad_error:
            usuarios_ad = []
            error_ad = str(ad_error)

    return templates.TemplateResponse(request, "usuarios.html", {
        "active_page": "usuarios",
        "usuarios": usuarios,
        "tipos_contratacion": tipos_contratacion,
        "convenios": convenios,
        "usuario_edicion": usuario_edicion,
        "usuarios_ad": usuarios_ad,
        "error_ad": error_ad,
        "guardado": guardado,
        "error": error,
        "roles_disponibles": ROLES_DISPONIBLES,
        "perfiles_disponibles": PERFILES_DISPONIBLES,
    })


def require_user_management_role(request: Request) -> None:
    role = str(request.session.get("user", {}).get("role") or "").upper()
    if role not in {"ADMIN", "RRHH"}:
        raise HTTPException(status_code=403, detail="Se requiere el rol ADMIN o RRHH.")


@app.get("/configuracion/usuarios/sincronizar", response_class=HTMLResponse)
def previsualizar_usuarios_ad(
    request: Request,
    guardado: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    require_user_management_role(request)
    try:
        ad_users = list_ad_group_users()
    except ActiveDirectoryAuthError as ad_error:
        return templates.TemplateResponse(request, "usuarios_ad_sync.html", {
            "active_page": "usuarios",
            "error": str(ad_error),
            "guardado": guardado,
            "new_users": [],
            "existing_users": [],
            "inactive_users": [],
            "reactivable_users": [],
            "convenios": [],
            "tipos_contratacion": [],
        }, status_code=502)

    local_users = list(db.scalars(select(Usuario).options(selectinload(Usuario.roles))))
    local_by_username = {user.username.strip().lower(): user for user in local_users}
    ad_by_username = {user.username.strip().lower(): user for user in ad_users}
    new_users = [user for key, user in ad_by_username.items() if key not in local_by_username]
    existing_users = [
        local_by_username[key] for key in ad_by_username
        if key in local_by_username and local_by_username[key].status
    ]
    reactivable_users = [
        local_by_username[key] for key in ad_by_username
        if key in local_by_username and not local_by_username[key].status
    ]
    protected = local_usernames()
    inactive_users = [
        user for key, user in local_by_username.items()
        if user.origen.upper() in {"LDAP", "AD"}
        and user.status and key not in ad_by_username and key not in protected
    ]
    convenios = list(CONVENIOS_DISPONIBLES)
    tipos = list(db.scalars(
        select(TipoContratacion).order_by(TipoContratacion.id_tipo_contratacion)
    ))
    return templates.TemplateResponse(request, "usuarios_ad_sync.html", {
        "active_page": "usuarios",
        "error": error,
        "guardado": guardado,
        "new_users": new_users,
        "existing_users": existing_users,
        "inactive_users": inactive_users,
        "reactivable_users": reactivable_users,
        "convenios": convenios,
        "tipos_contratacion": tipos,
        "roles_disponibles": ROLES_DISPONIBLES,
        "perfiles_disponibles": PERFILES_DISPONIBLES,
    })


@app.post("/configuracion/usuarios/sincronizar/incorporar")
def incorporar_usuario_ad(
    request: Request,
    username: Annotated[str, Form()],
    nombre: Annotated[str, Form()],
    apellido: Annotated[str, Form()],
    correo: Annotated[str | None, Form()] = None,
    legajo: Annotated[str | None, Form()] = None,
    convenio: Annotated[str, Form()] = "",
    id_tipo_contratacion: Annotated[int, Form()] = 0,
    rol: Annotated[str, Form()] = "",
    perfil: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    require_user_management_role(request)
    normalized = normalize_username(username)
    if find_user(db, normalized) is not None:
        return RedirectResponse("/configuracion/usuarios/sincronizar?error=El+usuario+ya+existe+y+no+fue+modificado", status_code=303)
    try:
        ad_user = next(
            (item for item in list_ad_group_users() if normalize_username(item.username) == normalized),
            None,
        )
    except ActiveDirectoryAuthError:
        return RedirectResponse("/configuracion/usuarios/sincronizar?error=No+se+pudo+verificar+el+usuario+en+AD", status_code=303)
    if ad_user is None:
        return RedirectResponse("/configuracion/usuarios/sincronizar?error=El+usuario+ya+no+pertenece+al+grupo+de+AD", status_code=303)
    role_name, profile_name = rol.strip().upper(), perfil.strip().upper()
    if role_name not in ROLES_DISPONIBLES or profile_name not in PERFILES_DISPONIBLES:
        raise HTTPException(status_code=422, detail="El rol o perfil no es válido.")
    if db.get(TipoContratacion, id_tipo_contratacion) is None:
        raise HTTPException(status_code=422, detail="La contratación no existe.")
    if convenio.strip().upper() not in CONVENIOS_DISPONIBLES:
        raise HTTPException(status_code=422, detail="El convenio no existe.")
    user = Usuario(
        legajo=clean_optional(legajo),
        nombre=nombre.strip() or ad_user.first_name,
        apellido=apellido.strip() or ad_user.last_name,
        correo=clean_optional(correo) or ad_user.email,
        convenio=convenio.strip().upper(),
        id_tipo_contratacion=id_tipo_contratacion,
        username=normalized,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        origen="AD",
        status=True,
    )
    user.roles.append(UsuarioRol(rol=role_name, perfil=profile_name))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/configuracion/usuarios/sincronizar?error=El+legajo,+correo+o+usuario+ya+está+registrado", status_code=303)
    return RedirectResponse("/configuracion/usuarios/sincronizar?guardado=usuario+incorporado", status_code=303)


@app.post("/configuracion/usuarios/sincronizar/{usuario_id}/reactivar")
def reactivar_usuario_ad(usuario_id: int, request: Request, db: Session = Depends(get_db)):
    require_user_management_role(request)
    user = db.get(Usuario, usuario_id)
    if user is None or user.origen.upper() not in {"LDAP", "AD"}:
        raise HTTPException(status_code=404, detail="El usuario LDAP no existe.")
    try:
        present = any(normalize_username(item.username) == normalize_username(user.username) for item in list_ad_group_users())
    except ActiveDirectoryAuthError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if not present:
        raise HTTPException(status_code=409, detail="El usuario no pertenece actualmente al grupo de AD.")
    user.status = True
    db.commit()
    return RedirectResponse("/configuracion/usuarios/sincronizar?guardado=usuario+reactivado", status_code=303)


@app.post("/configuracion/usuarios/sincronizar/desactivar-ausentes")
def desactivar_usuarios_ad_ausentes(request: Request, db: Session = Depends(get_db)):
    require_user_management_role(request)
    try:
        present = {normalize_username(item.username) for item in list_ad_group_users()}
    except ActiveDirectoryAuthError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    protected = local_usernames()
    users = list(db.scalars(select(Usuario).where(Usuario.status.is_(True))))
    changed = 0
    for user in users:
        username = normalize_username(user.username)
        if user.origen.upper() in {"LDAP", "AD"} and username not in present and username not in protected:
            user.status = False
            changed += 1
    db.commit()
    return RedirectResponse(f"/configuracion/usuarios/sincronizar?guardado={changed}+usuarios+desactivados", status_code=303)


@app.post("/configuracion/usuarios/{usuario_id}/roles")
def crear_asignacion_usuario(
    usuario_id: int,
    rol: Annotated[str, Form()],
    perfil: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="El usuario no existe.")
    nombre_rol = rol.strip().upper()
    nombre_perfil = perfil.strip().upper()
    if nombre_rol not in ROLES_DISPONIBLES or nombre_perfil not in PERFILES_DISPONIBLES:
        raise HTTPException(status_code=422, detail="La asignación de rol y perfil no es válida.")
    db.add(UsuarioRol(usuario_id=usuario_id, rol=nombre_rol, perfil=nombre_perfil))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            f"/configuracion/usuarios?editar_usuario={usuario_id}&error=La+asignación+ya+existe#asignaciones",
            status_code=303,
        )
    return RedirectResponse(
        f"/configuracion/usuarios?editar_usuario={usuario_id}&guardado=asignación+creada#asignaciones",
        status_code=303,
    )


@app.post("/configuracion/usuarios/{usuario_id}/roles/{rol_id}/eliminar")
def eliminar_asignacion_usuario(
    usuario_id: int,
    rol_id: int,
    db: Session = Depends(get_db),
):
    registro = db.get(UsuarioRol, rol_id)
    if registro is None or registro.usuario_id != usuario_id:
        raise HTTPException(status_code=404, detail="La asignación no existe.")
    db.delete(registro)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo eliminar la asignación.")
    return RedirectResponse(
        f"/configuracion/usuarios?editar_usuario={usuario_id}&guardado=asignación+eliminada#asignaciones",
        status_code=303,
    )


def update_user_fields(
    usuario: Usuario,
    legajo: str | None,
    nombre: str,
    apellido: str,
    correo: str | None,
    convenio: str | None,
    id_tipo_contratacion: int,
    observaciones: str | None,
    username: str,
    origen: str,
    status: str | None,
) -> None:
    required = [nombre.strip(), apellido.strip(), username.strip(), origen.strip()]
    if not all(required):
        raise HTTPException(status_code=422, detail="Completá los campos obligatorios.")
    usuario.legajo = clean_optional(legajo)
    usuario.nombre = nombre.strip()
    usuario.apellido = apellido.strip()
    usuario.correo = clean_optional(correo)
    usuario.convenio = clean_optional(convenio)
    usuario.id_tipo_contratacion = id_tipo_contratacion
    usuario.observaciones = clean_optional(observaciones)
    usuario.username = username.strip()
    usuario.origen = origen.strip()
    usuario.status = status == "on"


@app.post("/configuracion/usuarios")
def crear_usuario(
    nombre: Annotated[str, Form()],
    apellido: Annotated[str, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    origen: Annotated[str, Form()],
    rol: Annotated[str, Form()],
    perfil: Annotated[str, Form()],
    legajo: Annotated[str | None, Form()] = None,
    correo: Annotated[str | None, Form()] = None,
    convenio: Annotated[str | None, Form()] = None,
    id_tipo_contratacion: Annotated[int, Form()] = 0,
    observaciones: Annotated[str | None, Form()] = None,
    status: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    nombre_rol = rol.strip().upper()
    nombre_perfil = perfil.strip().upper()
    if nombre_rol not in ROLES_DISPONIBLES or nombre_perfil not in PERFILES_DISPONIBLES:
        raise HTTPException(status_code=422, detail="La asignación de rol y perfil no es válida.")
    if len(password) < 8:
        return RedirectResponse("/configuracion/usuarios?error=La+contraseña+debe+tener+al+menos+8+caracteres", status_code=303)
    if db.get(TipoContratacion, id_tipo_contratacion) is None:
        raise HTTPException(status_code=422, detail="La contratación seleccionada no existe.")
    if not convenio or convenio.strip().upper() not in CONVENIOS_DISPONIBLES:
        raise HTTPException(status_code=422, detail="El convenio seleccionado no es válido.")
    convenio = convenio.strip().upper()
    usuario = Usuario(hashed_password=hash_password(password))
    update_user_fields(usuario, legajo, nombre, apellido, correo, convenio, id_tipo_contratacion, observaciones, username, origen, status)
    db.add(usuario)
    try:
        db.flush()
        db.add(UsuarioRol(
            usuario_id=usuario.id,
            rol=nombre_rol,
            perfil=nombre_perfil,
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/configuracion/usuarios?error=El+legajo,+correo+o+usuario+ya+está+registrado", status_code=303)
    return RedirectResponse("/configuracion/usuarios?guardado=usuario+creado", status_code=303)


@app.post("/configuracion/usuarios/{usuario_id}")
def actualizar_usuario(
    usuario_id: int,
    nombre: Annotated[str, Form()],
    apellido: Annotated[str, Form()],
    username: Annotated[str, Form()],
    origen: Annotated[str, Form()],
    legajo: Annotated[str | None, Form()] = None,
    correo: Annotated[str | None, Form()] = None,
    convenio: Annotated[str | None, Form()] = None,
    id_tipo_contratacion: Annotated[int, Form()] = 0,
    observaciones: Annotated[str | None, Form()] = None,
    password: Annotated[str | None, Form()] = None,
    status: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="El usuario no existe.")
    if db.get(TipoContratacion, id_tipo_contratacion) is None:
        raise HTTPException(status_code=422, detail="La contratación seleccionada no existe.")
    if not convenio or convenio.strip().upper() not in CONVENIOS_DISPONIBLES:
        raise HTTPException(status_code=422, detail="El convenio seleccionado no es válido.")
    convenio = convenio.strip().upper()
    if password:
        if len(password) < 8:
            return RedirectResponse(f"/configuracion/usuarios?editar_usuario={usuario_id}&error=La+contraseña+debe+tener+al+menos+8+caracteres", status_code=303)
        usuario.hashed_password = hash_password(password)
    update_user_fields(usuario, legajo, nombre, apellido, correo, convenio, id_tipo_contratacion, observaciones, username, origen, status)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(f"/configuracion/usuarios?editar_usuario={usuario_id}&error=El+legajo,+correo+o+usuario+ya+está+registrado", status_code=303)
    return RedirectResponse("/configuracion/usuarios?guardado=usuario+actualizado", status_code=303)


@app.get("/configuracion/feriados", response_class=HTMLResponse)
def administrar_feriados(
    request: Request,
    editar: int | None = None,
    guardado: str | None = None,
    error: str | None = None,
    anio_actualizado: int | None = None,
    cantidad: int | None = None,
    duplicados: int | None = None,
    db: Session = Depends(get_db),
):
    feriados: list[Feriado] = []
    feriado_edicion = None
    try:
        feriados = list(db.scalars(select(Feriado).order_by(Feriado.fecha)))
        if editar is not None:
            feriado_edicion = db.get(Feriado, editar)
            if feriado_edicion is None:
                error = "El feriado seleccionado no existe"
    except SQLAlchemyError:
        error = "No se pudieron cargar los feriados. Verificá la conexión con la base"
    return templates.TemplateResponse(request, "feriados.html", {
        "active_page": "feriados",
        "feriados": feriados,
        "feriado_edicion": feriado_edicion,
        "guardado": guardado,
        "error": error,
        "anio_actualizado": anio_actualizado,
        "cantidad": cantidad or 0,
        "duplicados": duplicados or 0,
        "anio_actualizacion": min(
            (feriado.fecha.year for feriado in feriados),
            default=None,
        ),
        "es_jefe": str((request.session.get("user") or {}).get("profile") or "").upper() == "JEFE",
    })


def pending_owned_hour(request: Request, hora_id: int, db: Session) -> HoraExtra:
    user_id = (request.session.get("user") or {}).get("id")
    hour = db.scalar(
        select(HoraExtra)
        .options(selectinload(HoraExtra.usuario))
        .where(HoraExtra.id == hora_id, HoraExtra.usuario_id == user_id)
    )
    if hour is None:
        raise HTTPException(status_code=404, detail="La carga no existe.")
    if hour.estado != "PENDIENTE":
        raise HTTPException(status_code=409, detail="Sólo se pueden modificar cargas pendientes.")
    return hour


@app.get("/horas/{hora_id}/editar", response_class=HTMLResponse)
def editar_hora_propia(hora_id: int, request: Request, db: Session = Depends(get_db)):
    hour = pending_owned_hour(request, hora_id, db)
    return templates.TemplateResponse(request, "hora_editar.html", {
        "active_page": "horas",
        "hora": hour,
    })


@app.post("/horas/{hora_id}/editar")
def guardar_hora_propia(
    hora_id: int,
    request: Request,
    fecha: Annotated[date, Form()],
    hora_inicio: Annotated[time | None, Form()] = None,
    hora_fin: Annotated[time | None, Form()] = None,
    marcar_como_franco: Annotated[str | None, Form()] = None,
    solicita_reintegro: Annotated[str | None, Form()] = None,
    observaciones: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    hour = pending_owned_hour(request, hora_id, db)
    try:
        if hour.tipo_registro == "REINTEGRO":
            result = calcular_resultado_reintegro(hour.usuario_id, fecha, db)
            hour.hora_inicio = None
            hour.hora_fin = None
            hour.horas_totales = None
            hour.horas_nocturnas = None
            hour.solicita_reintegro = True
        else:
            if hora_inicio is None or hora_fin is None:
                raise CalculoHorasError("Ingresá la hora de inicio y finalización.")
            result = calcular_resultado_horas_extra(
                hour.usuario_id, fecha, hora_inicio, hora_fin, db,
                marcar_como_franco=marcar_como_franco == "on",
            )
            hour.hora_inicio = result.hora_inicio
            hour.hora_fin = result.hora_fin
            hour.horas_totales = result.horas_totales
            hour.horas_nocturnas = result.horas_nocturnas
            hour.solicita_reintegro = result.permite_reintegro and solicita_reintegro == "on"
        hour.fecha = result.fecha
        hour.tipo_dia = result.tipo_dia
        hour.tipo_hora = result.tipo_hora
        hour.observaciones = clean_optional(observaciones)
        db.commit()
    except (CalculoHorasError, SQLAlchemyError) as error:
        db.rollback()
        message = str(error) if isinstance(error, CalculoHorasError) else "No se pudo actualizar la carga."
        raise HTTPException(status_code=422, detail=message) from error
    return RedirectResponse("/horas", status_code=303)


@app.post("/horas/{hora_id}/eliminar")
def eliminar_hora_propia(hora_id: int, request: Request, db: Session = Depends(get_db)):
    hour = pending_owned_hour(request, hora_id, db)
    try:
        db.delete(hour)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo eliminar la carga.") from error
    return RedirectResponse("/horas", status_code=303)


@app.post("/configuracion/feriados")
def crear_feriado(
    fecha: Annotated[date, Form()],
    observacion: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    observacion = observacion.strip()
    if not observacion:
        raise HTTPException(status_code=422, detail="La observación es obligatoria.")
    db.add(Feriado(fecha=fecha, observacion=observacion))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/configuracion/feriados?error=Ya+existe+un+feriado+en+esa+fecha", status_code=303)
    return RedirectResponse("/configuracion/feriados?guardado=creado", status_code=303)


@app.post("/configuracion/feriados/actualizar-anio")
def actualizar_anio_feriados(
    anio_origen: Annotated[int, Form()],
    db: Session = Depends(get_db),
):
    if anio_origen < 1900 or anio_origen > 9998:
        raise HTTPException(status_code=422, detail="El año indicado no es válido.")
    feriados_origen = list(db.scalars(
        select(Feriado)
        .where(
            Feriado.fecha >= date(anio_origen, 1, 1),
            Feriado.fecha <= date(anio_origen, 12, 31),
        )
        .order_by(Feriado.fecha)
    ))
    if not feriados_origen:
        return RedirectResponse(
            "/configuracion/feriados?error=No+hay+feriados+para+el+año+seleccionado",
            status_code=303,
        )

    anio_destino = anio_origen + 1
    fechas_destino = {
        feriado.fecha: feriado
        for feriado in db.scalars(
            select(Feriado).where(
                Feriado.fecha >= date(anio_destino, 1, 1),
                Feriado.fecha <= date(anio_destino, 12, 31),
            )
        )
    }
    actualizados = 0
    duplicados = 0
    try:
        for feriado in feriados_origen:
            try:
                nueva_fecha = feriado.fecha.replace(year=anio_destino)
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail=f"La fecha {feriado.fecha.strftime('%d/%m/%Y')} no existe en {anio_destino}.",
                ) from error
            if nueva_fecha in fechas_destino:
                db.delete(feriado)
                duplicados += 1
            else:
                feriado.fecha = nueva_fecha
                fechas_destino[nueva_fecha] = feriado
                actualizados += 1
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo actualizar el año de los feriados.") from error
    return RedirectResponse(
        f"/configuracion/feriados?anio_actualizado={anio_destino}&cantidad={actualizados}&duplicados={duplicados}",
        status_code=303,
    )


@app.post("/configuracion/feriados/{feriado_id}")
def actualizar_feriado(
    feriado_id: int,
    fecha: Annotated[date, Form()],
    observacion: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    feriado = db.get(Feriado, feriado_id)
    if feriado is None:
        raise HTTPException(status_code=404, detail="El feriado no existe.")
    feriado.fecha = fecha
    feriado.observacion = observacion.strip()
    if not feriado.observacion:
        raise HTTPException(status_code=422, detail="La observación es obligatoria.")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            f"/configuracion/feriados?editar={feriado_id}&error=Ya+existe+un+feriado+en+esa+fecha",
            status_code=303,
        )
    return RedirectResponse("/configuracion/feriados?guardado=actualizado", status_code=303)


@app.get("/autorizaciones", response_class=HTMLResponse)
def autorizaciones(
    request: Request,
    editar: int | None = None,
    guardado: int | None = None,
    rechazado: int | None = None,
    db: Session = Depends(get_db),
):
    autorizador, asignacion_activa, es_admin = obtener_autorizador(request, db)
    grupos = []
    hora_edicion = None
    condiciones = [HoraExtra.estado == "PENDIENTE"]
    if not es_admin:
        condiciones.extend([
            Usuario.id != autorizador.id,
            Usuario.roles.any(func.upper(UsuarioRol.rol) == asignacion_activa.rol.upper()),
        ])
    pendientes = list(db.scalars(
        select(HoraExtra)
        .join(HoraExtra.usuario)
        .options(selectinload(HoraExtra.usuario).selectinload(Usuario.roles))
        .where(*condiciones)
        .order_by(Usuario.apellido, Usuario.nombre, HoraExtra.fecha, HoraExtra.hora_inicio)
    ))
    agrupados: dict[int, dict] = {}
    for hora in pendientes:
        grupo = agrupados.setdefault(hora.usuario_id, {
            "usuario": hora.usuario,
            "horas": [],
            "total": Decimal("0.00"),
        })
        grupo["horas"].append(hora)
        grupo["total"] += hora.horas_totales or Decimal("0.00")
    grupos = list(agrupados.values())

    if editar is not None:
        hora_edicion = db.scalar(
            select(HoraExtra)
            .options(selectinload(HoraExtra.usuario).selectinload(Usuario.roles))
            .where(HoraExtra.id == editar)
        )
        validar_hora_para_autorizador(hora_edicion, asignacion_activa, es_admin)

    return templates.TemplateResponse(request, "autorizaciones.html", {
        "active_page": "autorizaciones",
        "jefe": autorizador,
        "asignacion_activa": asignacion_activa,
        "es_admin": es_admin,
        "grupos": grupos,
        "hora_edicion": hora_edicion,
        "guardado": guardado,
        "rechazado": rechazado,
        "cantidad_pendientes": sum(len(grupo["horas"]) for grupo in grupos),
        "total_pendiente": sum(
            (grupo["total"] for grupo in grupos),
            start=Decimal("0.00"),
        ),
    })


@app.post("/autorizaciones/aprobar")
def aprobar_horas(
    request: Request,
    hora_ids: Annotated[list[int] | None, Form()] = None,
    db: Session = Depends(get_db),
):
    ids = list(dict.fromkeys(hora_ids or []))
    if not ids:
        raise HTTPException(status_code=422, detail="Seleccioná al menos una carga para autorizar.")
    autorizador, asignacion_activa, es_admin = obtener_autorizador(request, db)
    try:
        horas = list(db.scalars(
            select(HoraExtra)
            .options(selectinload(HoraExtra.usuario).selectinload(Usuario.roles))
            .where(HoraExtra.id.in_(ids))
            .with_for_update()
        ))
        if len(horas) != len(ids):
            raise HTTPException(status_code=404, detail="Una de las cargas seleccionadas no existe.")
        for hora in horas:
            validar_hora_para_autorizador(hora, asignacion_activa, es_admin)
            hora.estado = "APROBADA"
            hora.aprobado_por = autorizador.id
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudieron autorizar las horas seleccionadas.") from error
    return RedirectResponse(
        f"/autorizaciones?guardado={len(horas)}",
        status_code=303,
    )


@app.post("/autorizaciones/{hora_id}/eliminar")
def eliminar_hora_pendiente(
    hora_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    _, assignment, is_admin = obtener_autorizador(request, db)
    hour = db.scalar(
        select(HoraExtra)
        .options(selectinload(HoraExtra.usuario).selectinload(Usuario.roles))
        .where(HoraExtra.id == hora_id)
    )
    validar_hora_para_autorizador(hour, assignment, is_admin)
    try:
        db.delete(hour)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo eliminar la carga.") from error
    return RedirectResponse("/autorizaciones", status_code=303)


@app.post("/autorizaciones/rechazar")
def rechazar_horas(
    request: Request,
    hora_ids: Annotated[list[int] | None, Form()] = None,
    db: Session = Depends(get_db),
):
    ids = list(dict.fromkeys(hora_ids or []))
    if not ids:
        raise HTTPException(status_code=422, detail="Seleccioná al menos una carga para rechazar.")
    autorizador, asignacion_activa, es_admin = obtener_autorizador(request, db)
    try:
        horas = list(db.scalars(
            select(HoraExtra)
            .options(selectinload(HoraExtra.usuario).selectinload(Usuario.roles))
            .where(HoraExtra.id.in_(ids))
            .with_for_update()
        ))
        if len(horas) != len(ids):
            raise HTTPException(status_code=404, detail="Una de las cargas seleccionadas no existe.")
        for hora in horas:
            validar_hora_para_autorizador(hora, asignacion_activa, es_admin)
            hora.estado = "RECHAZADA"
            hora.aprobado_por = autorizador.id
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudieron rechazar las horas seleccionadas.") from error
    return RedirectResponse(
        f"/autorizaciones?rechazado={len(horas)}",
        status_code=303,
    )


@app.post("/autorizaciones/{hora_id}/editar")
def editar_hora_pendiente(
    hora_id: int,
    request: Request,
    fecha: Annotated[date, Form()],
    hora_inicio: Annotated[time, Form()],
    hora_fin: Annotated[time, Form()],
    observaciones: Annotated[str | None, Form()] = None,
    solicita_reintegro: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    autorizador, asignacion_activa, es_admin = obtener_autorizador(request, db)
    hora = db.scalar(
        select(HoraExtra)
        .options(selectinload(HoraExtra.usuario).selectinload(Usuario.roles))
        .where(HoraExtra.id == hora_id)
    )
    validar_hora_para_autorizador(hora, asignacion_activa, es_admin)
    try:
        resultado = calcular_resultado_horas_extra(
            hora.usuario_id, fecha, hora_inicio, hora_fin, db,
            marcar_como_franco=hora.tipo_dia == "FRANCO",
        )
        hora.fecha = resultado.fecha
        hora.hora_inicio = resultado.hora_inicio
        hora.hora_fin = resultado.hora_fin
        hora.horas_totales = resultado.horas_totales
        hora.tipo_dia = resultado.tipo_dia
        hora.tipo_hora = resultado.tipo_hora
        hora.horas_nocturnas = resultado.horas_nocturnas
        hora.solicita_reintegro = (
            resultado.permite_reintegro and solicita_reintegro == "on"
        )
        hora.observaciones = clean_optional(observaciones)
        db.commit()
    except (CalculoHorasError, SQLAlchemyError) as error:
        db.rollback()
        mensaje = str(error) if isinstance(error, CalculoHorasError) else "No se pudo actualizar la carga."
        raise HTTPException(status_code=422, detail=mensaje) from error
    return RedirectResponse("/autorizaciones", status_code=303)


@app.get("/health")
def health_check():
    return {"estado": "ok", "aplicacion": "activa"}


@app.get("/health/db")
@app.get("/health/database")
def database_health_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"estado": "ok", "base_de_datos": "conectada"}
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503,
            detail={"estado": "error", "base_de_datos": "desconectada"},
        ) from error
