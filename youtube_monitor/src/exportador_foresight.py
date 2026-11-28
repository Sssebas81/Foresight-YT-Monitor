#!/usr/bin/env python3
"""
Exportador homologado INNLAB Foresight (CSV + PDFs individuales por ID).
Genera:
  1) CSV con columnas exactas:
     ID, Calidad, Enfoque, Title, Year, Source, Authors, Abstract, Keywords, Paper Link, DOI, Type, Origin
  2) Un archivo <ID>.pdf por cada registro con metadatos y la transcripción literal completa.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

try:
    from fpdf import FPDF
except ImportError as exc:
    raise ImportError("Instala fpdf2 para generar los PDFs: pip install fpdf2") from exc


CSV_HEADERS = [
    "ID",
    "Calidad",
    "Enfoque",
    "Title",
    "Year",
    "Source",
    "Authors",
    "Abstract",
    "Keywords",
    "Paper Link",
    "DOI",
    "Type",
    "Origin",
]


def _parse_next_id(ultimo_id: str | None, id_inicio: str | None) -> tuple[str, int, int]:
    """Calcula prefijo, número inicial y padding a partir de ultimo_id (ej. REC-0179 -> REC-0180)."""
    ref = (id_inicio or "").strip()
    if ref:
        m = re.match(r"^([A-Za-z_-]+?)(\d+)$", ref)
        if m:
            return m.group(1), int(m.group(2)), len(m.group(2))

    ref_last = (ultimo_id or "REC-0000").strip()
    m = re.match(r"^([A-Za-z_-]+?)(\d+)$", ref_last)
    if m:
        return m.group(1), int(m.group(2)) + 1, len(m.group(2))
    return "REC-", 1, 4


def _sanitizar_latin1(texto: Any) -> str:
    """Convierte caracteres Unicode conflictivos a texto seguro para FPDF."""
    s = str(texto or "")
    reemplazos = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2022": "-",
        "\u2026": "...",
        "\xa0": " ",
    }
    for orig, dest in reemplazos.items():
        s = s.replace(orig, dest)
    return s.encode("latin-1", errors="replace").decode("latin-1")


def _romper_tokens_largos(texto: str, max_len: int = 68) -> str:
    """Divide URLs o cadenas continuas sin espacios para evitar desbordamiento horizontal en multi_cell."""
    lineas_out: list[str] = []
    for linea in texto.splitlines():
        palabras = linea.split(" ")
        nuevas_palabras: list[str] = []
        for pal in palabras:
            if len(pal) <= max_len:
                nuevas_palabras.append(pal)
            else:
                trozos = [pal[i : i + max_len] for i in range(0, len(pal), max_len)]
                nuevas_palabras.append(" ".join(trozos))
        lineas_out.append(" ".join(nuevas_palabras))
    return "\n".join(lineas_out)


def _cargar_items(json_path: Path) -> list[dict[str, Any]]:
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        # Estructura generada por youtube_news.py: {"metadata": ..., "canales": {canal: [items...]}}
        canales = raw.get("canales")
        if isinstance(canales, dict):
            flat: list[dict[str, Any]] = []
            for canal_nombre, videos in canales.items():
                if not isinstance(videos, list):
                    continue
                for v in videos:
                    if isinstance(v, dict):
                        item_copy = dict(v)
                        if not item_copy.get("fuente") and not item_copy.get("canal"):
                            item_copy["fuente"] = canal_nombre
                        flat.append(item_copy)
            if flat:
                return flat

        for k in ("resultados", "items", "cards", "scancards"):
            if isinstance(raw.get(k), list):
                return [x for x in raw[k] if isinstance(x, dict)]
    return []


def _extraer_video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def _resolver_directorios_transcripts(json_path: Path) -> list[Path]:
    """Localiza carpetas transcripts/ asociadas al proyecto y a rutas.cache_db en config.yaml."""
    base_dir = json_path.parent.parent if json_path.parent.name == "output" else json_path.parent
    candidatos: list[Path] = [
        base_dir / "cache" / "transcripts",
        json_path.parent / "transcripts",
        Path("cache/transcripts"),
    ]

    cfg_path = base_dir / "config.yaml"
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            cache_db_str = ((cfg.get("rutas") or {}).get("cache_db") or "").strip()
            if cache_db_str:
                cache_db_path = Path(cache_db_str)
                if not cache_db_path.is_absolute():
                    cache_db_path = base_dir / cache_db_path
                candidatos.insert(0, cache_db_path.parent / "transcripts")
        except Exception:
            pass

    unicos: list[Path] = []
    vistos: set[str] = set()
    for p in candidatos:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in vistos:
            vistos.add(key)
            unicos.append(p)
    return unicos


def _cargar_mapa_sqlite(json_path: Path) -> dict[str, str]:
    """Busca en bases SQLite transcripciones guardadas si existieran."""
    mapa: dict[str, str] = {}
    base_dir = json_path.parent.parent if json_path.parent.name == "output" else json_path.parent
    search_dirs = [json_path.parent, base_dir / "cache"]

    for folder in search_dirs:
        if not folder.exists():
            continue
        for db_file in list(folder.glob("*.db")) + list(folder.glob("*.sqlite")) + list(folder.glob("*.sqlite3")):
            try:
                conn = sqlite3.connect(str(db_file))
                cur = conn.cursor()
                tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                for tbl in tables:
                    cols = [c[1] for c in cur.execute(f"PRAGMA table_info('{tbl}')").fetchall()]
                    col_lower = {c.lower(): c for c in cols}
                    text_col = next(
                        (
                            col_lower[k]
                            for k in ("transcripcion", "transcript", "texto_completo", "texto", "raw_text", "subtitulos", "contenido")
                            if k in col_lower
                        ),
                        None,
                    )
                    id_col = next(
                        (col_lower[k] for k in ("url", "video_url", "link", "video_id", "item_id", "id") if k in col_lower),
                        None,
                    )
                    if text_col and id_col:
                        for key_val, txt_val in cur.execute(f"SELECT {id_col}, {text_col} FROM {tbl}").fetchall():
                            if key_val and txt_val and str(txt_val).strip():
                                mapa[str(key_val).strip()] = str(txt_val).strip()
                conn.close()
            except Exception:
                continue
    return mapa


def _obtener_transcripcion_literal(
    item: dict[str, Any],
    transcript_dirs: list[Path],
    mapa_sqlite: dict[str, str],
) -> str:
    """Extrae la transcripción literal completa desde cache/transcripts/<item_id>.txt o campos/SQLite."""
    url = str(item.get("url") or item.get("link") or "").strip()
    raw_id = str(item.get("item_id") or item.get("video_id") or item.get("id") or "").strip()
    vid = _extraer_video_id(url) or raw_id

    posibles_ids = [x for x in (raw_id, vid) if x]
    for t_dir in transcript_dirs:
        if not t_dir.exists():
            continue
        for cand_id in posibles_ids:
            txt_file = t_dir / f"{cand_id}.txt"
            if txt_file.exists() and txt_file.is_file():
                contenido = txt_file.read_text(encoding="utf-8", errors="replace").strip()
                if contenido:
                    return contenido

    claves_directas = (
        "contenido_crudo",
        "transcripcion",
        "transcripcion_completa",
        "transcripcion_whisper",
        "whisper_text",
        "transcript",
        "full_transcript",
        "texto_completo",
        "texto_transcripcion",
        "raw_text",
        "subtitulos",
        "texto",
    )
    for k in claves_directas:
        val = item.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list) and val:
            partes = []
            for seg in val:
                if isinstance(seg, dict):
                    t = seg.get("text") or seg.get("texto") or ""
                    if str(t).strip():
                        partes.append(str(t).strip())
                elif str(seg).strip():
                    partes.append(str(seg).strip())
            if partes:
                return " ".join(partes)

    for key_lookup in ([url] + posibles_ids):
        if key_lookup and key_lookup in mapa_sqlite:
            return mapa_sqlite[key_lookup]

    return ""


def _extraer_anio(fecha_raw: Any) -> str:
    s = str(fecha_raw or "").strip()
    m = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    return m.group(1) if m else ""


def _generar_pdf_individual(pdf_path: Path, item: dict[str, Any], rec_id: str, transcripcion_literal: str) -> None:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(left=15, top=15, right=15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    usable_w = max(50.0, pdf.w - pdf.l_margin - pdf.r_margin)

    def write_block(text: str, size: int = 10, bold: bool = False, line_h: float = 5.3) -> None:
        clean = _romper_tokens_largos(_sanitizar_latin1(text), max_len=68)
        if not clean.strip():
            return
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", style="B" if bold else "", size=size)
        pdf.multi_cell(w=usable_w, h=line_h, text=clean)

    titulo = item.get("titulo") or item.get("title") or "Sin título"
    canal = item.get("fuente") or item.get("canal") or item.get("channel") or item.get("autor") or "N/D"
    vertical = item.get("vertical") or "General"
    fecha = item.get("fecha_publicacion") or item.get("fecha") or item.get("published_at") or "N/D"
    duracion = item.get("duracion") or item.get("duration") or "N/D"
    url = item.get("url") or item.get("link") or ""

    write_block(f"INNLAB Foresight · Universidad Icesi | {rec_id}", size=9, bold=True, line_h=5)
    pdf.ln(1.5)
    write_block(titulo, size=14, bold=True, line_h=7)
    pdf.ln(2)

    meta = f"Canal / Autor: {canal}  |  Vertical: {vertical}  |  Fecha: {fecha}  |  Duración: {duracion}"
    write_block(meta, size=9, bold=False, line_h=5)
    if url:
        write_block(f"URL: {url}", size=9, bold=False, line_h=5)

    pdf.ln(3.5)
    write_block("Transcripción literal completa (Speech-to-Text / Subtítulos)", size=11, bold=True, line_h=6)
    pdf.ln(1)

    cuerpo_transcripcion = (
        transcripcion_literal
        if transcripcion_literal.strip()
        else "No se encontró archivo de transcripción literal para este video."
    )
    write_block(cuerpo_transcripcion, size=10, bold=False, line_h=5.2)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(pdf_path))


def exportar_scancards_a_csv_y_pdfs(
    json_path: Path,
    csv_out: Path,
    pdf_dir: Path,
    id_inicio: str | None = None,
    ultimo_id: str | None = None,
    keywords_filtro: list[str] | None = None,
    interactivo: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Exporta registros a CSV homologado (13 columnas exactas) + PDFs individuales con transcripción completa."""
    json_path = Path(json_path)
    items = _cargar_items(json_path)
    if not items:
        raise ValueError("No se encontraron registros válidos para exportar.")

    prefix, current_num, pad = _parse_next_id(ultimo_id=ultimo_id, id_inicio=id_inicio)

    csv_out = Path(csv_out)
    pdf_dir = Path(pdf_dir)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    transcript_dirs = _resolver_directorios_transcripts(json_path)
    mapa_sqlite = _cargar_mapa_sqlite(json_path)
    kw_filtro_str = ", ".join(str(k).strip() for k in (keywords_filtro or []) if str(k).strip())

    pdfs_generados: list[str] = []

    with csv_out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()

        for item in items:
            rec_id = f"{prefix}{current_num:0{pad}d}"
            current_num += 1

            transcripcion_literal = _obtener_transcripcion_literal(item, transcript_dirs, mapa_sqlite)
            pdf_file = pdf_dir / f"{rec_id}.pdf"
            _generar_pdf_individual(pdf_file, item, rec_id, transcripcion_literal)
            pdfs_generados.append(str(pdf_file))

            titulo = str(item.get("titulo") or item.get("title") or "").strip()
            canal = str(item.get("fuente") or item.get("canal") or item.get("channel") or item.get("autor") or "").strip()
            fecha_raw = item.get("fecha_publicacion") or item.get("fecha") or item.get("published_at") or ""
            anio = _extraer_anio(fecha_raw)
            url = str(item.get("url") or item.get("link") or "").strip()
            resumen = str(item.get("resumen") or item.get("summary") or "").strip()

            if kw_filtro_str:
                keywords_val = kw_filtro_str
            else:
                item_kw = (
                    item.get("keywords_coincidentes")
                    or item.get("keywords")
                    or item.get("palabras_clave")
                    or item.get("tags")
                    or []
                )
                if isinstance(item_kw, list):
                    keywords_val = ", ".join(str(k).strip() for k in item_kw if str(k).strip())
                else:
                    keywords_val = str(item_kw or "").strip()

            calidad = "Alta" if len(transcripcion_literal) > 120 else "Media"

            writer.writerow(
                {
                    "ID": rec_id,
                    "Calidad": calidad,
                    "Enfoque": "",
                    "Title": titulo,
                    "Year": anio,
                    "Source": "YouTube",
                    "Authors": canal,
                    "Abstract": resumen,
                    "Keywords": keywords_val,
                    "Paper Link": url,
                    "DOI": "",
                    "Type": "Video / Audiovisual",
                    "Origin": "INNLAB Foresight · Radar YouTube",
                }
            )

    return {
        "csv_path": str(csv_out),
        "pdfs_generados": pdfs_generados,
        "total": len(items),
    }