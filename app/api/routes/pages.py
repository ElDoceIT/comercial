import csv
import json
import logging
import secrets
import zipfile
import calendar
from io import BytesIO, StringIO
import math
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from xml.sax.saxutils import escape
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, literal, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.db import get_db
from app.core.config import get_settings
from app.models.archivos_ingesta import ArchivoIngesta
from app.models.base_anunciantes import BaseAnunciante
from app.models.cronogramas import Cronograma
from app.models.maestro_productos import MaestroProducto
from app.models.productos_temas import ProductoTema
from app.models.usuarios import Usuario
from app.services.ad_auth import ActiveDirectoryAuthError, list_ad_group_users
from app.services.local_auth import hash_password, normalize_username
from app.services.processing_service import (
    DuplicateArchivoIngestaError,
    ProcessingService,
    ProcessingServiceError,
)
from app.services.drive_service import DriveService, DriveServiceError


BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)
PIE_COLORS = [
    "#2563eb",
    "#0f766e",
    "#f97316",
    "#7c3aed",
    "#dc2626",
    "#059669",
    "#ca8a04",
    "#0891b2",
]
CHANNEL_COLORS = {
    "Canal 12": "#f97316",
    "Canal 10": "#2563eb",
    "Telefe Córdoba": "#0f766e",
    "Telefe Cordoba": "#0f766e",
    "Canal 8": "#0f766e",
    "Sin canal": "#6b7280",
}
WEEKDAY_LABELS = {
    1: "Lunes",
    2: "Martes",
    3: "Miércoles",
    4: "Jueves",
    5: "Viernes",
    6: "Sábado",
    7: "Domingo",
}
MONTH_LABELS = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}
TRUSTED_AUTO_ASSIGNMENT_RULES = {
    "Regla canal",
    "Producto aprendido",
    "Marca aprendida",
}
USER_ROLES = ("ADMIN", "COMERCIAL", "ADMINISTRACION")
USER_PROFILES = ("USUARIO", "JEFE")
EXPORT_CRONOGRAMAS_COLUMNS = [
    ("Hora Inicio", "hora_inicio"),
    ("Hora Fin", "hora_fin"),
    ("Cod.Prod.", "cod_prod"),
    ("Producto", "producto"),
    ("Tema", "tema"),
    ("Duracion", "duracion"),
    ("T.Compra", "t_compra"),
    ("T.Material", "t_material"),
    ("Unnamed: 8", "columna_extra"),
    ("Alcance", "alcance"),
    ("Prom.Canal", "prom_canal"),
    ("Programa", "programa"),
    ("CANAL", "canal"),
    ("Fecha", "fecha"),
    ("Dia_semana", "dia_semana"),
    ("Diadesemana", "dia_de_semana"),
    ("Tipo_dia", "tipo_dia"),
]
EXPORT_CRONOGRAMAS_COMPLETA_COLUMNS = [
    *EXPORT_CRONOGRAMAS_COLUMNS,
    ("Producto_maestro", "producto_base"),
    ("Alcance_base", "alcance_base"),
    ("Categoria", "categoria"),
]


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse(url="/envio-monitor", status_code=303)


@router.post("/run-drive-process", response_class=HTMLResponse)
def run_drive_process(db: Session = Depends(get_db)):
    settings = get_settings()
    drive_service = DriveService(settings)
    service = ProcessingService()

    try:
        files = drive_service.list_xls_files()
    except DriveServiceError as exc:
        return RedirectResponse(
            url=_home_process_redirect_url(
                status="error",
                message=str(exc),
            ),
            status_code=303,
        )

    if not files:
        return RedirectResponse(
            url=_home_process_redirect_url(
                status="error",
                message="No hay archivos XLS en la carpeta de Drive configurada.",
            ),
            status_code=303,
        )

    processed = 0
    skipped = 0
    failed = 0
    for file in files:
        file_id = str(file.get("id") or "")
        if not file_id:
            failed += 1
            continue

        try:
            service.load_monitor_file_to_db(db=db, file_id=file_id)
            processed += 1
        except DuplicateArchivoIngestaError:
            skipped += 1
        except (DriveServiceError, ProcessingServiceError, Exception):
            failed += 1

    suggestions, ambiguous, missing_advertiser, unresolved = _get_auto_assignment_suggestions(
        db
    )
    assignment_result = _apply_auto_assignment_suggestions(
        db,
        suggestions,
        allowed_rules=TRUSTED_AUTO_ASSIGNMENT_RULES,
    )

    message = (
        f"Drive XLS: {len(files)}. "
        f"Procesados: {processed}. "
        f"Ya cargados: {skipped}. "
        f"Con error: {failed}. "
        f"Autoasignados: {assignment_result['created']}. "
        f"Ambiguos: {ambiguous}. "
        f"Sin anunciante configurado: {missing_advertiser}. "
        f"Sin regla: {unresolved}. "
        f"Para revisar: {assignment_result['skipped_by_rule']}."
    )
    return RedirectResponse(
        url=_home_process_redirect_url(status="ok", message=message),
        status_code=303,
    )


@router.post("/archivos/complementario")
async def upload_complementary_file(
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = Path(archivo.filename or "archivo_complementario").name
    try:
        result = ProcessingService().load_complementary_file_to_db(
            db=db,
            filename=filename,
            content=await archivo.read(),
        )
        master_created = 0
        topics_created = 0
        for product, topic in result["productos"]:
            maestro = (
                db.query(MaestroProducto)
                .filter(_match_text_expr(MaestroProducto.producto) == product.strip().lower())
                .first()
            )
            if maestro is None:
                maestro = MaestroProducto(producto=product.strip(), id_anunciante=None)
                db.add(maestro)
                db.flush()
                master_created += 1
            normalized_topic = topic.strip()
            topic_query = db.query(ProductoTema).filter(ProductoTema.id_producto == maestro.id)
            if normalized_topic:
                topic_query = topic_query.filter(
                    _match_text_expr(ProductoTema.tema) == normalized_topic.lower()
                )
            else:
                topic_query = topic_query.filter(ProductoTema.tema.is_(None))
            product_topic = topic_query.first()
            if product_topic is None:
                product_topic = ProductoTema(
                    id_producto=maestro.id,
                    tema=normalized_topic or None,
                    cod_producto=None,
                )
                db.add(product_topic)
                topics_created += 1

            product_code = product_topic.cod_producto
            if not product_code:
                product_code = f"C{secrets.randbelow(10**12):012d}"
                product_topic.cod_producto = product_code

            cronograma_query = db.query(Cronograma).filter(
                Cronograma.archivo_ingesta_id.in_(result["archivo_ingesta_ids"]),
                _match_text_expr(Cronograma.producto) == product.strip().lower(),
            )
            if normalized_topic:
                cronograma_query = cronograma_query.filter(
                    _match_text_expr(Cronograma.tema) == normalized_topic.lower()
                )
            else:
                cronograma_query = cronograma_query.filter(Cronograma.tema.is_(None))
            cronograma_query.update(
                {Cronograma.cod_prod: product_code},
                synchronize_session=False,
            )
        db.commit()
        message = (
            f"Complementario procesado: {result['filas_insertadas']} filas, "
            f"{result['bloques_insertados']} fechas/canales cargados y "
            f"{result['bloques_omitidos']} omitidos por duplicados. "
            f"Productos nuevos: {master_created}. Temas nuevos: {topics_created}."
        )
        status = "ok"
    except (DuplicateArchivoIngestaError, ProcessingServiceError) as exc:
        db.rollback()
        message = str(exc)
        status = "error"
    except Exception:
        db.rollback()
        logger.exception("Error al procesar archivo complementario")
        message = "No se pudo procesar el archivo complementario."
        status = "error"

    return RedirectResponse(
        url=f"/archivos?process_status={quote(status)}&process_message={quote(message)}",
        status_code=303,
    )


def _home_process_redirect_url(*, status: str, message: str) -> str:
    return f"/archivos?process_status={quote(status)}&process_message={quote(message)}"


def _get_unmatched_product_rows(
    db: Session,
    *,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    archivo_id: int | None,
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("segundos")
    filas_expr = func.count(Cronograma.id).label("filas")
    ultima_fecha_expr = func.max(Cronograma.fecha).label("ultima_fecha")
    query = (
        db.query(
            Cronograma.cod_prod,
            Cronograma.producto,
            Cronograma.tema,
            filas_expr,
            duracion_expr,
            ultima_fecha_expr,
        )
        .select_from(Cronograma)
    )
    query = _join_resolved_maestro(query, db)
    query = query.filter(
        or_(MaestroProducto.id.is_(None), MaestroProducto.id_anunciante.is_(None))
    )
    query = query.filter(Cronograma.producto.isnot(None))
    query = query.filter(func.trim(Cronograma.producto) != "")
    query = _apply_control_product_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        archivo_id=archivo_id,
    )
    rows = (
        query
        .group_by(Cronograma.cod_prod, Cronograma.producto, Cronograma.tema)
        .order_by(duracion_expr.desc())
        .limit(200)
        .all()
    )

    return [
        {
            "cod_prod": row.cod_prod,
            "producto": row.producto,
            "tema": row.tema,
            "filas": int(row.filas or 0),
            "segundos": int(row.segundos or 0),
            "ultima_fecha": row.ultima_fecha,
        }
        for row in rows
    ]


def _get_matched_product_rows(
    db: Session,
    *,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    archivo_id: int | None,
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("segundos")
    filas_expr = func.count(Cronograma.id).label("filas")
    ultima_fecha_expr = func.max(Cronograma.fecha).label("ultima_fecha")
    temas_registrados = (
        db.query(
            ProductoTema.id_producto.label("id_producto"),
            _match_text_expr(ProductoTema.tema).label("tema_normalizado"),
            func.min(ProductoTema.tema).label("tema"),
        )
        .filter(ProductoTema.tema.isnot(None))
        .filter(func.trim(ProductoTema.tema) != "")
        .group_by(
            ProductoTema.id_producto,
            _match_text_expr(ProductoTema.tema),
        )
        .subquery()
    )

    query = (
        db.query(
            Cronograma.cod_prod,
            Cronograma.producto.label("producto_cronograma"),
            Cronograma.tema.label("tema_cronograma"),
            MaestroProducto.producto.label("producto_base"),
            temas_registrados.c.tema.label("tema_base"),
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
            filas_expr,
            duracion_expr,
            ultima_fecha_expr,
        )
        .select_from(Cronograma)
    )
    query = _join_resolved_maestro(query, db)
    query = (
        query
        .filter(MaestroProducto.id.isnot(None))
        .outerjoin(
            temas_registrados,
            (temas_registrados.c.id_producto == MaestroProducto.id)
            & (
                temas_registrados.c.tema_normalizado
                == _match_text_expr(Cronograma.tema)
            ),
        )
        .outerjoin(
            BaseAnunciante,
            MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante,
        )
    )
    query = _apply_control_product_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        archivo_id=archivo_id,
    )
    rows = (
        query
        .group_by(
            Cronograma.cod_prod,
            Cronograma.producto,
            Cronograma.tema,
            MaestroProducto.producto,
            temas_registrados.c.tema,
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
        )
        .order_by(ultima_fecha_expr.desc(), duracion_expr.desc())
        .limit(200)
        .all()
    )

    return [
        {
            "cod_prod": row.cod_prod,
            "producto_cronograma": row.producto_cronograma,
            "tema_cronograma": row.tema_cronograma,
            "producto_base": row.producto_base,
            "tema_base": row.tema_base,
            "anunciante": row.anunciante,
            "alcance": row.alcance,
            "categoria": row.categoria,
            "filas": int(row.filas or 0),
            "segundos": int(row.segundos or 0),
            "ultima_fecha": row.ultima_fecha,
        }
        for row in rows
    ]


def _apply_control_product_filters(
    query,
    *,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    archivo_id: int | None,
):
    if fecha_desde:
        query = query.filter(Cronograma.fecha >= fecha_desde)
    if fecha_hasta:
        query = query.filter(Cronograma.fecha <= fecha_hasta)
    if archivo_id:
        query = query.filter(Cronograma.archivo_ingesta_id == archivo_id)

    return query


def _get_distinct_base_anunciante_values(db: Session, column) -> list[str]:
    return _clean_multi_values(
        [
            row[0]
            for row in db.query(column)
            .filter(column.isnot(None))
            .distinct()
            .order_by(column.asc())
            .all()
        ]
    )


def _normalize_match_text(value: object) -> str:
    text = str(value or "").upper().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split())


def _is_commercial_excluded_category(value: object) -> bool:
    category = _normalize_match_text(value)
    return category in {"SIN CARGO", "ASOCIACION CIVIL", "ELECCIONES"} or (
        "PROM" in category and "CANAL" in category
    )


def _match_text_expr(column):
    """Normalize SQL text under one collation for cross-table comparisons."""
    return func.lower(func.trim(column)).collate("utf8mb4_unicode_ci")


def _join_resolved_maestro(query, db: Session):
    """Join one master, preferring its name over an unambiguous historical code."""
    unique_codes = (
        db.query(
            ProductoTema.cod_producto.label("cod_producto"),
            func.min(ProductoTema.id_producto).label("id_producto"),
        )
        .filter(ProductoTema.cod_producto.isnot(None))
        .filter(func.trim(ProductoTema.cod_producto) != "")
        .group_by(ProductoTema.cod_producto)
        .having(func.count(func.distinct(ProductoTema.id_producto)) == 1)
        .subquery()
    )
    maestro_por_nombre = aliased(MaestroProducto)

    return (
        query
        .outerjoin(
            maestro_por_nombre,
            _match_text_expr(Cronograma.producto)
            == _match_text_expr(maestro_por_nombre.producto),
        )
        .outerjoin(
            unique_codes,
            _match_text_expr(Cronograma.cod_prod)
            == _match_text_expr(unique_codes.c.cod_producto),
        )
        .outerjoin(
            MaestroProducto,
            MaestroProducto.id
            == func.coalesce(maestro_por_nombre.id, unique_codes.c.id_producto),
        )
    )


SIGNIFICANT_WORD_STOPLIST = {
    "ACCION",
    "ALIMENTOS",
    "ANALGESICO",
    "AVISO",
    "CANAL",
    "COMPRAS",
    "CORDOBA",
    "CREMA",
    "FLEX",
    "INSTITUCIONAL",
    "JABON",
    "LIQUIDO",
    "MARCA",
    "NACIONAL",
    "OLOR",
    "PRODUCTO",
    "PROM",
    "PROMO",
    "PROMOCION",
    "PUBLICIDAD",
    "RAPIDA",
    "SERVICIO",
    "SIN",
    "SPOT",
    "SUPERMERCADO",
    "TELEVISION",
    "TV",
}

LEARNED_BRAND_WEAK_TOKENS = SIGNIFICANT_WORD_STOPLIST | {
    "ABIERTO",
    "AHORA",
    "BUEN",
    "BUENA",
    "BUENOS",
    "CASA",
    "COMERCIAL",
    "COMERCIALIZADORA",
    "CORDOBES",
    "CORDOBESA",
    "DESCUENTO",
    "DIA",
    "ENCUENTRO",
    "ENVIOS",
    "ENVASADO",
    "ESPACIO",
    "ESPECIAL",
    "FAMILIA",
    "FINAL",
    "GAS",
    "GRAN",
    "GRUPO",
    "HOGAR",
    "LINEA",
    "MARTES",
    "MAYO",
    "MEGA",
    "MES",
    "NUEVA",
    "NUEVO",
    "OFERTA",
    "OFERTAS",
    "ONLINE",
    "PLAN",
    "PLAZA",
    "PLUS",
    "PRECIOS",
    "PRIMAVERA",
    "SABADO",
    "SEMANAL",
    "TEMPORADA",
    "TODO",
    "VAMOS",
    "VENTA",
    "VIERNES",
}


def _significant_tokens(value: object) -> set[str]:
    text = _normalize_match_text(value)
    cleaned = "".join(character if character.isalnum() else " " for character in text)
    return {
        token
        for token in cleaned.split()
        if len(token) >= 4
        and not token.isdigit()
        and token not in LEARNED_BRAND_WEAK_TOKENS
    }


def _learnable_product_tokens(value: object) -> list[str]:
    text = _normalize_match_text(value)
    cleaned = "".join(character if character.isalnum() else " " for character in text)
    tokens = []
    for token in cleaned.split():
        if len(token) < 3 or token.isdigit() or token in SIGNIFICANT_WORD_STOPLIST:
            continue
        tokens.append(token)
    return tokens


def _learned_product_signatures(value: object) -> set[str]:
    tokens = _learnable_product_tokens(value)
    signatures = set(tokens)
    signatures.update(
        f"{left} {right}"
        for left, right in zip(tokens, tokens[1:])
    )
    signatures.update(
        f"{first} {second} {third}"
        for first, second, third in zip(tokens, tokens[1:], tokens[2:])
    )
    return signatures


def _is_specific_learned_signature(signature: str, distinct_products: int) -> bool:
    tokens = signature.split()
    if not tokens:
        return False

    weak_count = sum(token in LEARNED_BRAND_WEAK_TOKENS for token in tokens)
    strong_tokens = [token for token in tokens if token not in LEARNED_BRAND_WEAK_TOKENS]
    if not strong_tokens:
        return False

    if len(tokens) == 1:
        token = tokens[0]
        if token in LEARNED_BRAND_WEAK_TOKENS:
            return False
        if len(token) <= 3:
            return distinct_products >= 4
        return distinct_products >= 2

    if weak_count:
        return len(strong_tokens) >= 1 and distinct_products >= 2

    return any(len(token) >= 5 for token in tokens)


def _has_prom_canal_category(anunciante: BaseAnunciante) -> bool:
    category = _normalize_match_text(anunciante.categoria)
    return "PROM" in category and "CANAL" in category


def _find_channel_anunciantes(
    anunciantes: list[BaseAnunciante],
) -> dict[str, BaseAnunciante]:
    matches: dict[str, list[BaseAnunciante]] = {
        "telefe": [],
        "artear": [],
        "canal10": [],
        "canal12": [],
    }

    for anunciante in anunciantes:
        name = _normalize_match_text(anunciante.anunciante)
        if "TELEFE" in name:
            matches["telefe"].append(anunciante)
        if "ARTEAR" in name:
            matches["artear"].append(anunciante)
        if "CANAL" in name and "10" in name:
            matches["canal10"].append(anunciante)
        if ("CANAL" in name and "12" in name) or "DOCE" in name:
            matches["canal12"].append(anunciante)

    selected = {}
    for key, rows in matches.items():
        if rows:
            selected[key] = sorted(
                rows,
                key=lambda row: (
                    not _has_prom_canal_category(row),
                    _channel_anunciante_rank(key, row),
                    _normalize_match_text(row.anunciante),
                    row.id_anunciante,
                ),
            )[0]

    return selected


def _channel_anunciante_rank(key: str, anunciante: BaseAnunciante) -> int:
    name = _normalize_match_text(anunciante.anunciante)
    if key == "telefe" and "CORDOBA" in name:
        return 0
    if key == "artear" and "PROMOCION CANAL" in name:
        return 0
    if key == "canal10" and "CANAL 10 CORDOBA" in name:
        return 0
    if key == "canal12" and ("CANAL 12" in name or "DOCE" in name):
        return 0
    return 1


def _is_generic_channel_product(producto: object) -> bool:
    product_text = _normalize_match_text(producto)
    generic_patterns = (
        "IDENTIFICACION DEL CANAL",
        "PROMOCION CANAL",
        "PROMO CANAL",
    )
    return any(pattern in product_text for pattern in generic_patterns)


def _has_closed_channel_rule(producto: object) -> bool:
    product_text = _normalize_match_text(producto)
    return any(
        phrase in product_text
        for phrase in (
            "TELEFE PROMOCION CANAL",
            "CANAL 10 CORDOBA",
            "ARTEAR PROMOCION CANAL",
        )
    )


def _resolve_channel_promo_key(producto: object, canal: object) -> str | None:
    product_text = _normalize_match_text(producto)

    if "TELEFE" in product_text:
        return "telefe"
    if "ARTEAR" in product_text:
        return "artear"
    if "CANAL 10" in product_text:
        return "canal10"
    if "CANAL 12" in product_text or "EL DOCE" in product_text:
        return "canal12"
    if "CANAL 8" in product_text:
        return "telefe"

    if not _is_generic_channel_product(producto):
        return None

    channel_text = _normalize_match_text(canal)
    if "CANAL 10" in channel_text:
        return "canal10"
    if "CANAL 12" in channel_text or "DOCE" in channel_text:
        return "canal12"
    if "CANAL 8" in channel_text or "TELEFE" in channel_text:
        return "telefe"

    return None


def _score_word_match(
    producto: object,
    anunciante: BaseAnunciante,
    advertiser_token_counts: dict[str, int],
    rare_token_threshold: int,
) -> tuple[int, set[str]]:
    product_tokens = _significant_tokens(producto)
    advertiser_tokens = _significant_tokens(anunciante.anunciante)
    overlap = {
        token
        for token in product_tokens & advertiser_tokens
        if advertiser_token_counts.get(token, 0) <= rare_token_threshold
    }
    if not overlap:
        return 0, set()

    advertiser_order = [
        token
        for token in _normalize_match_text(anunciante.anunciante).split()
        if token in advertiser_tokens
    ]
    first_token = advertiser_order[0] if advertiser_order else None
    if first_token not in overlap:
        return 0, set()

    longest_token = max(len(token) for token in overlap)
    score = (len(overlap) * 100) + longest_token
    if first_token in overlap:
        score += 50
    return score, overlap


def _build_advertiser_token_counts(
    anunciantes: list[BaseAnunciante],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for anunciante in anunciantes:
        for token in _significant_tokens(anunciante.anunciante):
            counts[token] += 1
    return counts


def _get_learned_product_matches(
    db: Session,
) -> dict[str, list[dict[str, object]]]:
    rows = (
        db.query(
            MaestroProducto.producto,
            MaestroProducto.id_anunciante,
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
        )
        .outerjoin(
            BaseAnunciante,
            MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante,
        )
        .filter(MaestroProducto.producto.isnot(None))
        .filter(MaestroProducto.producto != "")
        .filter(MaestroProducto.id_anunciante.isnot(None))
        .all()
    )

    learned: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = _normalize_match_text(row.producto)
        if not key:
            continue
        learned[key].append(
            {
                "id_anunciante": row.id_anunciante,
                "anunciante": row.anunciante,
                "alcance": row.alcance,
                "categoria": row.categoria,
            }
        )
    return learned


def _get_learned_brand_matches(
    db: Session,
) -> dict[str, list[dict[str, object]]]:
    rows = (
        db.query(
            MaestroProducto.producto,
            MaestroProducto.id_anunciante,
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
        )
        .outerjoin(
            BaseAnunciante,
            MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante,
        )
        .filter(MaestroProducto.producto.isnot(None))
        .filter(MaestroProducto.producto != "")
        .filter(MaestroProducto.id_anunciante.isnot(None))
        .filter(BaseAnunciante.anunciante.isnot(None))
        .all()
    )

    learned: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        for signature in _learned_product_signatures(row.producto):
            learned[signature].append(
                {
                    "producto": row.producto,
                    "id_anunciante": row.id_anunciante,
                    "anunciante": row.anunciante,
                    "alcance": row.alcance,
                    "categoria": row.categoria,
                }
            )
    return learned


def _resolve_learned_product_match(
    producto: object,
    learned_product_matches: dict[str, list[dict[str, object]]],
) -> dict[str, object] | None:
    rows = learned_product_matches.get(_normalize_match_text(producto), [])
    if not rows:
        return None

    target_ids = {row["id_anunciante"] for row in rows}
    if len(target_ids) != 1:
        return None

    return rows[0]


def _resolve_consistent_learned_signature(
    signature: str,
    rows: list[dict[str, object]],
) -> dict[str, object] | None:
    if not rows:
        return None

    distinct_products = {
        _normalize_match_text(row.get("producto"))
        for row in rows
        if row.get("producto")
    }
    if not _is_specific_learned_signature(signature, len(distinct_products)):
        return None

    target_names = {
        _normalize_match_text(row["anunciante"])
        for row in rows
        if row.get("anunciante")
    }
    if len(target_names) != 1:
        return None

    counts_by_id: dict[int, int] = defaultdict(int)
    rows_by_id: dict[int, dict[str, object]] = {}
    for row in rows:
        id_anunciante = int(row["id_anunciante"])
        counts_by_id[id_anunciante] += 1
        rows_by_id[id_anunciante] = row

    selected_id = sorted(
        counts_by_id,
        key=lambda id_anunciante: (counts_by_id[id_anunciante], -id_anunciante),
        reverse=True,
    )[0]
    selected = rows_by_id[selected_id]
    return {
        "id_anunciante": selected["id_anunciante"],
        "anunciante": selected["anunciante"],
        "alcance": selected["alcance"],
        "categoria": selected["categoria"],
        "evidencia": len(distinct_products),
    }


def _resolve_learned_brand_match(
    producto: object,
    learned_brand_matches: dict[str, list[dict[str, object]]],
) -> tuple[dict[str, object] | None, str | None]:
    scored = []
    for signature in _learned_product_signatures(producto):
        learned = _resolve_consistent_learned_signature(
            signature,
            learned_brand_matches.get(signature, []),
        )
        if learned is None:
            continue

        score = (len(signature.split()) * 1000) + int(learned["evidencia"])
        scored.append((score, signature, learned))

    if not scored:
        return None, None

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top_score, top_signature, top_learned = scored[0]
    tied = [
        learned
        for score, _signature, learned in scored
        if score == top_score
        and int(learned["id_anunciante"]) != int(top_learned["id_anunciante"])
    ]
    if tied:
        return None, "Ambiguo"

    return top_learned, top_signature


def _resolve_auto_anunciante(
    row,
    *,
    anunciantes: list[BaseAnunciante],
    channel_anunciantes: dict[str, BaseAnunciante],
    learned_product_matches: dict[str, list[dict[str, object]]],
    learned_brand_matches: dict[str, list[dict[str, object]]],
    advertiser_token_counts: dict[str, int],
    rare_token_threshold: int,
) -> tuple[BaseAnunciante | None, str | None, str | None]:
    channel_key = _resolve_channel_promo_key(row.producto, row.canal)
    if channel_key is not None and channel_key in channel_anunciantes:
        return channel_anunciantes[channel_key], "Regla canal", channel_key
    if channel_key is not None and _has_closed_channel_rule(row.producto):
        return None, "Regla canal sin anunciante", channel_key

    learned = _resolve_learned_product_match(row.producto, learned_product_matches)
    if learned is not None:
        return (
            BaseAnunciante(
                id_anunciante=int(learned["id_anunciante"]),
                anunciante=learned["anunciante"],
                alcance=learned["alcance"],
                categoria=learned["categoria"],
            ),
            "Producto aprendido",
            "nombre exacto",
        )

    learned_brand, learned_signature = _resolve_learned_brand_match(
        row.producto,
        learned_brand_matches,
    )
    if learned_brand is not None:
        return (
            BaseAnunciante(
                id_anunciante=int(learned_brand["id_anunciante"]),
                anunciante=learned_brand["anunciante"],
                alcance=learned_brand["alcance"],
                categoria=learned_brand["categoria"],
            ),
            "Marca aprendida",
            str(learned_signature),
        )
    if learned_signature == "Ambiguo":
        return None, "Ambiguo", "marca aprendida"

    scored = []
    for anunciante in anunciantes:
        score, tokens = _score_word_match(
            row.producto,
            anunciante,
            advertiser_token_counts,
            rare_token_threshold,
        )
        if score:
            scored.append((score, tokens, anunciante))

    if not scored:
        return None, None, None

    scored.sort(
        key=lambda item: (
            item[0],
            _has_prom_canal_category(item[2]),
            -item[2].id_anunciante,
        ),
        reverse=True,
    )
    top_score, top_tokens, top_anunciante = scored[0]
    tied = [
        anunciante
        for score, _tokens, anunciante in scored
        if score == top_score and anunciante.id_anunciante != top_anunciante.id_anunciante
    ]
    if tied:
        return None, "Ambiguo", ", ".join(sorted(top_tokens))

    if top_score:
        return top_anunciante, "Palabra clave", ", ".join(sorted(top_tokens))

    return None, None, None


def _get_auto_assignment_candidates(db: Session):
    duracion_expr = func.sum(Cronograma.duracion).label("segundos")
    filas_expr = func.count(Cronograma.id).label("filas")
    query = (
        db.query(
            Cronograma.cod_prod,
            Cronograma.producto,
            Cronograma.tema,
            Cronograma.canal,
            filas_expr,
            duracion_expr,
        )
        .select_from(Cronograma)
    )
    query = _join_resolved_maestro(query, db)
    return (
        query
        .filter(or_(MaestroProducto.id.is_(None), MaestroProducto.id_anunciante.is_(None)))
        .filter(Cronograma.producto.isnot(None))
        .filter(func.trim(Cronograma.producto) != "")
        .filter(Cronograma.cod_prod.isnot(None))
        .filter(Cronograma.cod_prod != "")
        .filter(Cronograma.cod_prod.op("REGEXP")("^([0-9]+|C[0-9]+)$"))
        .group_by(Cronograma.cod_prod, Cronograma.producto, Cronograma.tema, Cronograma.canal)
        .order_by(duracion_expr.desc())
        .all()
    )


def _get_pending_product_codes_by_name(
    db: Session,
    producto: object,
) -> list[dict[str, object]]:
    product_name = _normalize_match_text(producto)
    if not product_name:
        return []

    rows = (
        db.query(Cronograma.cod_prod, Cronograma.producto, Cronograma.tema)
        .select_from(Cronograma)
        .filter(Cronograma.cod_prod.isnot(None))
        .filter(Cronograma.cod_prod != "")
        .filter(Cronograma.cod_prod.op("REGEXP")("^([0-9]+|C[0-9]+)$"))
        .group_by(Cronograma.cod_prod, Cronograma.producto, Cronograma.tema)
        .all()
    )

    pending_variants = {}
    for row in rows:
        if _normalize_match_text(row.producto) != product_name:
            continue
        cod_producto = str(row.cod_prod)
        key = (cod_producto, _normalize_match_text(row.tema))
        pending_variants[key] = {
            "cod_producto": cod_producto,
            "producto": row.producto,
            "tema": row.tema,
        }

    return list(pending_variants.values())


def _get_or_create_maestro_producto(
    db: Session,
    *,
    producto: str,
    id_anunciante: int,
) -> tuple[MaestroProducto, bool]:
    maestro = (
        db.query(MaestroProducto)
        .filter(
            _match_text_expr(MaestroProducto.producto)
            == producto.strip().lower()
        )
        .first()
    )
    if maestro is None:
        maestro = MaestroProducto(
            producto=producto.strip(),
            id_anunciante=id_anunciante,
        )
        db.add(maestro)
        db.flush()
        return maestro, True

    maestro.id_anunciante = id_anunciante
    return maestro, False


def _get_or_create_producto_tema(
    db: Session,
    *,
    id_producto: int,
    tema: str | None,
    cod_producto: str | None,
) -> tuple[ProductoTema, bool]:
    normalized_tema = _normalize_match_text(tema)
    query = db.query(ProductoTema).filter(
        ProductoTema.id_producto == id_producto,
        ProductoTema.cod_producto == cod_producto,
    )
    if normalized_tema:
        query = query.filter(
            _match_text_expr(ProductoTema.tema) == str(tema).strip().lower()
        )
    else:
        query = query.filter(ProductoTema.tema.is_(None))

    producto_tema = query.first()
    if producto_tema is not None:
        return producto_tema, False

    producto_tema = ProductoTema(
        id_producto=id_producto,
        cod_producto=cod_producto,
        tema=tema,
    )
    db.add(producto_tema)
    return producto_tema, True


def _build_auto_assignment_suggestions(
    candidates,
    *,
    anunciantes: list[BaseAnunciante],
    channel_anunciantes: dict[str, BaseAnunciante],
    learned_product_matches: dict[str, list[dict[str, object]]],
    learned_brand_matches: dict[str, list[dict[str, object]]],
) -> tuple[list[dict[str, object]], int, int, int]:
    by_code: dict[str, list[dict[str, object]]] = defaultdict(list)
    missing_advertiser = 0
    unresolved = 0
    advertiser_token_counts = _build_advertiser_token_counts(anunciantes)
    rare_token_threshold = max(1, min(3, math.ceil(len(anunciantes) * 0.01)))

    for row in candidates:
        anunciante, rule, detail = _resolve_auto_anunciante(
            row,
            anunciantes=anunciantes,
            channel_anunciantes=channel_anunciantes,
            learned_product_matches=learned_product_matches,
            learned_brand_matches=learned_brand_matches,
            advertiser_token_counts=advertiser_token_counts,
            rare_token_threshold=rare_token_threshold,
        )
        if anunciante is None:
            if rule == "Ambiguo":
                by_code[str(row.cod_prod)].append(
                    {
                        "status": "ambiguous",
                        "producto": row.producto,
                        "tema": row.tema,
                        "canal": row.canal,
                        "filas": int(row.filas or 0),
                        "segundos": int(row.segundos or 0),
                        "detail": detail,
                    }
                )
            elif _resolve_channel_promo_key(row.producto, row.canal) is not None:
                missing_advertiser += 1
            else:
                unresolved += 1
            continue

        by_code[str(row.cod_prod)].append(
            {
                "status": "resolved",
                "producto": row.producto,
                "tema": row.tema,
                "canal": row.canal,
                "filas": int(row.filas or 0),
                "segundos": int(row.segundos or 0),
                "id_anunciante": anunciante.id_anunciante,
                "anunciante": anunciante.anunciante,
                "alcance": anunciante.alcance,
                "categoria": anunciante.categoria,
                "rule": rule,
                "detail": detail,
            }
        )

    suggestions = []
    ambiguous = 0
    for cod_producto, rows in by_code.items():
        if any(row["status"] == "ambiguous" for row in rows):
            ambiguous += 1
            continue

        resolved_rows = [row for row in rows if row["status"] == "resolved"]
        target_ids = {row["id_anunciante"] for row in resolved_rows}
        if not resolved_rows or len(target_ids) != 1:
            ambiguous += 1
            continue

        selected = sorted(
            resolved_rows,
            key=lambda row: int(row["segundos"] or 0),
            reverse=True,
        )[0]
        suggestions.append(
            {
                "cod_prod": cod_producto,
                "producto": selected["producto"],
                "tema": selected["tema"],
                "canal": selected["canal"],
                "filas": sum(int(row["filas"] or 0) for row in resolved_rows),
                "segundos": sum(int(row["segundos"] or 0) for row in resolved_rows),
                "id_anunciante": selected["id_anunciante"],
                "anunciante": selected["anunciante"],
                "alcance": selected["alcance"],
                "categoria": selected["categoria"],
                "rule": selected["rule"],
                "detail": selected["detail"],
            }
        )

    suggestions.sort(key=lambda row: int(row["segundos"] or 0), reverse=True)
    return suggestions, ambiguous, missing_advertiser, unresolved


def _get_auto_assignment_suggestion_rows(db: Session) -> list[dict[str, object]]:
    suggestions, _ambiguous, _missing, _unresolved = _get_auto_assignment_suggestions(db)
    return suggestions[:100]


def _get_auto_assignment_suggestions(
    db: Session,
) -> tuple[list[dict[str, object]], int, int, int]:
    anunciantes = db.query(BaseAnunciante).all()
    channel_anunciantes = _find_channel_anunciantes(anunciantes)
    return _build_auto_assignment_suggestions(
        _get_auto_assignment_candidates(db),
        anunciantes=anunciantes,
        channel_anunciantes=channel_anunciantes,
        learned_product_matches=_get_learned_product_matches(db),
        learned_brand_matches=_get_learned_brand_matches(db),
    )


def _apply_auto_assignment_suggestions(
    db: Session,
    suggestions: list[dict[str, object]],
    *,
    allowed_rules: set[str] | None = None,
    selected_ids_by_code: dict[str, int | None] | None = None,
) -> dict[str, object]:
    created = 0
    already_exists = 0
    skipped_by_rule = 0
    created_codes = []
    processed_product_names: set[str] = set()

    for suggestion in suggestions:
        rule = str(suggestion.get("rule") or "")
        if allowed_rules is not None and rule not in allowed_rules:
            skipped_by_rule += 1
            continue

        cod_producto = str(suggestion["cod_prod"])
        if selected_ids_by_code is None:
            id_anunciante = _parse_optional_int(suggestion.get("id_anunciante"))
        else:
            id_anunciante = selected_ids_by_code.get(cod_producto)
        if id_anunciante is None:
            continue

        product_name = _normalize_match_text(suggestion["producto"])
        if product_name in processed_product_names:
            already_exists += 1
            continue
        processed_product_names.add(product_name)

        pending_products = _get_pending_product_codes_by_name(db, suggestion["producto"])
        if not pending_products:
            already_exists += 1
            continue

        producto = str(suggestion["producto"] or "").strip()
        if not producto:
            continue
        maestro, maestro_created = _get_or_create_maestro_producto(
            db,
            producto=producto,
            id_anunciante=id_anunciante,
        )
        product_changed = maestro_created
        for pending in pending_products:
            pending_code = str(pending["cod_producto"])
            _producto_tema, tema_created = _get_or_create_producto_tema(
                db,
                id_producto=maestro.id,
                cod_producto=pending_code,
                tema=str(pending["tema"] or "").strip() or None,
            )
            if tema_created:
                product_changed = True
                if pending_code not in created_codes:
                    created_codes.append(pending_code)

        if product_changed:
            created += 1
        else:
            already_exists += 1

    db.commit()
    return {
        "created": created,
        "already_exists": already_exists,
        "skipped_by_rule": skipped_by_rule,
        "created_codes": created_codes,
    }


def _get_control_rows_by_product_codes(
    db: Session,
    cod_productos: list[int],
) -> list[dict[str, object]]:
    if not cod_productos:
        return []

    duracion_expr = func.sum(Cronograma.duracion).label("segundos")
    filas_expr = func.count(Cronograma.id).label("filas")
    ultima_fecha_expr = func.max(Cronograma.fecha).label("ultima_fecha")
    temas_registrados = (
        db.query(
            ProductoTema.id_producto.label("id_producto"),
            _match_text_expr(ProductoTema.tema).label("tema_normalizado"),
            func.min(ProductoTema.tema).label("tema"),
        )
        .filter(ProductoTema.tema.isnot(None))
        .filter(func.trim(ProductoTema.tema) != "")
        .group_by(
            ProductoTema.id_producto,
            _match_text_expr(ProductoTema.tema),
        )
        .subquery()
    )

    query = (
        db.query(
            Cronograma.cod_prod,
            Cronograma.producto.label("producto_cronograma"),
            Cronograma.tema.label("tema_cronograma"),
            MaestroProducto.producto.label("producto_base"),
            temas_registrados.c.tema.label("tema_base"),
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
            filas_expr,
            duracion_expr,
            ultima_fecha_expr,
        )
        .select_from(Cronograma)
    )
    query = _join_resolved_maestro(query, db)
    rows = (
        query
        .filter(MaestroProducto.id.isnot(None))
        .outerjoin(
            temas_registrados,
            (temas_registrados.c.id_producto == MaestroProducto.id)
            & (
                temas_registrados.c.tema_normalizado
                == _match_text_expr(Cronograma.tema)
            ),
        )
        .outerjoin(
            BaseAnunciante,
            MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante,
        )
        .filter(Cronograma.cod_prod.in_([str(code) for code in cod_productos]))
        .group_by(
            Cronograma.cod_prod,
            Cronograma.producto,
            Cronograma.tema,
            MaestroProducto.producto,
            temas_registrados.c.tema,
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
        )
        .order_by(duracion_expr.desc())
        .all()
    )

    return [
        {
            "cod_prod": row.cod_prod,
            "producto_cronograma": row.producto_cronograma,
            "tema_cronograma": row.tema_cronograma,
            "producto_base": row.producto_base,
            "tema_base": row.tema_base,
            "anunciante": row.anunciante,
            "alcance": row.alcance,
            "categoria": row.categoria,
            "filas": int(row.filas or 0),
            "segundos": int(row.segundos or 0),
            "ultima_fecha": row.ultima_fecha,
        }
        for row in rows
    ]


@router.get("/cronogramas", response_class=HTMLResponse)
def cronogramas_view(
    request: Request,
    db: Session = Depends(get_db),
    fecha: str | None = Query(default=None),
    canal: str | None = Query(default=None),
    q: str | None = Query(default=None),
):
    query = db.query(Cronograma)

    if fecha:
        query = query.filter(Cronograma.fecha == fecha)
    if canal:
        query = query.filter(Cronograma.canal == canal)
    if q:
        search = f"%{q.strip()}%"
        query = query.filter(
            or_(Cronograma.producto.ilike(search), Cronograma.tema.ilike(search))
        )

    cronogramas = (
        query.order_by(Cronograma.fecha.desc(), Cronograma.id.desc()).limit(300).all()
    )
    canales = [
        row[0]
        for row in db.query(Cronograma.canal)
        .filter(Cronograma.canal.isnot(None))
        .distinct()
        .order_by(Cronograma.canal.asc())
        .all()
    ]

    return templates.TemplateResponse(
        request,
        "cronogramas.html",
        {
            "cronogramas": cronogramas,
            "filters": {"fecha": fecha or "", "canal": canal or "", "q": q or ""},
            "canales": canales,
        },
    )


@router.get("/productos", response_class=HTMLResponse)
def productos_view(
    request: Request,
    db: Session = Depends(get_db),
    q: str | None = Query(default=None),
    id_anunciante: str | None = Query(default=None),
    categoria: str | None = Query(default=None),
):
    selected_id_anunciante = _parse_optional_int(id_anunciante)
    query = (
        db.query(
            MaestroProducto.id.label("id_producto"),
            MaestroProducto.producto,
            MaestroProducto.id_anunciante,
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
        )
        .select_from(MaestroProducto)
        .outerjoin(
            BaseAnunciante,
            MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante,
        )
    )

    if q:
        search = f"%{q.strip()}%"
        query = query.filter(MaestroProducto.producto.ilike(search))
    if selected_id_anunciante is not None:
        query = query.filter(MaestroProducto.id_anunciante == selected_id_anunciante)
    if categoria:
        query = query.filter(BaseAnunciante.categoria == categoria)

    productos = (
        query.order_by(MaestroProducto.producto.asc(), MaestroProducto.id.asc())
        .limit(500)
        .all()
    )
    anunciantes = (
        db.query(BaseAnunciante)
        .order_by(BaseAnunciante.anunciante.asc(), BaseAnunciante.id_anunciante.asc())
        .all()
    )
    categorias = [
        row[0]
        for row in db.query(BaseAnunciante.categoria)
        .filter(BaseAnunciante.categoria.isnot(None))
        .distinct()
        .order_by(BaseAnunciante.categoria.asc())
        .all()
    ]

    return templates.TemplateResponse(
        request,
        "productos.html",
        {
            "productos": productos,
            "anunciantes": anunciantes,
            "categorias": categorias,
            "filters": {
                "q": q or "",
                "id_anunciante": selected_id_anunciante or "",
                "categoria": categoria or "",
            },
            "status": request.query_params.get("status", ""),
            "message": request.query_params.get("message", ""),
        },
    )


@router.post("/productos/actualizar", response_class=HTMLResponse)
async def update_producto(request: Request, db: Session = Depends(get_db)):
    form = await _read_urlencoded_form(request)
    producto_id = _parse_optional_int(form.get("id_producto"))
    id_anunciante = _parse_optional_int(form.get("id_anunciante"))
    nombre_producto = str(form.get("producto") or "").strip()

    if producto_id is None or not nombre_producto:
        return RedirectResponse(
            url=_productos_redirect_url(status="error", message="Producto inválido."),
            status_code=303,
        )

    producto = (
        db.query(MaestroProducto)
        .filter(MaestroProducto.id == producto_id)
        .first()
    )
    if producto is None:
        return RedirectResponse(
            url=_productos_redirect_url(status="error", message="Producto no encontrado."),
            status_code=303,
        )

    producto.producto = nombre_producto
    producto.id_anunciante = id_anunciante

    db.commit()

    return RedirectResponse(
        url=_productos_redirect_url(status="ok", message="Producto actualizado."),
        status_code=303,
    )


@router.post("/anunciantes/actualizar", response_class=HTMLResponse)
async def update_anunciante(request: Request, db: Session = Depends(get_db)):
    form = await _read_urlencoded_form(request)
    id_anunciante = _parse_optional_int(form.get("id_anunciante"))

    if id_anunciante is None:
        return RedirectResponse(
            url=_anunciantes_redirect_url(status="error", message="Anunciante inválido."),
            status_code=303,
        )

    anunciante = (
        db.query(BaseAnunciante)
        .filter(BaseAnunciante.id_anunciante == id_anunciante)
        .first()
    )
    if anunciante is None:
        return RedirectResponse(
            url=_anunciantes_redirect_url(status="error", message="Anunciante no encontrado."),
            status_code=303,
        )

    anunciante.anunciante = str(form.get("anunciante") or "").strip() or None
    anunciante.alcance = str(form.get("alcance") or "").strip() or None
    anunciante.categoria = str(form.get("categoria") or "").strip() or None
    db.commit()

    return RedirectResponse(
        url=_anunciantes_redirect_url(status="ok", message="Anunciante actualizado."),
        status_code=303,
    )


def _productos_redirect_url(*, status: str, message: str) -> str:
    return f"/productos?status={quote(status)}&message={quote(message)}"


def _anunciantes_redirect_url(*, status: str, message: str) -> str:
    return f"/anunciantes?status={quote(status)}&message={quote(message)}"


@router.get("/anunciantes", response_class=HTMLResponse)
def anunciantes_view(
    request: Request,
    db: Session = Depends(get_db),
    q: str | None = Query(default=None),
    categoria: str | None = Query(default=None),
):
    query = db.query(BaseAnunciante)
    if q:
        query = query.filter(BaseAnunciante.anunciante.ilike(f"%{q.strip()}%"))
    if categoria:
        query = query.filter(BaseAnunciante.categoria == categoria)

    anunciantes = (
        query.order_by(BaseAnunciante.anunciante.asc(), BaseAnunciante.id_anunciante.asc())
        .limit(500)
        .all()
    )
    categorias = _get_distinct_base_anunciante_values(db, BaseAnunciante.categoria)
    return templates.TemplateResponse(
        request,
        "anunciantes.html",
        {
            "anunciantes": anunciantes,
            "alcances": _get_distinct_base_anunciante_values(db, BaseAnunciante.alcance),
            "categorias": categorias,
            "filters": {"q": q or "", "categoria": categoria or ""},
            "status": request.query_params.get("status", ""),
            "message": request.query_params.get("message", ""),
        },
    )


def _require_admin(request: Request) -> None:
    role = str((request.session.get("user") or {}).get("role") or "").upper()
    if role != "ADMIN":
        raise HTTPException(status_code=403, detail="Se requiere el rol ADMIN.")


def _usuarios_redirect(*, guardado: str | None = None, error: str | None = None) -> str:
    if error:
        return f"/configuracion/usuarios?error={quote(error)}"
    return f"/configuracion/usuarios?guardado={quote(guardado or '')}"


@router.get("/configuracion/usuarios", response_class=HTMLResponse)
def usuarios_view(
    request: Request,
    editar_usuario: int | None = Query(default=None),
    consultar_ad: bool = Query(default=False),
    guardado: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _require_admin(request)
    usuarios = db.query(Usuario).order_by(Usuario.apellido, Usuario.nombre).all()
    usuario_edicion = db.get(Usuario, editar_usuario) if editar_usuario else None
    usuarios_ad = None
    error_ad = None
    if consultar_ad:
        try:
            usuarios_ad = list_ad_group_users()
        except ActiveDirectoryAuthError as exc:
            usuarios_ad, error_ad = [], str(exc)
    return templates.TemplateResponse(request, "usuarios.html", {
        "usuarios": usuarios,
        "usuario_edicion": usuario_edicion,
        "usuarios_ad": usuarios_ad,
        "error_ad": error_ad,
        "guardado": guardado,
        "error": error,
        "roles": USER_ROLES,
        "perfiles": USER_PROFILES,
    })


@router.post("/configuracion/usuarios")
async def crear_usuario(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    form = await _read_urlencoded_form(request)
    username = normalize_username(form.get("username", ""))
    password = form.get("password", "")
    origen = form.get("origen", "LOCAL").strip().upper()
    rol = form.get("rol", "").strip().upper()
    perfil = form.get("perfil", "").strip().upper()
    if not all((form.get("nombre", "").strip(), form.get("apellido", "").strip(), username)):
        return RedirectResponse(_usuarios_redirect(error="Completá los campos obligatorios."), 303)
    if db.query(Usuario).filter(func.lower(Usuario.username) == username).first():
        return RedirectResponse(_usuarios_redirect(error="El usuario ya existe."), 303)
    if origen == "LOCAL" and len(password) < 8:
        return RedirectResponse(_usuarios_redirect(error="La contraseña debe tener al menos 8 caracteres."), 303)
    if rol not in USER_ROLES or perfil not in USER_PROFILES:
        return RedirectResponse(_usuarios_redirect(error="El rol o perfil seleccionado no es válido."), 303)
    usuario = Usuario(
        nombre=form.get("nombre", "").strip(),
        apellido=form.get("apellido", "").strip(),
        correo=form.get("correo", "").strip() or None,
        username=username,
        hashed_password=hash_password(password or secrets.token_urlsafe(32)),
        origen=origen,
        status=form.get("status") == "on",
        rol=rol,
        perfil=perfil,
    )
    db.add(usuario)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(_usuarios_redirect(error="No se pudo crear: hay datos duplicados."), 303)
    return RedirectResponse(_usuarios_redirect(guardado="Usuario creado."), 303)


@router.post("/configuracion/usuarios/{usuario_id}")
async def actualizar_usuario(usuario_id: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="El usuario no existe.")
    form = await _read_urlencoded_form(request)
    username = normalize_username(form.get("username", ""))
    rol = form.get("rol", "").strip().upper()
    perfil = form.get("perfil", "").strip().upper()
    if rol not in USER_ROLES or perfil not in USER_PROFILES:
        return RedirectResponse(_usuarios_redirect(error="El rol o perfil seleccionado no es válido."), 303)
    duplicate = db.query(Usuario).filter(
        func.lower(Usuario.username) == username,
        Usuario.id != usuario_id,
    ).first()
    if duplicate:
        return RedirectResponse(_usuarios_redirect(error="El nombre de usuario ya existe."), 303)
    usuario.nombre = form.get("nombre", "").strip()
    usuario.apellido = form.get("apellido", "").strip()
    usuario.correo = form.get("correo", "").strip() or None
    usuario.username = username
    usuario.origen = form.get("origen", "LOCAL").strip().upper()
    usuario.status = form.get("status") == "on"
    usuario.rol = rol
    usuario.perfil = perfil
    password = form.get("password", "")
    if password:
        if len(password) < 8:
            return RedirectResponse(_usuarios_redirect(error="La contraseña debe tener al menos 8 caracteres."), 303)
        usuario.hashed_password = hash_password(password)
    db.commit()
    return RedirectResponse(_usuarios_redirect(guardado="Usuario actualizado."), 303)


@router.get("/configuracion/usuarios/sincronizar", response_class=HTMLResponse)
def usuarios_ad_sync_view(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    try:
        ad_users = list_ad_group_users()
        error = None
    except ActiveDirectoryAuthError as exc:
        ad_users, error = [], str(exc)
    usuarios = db.query(Usuario).all()
    local = {normalize_username(user.username): user for user in usuarios}
    ad = {normalize_username(user.username): user for user in ad_users}
    return templates.TemplateResponse(request, "usuarios_ad_sync.html", {
        "new_users": [user for key, user in ad.items() if key not in local],
        "existing_users": [local[key] for key in ad if key in local and local[key].status],
        "reactivable_users": [local[key] for key in ad if key in local and not local[key].status],
        "inactive_users": [user for key, user in local.items() if user.origen.upper() == "AD" and user.status and key not in ad],
        "error": error,
    })


@router.post("/configuracion/usuarios/sincronizar/incorporar")
async def incorporar_usuario_ad(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    form = await _read_urlencoded_form(request)
    username = normalize_username(form.get("username", ""))
    if db.query(Usuario).filter(func.lower(Usuario.username) == username).first():
        return RedirectResponse("/configuracion/usuarios/sincronizar", 303)
    try:
        ad_user = next(
            (item for item in list_ad_group_users() if normalize_username(item.username) == username),
            None,
        )
    except ActiveDirectoryAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if ad_user is None:
        raise HTTPException(status_code=409, detail="El usuario ya no pertenece al grupo AD.")
    rol = form.get("rol", "COMERCIAL").strip().upper()
    perfil = form.get("perfil", "USUARIO").strip().upper()
    if rol not in USER_ROLES or perfil not in USER_PROFILES:
        raise HTTPException(status_code=400, detail="El rol o perfil seleccionado no es válido.")
    usuario = Usuario(
        nombre=form.get("nombre", "").strip() or ad_user.first_name,
        apellido=form.get("apellido", "").strip() or ad_user.last_name,
        correo=form.get("correo", "").strip() or ad_user.email,
        username=username,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        origen="AD",
        status=True,
        rol=rol,
        perfil=perfil,
    )
    db.add(usuario)
    db.commit()
    return RedirectResponse("/configuracion/usuarios/sincronizar", 303)


@router.post("/configuracion/usuarios/sincronizar/{usuario_id}/reactivar")
def reactivar_usuario_ad(usuario_id: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    usuario = db.get(Usuario, usuario_id)
    if usuario is None or usuario.origen.upper() != "AD":
        raise HTTPException(status_code=404, detail="El usuario AD no existe.")
    usuario.status = True
    db.commit()
    return RedirectResponse("/configuracion/usuarios/sincronizar", 303)


@router.get("/control-productos", response_class=HTMLResponse)
def control_productos_view(
    request: Request,
    db: Session = Depends(get_db),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    archivo_id: str | None = Query(default=None),
    auto_codes: str | None = Query(default=None),
    mostrar_sugerencias: bool = Query(default=False),
):
    page_started_at = perf_counter()

    def log_block(label: str, started_at: float) -> None:
        logger.warning(
            "CONTROL %-24s %8.1f ms",
            label,
            (perf_counter() - started_at) * 1000,
        )

    selected_archivo_id = _parse_optional_int(archivo_id)
    auto_cod_productos = _parse_int_list(auto_codes)

    block_started_at = perf_counter()
    anunciantes = (
        db.query(BaseAnunciante)
        .order_by(BaseAnunciante.anunciante.asc(), BaseAnunciante.id_anunciante.asc())
        .all()
    )
    log_block("anunciantes", block_started_at)

    block_started_at = perf_counter()
    archivos = (
        db.query(ArchivoIngesta)
        .order_by(ArchivoIngesta.fecha_inicio_proceso.desc(), ArchivoIngesta.id.desc())
        .limit(100)
        .all()
    )
    log_block("archivos", block_started_at)

    block_started_at = perf_counter()
    min_fecha = db.query(func.min(Cronograma.fecha)).scalar()
    max_fecha = db.query(func.max(Cronograma.fecha)).scalar()
    log_block("date_limits", block_started_at)

    block_started_at = perf_counter()
    alcances = _get_distinct_base_anunciante_values(db, BaseAnunciante.alcance)
    categorias = _get_distinct_base_anunciante_values(db, BaseAnunciante.categoria)
    log_block("distinct_options", block_started_at)

    block_started_at = perf_counter()
    unmatched_rows = _get_unmatched_product_rows(
        db,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        archivo_id=selected_archivo_id,
    )
    log_block("unmatched_rows", block_started_at)

    if mostrar_sugerencias:
        block_started_at = perf_counter()
        auto_suggestion_rows = _get_auto_assignment_suggestion_rows(db)
        log_block("auto_suggestion_rows", block_started_at)
    else:
        auto_suggestion_rows = []

    block_started_at = perf_counter()
    auto_assigned_rows = _get_control_rows_by_product_codes(db, auto_cod_productos)
    log_block("auto_assigned_rows", block_started_at)
    log_block("TOTAL", page_started_at)

    return templates.TemplateResponse(
        request,
        "control_productos.html",
        {
            "anunciantes": anunciantes,
            "archivos": archivos,
            "alcances": alcances,
            "categorias": categorias,
            "unmatched_rows": unmatched_rows,
            "auto_suggestion_rows": auto_suggestion_rows,
            "mostrar_sugerencias": mostrar_sugerencias,
            "auto_assigned_rows": auto_assigned_rows,
            "min_fecha": min_fecha,
            "max_fecha": max_fecha,
            "filters": {
                "fecha_desde": fecha_desde or "",
                "fecha_hasta": fecha_hasta or "",
                "archivo_id": selected_archivo_id or "",
            },
            "status": request.query_params.get("status", ""),
            "message": request.query_params.get("message", ""),
        },
    )


@router.post("/control-productos/asignar", response_class=HTMLResponse)
async def assign_control_producto(request: Request, db: Session = Depends(get_db)):
    form = await _read_urlencoded_form(request)
    cod_prod = str(form.get("cod_prod") or "").strip()
    producto = str(form.get("producto") or "").strip() or None
    tema = str(form.get("tema") or "").strip() or None
    id_anunciante = _parse_optional_int(form.get("id_anunciante"))

    if not producto:
        return RedirectResponse(
            url=_control_productos_redirect_url(
                status="error",
                message="El nombre del producto es obligatorio.",
            ),
            status_code=303,
        )

    if id_anunciante is None:
        return RedirectResponse(
            url=_control_productos_redirect_url(
                status="error",
                message="Seleccioná un anunciante para asignar el producto.",
            ),
            status_code=303,
        )

    maestro, maestro_created = _get_or_create_maestro_producto(
        db,
        producto=producto,
        id_anunciante=id_anunciante,
    )
    _producto_tema, tema_created = _get_or_create_producto_tema(
        db,
        id_producto=maestro.id,
        cod_producto=cod_prod or None,
        tema=tema,
    )

    if maestro_created:
        message = f"Producto {producto} agregado al maestro."
    elif tema_created:
        message = f"Tema agregado al producto {producto}."
    else:
        message = f"Producto {producto} actualizado."

    db.commit()
    return RedirectResponse(
        url=_control_productos_redirect_url(status="ok", message=message),
        status_code=303,
    )


@router.post("/anunciantes/crear", response_class=HTMLResponse)
async def create_control_anunciante(request: Request, db: Session = Depends(get_db)):
    form = await _read_urlencoded_form(request)
    return_to_control = form.get("return_to") == "control-productos"

    def redirect_url(*, status: str, message: str) -> str:
        if return_to_control:
            return _control_productos_redirect_url(status=status, message=message)
        return _anunciantes_redirect_url(status=status, message=message)

    anunciante = str(form.get("anunciante") or "").strip()
    alcance = str(form.get("alcance") or "").strip() or None
    categoria = str(form.get("categoria") or "").strip() or None

    if not anunciante:
        return RedirectResponse(
            url=redirect_url(
                status="error",
                message="El nombre del anunciante es obligatorio.",
            ),
            status_code=303,
        )

    db.add(
        BaseAnunciante(
            anunciante=anunciante,
            alcance=alcance,
            categoria=categoria,
        )
    )
    db.commit()
    return RedirectResponse(
        url=redirect_url(
            status="ok",
            message=f"Anunciante {anunciante} creado.",
        ),
        status_code=303,
    )


@router.post("/control-productos/asignar-promos-canal", response_class=HTMLResponse)
async def assign_channel_promos_and_word_matches(
    request: Request,
    db: Session = Depends(get_db),
):
    form = await _read_urlencoded_form(request)
    suggestions, ambiguous, missing_advertiser, unresolved = _get_auto_assignment_suggestions(
        db
    )
    selected_ids_by_code = {
        str(suggestion["cod_prod"]): _parse_optional_int(
            form.get(f"id_anunciante_{suggestion['cod_prod']}")
        )
        for suggestion in suggestions
    }
    assignment_result = _apply_auto_assignment_suggestions(
        db,
        suggestions,
        selected_ids_by_code=selected_ids_by_code,
    )

    message = (
        f"Asignados automáticamente: {assignment_result['created']}. "
        f"Ambiguos: {ambiguous}. "
        f"Sin anunciante configurado: {missing_advertiser}. "
        f"Sin regla: {unresolved}. "
        f"Ya existían: {assignment_result['already_exists']}."
    )
    redirect_url = _control_productos_redirect_url(status="ok", message=message)
    created_codes = assignment_result["created_codes"]
    if created_codes:
        code_list = ",".join(str(code) for code in created_codes[:80])
        redirect_url = f"{redirect_url}&auto_codes={quote(code_list)}"

    return RedirectResponse(
        url=redirect_url,
        status_code=303,
    )


def _control_productos_redirect_url(*, status: str, message: str) -> str:
    return f"/control-productos?status={quote(status)}&message={quote(message)}"


async def _read_urlencoded_form(request: Request) -> dict[str, str]:
    body = await request.body()
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


@router.get("/envio-monitor", response_class=HTMLResponse)
def envio_monitor_view(
    request: Request,
    db: Session = Depends(get_db),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    alcance: list[str] | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
    anunciante: list[str] | None = Query(default=None),
    tipo_dia: list[str] | None = Query(default=None),
    prom_canal: str | None = Query(default=None),
    ranking_alcance: str | None = Query(default=None),
    ranking_categoria: str | None = Query(default=None),
    filtro_comercial: bool = Query(default=False),
):
    page_started_at = perf_counter()

    def log_block(label: str, started_at: float) -> None:
        logger.warning(
            "ENVIO_MONITOR %-24s %8.1f ms",
            label,
            (perf_counter() - started_at) * 1000,
        )

    today = date.today()
    block_started_at = perf_counter()
    min_fecha, max_fecha = db.query(
        func.min(Cronograma.fecha),
        func.max(Cronograma.fecha),
    ).one()
    ultima_fecha_cargada = max_fecha
    log_block("date_limits", block_started_at)

    default_fecha_desde = today.replace(day=1)
    default_fecha_hasta = ultima_fecha_cargada or (today - timedelta(days=1))
    fecha_desde = fecha_desde or default_fecha_desde.isoformat()
    fecha_hasta = fecha_hasta or default_fecha_hasta.isoformat()
    quick_date_ranges = {
        "ultimos_15": {
            "fecha_desde": (today - timedelta(days=15)).isoformat(),
            "fecha_hasta": default_fecha_hasta.isoformat(),
        },
        "ultimo_mes": {
            "fecha_desde": (today - timedelta(days=30)).isoformat(),
            "fecha_hasta": default_fecha_hasta.isoformat(),
        },
    }

    selected_alcances = _clean_multi_values(alcance)
    selected_categorias = _clean_multi_values(categoria)
    selected_anunciantes = _clean_multi_values(anunciante)
    selected_tipos_dia = _clean_multi_values(tipo_dia)
    block_started_at = perf_counter()
    filter_options = _get_envio_filter_options(db)
    log_block("filter_options", block_started_at)
    if filtro_comercial:
        selected_alcances = [
            item
            for item in filter_options["alcances"]
            if _normalize_match_text(item) == "LOCAL"
        ]
        selected_categorias = [
            item
            for item in filter_options["categorias"]
            if not _is_commercial_excluded_category(item)
        ]
    selected_alcances = _normalize_all_selected(selected_alcances, filter_options["alcances"])
    selected_categorias = _normalize_all_selected(
        selected_categorias,
        filter_options["categorias"],
    )
    selected_anunciantes = _normalize_all_selected(
        selected_anunciantes,
        filter_options["anunciantes"],
    )
    selected_tipos_dia = _normalize_all_selected(
        selected_tipos_dia,
        filter_options["tipos_dia"],
    )
    block_started_at = perf_counter()
    total_duracion = _get_envio_total_duration(
        db=db,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=selected_alcances,
        categorias=selected_categorias,
        anunciantes=selected_anunciantes,
        tipos_dia=selected_tipos_dia,
        prom_canal=prom_canal,
    )
    log_block("total_duration", block_started_at)

    block_started_at = perf_counter()
    main_rows = _get_envio_main_rows(
        db=db,
        total_duracion=total_duracion,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=selected_alcances,
        categorias=selected_categorias,
        anunciantes=selected_anunciantes,
        tipos_dia=selected_tipos_dia,
        prom_canal=prom_canal,
        ranking_alcance=ranking_alcance,
        ranking_categoria=ranking_categoria,
    )
    log_block("main_rows", block_started_at)

    block_started_at = perf_counter()
    product_breakdowns = _get_envio_ranking_product_rows(
        db=db,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=selected_alcances,
        categorias=selected_categorias,
        anunciantes=selected_anunciantes,
        tipos_dia=selected_tipos_dia,
        prom_canal=prom_canal,
        ranking_alcance=ranking_alcance,
        ranking_categoria=ranking_categoria,
        main_rows=main_rows,
    )
    log_block("ranking_product_rows", block_started_at)
    for row in main_rows:
        row["products"] = product_breakdowns.get(
            _envio_ranking_key(row["alcance"], row["anunciante"], row["canal"]),
            [],
        )
    block_started_at = perf_counter()
    side_rows = _get_envio_side_rows(
        db=db,
        total_duracion=total_duracion,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=selected_alcances,
        categorias=selected_categorias,
        anunciantes=selected_anunciantes,
        tipos_dia=selected_tipos_dia,
    )
    log_block("side_rows", block_started_at)

    block_started_at = perf_counter()
    category_side_rows = _get_envio_category_side_rows(
        db=db,
        total_duracion=total_duracion,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=selected_alcances,
        categorias=selected_categorias,
        anunciantes=selected_anunciantes,
        tipos_dia=selected_tipos_dia,
    )
    log_block("category_side_rows", block_started_at)

    block_started_at = perf_counter()
    channel_rows = _get_envio_channel_rows(
        db=db,
        total_duracion=total_duracion,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=selected_alcances,
        categorias=selected_categorias,
        anunciantes=selected_anunciantes,
        tipos_dia=selected_tipos_dia,
        prom_canal=prom_canal,
    )
    log_block("channel_rows", block_started_at)

    block_started_at = perf_counter()
    line_chart = _build_line_chart(
        _get_envio_date_channel_rows(
            db=db,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            alcances=selected_alcances,
            categorias=selected_categorias,
            anunciantes=selected_anunciantes,
            tipos_dia=selected_tipos_dia,
            prom_canal=prom_canal,
        )
    )
    log_block("date_channel_chart", block_started_at)

    block_started_at = perf_counter()
    hour_chart = _build_grouped_bar_chart(
        _get_envio_hour_channel_rows(
            db=db,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            alcances=selected_alcances,
            categorias=selected_categorias,
            anunciantes=selected_anunciantes,
            tipos_dia=selected_tipos_dia,
            prom_canal=prom_canal,
        ),
        label_key="hora",
        label_order=None,
    )
    log_block("hour_channel_chart", block_started_at)

    block_started_at = perf_counter()
    weekday_chart = _build_grouped_bar_chart(
        _get_envio_weekday_channel_rows(
            db=db,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            alcances=selected_alcances,
            categorias=selected_categorias,
            anunciantes=selected_anunciantes,
            tipos_dia=selected_tipos_dia,
            prom_canal=prom_canal,
        ),
        label_key="dia",
        label_order=[WEEKDAY_LABELS[index] for index in range(1, 8)],
    )
    log_block("weekday_channel_chart", block_started_at)

    block_started_at = perf_counter()
    total_validation = _get_envio_total_validation(
        db=db,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        tipos_dia=selected_tipos_dia,
        prom_canal=prom_canal,
    )
    log_block("total_validation", block_started_at)

    log_block("TOTAL", page_started_at)

    return templates.TemplateResponse(
        request,
        "envio_monitor.html",
        {
            "filters": {
                "fecha_desde": fecha_desde or "",
                "fecha_hasta": fecha_hasta or "",
                "alcances": selected_alcances,
                "categorias": selected_categorias,
                "anunciantes": selected_anunciantes,
                "tipos_dia": selected_tipos_dia,
                "prom_canal": prom_canal or "",
                "ranking_alcance": ranking_alcance or "",
                "ranking_categoria": ranking_categoria or "",
                "filtro_comercial": filtro_comercial,
            },
            "filter_options": filter_options,
            "quick_date_ranges": quick_date_ranges,
            "min_fecha": min_fecha,
            "max_fecha": max_fecha,
            "ultima_fecha_cargada": ultima_fecha_cargada,
            "total_duracion": total_duracion,
            "main_rows": main_rows,
            "side_rows": side_rows,
            "category_side_rows": category_side_rows,
            "channel_rows": channel_rows,
            "line_chart": line_chart,
            "hour_chart": hour_chart,
            "weekday_chart": weekday_chart,
            "total_validation": total_validation,
        },
    )


@router.get("/alertas-comerciales", response_class=HTMLResponse)
def alertas_comerciales_view(
    request: Request,
    db: Session = Depends(get_db),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
):
    today = date.today()
    default_fecha_hasta = today - timedelta(days=1)
    quick_date_ranges = {
        "ultimos_15": {
            "fecha_desde": (today - timedelta(days=15)).isoformat(),
            "fecha_hasta": default_fecha_hasta.isoformat(),
        },
        "ultimo_mes": {
            "fecha_desde": (today - timedelta(days=30)).isoformat(),
            "fecha_hasta": default_fecha_hasta.isoformat(),
        },
    }
    fecha_desde = fecha_desde or quick_date_ranges["ultimos_15"]["fecha_desde"]
    fecha_hasta = fecha_hasta or quick_date_ranges["ultimos_15"]["fecha_hasta"]

    category_options = _get_distinct_base_anunciante_values(db, BaseAnunciante.categoria)
    selected_categories = _clean_multi_values(categoria)
    if categoria is None:
        selected_categories = [
            item
            for item in category_options
            if _normalize_match_text(item) in {"OFICIAL", "OFICIAL INTERIOR"}
        ]

    alert_rows = _get_commercial_alert_rows(
        db=db,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        categories=selected_categories,
    )
    advertiser_count = len({str(item["anunciante"]) for item in alert_rows})
    critical_count = len({
        str(item["anunciante"])
        for item in alert_rows
        if item["critical"]
    })

    return templates.TemplateResponse(
        request,
        "alertas_comerciales.html",
        {
            "filters": {
                "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta,
                "categorias": selected_categories,
            },
            "quick_date_ranges": quick_date_ranges,
            "category_options": category_options,
            "alert_rows": alert_rows,
            "advertiser_count": advertiser_count,
            "critical_count": critical_count,
        },
    )


@router.get("/comparativos", response_class=HTMLResponse)
def comparativos_view(
    request: Request,
    db: Session = Depends(get_db),
    mes: str | None = Query(default=None),
    mes_desde: str | None = Query(default=None),
    mes_hasta: str | None = Query(default=None),
    alcance: list[str] | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
    anunciante: list[str] | None = Query(default=None),
    canal: list[str] | None = Query(default=None),
):
    max_fecha = db.query(func.max(Cronograma.fecha)).scalar()
    reference_date = max_fecha or date.today()
    reference_month = reference_date.replace(day=1)

    def parse_selected_month(value: str | None) -> date:
        try:
            parsed = datetime.strptime(value or "", "%Y-%m").date().replace(day=1)
        except ValueError:
            return reference_month
        return min(parsed, reference_month)

    legacy_month = mes or None
    selected_start_month = parse_selected_month(mes_desde or legacy_month)
    selected_end_month = parse_selected_month(mes_hasta or legacy_month)
    if selected_start_month > selected_end_month:
        selected_start_month, selected_end_month = selected_end_month, selected_start_month

    selected_last_day = calendar.monthrange(selected_end_month.year, selected_end_month.month)[1]
    if selected_end_month == reference_month:
        selected_cutoff_day = min(reference_date.day, selected_last_day)
    else:
        selected_cutoff_day = selected_last_day
    current_end = selected_end_month.replace(day=selected_cutoff_day)
    show_previous = selected_start_month == selected_end_month

    previous_month = (selected_start_month - timedelta(days=1)).replace(day=1)
    previous_end = previous_month.replace(
        day=min(selected_cutoff_day, calendar.monthrange(previous_month.year, previous_month.month)[1])
    )
    previous_year = selected_start_month.replace(year=selected_start_month.year - 1)
    previous_year_end_month = selected_end_month.replace(year=selected_end_month.year - 1)
    previous_year_end = previous_year_end_month.replace(
        day=min(selected_cutoff_day, calendar.monthrange(previous_year_end_month.year, previous_year_end_month.month)[1])
    )

    options = _get_envio_filter_options(db)
    selected_alcances = _normalize_all_selected(_clean_multi_values(alcance), options["alcances"])
    selected_categorias = _normalize_all_selected(_clean_multi_values(categoria), options["categorias"])
    selected_anunciantes = _normalize_all_selected(_clean_multi_values(anunciante), options["anunciantes"])
    selected_canales = _normalize_all_selected(_clean_multi_values(canal), options["canales"])
    periods = {
        "actual": (selected_start_month, current_end),
        "anual": (previous_year, previous_year_end),
    }
    if show_previous:
        periods["anterior"] = (previous_month, previous_end)
    period_rows = {
        key: _get_comparison_base_rows(
            db=db,
            fecha_desde=start,
            fecha_hasta=end,
            alcances=selected_alcances,
            categorias=selected_categorias,
            anunciantes=selected_anunciantes,
            canales=selected_canales,
        )
        for key, (start, end) in periods.items()
    }
    comparison_tables = {
        "alcances": _build_comparison_table(period_rows, "alcance"),
        "categorias": _build_comparison_table(period_rows, "categoria"),
        "anunciantes": _build_comparison_table(period_rows, "anunciante"),
    }
    for row in comparison_tables["anunciantes"]:
        metadata = options["anunciante_metadata"].get(
            row["label"],
            {"alcances": [], "categorias": []},
        )
        row["alcances"] = metadata["alcances"]
        row["categorias"] = metadata["categorias"]
    return templates.TemplateResponse(
        request,
        "comparativos.html",
        {
            "tables": comparison_tables,
            "options": options,
            "filters": {
                "mes_desde": selected_start_month.strftime("%Y-%m"),
                "mes_hasta": selected_end_month.strftime("%Y-%m"),
                "alcances": selected_alcances,
                "categorias": selected_categorias,
                "anunciantes": selected_anunciantes,
                "canales": selected_canales,
            },
            "period_labels": {
                "actual": f"{selected_start_month.strftime('%m/%Y')}–{current_end.strftime('%d/%m/%Y')}",
                "anterior": f"{previous_month.strftime('%m/%Y')} (01–{previous_end.day:02d})",
                "anual": f"{previous_year.strftime('%m/%Y')}–{previous_year_end.strftime('%d/%m/%Y')}",
            },
            "show_previous": show_previous,
            "max_month": reference_date.strftime("%Y-%m"),
        },
    )


PIVOT_DIMENSIONS = {
    "alcance": "Alcance",
    "categoria": "Categoría",
    "anunciante": "Anunciante",
    "producto": "Producto",
    "canal": "Canal",
    "mes": "Mes",
    "fecha": "Fecha",
    "dia_semana": "Día de la semana",
}
HIERARCHY_DIMENSIONS = ("alcance", "categoria", "anunciante", "producto")


@router.get("/prueba", response_class=HTMLResponse)
def prueba_view(
    request: Request,
    db: Session = Depends(get_db),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    nivel: str = Query(default="alcance"),
    dimension: list[str] | None = Query(default=None),
    columnas: str = Query(default="canal"),
    valores: str = Query(default="ambos"),
    base_share: str = Query(default="fila"),
    comparar: str = Query(default="ninguno"),
    alcance: list[str] | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
    anunciante: list[str] | None = Query(default=None),
    canal: list[str] | None = Query(default=None),
):
    max_fecha = db.query(func.max(Cronograma.fecha)).scalar() or date.today()
    min_fecha = db.query(func.min(Cronograma.fecha)).scalar() or max_fecha
    default_from = max(min_fecha, max_fecha - timedelta(days=29))
    try:
        parsed_from = date.fromisoformat(fecha_desde or "")
    except ValueError:
        parsed_from = default_from
    try:
        parsed_to = date.fromisoformat(fecha_hasta or "")
    except ValueError:
        parsed_to = max_fecha
    parsed_from = min(max(parsed_from, min_fecha), max_fecha)
    parsed_to = min(max(parsed_to, min_fecha), max_fecha)
    if parsed_from > parsed_to:
        parsed_from, parsed_to = parsed_to, parsed_from
    fecha_desde = parsed_from.isoformat()
    fecha_hasta = parsed_to.isoformat()

    if nivel not in HIERARCHY_DIMENSIONS:
        nivel = "alcance"
    hierarchy_end = HIERARCHY_DIMENSIONS.index(nivel) + 1
    available_hierarchy = list(HIERARCHY_DIMENSIONS[:hierarchy_end])
    selected_hierarchy = [key for key in _clean_multi_values(dimension) if key in available_hierarchy]
    if dimension is None or not selected_hierarchy:
        selected_hierarchy = available_hierarchy
    if columnas not in {*PIVOT_DIMENSIONS, "ninguna"} or columnas == "producto":
        columnas = "canal"
    if valores not in {"segundos", "share", "ambos"}:
        valores = "ambos"
    if base_share not in {"general", "fila", "columna"}:
        base_share = "fila"
    if comparar not in {"ninguno", "anterior", "anual"}:
        comparar = "ninguno"

    options = _get_envio_filter_options(db)
    filters = {
        "alcances": _normalize_all_selected(_clean_multi_values(alcance), options["alcances"]),
        "categorias": _normalize_all_selected(_clean_multi_values(categoria), options["categorias"]),
        "anunciantes": _normalize_all_selected(_clean_multi_values(anunciante), options["anunciantes"]),
        "canales": _normalize_all_selected(_clean_multi_values(canal), options["canales"]),
    }
    pivot = _build_prueba_pivot(
        db=db,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        row_dimensions=selected_hierarchy,
        column_dimension=columnas,
        base_share=base_share,
        filters=filters,
    )
    comparison_period = None
    if comparar != "ninguno":
        if comparar == "anterior":
            period_days = (parsed_to - parsed_from).days
            comparison_to = parsed_from - timedelta(days=1)
            comparison_from = comparison_to - timedelta(days=period_days)
            comparison_title = "Período anterior"
        else:
            comparison_from = _shift_prueba_year(parsed_from)
            comparison_to = _shift_prueba_year(parsed_to)
            comparison_title = "Mismo período del año anterior"
        comparison_pivot = _build_prueba_pivot(
            db=db,
            fecha_desde=comparison_from.isoformat(),
            fecha_hasta=comparison_to.isoformat(),
            row_dimensions=selected_hierarchy,
            column_dimension=columnas,
            base_share=base_share,
            filters=filters,
        )
        pivot = _merge_prueba_comparison(pivot, comparison_pivot)
        comparison_period = {
            "title": comparison_title,
            "from": comparison_from.strftime("%d/%m/%Y"),
            "to": comparison_to.strftime("%d/%m/%Y"),
            "has_data": comparison_pivot["grand_total"] > 0,
        }
    base_descriptions = {
        "general": "Cada porcentaje usa como base todos los segundos del universo filtrado.",
        "fila": "Cada porcentaje muestra cómo se distribuye el total de su fila entre las columnas.",
        "columna": "Cada porcentaje muestra qué peso tiene la fila dentro del total de cada columna.",
    }
    return templates.TemplateResponse(
        request,
        "prueba.html",
        {
            "pivot": pivot,
            "dimensions": PIVOT_DIMENSIONS,
            "options": options,
            "filters": {
                **filters,
                "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta,
                "nivel": nivel,
                "dimensions": selected_hierarchy,
                "columnas": columnas,
                "valores": valores,
                "base_share": base_share,
                "comparar": comparar,
            },
            "base_description": base_descriptions[base_share],
            "comparison_period": comparison_period,
            "current_period": {
                "from": parsed_from.strftime("%d/%m/%Y"),
                "to": parsed_to.strftime("%d/%m/%Y"),
            },
            "available_hierarchy": [
                {"key": key, "label": PIVOT_DIMENSIONS[key]}
                for key in available_hierarchy
            ],
            "advanced_open": bool(
                nivel != "alcance"
                or columnas != "canal"
                or valores != "ambos"
                or base_share != "fila"
                or comparar != "ninguno"
                or any(filters.values())
            ),
            "min_fecha": min_fecha.isoformat(),
            "max_fecha": max_fecha.isoformat(),
        },
    )


def _shift_prueba_year(value: date) -> date:
    target_year = value.year - 1
    target_day = min(value.day, calendar.monthrange(target_year, value.month)[1])
    return value.replace(year=target_year, day=target_day)


def _merge_prueba_comparison(
    current: dict[str, object],
    previous: dict[str, object],
) -> dict[str, object]:
    columns = list(current["columns"])
    columns.extend(column for column in previous["columns"] if column not in columns)

    def row_map(pivot: dict[str, object]) -> dict[tuple[str, ...], dict[str, object]]:
        result = {}
        for row in pivot["rows"]:
            cells = {
                column: cell
                for column, cell in zip(pivot["columns"], row["cells"])
            }
            result[tuple(row["labels"])] = {**row, "cell_map": cells}
        return result

    current_rows = row_map(current)
    previous_rows = row_map(previous)
    has_previous_data = int(previous["grand_total"] or 0) > 0
    merged_rows = []
    all_keys = sorted(
        current_rows.keys() | previous_rows.keys(),
        key=lambda value: tuple(part.casefold() for part in value),
    )
    for key in all_keys:
        current_row = current_rows.get(key)
        previous_row = previous_rows.get(key)
        cells = []
        for column in columns:
            current_cell = current_row["cell_map"].get(column) if current_row else None
            previous_cell = previous_row["cell_map"].get(column) if previous_row else None
            seconds = int(current_cell["seconds"] if current_cell else 0)
            previous_seconds = int(previous_cell["seconds"] if previous_cell else 0)
            share = float(current_cell["share"] if current_cell else 0)
            previous_share = float(previous_cell["share"] if previous_cell else 0)
            cells.append({
                "seconds": seconds,
                "share": share,
                "previous_seconds": previous_seconds if has_previous_data else None,
                "previous_share": previous_share if has_previous_data else None,
                "seconds_delta": seconds - previous_seconds if has_previous_data else None,
                "seconds_delta_pct": (
                    (seconds - previous_seconds) / previous_seconds * 100
                    if has_previous_data and previous_seconds
                    else None
                ),
                "share_delta": share - previous_share if has_previous_data else None,
            })
        seconds = int(current_row["seconds"] if current_row else 0)
        previous_seconds = int(previous_row["seconds"] if previous_row else 0)
        share = float(current_row["share"] if current_row else 0)
        previous_share = float(previous_row["share"] if previous_row else 0)
        merged_rows.append({
            "labels": list(key),
            "cells": cells,
            "seconds": seconds,
            "share": share,
            "previous_seconds": previous_seconds if has_previous_data else None,
            "previous_share": previous_share if has_previous_data else None,
            "seconds_delta": seconds - previous_seconds if has_previous_data else None,
            "seconds_delta_pct": (
                (seconds - previous_seconds) / previous_seconds * 100
                if has_previous_data and previous_seconds
                else None
            ),
            "share_delta": share - previous_share if has_previous_data else None,
        })
    current["columns"] = columns
    current["rows"] = merged_rows
    current["column_totals"] = [
        next((total for name, total in zip(current["columns"], current["column_totals"]) if name == column), 0)
        for column in columns
    ]
    current["comparison_grand_total"] = int(previous["grand_total"] or 0)
    return current


def _prueba_dimension_expression(key: str):
    expressions = {
        "alcance": _envio_alcance_expr(),
        "categoria": _envio_categoria_expr(),
        "anunciante": _envio_anunciante_expr(),
        "canal": Cronograma.canal,
        "mes": func.date_format(Cronograma.fecha, "%Y-%m"),
        "fecha": Cronograma.fecha,
        "dia_semana": Cronograma.dia_semana,
    }
    return expressions[key]


def _prueba_dimension_label(key: str, value: object) -> str:
    if value in {None, ""}:
        return f"Sin {PIVOT_DIMENSIONS[key].lower()}"
    if key == "fecha" and isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if key == "mes":
        try:
            parsed = datetime.strptime(str(value), "%Y-%m")
            return f"{MONTH_LABELS[parsed.month]} {parsed.year}"
        except ValueError:
            pass
    if key == "dia_semana":
        try:
            return WEEKDAY_LABELS.get(int(value), str(value))
        except (TypeError, ValueError):
            pass
    return str(value).strip()


def _build_prueba_pivot(
    *,
    db: Session,
    fecha_desde: str,
    fecha_hasta: str,
    row_dimensions: list[str],
    column_dimension: str,
    base_share: str,
    filters: dict[str, list[str]],
) -> dict[str, object]:
    # Pre-aggregate the airings before resolving products. This keeps the
    # normalized text joins away from the large cronogramas table.
    raw_dimensions = {"canal", "mes", "fecha", "dia_semana"}
    entities = [Cronograma.producto, Cronograma.cod_prod]
    group_expressions = [Cronograma.producto, Cronograma.cod_prod]
    if column_dimension in raw_dimensions:
        column_expr = _prueba_dimension_expression(column_dimension).label("pivot_column")
        entities.append(column_expr)
        group_expressions.append(column_expr)
    duration_expr = func.sum(Cronograma.duracion).label("seconds")
    entities.append(duration_expr)
    query = db.query(*entities).select_from(Cronograma)
    query = query.filter(Cronograma.fecha >= fecha_desde, Cronograma.fecha <= fecha_hasta)
    if filters["canales"]:
        query = query.filter(Cronograma.canal.in_(filters["canales"]))
    raw_rows = query.group_by(*group_expressions).all()

    master_rows = (
        db.query(
            MaestroProducto.id,
            MaestroProducto.producto,
            BaseAnunciante.anunciante,
            BaseAnunciante.alcance,
            BaseAnunciante.categoria,
        )
        .outerjoin(BaseAnunciante, MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante)
        .all()
    )
    metadata_by_id = {
        int(item.id): {
            "producto": str(item.producto or "Sin producto").strip(),
            "anunciante": str(item.anunciante or item.producto or "Sin anunciante").strip(),
            "alcance": str(item.alcance or ("Sin anunciante asignado" if not item.anunciante else "Sin alcance")).strip(),
            "categoria": str(item.categoria or ("Sin anunciante asignado" if not item.anunciante else "Sin categoría")).strip(),
        }
        for item in master_rows
    }
    master_by_name = {
        _normalize_match_text(item.producto): metadata_by_id[int(item.id)]
        for item in master_rows
    }
    ids_by_code: dict[str, set[int]] = defaultdict(set)
    for item in db.query(ProductoTema.cod_producto, ProductoTema.id_producto).filter(
        ProductoTema.cod_producto.isnot(None),
        func.trim(ProductoTema.cod_producto) != "",
    ).all():
        ids_by_code[_normalize_match_text(item.cod_producto)].add(int(item.id_producto))
    master_by_code = {
        code: metadata_by_id[next(iter(ids))]
        for code, ids in ids_by_code.items()
        if len(ids) == 1 and next(iter(ids)) in metadata_by_id
    }

    values: dict[tuple[tuple[str, ...], str], int] = defaultdict(int)
    row_totals: dict[tuple[str, ...], int] = defaultdict(int)
    column_totals: dict[str, int] = defaultdict(int)
    column_order: list[str] = []
    hierarchy_keys = tuple(row_dimensions)
    for item in raw_rows:
        product = str(item.producto or "").strip()
        metadata = master_by_name.get(_normalize_match_text(product))
        if metadata is None:
            metadata = master_by_code.get(_normalize_match_text(item.cod_prod))
        resolved = metadata or {
            "producto": product or "Sin producto",
            "anunciante": product or "Sin anunciante",
            "alcance": "Sin anunciante asignado",
            "categoria": "Sin anunciante asignado",
        }
        if filters["alcances"] and resolved["alcance"] not in filters["alcances"]:
            continue
        if filters["categorias"] and resolved["categoria"] not in filters["categorias"]:
            continue
        if filters["anunciantes"] and resolved["anunciante"] not in filters["anunciantes"]:
            continue
        row_key = tuple(resolved[key] for key in hierarchy_keys)
        column_label = (
            _prueba_dimension_label(
                column_dimension,
                item.pivot_column if column_dimension in raw_dimensions else resolved[column_dimension],
            )
            if column_dimension != "ninguna"
            else "Total"
        )
        seconds = int(item.seconds or 0)
        values[(row_key, column_label)] += seconds
        row_totals[row_key] += seconds
        column_totals[column_label] += seconds
        if column_label not in column_order:
            column_order.append(column_label)

    grand_total = sum(row_totals.values())
    if column_dimension == "canal":
        preferred = ["Canal 8", "Telefe Córdoba", "Canal 10", "Canal 12"]
        column_order.sort(key=lambda value: (preferred.index(value) if value in preferred else len(preferred), value))
    else:
        column_order.sort()

    rows = []
    for row_key in sorted(row_totals, key=lambda value: tuple(part.casefold() for part in value)):
        cells = []
        for column_label in column_order:
            seconds = values[(row_key, column_label)]
            denominator = {
                "general": grand_total,
                "fila": row_totals[row_key],
                "columna": column_totals[column_label],
            }[base_share]
            cells.append({"seconds": seconds, "share": _percentage(seconds, denominator)})
        rows.append({
            "labels": list(row_key),
            "cells": cells,
            "seconds": row_totals[row_key],
            "share": _percentage(row_totals[row_key], grand_total),
        })
    return {
        "columns": column_order,
        "rows": rows,
        "column_totals": [column_totals[column] for column in column_order],
        "grand_total": grand_total,
        "row_label": " + ".join(PIVOT_DIMENSIONS[key] for key in hierarchy_keys),
        "row_headers": [PIVOT_DIMENSIONS[key] for key in hierarchy_keys],
        "column_label": PIVOT_DIMENSIONS.get(column_dimension, "Total"),
    }


def _get_comparison_base_rows(
    *,
    db: Session,
    fecha_desde: date,
    fecha_hasta: date,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    canales: list[str],
) -> list[dict[str, object]]:
    advertiser_expr = _envio_anunciante_expr().label("anunciante")
    alcance_expr = _envio_alcance_expr().label("alcance")
    categoria_expr = _envio_categoria_expr().label("categoria")
    duration_expr = func.sum(Cronograma.duracion).label("duracion_total")
    query = _envio_base_query(
        db,
        advertiser_expr,
        alcance_expr,
        categoria_expr,
        duration_expr,
    )
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde.isoformat(),
        fecha_hasta=fecha_hasta.isoformat(),
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=[],
        prom_canal=None,
    )
    if canales:
        query = query.filter(Cronograma.canal.in_(canales))
    rows = query.group_by(advertiser_expr, alcance_expr, categoria_expr).all()
    return [
        {
            "anunciante": str(row.anunciante or "Sin anunciante").strip(),
            "alcance": str(row.alcance or "Sin alcance").strip(),
            "categoria": str(row.categoria or "Sin categoría").strip(),
            "duracion_total": int(row.duracion_total or 0),
        }
        for row in rows
    ]


def _build_comparison_table(
    period_rows: dict[str, list[dict[str, object]]],
    dimension: str,
) -> list[dict[str, object]]:
    values: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    for period, rows in period_rows.items():
        for row in rows:
            duration = int(row["duracion_total"])
            values[str(row[dimension])][period] += duration
            totals[period] += duration

    result = []
    for label, durations in values.items():
        shares = {
            period: (durations.get(period, 0) / totals[period] * 100) if totals[period] else None
            for period in ("actual", "anterior", "anual")
        }
        result.append({
            "label": label,
            "actual_seconds": durations.get("actual", 0),
            "actual_share": shares["actual"],
            "previous_share": shares["anterior"],
            "previous_delta": (
                shares["actual"] - shares["anterior"]
                if shares["actual"] is not None and shares["anterior"] is not None
                else None
            ),
            "year_share": shares["anual"],
            "year_delta": (
                shares["actual"] - shares["anual"]
                if shares["actual"] is not None and shares["anual"] is not None
                else None
            ),
        })
    return sorted(result, key=lambda row: (-int(row["actual_seconds"]), str(row["label"])))


@router.get("/alertas-comerciales/exportar")
def export_alertas_comerciales(
    db: Session = Depends(get_db),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
) -> Response:
    selected_categories = _clean_multi_values(categoria)
    if categoria is None:
        selected_categories = [
            item
            for item in _get_distinct_base_anunciante_values(db, BaseAnunciante.categoria)
            if _normalize_match_text(item) in {"OFICIAL", "OFICIAL INTERIOR"}
        ]
    alert_rows = _get_commercial_alert_rows(
        db=db,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        categories=selected_categories,
    )
    export_rows = [
        {
            "estado": "Crítica" if row["critical"] else "Atención",
            "alcance": row["alcance"],
            "categoria": row["categoria"],
            "anunciante": row["anunciante"],
            "canal_12_seconds": row["canal_12_seconds"],
            "competitor": row["competitor"],
            "competitor_seconds": row["competitor_seconds"],
            "difference": row["difference"],
            "brecha": (
                f'{float(row["difference_percentage"]):.1f}%'
                if row["difference_percentage"] is not None
                else "Sin pauta en C12"
            ),
        }
        for row in alert_rows
    ]
    columns = [
        ("Estado", "estado"),
        ("Alcance", "alcance"),
        ("Categoría", "categoria"),
        ("Anunciante", "anunciante"),
        ("Segundos Canal 12", "canal_12_seconds"),
        ("Canal competidor", "competitor"),
        ("Segundos competidor", "competitor_seconds"),
        ("Diferencia segundos", "difference"),
        ("Brecha", "brecha"),
    ]
    content = _build_xlsx_response_content(export_rows, columns=columns)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="alertas_comerciales.xlsx"'},
    )


@router.get("/alertas-comerciales/detalle", response_class=HTMLResponse)
def alerta_comercial_detail_view(
    request: Request,
    db: Session = Depends(get_db),
    anunciante: str = Query(...),
    canal: str = Query(...),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
    alcance: str | None = Query(default=None),
):
    selected_categories = _clean_multi_values(categoria)
    if categoria is None:
        selected_categories = [
            item
            for item in _get_distinct_base_anunciante_values(db, BaseAnunciante.categoria)
            if _normalize_match_text(item) in {"OFICIAL", "OFICIAL INTERIOR"}
        ]

    def apply_detail_filters(query):
        query = query.filter(_envio_anunciante_expr() == anunciante)
        if fecha_desde:
            query = query.filter(Cronograma.fecha >= fecha_desde)
        if fecha_hasta:
            query = query.filter(Cronograma.fecha <= fecha_hasta)
        if selected_categories:
            query = query.filter(_envio_categoria_expr().in_(selected_categories))
        if alcance:
            query = query.filter(_envio_alcance_expr() == alcance)
        return query

    daily_query = _envio_base_query(
        db,
        Cronograma.fecha,
        Cronograma.canal,
        func.sum(Cronograma.duracion).label("duracion_total"),
    )
    daily_rows = (
        apply_detail_filters(daily_query)
        .filter(
            Cronograma.fecha.isnot(None),
            Cronograma.canal.in_([canal, "Canal 12"]),
        )
        .group_by(Cronograma.fecha, Cronograma.canal)
        .order_by(Cronograma.fecha.asc(), Cronograma.canal.asc())
        .all()
    )
    line_chart = _build_line_chart([
        {
            "fecha": row.fecha,
            "canal": row.canal,
            "duracion_total": int(row.duracion_total or 0),
        }
        for row in daily_rows
    ])
    airing_query = _envio_base_query(
        db,
        Cronograma.fecha,
        Cronograma.hora_inicio,
        Cronograma.canal,
        func.sum(Cronograma.duracion).label("duracion_total"),
    )
    airing_rows = (
        apply_detail_filters(airing_query)
        .filter(
            Cronograma.canal == canal,
            Cronograma.fecha.isnot(None),
        )
        .group_by(Cronograma.fecha, Cronograma.hora_inicio, Cronograma.canal)
        .order_by(Cronograma.fecha.asc(), Cronograma.hora_inicio.asc())
        .all()
    )
    airing_rows = [
        {
            "fecha": row.fecha,
            "hora_inicio": row.hora_inicio or "—",
            "canal": row.canal or "Sin canal",
            "duracion_total": int(row.duracion_total or 0),
            "mes": f"{MONTH_LABELS[row.fecha.month]} {row.fecha.year}",
        }
        for row in airing_rows
    ]
    month_totals: dict[str, int] = defaultdict(int)
    for row in airing_rows:
        month_totals[str(row["mes"])] += int(row["duracion_total"])
    for row in airing_rows:
        row["mes_duracion_total"] = month_totals[str(row["mes"])]
    product_query = _envio_base_query(
        db,
        Cronograma.producto,
        Cronograma.tema,
        func.sum(Cronograma.duracion).label("duracion_total"),
    )
    product_rows = (
        apply_detail_filters(product_query)
        .filter(Cronograma.canal == canal)
        .group_by(Cronograma.producto, Cronograma.tema)
        .order_by(func.sum(Cronograma.duracion).desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "partials/alerta_comercial_detalle.html",
        {
            "anunciante": anunciante,
            "canal": canal,
            "line_chart": line_chart,
            "airing_rows": airing_rows,
            "product_rows": product_rows,
        },
    )


def _get_commercial_alert_rows(
    *,
    db: Session,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    categories: list[str],
) -> list[dict[str, object]]:
    advertiser_expr = _envio_anunciante_expr().label("anunciante")
    alcance_expr = _envio_alcance_expr().label("alcance")
    categoria_expr = _envio_categoria_expr().label("categoria")
    query = _envio_base_query(
        db,
        advertiser_expr,
        alcance_expr,
        categoria_expr,
        Cronograma.canal,
        func.sum(Cronograma.duracion).label("duracion_total"),
    )
    if fecha_desde:
        query = query.filter(Cronograma.fecha >= fecha_desde)
    if fecha_hasta:
        query = query.filter(Cronograma.fecha <= fecha_hasta)
    if categories:
        query = query.filter(_envio_categoria_expr().in_(categories))
    grouped_rows = (
        query.filter(Cronograma.canal.isnot(None))
        .group_by(advertiser_expr, alcance_expr, categoria_expr, Cronograma.canal)
        .all()
    )

    seconds_by_advertiser: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    channel_labels: dict[str, str] = {}
    for row in grouped_rows:
        advertiser = str(row.anunciante or "Sin anunciante").strip()
        alcance = str(row.alcance or "Sin alcance").strip()
        categoria = str(row.categoria or "Sin categoría").strip()
        channel = str(row.canal or "").strip()
        channel_key = _normalize_match_text(channel)
        if not channel_key:
            continue
        seconds_by_advertiser[(alcance, categoria, advertiser)][channel_key] += int(row.duracion_total or 0)
        channel_labels.setdefault(channel_key, channel)

    alert_rows: list[dict[str, object]] = []
    for (alcance, categoria, advertiser), channel_seconds in seconds_by_advertiser.items():
        canal_12_seconds = channel_seconds.get("CANAL 12", 0)
        for channel_key, competitor_seconds in channel_seconds.items():
            if channel_key == "CANAL 12" or competitor_seconds <= canal_12_seconds:
                continue
            difference = competitor_seconds - canal_12_seconds
            percentage = difference / canal_12_seconds * 100 if canal_12_seconds else None
            alert_rows.append({
                "anunciante": advertiser,
                "alcance": alcance,
                "categoria": categoria,
                "canal_12_seconds": canal_12_seconds,
                "competitor": channel_labels[channel_key],
                "competitor_seconds": competitor_seconds,
                "difference": difference,
                "difference_percentage": percentage,
                "critical": canal_12_seconds == 0 or percentage >= 50,
            })
    alert_rows = sorted(
        alert_rows,
        key=lambda item: (
            _normalize_match_text(item["alcance"]),
            _normalize_match_text(item["categoria"]),
            0 if item["critical"] else 1,
            -int(item["difference"]),
        ),
    )
    group_totals: dict[tuple[str, str], dict[str, object]] = {}
    alcance_totals: dict[str, dict[str, object]] = {}
    for row in alert_rows:
        group_key = (str(row["alcance"]), str(row["categoria"]))
        totals = group_totals.setdefault(
            group_key,
            {"competitor_seconds": 0, "canal_12_by_advertiser": {}},
        )
        totals["competitor_seconds"] = int(totals["competitor_seconds"]) + int(
            row["competitor_seconds"]
        )
        totals["canal_12_by_advertiser"][str(row["anunciante"])] = int(
            row["canal_12_seconds"]
        )
        alcance = str(row["alcance"])
        scope_totals = alcance_totals.setdefault(
            alcance,
            {"competitor_seconds": 0, "canal_12_by_advertiser": {}},
        )
        scope_totals["competitor_seconds"] = int(scope_totals["competitor_seconds"]) + int(
            row["competitor_seconds"]
        )
        scope_totals["canal_12_by_advertiser"][str(row["anunciante"])] = int(
            row["canal_12_seconds"]
        )

    for row in alert_rows:
        totals = group_totals[(str(row["alcance"]), str(row["categoria"]))]
        canal_12_total = sum(totals["canal_12_by_advertiser"].values())
        competitor_total = int(totals["competitor_seconds"])
        row["group_canal_12_seconds"] = canal_12_total
        row["group_competitor_seconds"] = competitor_total
        row["group_difference"] = competitor_total - canal_12_total
        scope_totals = alcance_totals[str(row["alcance"])]
        scope_canal_12_total = sum(scope_totals["canal_12_by_advertiser"].values())
        scope_competitor_total = int(scope_totals["competitor_seconds"])
        row["scope_canal_12_seconds"] = scope_canal_12_total
        row["scope_competitor_seconds"] = scope_competitor_total
        row["scope_difference"] = scope_competitor_total - scope_canal_12_total

    return alert_rows


@router.get("/exportaciones", response_class=HTMLResponse)
def exportaciones_view(
    request: Request,
    db: Session = Depends(get_db),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    alcance: list[str] | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
    anunciante: list[str] | None = Query(default=None),
):
    fecha_desde_value = _parse_export_date(fecha_desde)
    fecha_hasta_value = _parse_export_date(fecha_hasta)
    options = _get_envio_filter_options(db)
    selected_alcances = _normalize_all_selected(_clean_multi_values(alcance), options["alcances"])
    selected_categorias = _normalize_all_selected(_clean_multi_values(categoria), options["categorias"])
    selected_anunciantes = _normalize_all_selected(_clean_multi_values(anunciante), options["anunciantes"])
    export_count = _get_export_cronogramas_query(
        db,
        fecha_desde=fecha_desde_value,
        fecha_hasta=fecha_hasta_value,
        alcances=selected_alcances,
        categorias=selected_categorias,
        anunciantes=selected_anunciantes,
    ).count()
    min_fecha = db.query(func.min(Cronograma.fecha)).scalar()
    max_fecha = db.query(func.max(Cronograma.fecha)).scalar()
    export_query_params: list[tuple[str, str]] = []
    if fecha_desde:
        export_query_params.append(("fecha_desde", fecha_desde))
    if fecha_hasta:
        export_query_params.append(("fecha_hasta", fecha_hasta))
    export_query_params.extend(("alcance", value) for value in selected_alcances)
    export_query_params.extend(("categoria", value) for value in selected_categorias)
    export_query_params.extend(("anunciante", value) for value in selected_anunciantes)

    return templates.TemplateResponse(
        request,
        "exportaciones.html",
        {
            "fecha_desde": fecha_desde or "",
            "fecha_hasta": fecha_hasta or "",
            "min_fecha": min_fecha,
            "max_fecha": max_fecha,
            "export_count": export_count,
            "options": options,
            "filters": {
                "alcances": selected_alcances,
                "categorias": selected_categorias,
                "anunciantes": selected_anunciantes,
            },
            "export_query": urlencode(export_query_params),
        },
    )


@router.get("/exportaciones/cronogramas")
def export_cronogramas(
    db: Session = Depends(get_db),
    fecha_desde: str | None = Query(default=None),
    fecha_hasta: str | None = Query(default=None),
    formato: str = Query(default="csv"),
    tipo: str = Query(default="simple"),
    alcance: list[str] | None = Query(default=None),
    categoria: list[str] | None = Query(default=None),
    anunciante: list[str] | None = Query(default=None),
) -> Response:
    fecha_desde_value = _parse_export_date(fecha_desde)
    fecha_hasta_value = _parse_export_date(fecha_hasta)
    if tipo == "completa":
        columns = EXPORT_CRONOGRAMAS_COMPLETA_COLUMNS
        rows = _get_export_cronogramas_completa_rows(
            db,
            fecha_desde=fecha_desde_value,
            fecha_hasta=fecha_hasta_value,
            alcances=_clean_multi_values(alcance),
            categorias=_clean_multi_values(categoria),
            anunciantes=_clean_multi_values(anunciante),
        )
    else:
        columns = EXPORT_CRONOGRAMAS_COLUMNS
        rows = _get_export_cronogramas_rows(
            db,
            fecha_desde=fecha_desde_value,
            fecha_hasta=fecha_hasta_value,
            alcances=_clean_multi_values(alcance),
            categorias=_clean_multi_values(categoria),
            anunciantes=_clean_multi_values(anunciante),
        )
    file_stem = _build_export_file_stem(
        fecha_desde=fecha_desde_value,
        fecha_hasta=fecha_hasta_value,
        tipo=tipo,
    )

    if formato == "xlsx":
        content = _build_xlsx_response_content(rows, columns=columns)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{file_stem}.xlsx"',
            },
        )

    content = _build_csv_response_content(rows, columns=columns)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_stem}.csv"'},
    )


@router.get("/archivos", response_class=HTMLResponse)
def archivos_view(request: Request, db: Session = Depends(get_db)):
    archivos_count = db.query(func.count(ArchivoIngesta.id)).scalar() or 0
    cronogramas_count = db.query(func.count(Cronograma.id)).scalar() or 0
    productos_count = db.query(func.count(MaestroProducto.id)).scalar() or 0
    anunciantes_count = db.query(func.count(BaseAnunciante.id_anunciante)).scalar() or 0
    ultima_fecha_cargada = db.query(func.max(Cronograma.fecha)).scalar()
    archivos = (
        db.query(ArchivoIngesta)
        .order_by(ArchivoIngesta.created_at.desc(), ArchivoIngesta.id.desc())
        .limit(300)
        .all()
    )
    missing_period_end = date.today() - timedelta(days=1)
    missing_period_start = date.today() - timedelta(days=30)
    channel_codes = {
        "1551": "Canal 8",
        "1553": "Canal 10",
        "1554": "Canal 12",
    }
    expected_channels = set(channel_codes)
    received_by_date: dict[date, set[str]] = defaultdict(set)
    received_rows = (
        db.query(
            ArchivoIngesta.fecha_nombre_archivo,
            ArchivoIngesta.canal_codigo,
            ArchivoIngesta.fechas_detectadas,
        )
        .filter(
            ArchivoIngesta.fecha_nombre_archivo >= missing_period_start,
            ArchivoIngesta.fecha_nombre_archivo <= missing_period_end,
            ArchivoIngesta.canal_codigo.in_(expected_channels),
        )
        .distinct()
        .all()
    )
    for filename_date, channel_code, detected_dates_json in received_rows:
        detected_dates: list[date] = []
        try:
            detected_dates = [
                date.fromisoformat(str(value))
                for value in json.loads(detected_dates_json or "[]")
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            detected_dates = []
        if not detected_dates and filename_date:
            detected_dates = [filename_date]
        for received_date in detected_dates:
            if (
                missing_period_start <= received_date <= missing_period_end
                and channel_code
            ):
                received_by_date[received_date].add(str(channel_code).strip())

    missing_channels = [
        {"fecha": received_date, "canal": channel_codes[channel_code]}
        for received_date, received_channels in received_by_date.items()
        for channel_code in sorted(expected_channels - received_channels, key=int)
    ]
    missing_channels.sort(key=lambda item: (item["fecha"], item["canal"]), reverse=True)

    return templates.TemplateResponse(
        request,
        "archivos.html",
        {
            "archivos": archivos,
            "archivos_count": archivos_count,
            "cronogramas_count": cronogramas_count,
            "productos_count": productos_count,
            "anunciantes_count": anunciantes_count,
            "ultima_fecha_cargada": ultima_fecha_cargada,
            "missing_channels": missing_channels,
            "process_status": request.query_params.get("process_status", ""),
            "process_message": request.query_params.get("process_message", ""),
        },
    )


def _parse_export_date(value: str | None) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _get_export_cronogramas_query(
    db: Session,
    *,
    fecha_desde: date | None,
    fecha_hasta: date | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
):
    query = _envio_base_query(db, Cronograma)
    if fecha_desde is not None:
        query = query.filter(Cronograma.fecha >= fecha_desde)
    if fecha_hasta is not None:
        query = query.filter(Cronograma.fecha <= fecha_hasta)
    if alcances:
        query = query.filter(_envio_alcance_expr().in_(alcances))
    if categorias:
        query = query.filter(_envio_categoria_expr().in_(categorias))
    if anunciantes:
        query = query.filter(_envio_anunciante_expr().in_(anunciantes))

    return query.order_by(Cronograma.fecha.asc(), Cronograma.hora_inicio.asc(), Cronograma.id.asc())


def _get_export_cronogramas_rows(
    db: Session,
    *,
    fecha_desde: date | None,
    fecha_hasta: date | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
) -> list[dict[str, object]]:
    return [
        _format_export_cronograma_row(row)
        for row in _get_export_cronogramas_query(
            db,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            alcances=alcances,
            categorias=categorias,
            anunciantes=anunciantes,
        ).all()
    ]


def _get_export_cronogramas_completa_rows(
    db: Session,
    *,
    fecha_desde: date | None,
    fecha_hasta: date | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
) -> list[dict[str, object]]:
    query = (
        db.query(
            Cronograma,
            MaestroProducto.producto.label("producto_base"),
            BaseAnunciante.alcance.label("alcance_base"),
            BaseAnunciante.categoria.label("categoria"),
        )
        .select_from(Cronograma)
    )
    query = _join_resolved_maestro(query, db)
    query = (
        query
        .outerjoin(
            BaseAnunciante,
            MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante,
        )
    )
    if fecha_desde is not None:
        query = query.filter(Cronograma.fecha >= fecha_desde)
    if fecha_hasta is not None:
        query = query.filter(Cronograma.fecha <= fecha_hasta)
    if alcances:
        query = query.filter(_envio_alcance_expr().in_(alcances))
    if categorias:
        query = query.filter(_envio_categoria_expr().in_(categorias))
    if anunciantes:
        query = query.filter(_envio_anunciante_expr().in_(anunciantes))

    rows = query.order_by(
        Cronograma.fecha.asc(),
        Cronograma.hora_inicio.asc(),
        Cronograma.id.asc(),
    ).all()
    formatted_rows = []
    for cronograma, producto_base, alcance_base, categoria in rows:
        formatted_rows.append(
            _format_export_cronograma_completa_row(
                cronograma=cronograma,
                producto_base=producto_base,
                alcance_base=alcance_base,
                categoria=categoria,
            )
        )
    return formatted_rows


def _format_export_cronograma_row(row: Cronograma) -> dict[str, object]:
    return {
        "hora_inicio": _export_value(row.hora_inicio),
        "hora_fin": _export_value(row.hora_fin),
        "cod_prod": _export_value(row.cod_prod),
        "producto": _export_value(row.producto),
        "tema": _export_value(row.tema),
        "duracion": _export_value(row.duracion),
        "t_compra": _export_value(row.t_compra),
        "t_material": _export_value(row.t_material),
        "columna_extra": _export_value(row.columna_extra),
        "alcance": _export_value(row.alcance),
        "prom_canal": _export_value(row.prom_canal),
        "programa": _export_value(row.programa),
        "canal": _export_value(row.canal),
        "fecha": _format_export_date(row.fecha),
        "dia_semana": _export_value(row.dia_semana),
        "dia_de_semana": _format_export_weekday(row.fecha, row.dia_semana),
        "tipo_dia": _normalize_export_tipo_dia(row.tipo_dia),
    }


def _format_export_cronograma_completa_row(
    *,
    cronograma: Cronograma,
    producto_base: str | None,
    alcance_base: str | None,
    categoria: str | None,
) -> dict[str, object]:
    row = _format_export_cronograma_row(cronograma)
    row.update(
        {
            "producto_base": _export_value(producto_base),
            "alcance_base": _export_value(alcance_base),
            "categoria": _export_value(categoria),
        }
    )
    return row


def _export_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()

    return value


def _format_export_date(value: date | None) -> str:
    if value is None:
        return ""

    return f"{value.day}/{value.month}/{value.year}"


def _format_export_weekday(value: date | None, dia_semana: int | None) -> str:
    if dia_semana is not None:
        return WEEKDAY_LABELS.get(dia_semana, "")
    if value is None:
        return ""

    return WEEKDAY_LABELS.get(value.isoweekday(), "")


def _normalize_export_tipo_dia(value: str | None) -> str:
    if not value:
        return ""

    return value.replace("HÃ¡bil", "Hábil")


def _build_export_file_stem(
    *,
    fecha_desde: date | None,
    fecha_hasta: date | None,
    tipo: str = "simple",
) -> str:
    prefix = "cronogramas_completo" if tipo == "completa" else "cronogramas"
    if fecha_desde and fecha_hasta:
        return f"{prefix}_{fecha_desde.isoformat()}_{fecha_hasta.isoformat()}"
    if fecha_desde:
        return f"{prefix}_desde_{fecha_desde.isoformat()}"
    if fecha_hasta:
        return f"{prefix}_hasta_{fecha_hasta.isoformat()}"

    return prefix


def _build_csv_response_content(
    rows: list[dict[str, object]],
    *,
    columns: list[tuple[str, str]],
) -> bytes:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([header for header, _ in columns])
    for row in rows:
        writer.writerow([row[key] for _, key in columns])

    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _build_xlsx_response_content(
    rows: list[dict[str, object]],
    *,
    columns: list[tuple[str, str]],
) -> bytes:
    workbook = BytesIO()
    sheet_rows = [
        [header for header, _ in columns],
        *[[row[key] for _, key in columns] for row in rows],
    ]

    with zipfile.ZipFile(workbook, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types_xml())
        archive.writestr("_rels/.rels", _xlsx_root_rels_xml())
        archive.writestr("xl/workbook.xml", _xlsx_workbook_xml())
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels_xml())
        archive.writestr("xl/styles.xml", _xlsx_styles_xml())
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml(sheet_rows))

    return workbook.getvalue()


def _xlsx_content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""


def _xlsx_root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _xlsx_workbook_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Cronogramas" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>"""


def _xlsx_workbook_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def _xlsx_styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""


def _xlsx_sheet_xml(rows: list[list[object]]) -> str:
    row_xml = []
    for row_index, row_values in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row_values, start=1):
            cell_reference = f"{_xlsx_column_name(column_index)}{row_index}"
            text = escape(str(_export_value(value)))
            cells.append(
                f'<c r="{cell_reference}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
            )
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        '</worksheet>'
    )


def _xlsx_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _envio_anunciante_expr():
    return func.coalesce(
        BaseAnunciante.anunciante,
        Cronograma.producto,
        literal("Sin anunciante"),
    )


def _envio_alcance_expr():
    return _clean_sql_text(BaseAnunciante.alcance)


def _envio_categoria_expr():
    return _clean_sql_text(BaseAnunciante.categoria)


def _envio_tipo_dia_expr():
    return _clean_sql_text(
        func.replace(Cronograma.tipo_dia, "HÃ¡bil", "Hábil")
    )


def _clean_sql_text(column):
    return func.trim(func.replace(func.replace(column, "\r", ""), "\n", ""))


def _envio_base_query(db: Session, *entities):
    query = (
        db.query(*entities)
        .select_from(Cronograma)
    )
    query = _join_resolved_maestro(query, db)
    return (
        query
        .outerjoin(
            BaseAnunciante,
            MaestroProducto.id_anunciante == BaseAnunciante.id_anunciante,
        )
    )


def _apply_envio_filters(
    query,
    *,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
):
    if fecha_desde:
        query = query.filter(Cronograma.fecha >= fecha_desde)
    if fecha_hasta:
        query = query.filter(Cronograma.fecha <= fecha_hasta)
    if alcances:
        query = query.filter(_envio_alcance_expr().in_(alcances))
    if categorias:
        query = query.filter(_envio_categoria_expr().in_(categorias))
    if anunciantes:
        query = query.filter(_envio_anunciante_expr().in_(anunciantes))
    if tipos_dia:
        query = query.filter(_envio_tipo_dia_expr().in_(tipos_dia))
    if prom_canal:
        query = query.filter(Cronograma.prom_canal == prom_canal)

    return query


def _get_envio_total_duration(
    *,
    db: Session,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
) -> int:
    query = _envio_base_query(
        db,
        func.sum(Cronograma.duracion).label("duracion_total"),
    )
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    return int(query.scalar() or 0)


def _apply_envio_raw_control_filters(
    query,
    *,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    tipos_dia: list[str],
    prom_canal: str | None,
):
    if fecha_desde:
        query = query.filter(Cronograma.fecha >= fecha_desde)
    if fecha_hasta:
        query = query.filter(Cronograma.fecha <= fecha_hasta)
    if tipos_dia:
        query = query.filter(_envio_tipo_dia_expr().in_(tipos_dia))
    if prom_canal:
        query = query.filter(Cronograma.prom_canal == prom_canal)
    return query


def _get_envio_total_validation(
    *,
    db: Session,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    tipos_dia: list[str],
    prom_canal: str | None,
) -> dict[str, object]:
    raw_query = db.query(
        func.sum(Cronograma.duracion).label("duracion_total")
    )
    raw_query = _apply_envio_raw_control_filters(
        raw_query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    raw_total = int(raw_query.scalar() or 0)

    joined_query = _envio_base_query(
        db,
        func.sum(Cronograma.duracion).label("duracion_total"),
    )
    joined_query = _apply_envio_raw_control_filters(
        joined_query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    joined_total = int(joined_query.scalar() or 0)
    difference = joined_total - raw_total

    return {
        "raw_total": raw_total,
        "joined_total": joined_total,
        "difference": difference,
        "ok": difference == 0,
    }


def _get_envio_main_rows(
    *,
    db: Session,
    total_duracion: int,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
    ranking_alcance: str | None,
    ranking_categoria: str | None,
) -> list[dict[str, object]]:
    alcance_expr = _envio_alcance_expr().label("alcance")
    anunciante_expr = _envio_anunciante_expr().label("anunciante")
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")
    query = _envio_base_query(
        db,
        alcance_expr,
        anunciante_expr,
        Cronograma.canal,
        duracion_expr,
    )
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    if ranking_alcance:
        query = query.filter(_envio_alcance_expr() == ranking_alcance)
    if ranking_categoria:
        query = query.filter(_envio_categoria_expr() == ranking_categoria)
    rows = (
        query.group_by(alcance_expr, anunciante_expr, Cronograma.canal)
        .order_by(duracion_expr.desc())
        .limit(50)
        .all()
    )

    return [
        {
            "alcance": row.alcance or "Sin alcance",
            "anunciante": row.anunciante or "Sin anunciante",
            "canal": row.canal or "Sin canal",
            "duracion_total": int(row.duracion_total or 0),
            "porcentaje": _percentage(row.duracion_total, total_duracion),
        }
        for row in rows
    ]


def _envio_ranking_key(alcance: object, anunciante: object, canal: object) -> tuple[str, str, str]:
    return (
        str(alcance or "Sin alcance"),
        str(anunciante or "Sin anunciante"),
        str(canal or "Sin canal"),
    )


def _get_envio_ranking_product_rows(
    *,
    db: Session,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
    ranking_alcance: str | None,
    ranking_categoria: str | None,
    main_rows: list[dict[str, object]],
) -> dict[tuple[str, str, str], list[dict[str, object]]]:
    main_keys = {
        _envio_ranking_key(row["alcance"], row["anunciante"], row["canal"])
        for row in main_rows
    }
    if not main_keys:
        return {}

    alcance_expr = _envio_alcance_expr().label("alcance")
    anunciante_expr = _envio_anunciante_expr().label("anunciante")
    producto_expr = func.coalesce(Cronograma.producto, literal("Sin producto")).label("producto")
    tema_expr = func.coalesce(Cronograma.tema, literal("Sin tema")).label("tema")
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")

    query = _envio_base_query(
        db,
        alcance_expr,
        anunciante_expr,
        Cronograma.canal,
        producto_expr,
        tema_expr,
        duracion_expr,
    )
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    if ranking_alcance:
        query = query.filter(_envio_alcance_expr() == ranking_alcance)
    if ranking_categoria:
        query = query.filter(_envio_categoria_expr() == ranking_categoria)

    rows = (
        query.group_by(
            alcance_expr,
            anunciante_expr,
            Cronograma.canal,
            producto_expr,
            tema_expr,
        )
        .order_by(duracion_expr.desc())
        .all()
    )

    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = _envio_ranking_key(row.alcance, row.anunciante, row.canal)
        if key not in main_keys:
            continue
        grouped[key].append(
            {
                "producto": row.producto or "Sin producto",
                "tema": row.tema or "Sin tema",
                "duracion_total": int(row.duracion_total or 0),
            }
        )

    return grouped


def _get_envio_side_rows(
    *,
    db: Session,
    total_duracion: int,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")
    alcance_expr = _envio_alcance_expr().label("alcance")
    query = _envio_base_query(db, alcance_expr, duracion_expr)
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=None,
    )
    rows = (
        query.filter(BaseAnunciante.alcance.isnot(None))
        .group_by(alcance_expr)
        .order_by(duracion_expr.desc())
        .all()
    )
    alcance_total_duracion = sum(int(row.duracion_total or 0) for row in rows)
    percentage_total = alcance_total_duracion or total_duracion

    return [
        {
            "label": row.alcance or "Sin alcance",
            "duracion_total": int(row.duracion_total or 0),
            "porcentaje": _percentage(row.duracion_total, percentage_total),
        }
        for row in rows
    ]


def _get_envio_category_side_rows(
    *,
    db: Session,
    total_duracion: int,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")
    categoria_expr = _envio_categoria_expr().label("categoria")
    query = _envio_base_query(db, categoria_expr, duracion_expr)
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=None,
    )
    rows = (
        query.filter(BaseAnunciante.categoria.isnot(None))
        .group_by(categoria_expr)
        .order_by(duracion_expr.desc())
        .all()
    )
    categoria_total_duracion = sum(int(row.duracion_total or 0) for row in rows)
    percentage_total = categoria_total_duracion or total_duracion

    return [
        {
            "label": row.categoria or "Sin categoría",
            "duracion_total": int(row.duracion_total or 0),
            "porcentaje": _percentage(row.duracion_total, percentage_total),
        }
        for row in rows
    ]


def _get_envio_channel_rows(
    *,
    db: Session,
    total_duracion: int,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")
    query = _envio_base_query(db, Cronograma.canal, duracion_expr)
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    rows = (
        query.group_by(Cronograma.canal)
        .order_by(duracion_expr.desc())
        .all()
    )

    return [
        {
            "canal": row.canal or "Sin canal",
            "duracion_total": int(row.duracion_total or 0),
            "porcentaje": _percentage(row.duracion_total, total_duracion),
            "color": _channel_color(row.canal),
        }
        for row in rows
    ]


def _get_envio_date_channel_rows(
    *,
    db: Session,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")
    query = _envio_base_query(db, Cronograma.fecha, Cronograma.canal, duracion_expr)
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    rows = (
        query.filter(Cronograma.fecha.isnot(None))
        .group_by(Cronograma.fecha, Cronograma.canal)
        .order_by(Cronograma.fecha.asc(), Cronograma.canal.asc())
        .all()
    )

    return [
        {
            "fecha": row.fecha,
            "canal": row.canal or "Sin canal",
            "duracion_total": int(row.duracion_total or 0),
        }
        for row in rows
    ]


def _get_envio_hour_channel_rows(
    *,
    db: Session,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")
    query = _envio_base_query(db, Cronograma.hora_inicio, Cronograma.canal, duracion_expr)
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    rows = (
        query.filter(Cronograma.hora_inicio.isnot(None))
        .group_by(Cronograma.hora_inicio, Cronograma.canal)
        .all()
    )

    grouped: defaultdict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        hour = _normalize_hour(row.hora_inicio)
        if hour is None:
            continue

        grouped[(hour, row.canal or "Sin canal")] += int(row.duracion_total or 0)

    return [
        {"hora": hour, "canal": canal, "duracion_total": duracion_total}
        for (hour, canal), duracion_total in sorted(grouped.items())
    ]


def _get_envio_weekday_channel_rows(
    *,
    db: Session,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    alcances: list[str],
    categorias: list[str],
    anunciantes: list[str],
    tipos_dia: list[str],
    prom_canal: str | None,
) -> list[dict[str, object]]:
    duracion_expr = func.sum(Cronograma.duracion).label("duracion_total")
    query = _envio_base_query(db, Cronograma.dia_semana, Cronograma.canal, duracion_expr)
    query = _apply_envio_filters(
        query,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        alcances=alcances,
        categorias=categorias,
        anunciantes=anunciantes,
        tipos_dia=tipos_dia,
        prom_canal=prom_canal,
    )
    rows = (
        query.filter(Cronograma.dia_semana.isnot(None))
        .group_by(Cronograma.dia_semana, Cronograma.canal)
        .order_by(Cronograma.dia_semana.asc(), Cronograma.canal.asc())
        .all()
    )

    return [
        {
            "dia": WEEKDAY_LABELS.get(row.dia_semana, str(row.dia_semana)),
            "canal": row.canal or "Sin canal",
            "duracion_total": int(row.duracion_total or 0),
        }
        for row in rows
    ]


def _get_envio_filter_options(db: Session) -> dict[str, object]:
    alcance_expr = _clean_sql_text(BaseAnunciante.alcance)
    categoria_expr = _clean_sql_text(BaseAnunciante.categoria)
    tipo_dia_expr = _envio_tipo_dia_expr()

    advertiser_rows = (
        db.query(
            BaseAnunciante.anunciante,
            alcance_expr.label("alcance"),
            categoria_expr.label("categoria"),
        )
        .filter(BaseAnunciante.anunciante.isnot(None))
        .order_by(BaseAnunciante.anunciante.asc())
        .all()
    )
    advertiser_metadata: dict[str, dict[str, set[str]]] = {}
    for row in advertiser_rows:
        advertiser = str(row.anunciante or "").strip()
        if not advertiser:
            continue
        metadata = advertiser_metadata.setdefault(
            advertiser,
            {"alcances": set(), "categorias": set()},
        )
        if row.alcance:
            metadata["alcances"].add(str(row.alcance).strip())
        if row.categoria:
            metadata["categorias"].add(str(row.categoria).strip())

    return {
        "alcances": _clean_multi_values(
            [
                row[0]
                for row in db.query(alcance_expr)
                .filter(BaseAnunciante.alcance.isnot(None))
                .distinct()
                .order_by(alcance_expr.asc())
                .all()
            ]
        ),
        "categorias": _clean_multi_values(
            [
                row[0]
                for row in db.query(categoria_expr)
                .filter(BaseAnunciante.categoria.isnot(None))
                .distinct()
                .order_by(categoria_expr.asc())
                .all()
            ]
        ),
        "anunciantes": list(advertiser_metadata),
        "anunciante_metadata": {
            advertiser: {
                "alcances": sorted(values["alcances"]),
                "categorias": sorted(values["categorias"]),
            }
            for advertiser, values in advertiser_metadata.items()
        },
        "tipos_dia": _clean_multi_values(
            [
                row[0]
                for row in db.query(tipo_dia_expr)
                .filter(Cronograma.tipo_dia.isnot(None))
                .distinct()
                .order_by(tipo_dia_expr.asc())
                .all()
            ]
        ),
        "prom_canales": [
            row[0]
            for row in db.query(Cronograma.prom_canal)
            .filter(Cronograma.prom_canal.isnot(None))
            .distinct()
            .order_by(Cronograma.prom_canal.asc())
            .all()
        ],
        "canales": _clean_multi_values([
            row[0]
            for row in db.query(Cronograma.canal)
            .filter(Cronograma.canal.isnot(None))
            .distinct()
            .order_by(Cronograma.canal.asc())
            .all()
        ]),
    }


def _clean_multi_values(values: list[str] | None) -> list[str]:
    if not values:
        return []

    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in cleaned:
            cleaned.append(text)

    return cleaned


def _normalize_all_selected(selected: list[str], options: list[str]) -> list[str]:
    if not selected:
        return []

    option_values = {str(option).strip() for option in options if str(option).strip()}
    selected_values = {str(value).strip() for value in selected if str(value).strip()}
    return [] if option_values and selected_values >= option_values else selected


def _percentage(value: object, total: int) -> float:
    return (int(value or 0) / total * 100) if total else 0.0


def _channel_color(channel: str | None) -> str:
    if not channel:
        return CHANNEL_COLORS["Sin canal"]

    if channel in CHANNEL_COLORS:
        return CHANNEL_COLORS[channel]

    color_index = sum(ord(character) for character in channel) % len(PIE_COLORS)
    return PIE_COLORS[color_index]


def _normalize_hour(value: object) -> str | None:
    if value in {None, ""}:
        return None

    text = str(value).strip()
    if not text:
        return None

    if ":" in text:
        text = text.split(":", 1)[0]
    else:
        digits_only = "".join(character for character in text if character.isdigit())
        if len(digits_only) == 5:
            digits_only = f"0{digits_only}"
        text = digits_only[:2]

    digits = "".join(character for character in text if character.isdigit())
    if not digits:
        return None

    hour = int(digits)
    if hour < 0 or hour > 23:
        return None

    return f"{hour:02d}"


def _parse_optional_int(value: object) -> int | None:
    if value in {None, ""}:
        return None

    text = str(value).strip()
    return int(text) if text.isdigit() else None


def _parse_int_list(value: object) -> list[int]:
    if value in {None, ""}:
        return []

    parsed = []
    for item in str(value).split(","):
        text = item.strip()
        if text.isdigit():
            parsed.append(int(text))

    return parsed


def _build_line_chart(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        return {"series": [], "x_labels": [], "y_labels": [], "width": 760, "height": 280}

    width = 760
    height = 280
    padding_left = 58
    padding_right = 24
    padding_top = 26
    padding_bottom = 48
    plot_width = width - padding_left - padding_right
    plot_height = height - padding_top - padding_bottom

    dates = sorted({row["fecha"] for row in rows if row["fecha"] is not None})
    channels = sorted({str(row["canal"]) for row in rows})
    values_by_key = {
        (row["fecha"], str(row["canal"])): int(row["duracion_total"] or 0)
        for row in rows
    }
    max_value = max(values_by_key.values()) if values_by_key else 0
    max_value = max(max_value, 1)

    def x_position(index: int) -> float:
        if len(dates) == 1:
            return padding_left + plot_width / 2
        return padding_left + (plot_width * index / (len(dates) - 1))

    def y_position(value: int) -> float:
        return padding_top + plot_height - (plot_height * value / max_value)

    series = []
    for channel in channels:
        points = []
        for index, current_date in enumerate(dates):
            value = values_by_key.get((current_date, channel), 0)
            points.append(
                {
                    "x": round(x_position(index), 2),
                    "y": round(y_position(value), 2),
                    "value": value,
                }
            )

        series.append(
            {
                "channel": channel,
                "color": _channel_color(channel),
                "points": points,
                "polyline": " ".join(f"{point['x']},{point['y']}" for point in points),
            }
        )

    y_labels = []
    for step in range(0, 5):
        value = round(max_value * step / 4)
        y_labels.append({"value": value, "y": round(y_position(value), 2)})

    if len(dates) <= 10:
        x_label_indexes = list(range(len(dates)))
    else:
        x_label_indexes = sorted({
            round(step * (len(dates) - 1) / 9)
            for step in range(10)
        })

    return {
        "series": series,
        "x_labels": [
            {
                "label": dates[index].strftime("%d/%m"),
                "x": round(x_position(index), 2),
            }
            for index in x_label_indexes
        ],
        "y_labels": y_labels,
        "width": width,
        "height": height,
        "plot": {
            "x": padding_left,
            "y": padding_top,
            "width": plot_width,
            "height": plot_height,
        },
    }


def _build_grouped_bar_chart(
    rows: list[dict[str, object]],
    *,
    label_key: str,
    label_order: list[str] | None,
) -> dict[str, object]:
    if not rows and label_order is None:
        return {"groups": [], "channels": [], "max_value": 0}

    channel_names = sorted({str(row["canal"]) for row in rows})
    if label_order is None:
        labels = sorted({str(row[label_key]) for row in rows})
    else:
        labels = label_order

    values_by_key = {
        (str(row[label_key]), str(row["canal"])): int(row["duracion_total"] or 0)
        for row in rows
    }
    max_value = max(values_by_key.values()) if values_by_key else 0
    max_value = max(max_value, 1)

    groups = []
    for label in labels:
        bars = []
        for channel in channel_names:
            value = values_by_key.get((label, channel), 0)
            bars.append(
                {
                    "channel": channel,
                    "value": value,
                    "height": round(value / max_value * 100, 2),
                    "color": _channel_color(channel),
                }
            )
        groups.append({"label": label, "bars": bars})

    return {
        "groups": groups,
        "channels": [
            {"name": channel, "color": _channel_color(channel)}
            for channel in channel_names
        ],
        "max_value": max_value,
    }


def _build_pie_chart_segments(share_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    if not share_rows:
        return []

    center_x = 110
    center_y = 110
    radius = 86
    start_angle = -90.0
    segments: list[dict[str, object]] = []

    for row in share_rows:
        percentage = float(row["porcentaje"])
        sweep_angle = 360 * (percentage / 100)
        end_angle = start_angle + sweep_angle

        segments.append(
            {
                "path": _describe_arc(
                    center_x=center_x,
                    center_y=center_y,
                    radius=radius,
                    start_angle=start_angle,
                    end_angle=end_angle,
                ),
                "color": row["color"],
                "canal": row["canal"],
                "porcentaje": percentage,
            }
        )
        start_angle = end_angle

    return segments


def _describe_arc(
    *,
    center_x: float,
    center_y: float,
    radius: float,
    start_angle: float,
    end_angle: float,
) -> str:
    start_x, start_y = _polar_to_cartesian(center_x, center_y, radius, end_angle)
    end_x, end_y = _polar_to_cartesian(center_x, center_y, radius, start_angle)
    large_arc_flag = 1 if end_angle - start_angle > 180 else 0

    return (
        f"M {center_x} {center_y} "
        f"L {start_x} {start_y} "
        f"A {radius} {radius} 0 {large_arc_flag} 0 {end_x} {end_y} Z"
    )


def _polar_to_cartesian(
    center_x: float, center_y: float, radius: float, angle_degrees: float
) -> tuple[float, float]:
    angle_radians = math.radians(angle_degrees)
    return (
        center_x + radius * math.cos(angle_radians),
        center_y + radius * math.sin(angle_radians),
    )
