#!/usr/bin/env python3
"""
Convierte output/resultados.json (generado por youtube_news.py) al formato
estándar de ScanCards (scancards_youtube.json) y renderiza el reporte HTML.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SCANCARD_START = "<!-- SCANCARD_TEMPLATE_START -->"
SCANCARD_END = "<!-- SCANCARD_TEMPLATE_END -->"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

NON_VOID_FIELD_RE = re.compile(
    r"<(?P<tag>[a-zA-Z][\w:-]*)"
    r'(?P<attrs>[^>]*?\sdata-field="(?P<field>[^"]+)"[^>]*)>'
    r"(?P<content>.*?)</(?P=tag)\s*>",
    re.DOTALL,
)
VOID_FIELD_RE = re.compile(
    r'<(?P<tag>img|source|input)(?P<attrs>[^>]*?\sdata-field="(?P<field>[^"]+)"[^>]*)>',
    re.IGNORECASE,
)
HREF_FIELD_RE = re.compile(
    r'<(?P<tag>a)(?P<attrs>[^>]*?\sdata-href-field="(?P<field>[^"]+)"[^>]*)>',
    re.IGNORECASE,
)


def parse_path(path: str) -> list[tuple[str, Any]]:
    """Convierte una ruta como `cards[0].title` en pasos de clave e índice."""
    tokens: list[tuple[str, Any]] = []
    i = 0
    while i < len(path):
        j = i
        while j < len(path) and path[j] not in ".[":
            j += 1
        key = path[i:j].strip()
        if key:
            tokens.append(("key", key))
        i = j
        while i < len(path) and path[i] == "[":
            k = path.find("]", i)
            if k == -1:
                break
            idx_text = path[i + 1 : k].strip()
            if idx_text.isdigit():
                tokens.append(("idx", int(idx_text)))
            i = k + 1
        if i < len(path) and path[i] == ".":
            i += 1
    return tokens


def resolve_path(data: dict[str, Any], path: str) -> Any:
    """Resuelve una ruta con claves e índices; devuelve texto vacío si no existe."""
    current: Any = data
    for kind, value in parse_path(path):
        if kind == "key":
            if isinstance(current, dict) and value in current:
                current = current[value]
            else:
                return ""
        else:
            if isinstance(current, list) and 0 <= value < len(current):
                current = current[value]
            else:
                return ""
    return current


def set_attr(attrs: str, name: str, value: str) -> str:
    """Reemplaza o agrega un atributo HTML y escapa su valor para usarlo con seguridad."""
    escaped = html.escape(str(value), quote=True)
    pattern = re.compile(rf'(\s{name}\s*=\s*")[^"]*(")', re.IGNORECASE)
    if pattern.search(attrs):
        return pattern.sub(rf"\1{escaped}\2", attrs)
    return attrs + f' {name}="{escaped}"'


def format_date_es(iso_str: str) -> str:
    """Convierte fecha ISO (2026-11-18T14:00:00+00:00) a formato legible (18 Nov 2026)."""
    meses = {
        1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
    }
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return f"{dt.day:02d} {meses[dt.month]} {dt.year}"
    except Exception:
        return iso_str[:10] if iso_str else "Sin fecha"


def build_scancards_payload(
    raw_data: dict[str, Any],
    only_with_summary: bool = False,
    subtitulo: str | None = None,
) -> dict[str, Any]:
    """Aplana resultados.json en la estructura estándar de ScanCards."""
    meta_in = raw_data.get("metadata", {})
    canales_map = raw_data.get("canales", {})

    flat_videos: list[dict[str, Any]] = []
    for canal_nombre, videos in canales_map.items():
        if not isinstance(videos, list):
            continue
        for v in videos:
            resumen = str(v.get("resumen", "")).strip()
            if only_with_summary and (not resumen or resumen.lower() == "no disponible"):
                continue
            item_copy = dict(v)
            item_copy.setdefault("fuente", canal_nombre)
            flat_videos.append(item_copy)

    # Ordenar globalmente del más reciente al más antiguo
    flat_videos.sort(key=lambda x: str(x.get("fecha_publicacion", "")), reverse=True)

    scancards: list[dict[str, Any]] = []
    for idx, v in enumerate(flat_videos, start=1):
        video_id = str(v.get("item_id", "")).strip()
        titulo = str(v.get("titulo", "Sin título")).strip()
        canal = str(v.get("fuente", "YouTube")).strip()
        vertical = str(v.get("vertical", "general")).strip()
        fecha_iso = str(v.get("fecha_publicacion", "")).strip()
        fecha_legible = format_date_es(fecha_iso)
        duracion = str(v.get("duracion", "N/A")).strip()
        url = str(v.get("url", "")).strip()
        resumen = str(v.get("resumen", "no disponible")).strip()
        puntos = v.get("puntos_clave") if isinstance(v.get("puntos_clave"), list) else []

        if puntos:
            items_li = "".join(f"<li>{html.escape(str(p))}</li>" for p in puntos)
            puntos_html = f"<ul>{items_li}</ul>"
        else:
            puntos_html = "<p><em>Sin puntos clave disponibles (video sin subtítulos).</em></p>"

        # Miniatura oficial de alta calidad de YouTube (única por video)
        imagen_url = (
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            if video_id
            else ""
        )

        scancards.append(
            {
                # Campos específicos de YouTube
                "id": f"YT-{idx:03d}",
                "video_id": video_id,
                "titulo": titulo,
                "canal": canal,
                "vertical": vertical,
                "fecha_publicacion": fecha_legible,
                "fecha_iso": fecha_iso,
                "duracion": duracion,
                "url": url,
                "resumen": resumen,
                "puntos_clave": puntos,
                "puntos_clave_html": puntos_html,
                "imagen": imagen_url,
                # Alias compatibles con plantillas tradicionales de render_report.py
                "title": titulo,
                "source": canal,
                "date": fecha_legible,
                "summary": resumen,
                "description": resumen,
                "link": url,
                "category": vertical,
            }
        )

    ventana = meta_in.get("ventana_dias", 30)
    subtitulo_final = (
        subtitulo
        or f"Radar Audiovisual YouTube · Últimos {ventana} días ({len(scancards)} señales)"
    )

    return {
        "metadata": {
            "subtitulo_portada": subtitulo_final,
            "generado_en": datetime.now().isoformat(),
            "total_scancards": len(scancards),
        },
        "scancards": scancards,
    }


def duplicate_scancard_block(html_text: str, count: int) -> str:
    """Repite el bloque marcado en la plantilla, asignando un índice a cada tarjeta."""
    start_idx = html_text.find(SCANCARD_START)
    end_idx = html_text.find(SCANCARD_END)
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise ValueError(
            f"La plantilla HTML no contiene los marcadores {SCANCARD_START} / {SCANCARD_END}."
        )

    block = html_text[start_idx + len(SCANCARD_START) : end_idx]
    rendered_blocks = "".join(
        block.replace("scancards[0]", f"scancards[{i}]") for i in range(count)
    )
    return html_text[:start_idx] + rendered_blocks + html_text[end_idx + len(SCANCARD_END) :]


def apply_fields(html_text: str, data: dict[str, Any]) -> str:
    """Sustituye campos de plantilla por valores del payload y escapa el contenido."""
    def repl_href(match: re.Match[str]) -> str:
        """Asigna a un enlace el valor de datos indicado por `data-href-field`."""
        tag = match.group("tag")
        attrs = match.group("attrs")
        field = match.group("field")
        val = str(resolve_path(data, field) or "#")
        attrs = set_attr(attrs, "href", val)
        return f"<{tag}{attrs}>"

    def repl_void(match: re.Match[str]) -> str:
        """Rellena el atributo `src` o `value` de elementos HTML sin etiqueta de cierre."""
        tag = match.group("tag")
        attrs = match.group("attrs")
        field = match.group("field")
        val = str(resolve_path(data, field) or "")
        if tag.lower() in ("img", "source"):
            attrs = set_attr(attrs, "src", val)
        elif tag.lower() == "input":
            attrs = set_attr(attrs, "value", val)
        return f"<{tag}{attrs}>"

    def repl_non_void(match: re.Match[str]) -> str:
        """Rellena el contenido de un elemento HTML sin modificar scripts ni estilos."""
        tag = match.group("tag")
        if tag.lower() in ("script", "style"):
            return match.group(0)
        attrs = match.group("attrs")
        field = match.group("field")
        raw_val = resolve_path(data, field)

        if isinstance(raw_val, list):
            text = ", ".join(str(x) for x in raw_val)
        elif raw_val is None:
            text = ""
        else:
            text = str(raw_val)

        # Permitir HTML controlado únicamente en puntos_clave_html
        if not field.endswith("puntos_clave_html"):
            text = html.escape(text)
            if "\n" in text:
                text = text.replace("\r\n", "\n").replace("\n", "<br/>")

        return f"<{tag}{attrs}>{text}</{tag}>"

    html_text = HREF_FIELD_RE.sub(repl_href, html_text)
    html_text = VOID_FIELD_RE.sub(repl_void, html_text)
    html_text = NON_VOID_FIELD_RE.sub(repl_non_void, html_text)
    return html_text


def main() -> None:
    """Lee los resultados, crea el JSON de tarjetas y renderiza el reporte HTML."""
    parser = argparse.ArgumentParser(
        description="Genera scancards_youtube.json y reporte_youtube.html desde resultados.json."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("output/resultados.json"),
        help="Ruta al archivo resultados.json (por defecto: output/resultados.json).",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=PROJECT_ROOT / "templates" / "youtube_scancards.html",
        help="Ruta a la plantilla HTML de ScanCards.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("output/scancards_youtube.json"),
        help="Ruta de salida para el JSON compatible con render_report.py.",
    )
    parser.add_argument(
        "--html-out",
        type=Path,
        default=Path("output/reporte_youtube.html"),
        help="Ruta de salida para el reporte HTML renderizado.",
    )
    parser.add_argument(
        "--solo-con-resumen",
        action="store_true",
        help="Excluye los videos cuya transcripción/resumen quedó como 'no disponible'.",
    )
    parser.add_argument(
        "--subtitulo",
        type=str,
        default=None,
        help="Subtítulo personalizado para la cabecera del reporte.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        raise FileNotFoundError(f"No se encontró el archivo de entrada: {args.input}")
    if not args.template.exists():
        raise FileNotFoundError(f"No se encontró la plantilla HTML: {args.template}")

    with args.input.open("r", encoding="utf-8") as f:
        raw_data = json.load(f)

    payload = build_scancards_payload(
        raw_data,
        only_with_summary=args.solo_con_resumen,
        subtitulo=args.subtitulo,
    )

    # 1. Guardar JSON compatible con tu render_report.py
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    with args.json_out.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 2. Renderizar HTML de ScanCards
    template_html = args.template.read_text(encoding="utf-8")
    rendered_html = duplicate_scancard_block(template_html, len(payload["scancards"]))
    rendered_html = apply_fields(rendered_html, payload)

    args.html_out.parent.mkdir(parents=True, exist_ok=True)
    args.html_out.write_text(rendered_html, encoding="utf-8")

    print(f"JSON ScanCards generado : {args.json_out} ({len(payload['scancards'])} tarjetas)")
    print(f"HTML ScanCards generado : {args.html_out}")


if __name__ == "__main__":
    main()