import argparse
import sys
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.session import SessionLocal
from app.models.base_anunciantes import BaseAnunciante
from app.models.base_productos import BaseProducto
from app.models.cronogramas import Cronograma


DEFAULT_XLSX_PATH = "ejemplos/base_productos_completa (7).xlsx"
XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Completa base_productos/base_anunciantes desde la base modelo XLSX."
    )
    parser.add_argument("--xlsx", default=DEFAULT_XLSX_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--include-unused",
        action="store_true",
        help="Tambien inserta codigos del XLSX que no aparecen en cronogramas.",
    )
    args = parser.parse_args()

    records, conflicts = read_model_records(Path(args.xlsx))
    conflict_codes = set(conflicts)

    db = SessionLocal()
    try:
        summary = sync_records(
            db=db,
            records=records,
            conflict_codes=conflict_codes,
            apply_changes=args.apply,
            only_used=not args.include_unused,
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print_summary(summary, conflicts=conflicts, applied=args.apply)


def sync_records(
    *,
    db,
    records: dict[int, dict[str, str | int]],
    conflict_codes: set[int],
    apply_changes: bool,
    only_used: bool,
) -> dict[str, object]:
    current_products = {
        item.cod_producto: item
        for item in db.query(BaseProducto)
        .filter(BaseProducto.cod_producto.in_(list(records)))
        .all()
    }
    advertisers = db.query(BaseAnunciante).all()
    advertisers_by_name = {
        normalize_key(item.anunciante): item
        for item in advertisers
        if item.anunciante
    }
    usage_by_code = get_cronograma_usage_by_code(db, records.keys())

    plan: list[dict[str, object]] = []
    skipped_conflict = 0
    skipped_unused = 0

    for cod_producto, source in sorted(records.items()):
        if cod_producto in conflict_codes:
            skipped_conflict += 1
            continue

        usage = usage_by_code.get(cod_producto, empty_usage())
        if only_used and usage["filas"] == 0:
            skipped_unused += 1
            continue

        product = current_products.get(cod_producto)
        advertiser = None
        if product and product.id_anunciante:
            advertiser = db.get(BaseAnunciante, product.id_anunciante)

        source_advertiser = None
        if product is None or product.id_anunciante is None:
            source_advertiser = find_or_build_advertiser(
                db=db,
                source=source,
                advertisers_by_name=advertisers_by_name,
                apply_changes=apply_changes,
                plan=plan,
            )

        if product is None:
            product = BaseProducto(
                cod_producto=cod_producto,
                producto=source["producto"] or None,
                tema=source["tema"] or None,
                id_anunciante=source_advertiser.id_anunciante if source_advertiser else None,
            )
            if apply_changes:
                db.add(product)
            plan.append(
                build_plan_row(
                    action="insert_base_producto",
                    cod_producto=cod_producto,
                    source=source,
                    usage=usage,
                )
            )
            continue

        changes = []
        if not clean_text(product.producto) and source["producto"]:
            changes.append(("producto", "", source["producto"]))
            if apply_changes:
                product.producto = source["producto"]
        if not clean_text(product.tema) and source["tema"]:
            changes.append(("tema", "", source["tema"]))
            if apply_changes:
                product.tema = source["tema"]
        if product.id_anunciante is None and source_advertiser is not None:
            changes.append(("id_anunciante", "", source_advertiser.id_anunciante))
            if apply_changes:
                product.id_anunciante = source_advertiser.id_anunciante

        target_advertiser = advertiser or source_advertiser
        if target_advertiser is not None:
            if not clean_text(target_advertiser.alcance) and source["alcance"]:
                changes.append(("alcance", "", source["alcance"]))
                if apply_changes:
                    target_advertiser.alcance = source["alcance"]
            if not clean_text(target_advertiser.categoria) and source["categoria"]:
                changes.append(("categoria", "", source["categoria"]))
                if apply_changes:
                    target_advertiser.categoria = source["categoria"]

        for field, old_value, new_value in changes:
            plan.append(
                build_plan_row(
                    action=f"update_{field}",
                    cod_producto=cod_producto,
                    source=source,
                    usage=usage,
                    old_value=old_value,
                    new_value=new_value,
                )
            )

    return {
        "plan": plan,
        "skipped_conflict": skipped_conflict,
        "skipped_unused": skipped_unused,
    }


def find_or_build_advertiser(
    *,
    db,
    source: dict[str, str | int],
    advertisers_by_name: dict[str, BaseAnunciante],
    apply_changes: bool,
    plan: list[dict[str, object]],
) -> BaseAnunciante | None:
    cliente = clean_text(source["cliente"])
    if not cliente:
        return None

    key = normalize_key(cliente)
    existing = advertisers_by_name.get(key)
    if existing is not None:
        return existing

    advertiser = BaseAnunciante(
        anunciante=cliente,
        alcance=source["alcance"] or None,
        categoria=source["categoria"] or None,
    )
    if apply_changes:
        db.add(advertiser)
        db.flush()
    advertisers_by_name[key] = advertiser
    plan.append(
        {
            "action": "insert_base_anunciante",
            "cod_producto": source["cod_producto"],
            "producto": source["producto"],
            "tema": source["tema"],
            "cliente": cliente,
            "alcance": source["alcance"],
            "categoria": source["categoria"],
            "filas_cronogramas": "",
            "duracion_cronogramas": "",
            "ultima_fecha": "",
            "old_value": "",
            "new_value": cliente,
        }
    )
    return advertiser


def build_plan_row(
    *,
    action: str,
    cod_producto: int,
    source: dict[str, str | int],
    usage: dict[str, object],
    old_value: object = "",
    new_value: object = "",
) -> dict[str, object]:
    return {
        "action": action,
        "cod_producto": cod_producto,
        "producto": source["producto"],
        "tema": source["tema"],
        "cliente": source["cliente"],
        "alcance": source["alcance"],
        "categoria": source["categoria"],
        "filas_cronogramas": usage["filas"],
        "duracion_cronogramas": usage["duracion"],
        "ultima_fecha": usage["ultima_fecha"] or "",
        "old_value": old_value,
        "new_value": new_value,
    }


def get_cronograma_usage_by_code(db, codigos: object) -> dict[int, dict[str, object]]:
    rows = (
        db.query(
            Cronograma.cod_prod_int.label("cod_producto"),
            func.count(Cronograma.id),
            func.sum(Cronograma.duracion),
            func.max(Cronograma.fecha),
        )
        .filter(Cronograma.cod_prod.op("REGEXP")("^[0-9]+$"))
        .filter(Cronograma.cod_prod_int.in_(list(codigos)))
        .group_by(Cronograma.cod_prod_int)
        .all()
    )

    return {
        int(cod_producto): {
            "filas": int(filas or 0),
            "duracion": int(duracion or 0),
            "ultima_fecha": ultima_fecha,
        }
        for cod_producto, filas, duracion, ultima_fecha in rows
    }


def empty_usage() -> dict[str, object]:
    return {
        "filas": 0,
        "duracion": 0,
        "ultima_fecha": None,
    }


def read_model_records(path: Path) -> tuple[dict[int, dict[str, str | int]], dict[int, list[dict[str, str | int]]]]:
    rows = read_xlsx_rows(path)
    headers = [clean_text(value) for value in rows[0]]
    records: dict[int, dict[str, str | int]] = {}
    conflicts: dict[int, list[dict[str, str | int]]] = {}

    for row in rows[1:]:
        row = row + [""] * (len(headers) - len(row))
        item = dict(zip(headers, row))
        cod_producto = parse_code(item.get("Cod.Prod."))
        if cod_producto is None:
            continue

        record = {
            "cod_producto": cod_producto,
            "producto": clean_text(item.get("Producto")),
            "tema": clean_text(item.get("Tema")),
            "alcance": clean_text(item.get("Alcance")),
            "categoria": clean_text(item.get("Prom.Canal")),
            "cliente": clean_text(item.get("CLIENTE")),
        }
        existing = records.get(cod_producto)
        if existing is not None and existing != record:
            conflicts.setdefault(cod_producto, [existing]).append(record)
            continue
        records[cod_producto] = record

    return records, conflicts


def read_xlsx_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = read_shared_strings(archive)
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows: list[list[str]] = []
    for row in sheet.findall(".//m:sheetData/m:row", XLSX_NS):
        values: dict[int, str] = {}
        for cell in row.findall("m:c", XLSX_NS):
            index = column_index(cell.attrib.get("r", ""))
            cell_type = cell.attrib.get("t")
            value_node = cell.find("m:v", XLSX_NS)
            value = ""
            if cell_type == "s" and value_node is not None and value_node.text is not None:
                value = shared_strings[int(value_node.text)]
            elif value_node is not None and value_node.text is not None:
                value = value_node.text
            values[index] = value

        if values:
            rows.append([values.get(index, "") for index in range(max(values) + 1)])

    return rows


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(text.text or "" for text in item.findall(".//m:t", XLSX_NS))
        for item in root.findall("m:si", XLSX_NS)
    ]


def column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter.upper()) - 64
    return index - 1


def parse_code(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None

    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def clean_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_key(value: object) -> str:
    return clean_text(value).casefold()


def print_summary(
    summary: dict[str, object],
    *,
    conflicts: dict[int, list[dict[str, str | int]]],
    applied: bool,
) -> None:
    plan = summary["plan"]
    print("mode", "apply" if applied else "dry-run")
    print("planned_actions", len(plan))
    print("skipped_conflict_codes", summary["skipped_conflict"])
    print("skipped_unused_codes", summary["skipped_unused"])
    print("xlsx_conflict_codes", len(conflicts))
    print(
        "action\tcod_producto\tproducto\ttema\tcliente\talcance\tcategoria\t"
        "filas_cronogramas\tduracion_cronogramas\tultima_fecha\told_value\tnew_value"
    )
    for row in plan:
        print(
            "\t".join(
                str(row.get(key, "") or "")
                for key in [
                    "action",
                    "cod_producto",
                    "producto",
                    "tema",
                    "cliente",
                    "alcance",
                    "categoria",
                    "filas_cronogramas",
                    "duracion_cronogramas",
                    "ultima_fecha",
                    "old_value",
                    "new_value",
                ]
            )
        )


if __name__ == "__main__":
    main()
