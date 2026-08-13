import json
import re
import hashlib
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

import pandas as pd
from sqlalchemy import tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.archivos_ingesta import ArchivoIngesta
from app.models.cronogramas import Cronograma
from app.services.drive_service import DriveService, DriveServiceError


class ProcessingServiceError(Exception):
    """Error controlado para fallas de lectura o inspeccion del archivo local."""


class DuplicateArchivoIngestaError(ProcessingServiceError):
    """Error controlado cuando el archivo ya fue registrado en archivos_ingesta."""


class ProcessingService:
    """Servicio inicial para inspeccionar archivos .xls descargados localmente."""

    FECHA_DE_TOMA_PATTERN = re.compile(r"FECHA\s+DE\s+TOMA\s*:\s*(.+)", re.IGNORECASE)
    DATE_PATTERN = re.compile(r"(\d{2}/\d{2}/\d{2,4})")
    PROGRAM_ROW_PATTERN = re.compile(
        r"^\s*\d{6}\s+\d{6}\s+\S+\s+(?P<programa>.+?)\s+TEOR\s*:",
        re.IGNORECASE,
    )
    HEADER_ROW_MARKERS = {
        "HORA INICIO",
        "HORA FIN",
        "COD.PROD.",
        "PRODUCTO",
        "TEMA",
        "DURACION",
    }
    FINAL_CRONOGRAMA_COLUMNS = [
        "archivo_ingesta_id",
        "hora_inicio",
        "hora_fin",
        "cod_prod",
        "producto",
        "tema",
        "duracion",
        "t_compra",
        "t_material",
        "columna_extra",
        "alcance",
        "prom_canal",
        "programa",
        "canal",
        "fecha",
        "dia_semana",
        "tipo_dia",
    ]
    CHANNEL_MAPPING = {
        "CB 10 CORDOBA 10": "Canal 10",
        "CB 08 CORDOBA 8": "Canal 8",
        "CB 12 CORDOBA EL DOCE": "Canal 12",
    }
    COMPLEMENTARY_CHANNEL_MAPPING = {
        "TELEFE": ("Canal 8", "1551"),
        "CANAL 8": ("Canal 8", "1551"),
        "CANAL 10": ("Canal 10", "1553"),
        "CANAL 12": ("Canal 12", "1554"),
    }

    def load_complementary_file_to_db(
        self,
        *,
        db: Session,
        filename: str,
        content: bytes,
    ) -> dict[str, object]:
        if not content:
            raise ProcessingServiceError("El archivo complementario está vacío.")

        suffix = Path(filename).suffix.lower()
        try:
            if suffix == ".csv":
                dataframe = pd.read_csv(BytesIO(content), sep=None, engine="python", dtype=object)
            elif suffix == ".xls":
                dataframe = pd.read_excel(BytesIO(content), dtype=object, engine="xlrd")
            elif suffix == ".xlsx":
                dataframe = pd.read_excel(BytesIO(content), dtype=object, engine="openpyxl")
            else:
                raise ProcessingServiceError("El archivo debe ser CSV, XLS o XLSX.")
        except ProcessingServiceError:
            raise
        except Exception as exc:
            raise ProcessingServiceError("No se pudo leer el archivo complementario.") from exc

        required = {"Canal", "Fecha", "Hora Inicio", "Producto / Tema", "Dur.Av.", "Mat.", "Programa", "Tipo Compra"}
        dataframe.columns = [str(column).strip() for column in dataframe.columns]
        missing = sorted(required - set(dataframe.columns))
        if missing:
            raise ProcessingServiceError(f"Faltan columnas requeridas: {', '.join(missing)}.")

        parsed_rows: list[dict[str, object]] = []
        detected_pairs: set[tuple[date, str]] = set()
        channel_codes: set[str] = set()
        for index, source_row in dataframe.iterrows():
            try:
                raw_channel = self._normalize_complementary_text(source_row["Canal"])
                channel_key = raw_channel.replace(" CORDOBA", "").strip()
                channel_info = self.COMPLEMENTARY_CHANNEL_MAPPING.get(channel_key)
                if channel_info is None:
                    raise ValueError(f"canal no reconocido: {raw_channel}")
                channel, channel_code = channel_info
                row_date = pd.to_datetime(source_row["Fecha"], dayfirst=True, errors="raise").date()
                start_time = self._parse_complementary_time(source_row["Hora Inicio"])
                duration = self._parse_duracion_for_mysql(source_row["Dur.Av."])
                if duration is None:
                    raise ValueError("duración vacía")
                start_datetime = datetime.combine(row_date, start_time)
                end_time = (start_datetime + timedelta(seconds=duration)).time()
                product_topic = self._clean_complementary_value(source_row["Producto / Tema"]) or ""
                product, separator, topic = product_topic.partition("/")
                if not product.strip():
                    raise ValueError("producto vacío")

                parsed_rows.append({
                    "hora_inicio": start_time.strftime("%H:%M:%S"),
                    "hora_fin": end_time.strftime("%H:%M:%S"),
                    "cod_prod": None,
                    "producto": product.strip(),
                    "tema": topic.strip() if separator and topic.strip() else None,
                    "duracion": duration,
                    "t_compra": self._clean_complementary_value(source_row["Tipo Compra"]),
                    "t_material": self._clean_complementary_value(source_row["Mat."]),
                    "columna_extra": None,
                    "alcance": None,
                    "prom_canal": None,
                    "programa": self._clean_complementary_value(source_row["Programa"]),
                    "canal": channel,
                    "fecha": row_date,
                    "dia_semana": row_date.isoweekday(),
                    "tipo_dia": "Fin de semana" if row_date.weekday() >= 5 else "Hábil",
                })
                detected_pairs.add((row_date, channel))
                channel_codes.add(channel_code)
            except Exception as exc:
                raise ProcessingServiceError(f"Fila {index + 2} inválida: {exc}.") from exc

        if not parsed_rows:
            raise ProcessingServiceError("No se encontraron filas para importar.")

        existing_pairs = set(
            db.query(Cronograma.fecha, Cronograma.canal)
            .filter(
                tuple_(Cronograma.fecha, Cronograma.canal).in_(detected_pairs)
            )
            .distinct()
            .all()
        )
        accepted_rows = [
            row for row in parsed_rows
            if (row["fecha"], row["canal"]) not in existing_pairs
        ]
        accepted_pairs = detected_pairs - existing_pairs
        if not accepted_rows:
            raise ProcessingServiceError("Todas las combinaciones de fecha y canal ya existen en cronogramas.")

        digest = hashlib.sha256(content).hexdigest()
        drive_file_id = f"complementario::{digest}"
        if db.query(ArchivoIngesta.id).filter(ArchivoIngesta.drive_file_id.like(f"{drive_file_id}::%")).first():
            raise DuplicateArchivoIngestaError("Este archivo complementario ya fue procesado.")

        try:
            code_by_channel = {value[0]: value[1] for value in self.COMPLEMENTARY_CHANNEL_MAPPING.values()}
            rows_by_pair: dict[tuple[date, str], list[dict[str, object]]] = {}
            ingesta_ids: list[int] = []
            for row in accepted_rows:
                rows_by_pair.setdefault((row["fecha"], row["canal"]), []).append(row)
            for (row_date, channel), pair_rows in rows_by_pair.items():
                ingesta = ArchivoIngesta(
                    drive_file_id=f"{drive_file_id}::{row_date.isoformat()}::{code_by_channel[channel]}",
                    nombre_archivo=filename,
                    canal_codigo=code_by_channel[channel],
                    fecha_nombre_archivo=row_date,
                    cantidad_dias_detectados=1,
                    fechas_detectadas=json.dumps([row_date.isoformat()]),
                    estado="procesado",
                    procesamiento_completo=True,
                    fecha_inicio_proceso=datetime.now(),
                    fecha_fin_proceso=datetime.now(),
                    filas_procesadas=len(pair_rows),
                )
                db.add(ingesta)
                db.flush()
                ingesta_ids.append(ingesta.id)
                for row in pair_rows:
                    row["archivo_ingesta_id"] = ingesta.id
                db.add_all(Cronograma(**row) for row in pair_rows)
            db.commit()
        except Exception as exc:
            db.rollback()
            raise ProcessingServiceError("No se pudo guardar la carga complementaria.") from exc

        return {
            "filas_insertadas": len(accepted_rows),
            "bloques_insertados": len(accepted_pairs),
            "bloques_omitidos": len(existing_pairs),
            "productos": {(str(row["producto"]), str(row["tema"] or "")) for row in accepted_rows},
            "archivo_ingesta_ids": ingesta_ids,
        }

    def _normalize_complementary_text(self, value: object) -> str:
        text = str(value or "").upper().strip()
        text = text.replace("Ó", "O")
        return " ".join(text.split())

    def _clean_complementary_value(self, value: object) -> str | None:
        if value is None or pd.isna(value):
            return None
        text = str(value).strip()
        return text or None

    def _parse_complementary_time(self, value: object) -> time:
        if isinstance(value, time):
            return value
        if isinstance(value, (datetime, pd.Timestamp)):
            return value.time()
        if isinstance(value, (int, float, Decimal)) and not pd.isna(value):
            seconds = round(float(value) * 24 * 60 * 60) % (24 * 60 * 60)
            return (datetime.min + timedelta(seconds=seconds)).time()
        text = str(value).strip()
        extended_match = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", text)
        if extended_match:
            hours, minutes, seconds = (int(part or 0) for part in extended_match.groups())
            if minutes > 59 or seconds > 59:
                raise ValueError(f"hora inválida: {text}")
            total_seconds = (hours * 3600 + minutes * 60 + seconds) % (24 * 60 * 60)
            return (datetime.min + timedelta(seconds=total_seconds)).time()
        return pd.to_datetime(text, errors="raise").time()

    def inspect_xls_file(self, file_path: str) -> dict[str, object]:
        path = Path(file_path)
        if not path.is_file():
            raise ProcessingServiceError(
                "No se encontro el archivo local indicado para procesar."
            )

        dataframe = self._read_xls_file(path)
        normalized_rows = [self._row_to_text(row) for _, row in dataframe.iterrows()]
        block_start_indexes = self._find_block_start_indexes(normalized_rows)

        blocks: list[dict[str, object]] = []
        total_rows = len(normalized_rows)

        for block_number, start_index in enumerate(block_start_indexes, start=1):
            end_index = (
                block_start_indexes[block_number] - 1
                if block_number < len(block_start_indexes)
                else total_rows - 1
            )

            fecha_row_text = normalized_rows[start_index]
            parsed_info = self._extract_fecha_and_channel(fecha_row_text)
            cronograma_rows = self._extract_cronograma_rows_from_block(
                dataframe=dataframe,
                start_index=start_index,
                end_index=end_index,
                fecha_detectada=parsed_info["fecha_detectada"],
                canal_detectado=parsed_info["canal_detectado"],
            )

            blocks.append(
                {
                    "block_number": block_number,
                    "start_index": start_index,
                    "end_index": end_index,
                    "fecha_de_toma_text": fecha_row_text,
                    "fecha_detectada": parsed_info["fecha_detectada"],
                    "canal_detectado": parsed_info["canal_detectado"],
                    "programas_detectados": self._collect_detected_programs(cronograma_rows),
                    "cronograma_rows_count": len(cronograma_rows),
                    "cronograma_rows_preview": cronograma_rows[:5],
                }
            )

        return {
            "file_path": str(path),
            "file_name": path.name,
            "total_rows": total_rows,
            "total_blocks": len(blocks),
            "blocks": blocks,
        }

    def extract_cronograma_rows(self, file_path: str) -> dict[str, object]:
        path = Path(file_path)
        if not path.is_file():
            raise ProcessingServiceError(
                "No se encontro el archivo local indicado para procesar."
            )

        dataframe = self._read_xls_file(path)
        normalized_rows = [self._row_to_text(row) for _, row in dataframe.iterrows()]
        block_start_indexes = self._find_block_start_indexes(normalized_rows)

        blocks: list[dict[str, object]] = []
        total_rows = len(normalized_rows)

        for block_number, start_index in enumerate(block_start_indexes, start=1):
            end_index = (
                block_start_indexes[block_number] - 1
                if block_number < len(block_start_indexes)
                else total_rows - 1
            )

            fecha_row_text = normalized_rows[start_index]
            parsed_info = self._extract_fecha_and_channel(fecha_row_text)
            cronograma_rows = self._extract_cronograma_rows_from_block(
                dataframe=dataframe,
                start_index=start_index,
                end_index=end_index,
                fecha_detectada=parsed_info["fecha_detectada"],
                canal_detectado=parsed_info["canal_detectado"],
            )

            blocks.append(
                {
                    "block_number": block_number,
                    "start_index": start_index,
                    "end_index": end_index,
                    "fecha_de_toma_text": fecha_row_text,
                    "fecha_detectada": parsed_info["fecha_detectada"],
                    "canal_detectado": parsed_info["canal_detectado"],
                    "rows": cronograma_rows,
                }
            )

        return {
            "file_path": str(path),
            "file_name": path.name,
            "total_blocks": len(blocks),
            "blocks": blocks,
        }

    def build_cronogramas_dataframe(
        self, file_path: str, archivo_ingesta_id: int | None = None
    ) -> pd.DataFrame:
        extracted = self.extract_cronograma_rows(file_path)
        return self._build_dataframe_from_extracted(
            extracted=extracted,
            archivo_ingesta_id=archivo_ingesta_id,
        )

    def load_monitor_file_to_db(
        self,
        *,
        db: Session,
        file_id: str | None = None,
        local_path: str | None = None,
    ) -> dict[str, object]:
        drive_file_id = self._build_drive_file_id(file_id=file_id, local_path=local_path)
        existing = (
            db.query(ArchivoIngesta)
            .filter(ArchivoIngesta.drive_file_id == drive_file_id)
            .first()
        )
        if existing is not None:
            raise DuplicateArchivoIngestaError(
                "Ya existe un registro en archivos_ingesta para ese drive_file_id."
            )

        source = self._resolve_source(file_id=file_id, local_path=local_path)

        now = datetime.now()
        archivo_ingesta = ArchivoIngesta(
            drive_file_id=source["drive_file_id"],
            nombre_archivo=source["nombre_archivo"],
            canal_codigo=source["canal_codigo"],
            fecha_nombre_archivo=source["fecha_nombre_archivo"],
            estado="procesando",
            procesamiento_completo=False,
            fecha_inicio_proceso=now,
        )
        db.add(archivo_ingesta)
        db.commit()
        db.refresh(archivo_ingesta)

        try:
            extracted = self.extract_cronograma_rows(source["local_path"])
            fechas_detectadas = self._collect_detected_dates(extracted["blocks"])
            self._validate_filename_date_matches_detected_dates(
                nombre_archivo=source["nombre_archivo"],
                fecha_nombre_archivo=source["fecha_nombre_archivo"],
                fechas_detectadas=fechas_detectadas,
            )
            dataframe = self._build_dataframe_from_extracted(
                extracted=extracted,
                archivo_ingesta_id=archivo_ingesta.id,
            )
            records = dataframe.to_dict(orient="records")

            if records:
                db.add_all([Cronograma(**record) for record in records])

            archivo_ingesta.estado = "procesado"
            archivo_ingesta.procesamiento_completo = True
            archivo_ingesta.fecha_fin_proceso = datetime.now()
            archivo_ingesta.filas_procesadas = len(records)
            archivo_ingesta.cantidad_dias_detectados = len(extracted["blocks"])
            archivo_ingesta.fechas_detectadas = json.dumps(fechas_detectadas, ensure_ascii=True)
            db.commit()
        except Exception as exc:
            db.rollback()
            self._mark_archivo_ingesta_as_error(
                db=db,
                archivo_ingesta=archivo_ingesta,
                error_detail=str(exc),
            )
            raise

        return {
            "status": "procesado",
            "archivo_ingesta_id": archivo_ingesta.id,
            "filas_insertadas": len(records),
            "cantidad_bloques": len(extracted["blocks"]),
            "fechas_detectadas": fechas_detectadas,
        }

    def _build_dataframe_from_extracted(
        self,
        *,
        extracted: dict[str, object],
        archivo_ingesta_id: int | None = None,
    ) -> pd.DataFrame:
        all_rows: list[dict[str, object]] = []

        for block in extracted["blocks"]:
            all_rows.extend(block["rows"])

        dataframe = pd.DataFrame(all_rows)
        if dataframe.empty:
            return pd.DataFrame(columns=self.FINAL_CRONOGRAMA_COLUMNS)

        dataframe["archivo_ingesta_id"] = archivo_ingesta_id
        dataframe = self._normalize_cronogramas_dataframe(dataframe)
        self._validate_final_columns(dataframe)

        return dataframe[self.FINAL_CRONOGRAMA_COLUMNS]

    def insert_cronogramas_from_file(
        self,
        *,
        file_path: str,
        db: Session,
        archivo_ingesta_id: int | None = None,
    ) -> dict[str, object]:
        dataframe = self.build_cronogramas_dataframe(
            file_path=file_path,
            archivo_ingesta_id=archivo_ingesta_id,
        )

        if dataframe.empty:
            return {
                "status": "success",
                "file_path": file_path,
                "inserted_rows": 0,
                "message": "No se encontraron filas validas para insertar en cronogramas.",
            }

        records = dataframe.to_dict(orient="records")

        try:
            db.add_all(Cronograma(**record) for record in records)
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise ProcessingServiceError(
                "Ocurrio un error al insertar filas en la tabla cronogramas."
            ) from exc

        return {
            "status": "success",
            "file_path": file_path,
            "inserted_rows": len(records),
            "archivo_ingesta_id": archivo_ingesta_id,
        }

    def _read_xls_file(self, path: Path) -> pd.DataFrame:
        try:
            return pd.read_excel(path, header=None, dtype=object, engine="xlrd")
        except Exception as exc:
            raise ProcessingServiceError(
                "No se pudo leer el archivo .xls local con pandas."
            ) from exc

    def _resolve_source(
        self, *, file_id: str | None, local_path: str | None
    ) -> dict[str, str | date | None]:
        if file_id:
            settings = get_settings()
            drive_service = DriveService(settings)
            try:
                download_result = drive_service.download_file(file_id=file_id)
            except DriveServiceError as exc:
                raise ProcessingServiceError(str(exc)) from exc

            return {
                "drive_file_id": file_id,
                "local_path": download_result["local_path"],
                "nombre_archivo": download_result["name"],
                "canal_codigo": self._extract_canal_codigo_from_filename(
                    download_result["name"]
                ),
                "fecha_nombre_archivo": self._extract_fecha_from_filename(
                    download_result["name"]
                ),
            }

        if local_path:
            path = Path(local_path)
            if not path.is_file():
                raise ProcessingServiceError(
                    "No se encontro el archivo local indicado para procesar."
                )

            return {
                "drive_file_id": f"local::{path.name}",
                "local_path": str(path),
                "nombre_archivo": path.name,
                "canal_codigo": self._extract_canal_codigo_from_filename(path.name),
                "fecha_nombre_archivo": self._extract_fecha_from_filename(path.name),
            }

        raise ProcessingServiceError("Debes indicar un file_id o un local_path.")

    def _build_drive_file_id(self, *, file_id: str | None, local_path: str | None) -> str:
        if file_id:
            return file_id

        if local_path:
            return f"local::{Path(local_path).name}"

        raise ProcessingServiceError("Debes indicar un file_id o un local_path.")

    def _mark_archivo_ingesta_as_error(
        self,
        *,
        db: Session,
        archivo_ingesta: ArchivoIngesta,
        error_detail: str,
    ) -> None:
        archivo_ingesta.estado = "error"
        archivo_ingesta.procesamiento_completo = False
        archivo_ingesta.fecha_fin_proceso = datetime.now()
        archivo_ingesta.error_detalle = error_detail
        db.add(archivo_ingesta)
        db.commit()

    def _collect_detected_dates(self, blocks: list[dict[str, object]]) -> list[str]:
        dates: list[str] = []

        for block in blocks:
            fecha_detectada = block.get("fecha_detectada")
            if fecha_detectada is None:
                continue

            normalized_date = self._parse_fecha_for_mysql(fecha_detectada).isoformat()
            if normalized_date not in dates:
                dates.append(normalized_date)

        return dates

    def _validate_filename_date_matches_detected_dates(
        self,
        *,
        nombre_archivo: str,
        fecha_nombre_archivo: date | None,
        fechas_detectadas: list[str],
    ) -> None:
        if fecha_nombre_archivo is None or not fechas_detectadas:
            return

        expected_date = fecha_nombre_archivo.isoformat()
        if expected_date in fechas_detectadas:
            return

        detected_dates = ", ".join(fechas_detectadas)
        raise ProcessingServiceError(
            "La fecha del nombre del archivo no coincide con las fechas detectadas "
            f"en el contenido. Archivo: {nombre_archivo}. "
            f"Fecha esperada: {expected_date}. Fechas detectadas: {detected_dates}."
        )

    def _find_block_start_indexes(self, normalized_rows: list[str]) -> list[int]:
        return [
            index
            for index, row_text in enumerate(normalized_rows)
            if "FECHA DE TOMA" in row_text.upper()
        ]

    def _normalize_cronogramas_dataframe(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        normalized_df = dataframe.copy()

        for column in self.FINAL_CRONOGRAMA_COLUMNS:
            if column not in normalized_df.columns:
                normalized_df[column] = None

        normalized_df["fecha"] = normalized_df["fecha"].apply(self._parse_fecha_for_mysql)
        normalized_df["duracion"] = normalized_df["duracion"].apply(
            self._parse_duracion_for_mysql
        )
        normalized_df["canal"] = normalized_df["canal"].apply(self._normalize_canal)
        normalized_df["dia_semana"] = normalized_df["fecha"].apply(
            lambda value: value.isoweekday() if value is not None else None
        )
        normalized_df["tipo_dia"] = normalized_df["fecha"].apply(self._derive_tipo_dia)

        normalized_df = normalized_df.where(pd.notnull(normalized_df), None)
        return normalized_df

    def _validate_final_columns(self, dataframe: pd.DataFrame) -> None:
        missing_columns = [
            column for column in self.FINAL_CRONOGRAMA_COLUMNS if column not in dataframe.columns
        ]
        if missing_columns:
            missing = ", ".join(missing_columns)
            raise ProcessingServiceError(
                f"Faltan columnas esperadas en el DataFrame final: {missing}."
            )

    def _parse_fecha_for_mysql(self, value: object):
        if value in {None, ""}:
            return None

        text = str(value).strip()
        for fmt in ("%d/%m/%y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue

        raise ProcessingServiceError(
            f"No se pudo convertir la fecha '{text}' a un formato valido para MySQL."
        )

    def _parse_duracion_for_mysql(self, value: object) -> int | None:
        if value is None or pd.isna(value) or str(value).strip() == "":
            return None

        text = str(value).strip().replace(",", ".")
        try:
            numeric_value = Decimal(text)
        except InvalidOperation as error:
            raise ProcessingServiceError(
                f"La duración '{value}' no es un número válido."
            ) from error

        if (
            not numeric_value.is_finite()
            or numeric_value != numeric_value.to_integral_value()
            or numeric_value < 0
            or numeric_value > 2_147_483_647
        ):
            raise ProcessingServiceError(
                f"La duración '{value}' debe ser un entero no negativo válido para MySQL."
            )
        return int(numeric_value)

    def _extract_fecha_from_filename(self, file_name: str) -> date | None:
        match = re.search(r"(\d{1,2})_(\d{1,2})_(\d{4})", file_name)
        if not match:
            return None

        day, month, year = match.groups()

        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None

    def _extract_canal_codigo_from_filename(self, file_name: str) -> str | None:
        match = re.match(r"(\d+)_", file_name)
        if not match:
            return None

        return match.group(1)

    def _normalize_canal(self, value: object) -> str | None:
        if value in {None, ""}:
            return None

        text = " ".join(str(value).strip().split()).upper()
        return self.CHANNEL_MAPPING.get(text, str(value).strip())

    def _derive_tipo_dia(self, value) -> str | None:
        if value is None:
            return None

        return "Fin de semana" if value.weekday() >= 5 else "Hábil"

    def _row_to_text(self, row: pd.Series) -> str:
        values: list[str] = []

        for value in row.tolist():
            if pd.isna(value):
                continue

            text = str(value).strip()
            if text:
                values.append(text)

        # Unificamos el contenido de la fila para detectar bloques aunque el texto este partido en columnas.
        return " ".join(values)

    def _extract_fecha_and_channel(self, row_text: str) -> dict[str, str | None]:
        match = self.FECHA_DE_TOMA_PATTERN.search(row_text)
        if not match:
            return {"fecha_detectada": None, "canal_detectado": None}

        trailing_text = " ".join(match.group(1).split())
        date_match = self.DATE_PATTERN.search(trailing_text)

        fecha_detectada = date_match.group(1) if date_match else None
        canal_detectado = None

        if fecha_detectada:
            channel_text = trailing_text.replace(fecha_detectada, "", 1).strip()
            canal_detectado = channel_text or None

        return {
            "fecha_detectada": fecha_detectada,
            "canal_detectado": canal_detectado,
        }

    def _extract_cronograma_rows_from_block(
        self,
        *,
        dataframe: pd.DataFrame,
        start_index: int,
        end_index: int,
        fecha_detectada: str | None,
        canal_detectado: str | None,
    ) -> list[dict[str, str | None]]:
        programa_actual: str | None = None
        cleaned_rows: list[dict[str, str | None]] = []

        for row_index in range(start_index + 1, end_index + 1):
            row = dataframe.iloc[row_index]
            row_values = self._normalize_row_values(row.tolist())
            row_text = " ".join(value for value in row_values if value)

            if not row_text:
                continue
            if self._is_non_data_row(row_values, row_text):
                continue

            detected_program = self._extract_program_name(row_values, row_text)
            if detected_program:
                programa_actual = detected_program
                continue

            if not self._looks_like_valid_ad_row(row_values):
                continue

            normalized_columns = self._map_row_to_cronograma_columns(row_values)
            normalized_columns["programa"] = programa_actual
            normalized_columns["fecha"] = fecha_detectada
            normalized_columns["canal"] = canal_detectado
            cleaned_rows.append(normalized_columns)

        return cleaned_rows

    def _normalize_row_values(self, row_values: list[object]) -> list[str | None]:
        normalized_values: list[str | None] = []

        for value in row_values:
            if pd.isna(value):
                normalized_values.append(None)
                continue

            if isinstance(value, float) and value.is_integer():
                normalized_values.append(str(int(value)))
                continue

            text = str(value).strip()
            normalized_values.append(text or None)

        return normalized_values

    def _is_non_data_row(self, row_values: list[str | None], row_text: str) -> bool:
        upper_text = row_text.upper()
        first_values = {value.upper() for value in row_values[:6] if value}

        if "FECHA DE TOMA" in upper_text:
            return True
        if "HORARIO CANAL" in upper_text:
            return True
        if "MONITOR DE MEDIOS PUBLICITARIOS" in upper_text:
            return True
        if "TOTAL SEG" in upper_text:
            return True
        if first_values & self.HEADER_ROW_MARKERS:
            return True

        return False

    def _extract_program_name(
        self, row_values: list[str | None], row_text: str
    ) -> str | None:
        non_empty_values = [value for value in row_values if value]
        if len(non_empty_values) != 1:
            return None

        if "TEOR:" not in row_text.upper() or "UNIDADES:" not in row_text.upper():
            return None

        match = self.PROGRAM_ROW_PATTERN.search(row_text)
        if not match:
            return None

        program_name = " ".join(match.group("programa").split())
        return program_name or None

    def _looks_like_valid_ad_row(self, row_values: list[str | None]) -> bool:
        if len(row_values) < 4:
            return False

        hora_inicio = row_values[0]
        hora_fin = row_values[1] if len(row_values) > 1 else None
        cod_prod = row_values[2] if len(row_values) > 2 else None
        producto = row_values[3] if len(row_values) > 3 else None

        return bool(hora_inicio and hora_fin and (cod_prod or producto))

    def _map_row_to_cronograma_columns(
        self, row_values: list[str | None]
    ) -> dict[str, str | None]:
        padded_row = (row_values + [None] * 11)[:11]

        return {
            "hora_inicio": padded_row[0],
            "hora_fin": padded_row[1],
            "cod_prod": padded_row[2],
            "producto": padded_row[3],
            "tema": padded_row[4],
            "duracion": padded_row[5],
            "t_compra": padded_row[6],
            "t_material": padded_row[7],
            "columna_extra": padded_row[8],
            "alcance": padded_row[9],
            "prom_canal": padded_row[10],
            "programa": None,
            "canal": None,
            "fecha": None,
            "archivo_ingesta_id": None,
            "dia_semana": None,
            "tipo_dia": None,
        }

    def _collect_detected_programs(
        self, cronograma_rows: list[dict[str, str | None]]
    ) -> list[str]:
        programs: list[str] = []

        for row in cronograma_rows:
            programa = row.get("programa")
            if programa and programa not in programs:
                programs.append(programa)

        return programs
