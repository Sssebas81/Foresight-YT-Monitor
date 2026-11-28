#!/usr/bin/env python3
"""
Interfaz visual local para INNLAB Foresight · Universidad Icesi.
Panel de control ampliado en http://127.0.0.1:8765 con identidad visual
Universidad Icesi + INNLAB Centro de Innovación, filtros por vertical,
ejes a medida (ej. Universidad del Quindío), keywords bilingües, ejecución
en vivo de youtube_news.py + Whisper + render_youtube_scancards.py y
exportación a CSV homologado + PDFs individuales por ID (.ZIP).
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
import zipfile
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# Detecta BASE_DIR tanto si app_foresight.py está en src/ como en la raíz
_HERE = Path(__file__).resolve().parent
BASE_DIR = _HERE.parent if _HERE.name == "src" else _HERE
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from exportador_foresight import exportar_scancards_a_csv_y_pdfs
except ImportError:
    exportar_scancards_a_csv_y_pdfs = None  # type: ignore

CONFIG_PATH = BASE_DIR / "config.yaml"
YOUTUBE_SCRIPT = BASE_DIR / "src" / "youtube_news.py"
RENDER_SCRIPT = BASE_DIR / "src" / "render_youtube_scancards.py"
TEMPLATE_PATH = BASE_DIR / "templates" / "youtube_scancards.html"
OUTPUT_DIR = BASE_DIR / "output"
JSON_OUT = OUTPUT_DIR / "resultados.json"
SCANCARDS_JSON_OUT = OUTPUT_DIR / "scancards_youtube.json"
HTML_OUT = OUTPUT_DIR / "reporte_foresight.html"

HOST = "127.0.0.1"
PORT = 8765

DEFAULT_UI_PARAMS = {
    "idioma": "todos",
    "modo": "canales",
    "dias": 30,
    "max_videos": 1,
    "max_minutos": 15,
    "max_canales": 12,
    "whisper_model": "small",
    "pausa_min": 1.5,
    "pausa_max": 3.5,
    "proxy": "",
    "cookies": "",
    "cookies_from_browser": "",
}


def _find_asset(candidates: list[str]) -> Path | None:
    """Busca archivos de imagen/logo locales en la raíz o en subcarpetas habituales."""
    search_dirs = [BASE_DIR, BASE_DIR / "assets", BASE_DIR / "static", BASE_DIR / "img", BASE_DIR / "logos"]
    for folder in search_dirs:
        if not folder.exists():
            continue
        for name in candidates:
            p = folder / name
            if p.exists() and p.is_file():
                return p
    return None


def _count_scancards(json_path: Path) -> int | None:
    """Cuenta cuántos registros hay en un JSON generado."""
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ("resultados", "cards", "scancards", "items"):
                if isinstance(data.get(key), list):
                    return len(data[key])
    except Exception:
        return None
    return None


def _load_config_safe() -> dict[str, Any]:
    """Carga config.yaml de forma tolerante y reporta diagnóstico claro si falla."""
    if not CONFIG_PATH.exists():
        return {"_config_error": f"No se encontró config.yaml en {CONFIG_PATH}", "verticales": {}}
    try:
        raw_text = CONFIG_PATH.read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw_text) or {}
        if not isinstance(loaded, dict):
            return {"_config_error": "config.yaml no tiene un objeto raíz válido.", "verticales": {}}
        if not isinstance(loaded.get("verticales"), dict):
            loaded["verticales"] = {}
        return loaded
    except Exception as exc:
        return {"_config_error": f"Error leyendo config.yaml: {exc}", "verticales": {}}


def _extract_cards_from_html_fallback(html_path: Path) -> list[dict[str, Any]]:
    """Extrae ScanCards desde reporte_foresight.html si no existen JSON en output/."""
    if not html_path.exists():
        return []
    text = html_path.read_text(encoding="utf-8")
    articles = re.findall(r'<article class="scancard">(.*?)</article>', text, flags=re.S)
    results: list[dict[str, Any]] = []

    def _grab(block: str, pattern: str) -> str:
        m = re.search(pattern, block, flags=re.S)
        if not m:
            return ""
        cleaned = re.sub(r"<[^>]+>", " ", m.group(1))
        return re.sub(r"\s+", " ", unescape(cleaned)).strip()

    for idx, art in enumerate(articles, start=1):
        m_url = re.search(r'class="scancard-title"[^>]*>.*?href="([^"]+)"', art, flags=re.S)
        bullets = [
            re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", b))).strip()
            for b in re.findall(r"<li>(.*?)</li>", art, flags=re.S)
        ]
        resumen = _grab(art, r'class="scancard-summary"[^>]*>(.*?)</p>')
        results.append({
            "id": _grab(art, r'class="scancard-id"[^>]*>(.*?)</span>') or f"YT-{idx:03d}",
            "titulo": _grab(art, r'class="scancard-title"[^>]*>.*?<a[^>]*>(.*?)</a>'),
            "canal": _grab(art, r'class="badge-channel"[^>]*>(.*?)</span>'),
            "vertical": _grab(art, r'class="badge-vertical"[^>]*>(.*?)</span>'),
            "fecha_publicacion": _grab(art, r"<time[^>]*>(.*?)</time>"),
            "duracion": _grab(art, r'class="scancard-duration"[^>]*>(.*?)</span>'),
            "url": m_url.group(1).strip() if m_url else "",
            "resumen": resumen,
            "puntos_clave": bullets,
            "transcripcion": "",
        })
    return results


class JobManager:
    """Gestiona la ejecución secuencial de youtube_news.py y render_youtube_scancards.py."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.logs: list[str] = []
        self.last_exit_code: int | None = None
        self.last_command: str = ""
        self.last_keywords: list[str] = []
        self.report_ready = HTML_OUT.exists() and (
            ((_count_scancards(SCANCARDS_JSON_OUT) or 0) > 0)
            or ((_count_scancards(JSON_OUT) or 0) > 0)
            or len(_extract_cards_from_html_fallback(HTML_OUT)) > 0
        )

    def start(self, params: dict[str, Any]) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "Ya hay una ejecución en curso. Espera a que finalice."
            self.running = True
            self.logs = []
            self.last_exit_code = None
            self.last_keywords = [str(k).strip() for k in (params.get("keywords") or []) if str(k).strip()]

        thread = threading.Thread(target=self._run_pipeline, args=(params,), daemon=True)
        thread.start()
        return True, "Ejecución iniciada."

    def _append_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line.rstrip("\r\n"))
            if len(self.logs) > 600:
                self.logs = self.logs[-600:]

    def _run_pipeline(self, params: dict[str, Any]) -> None:
        try:
            cmd = [sys.executable, str(YOUTUBE_SCRIPT), "--config", str(CONFIG_PATH)]

            vertical = str(params.get("vertical") or "").strip()
            if vertical and vertical != "todas":
                cmd.extend(["--vertical", vertical])

            idioma = str(params.get("idioma") or DEFAULT_UI_PARAMS["idioma"]).strip()
            if idioma in ("es", "en", "todos"):
                cmd.extend(["--idioma", idioma])

            modo = str(params.get("modo") or DEFAULT_UI_PARAMS["modo"]).strip()
            if modo in ("canales", "busqueda"):
                cmd.extend(["--modo", modo])

            dias = int(params.get("dias") or DEFAULT_UI_PARAMS["dias"])
            cmd.extend(["--dias", str(max(1, dias))])

            max_videos = int(params.get("max_videos") or DEFAULT_UI_PARAMS["max_videos"])
            cmd.extend(["--max-videos", str(max(1, max_videos))])

            raw_max_min = params.get("max_minutos")
            max_minutos = int(DEFAULT_UI_PARAMS["max_minutos"] if raw_max_min is None else raw_max_min)
            cmd.extend(["--max-minutos", str(max(0, max_minutos))])

            raw_max_can = params.get("max_canales")
            max_canales = int(DEFAULT_UI_PARAMS["max_canales"] if raw_max_can is None else raw_max_can)
            if max_canales > 0:
                cmd.extend(["--max-canales", str(max_canales)])

            whisper_model = str(params.get("whisper_model") or DEFAULT_UI_PARAMS["whisper_model"]).strip()
            if whisper_model:
                cmd.extend(["--whisper-model", whisper_model])

            raw_pausa_min = params.get("pausa_min")
            pausa_min = max(0.0, float(DEFAULT_UI_PARAMS["pausa_min"] if raw_pausa_min is None else raw_pausa_min))
            raw_pausa_max = params.get("pausa_max")
            pausa_max = max(pausa_min, float(DEFAULT_UI_PARAMS["pausa_max"] if raw_pausa_max is None else raw_pausa_max))
            cmd.extend(["--pausa-min", str(pausa_min), "--pausa-max", str(pausa_max)])

            proxy_val = str(params.get("proxy") or "").strip()
            if proxy_val:
                cmd.extend(["--proxy", proxy_val])

            cookies_val = str(params.get("cookies") or "").strip()
            if cookies_val:
                cmd.extend(["--cookies", cookies_val])

            cookies_browser_val = str(params.get("cookies_from_browser") or "").strip()
            if cookies_browser_val:
                cmd.extend(["--cookies-from-browser", cookies_browser_val])

            canales_custom = str(params.get("canales") or "").strip()
            if canales_custom:
                cmd.extend(["--canal", canales_custom])

            keywords = [str(k).strip() for k in (params.get("keywords") or []) if str(k).strip()]
            if keywords:
                cmd.extend(["--keywords", ",".join(keywords)])

            if bool(params.get("forzar")):
                cmd.append("--forzar")

            self.last_command = " ".join(f'"{c}"' if " " in c else c for c in cmd)
            self._append_log(f"▶ Ejecutando: {self.last_command}")
            self._append_log("-" * 78)

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"

            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self._append_log(line)
            code = proc.wait()

            if code == 0 and RENDER_SCRIPT.exists() and TEMPLATE_PATH.exists() and JSON_OUT.exists():
                self._append_log("-" * 78)
                self._append_log("▶ Renderizando tarjetas visuales ScanCards...")
                render_cmd = [
                    sys.executable,
                    str(RENDER_SCRIPT),
                    "--input",
                    str(JSON_OUT),
                    "--template",
                    str(TEMPLATE_PATH),
                    "--html-out",
                    str(HTML_OUT),
                ]
                r_proc = subprocess.Popen(
                    render_cmd,
                    cwd=str(BASE_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                assert r_proc.stdout is not None
                for line in r_proc.stdout:
                    self._append_log(line)
                r_code = r_proc.wait()

                total_cards = _count_scancards(SCANCARDS_JSON_OUT)
                if r_code == 0:
                    if total_cards is not None and total_cards > 0:
                        self._append_log(f"★ ScanCards generadas: {total_cards} tarjeta(s) listas en el reporte.")
                        with self.lock:
                            self.report_ready = True
                    elif total_cards == 0:
                        self._append_log("⚠ No se encontraron resultados ni ScanCards con estos filtros (0 tarjetas).")
                        self._append_log("ℹ Sugerencia: amplía la ventana de días, sube la duración máxima (ej. 25 o 30) o usa keywords más cortas.")
                        with self.lock:
                            self.report_ready = False
                    else:
                        self._append_log("✔ Reporte ScanCards actualizado correctamente.")
                        with self.lock:
                            self.report_ready = True
                else:
                    self._append_log(f"⚠ Error renderizando HTML (código {r_code}).")

            with self.lock:
                self.last_exit_code = code
        except Exception as exc:
            self._append_log(f"❌ Error inesperado: {exc}")
            with self.lock:
                self.last_exit_code = 1
        finally:
            with self.lock:
                self.running = False


JOBS = JobManager()


HTML_UI = r"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Universidad Icesi · INNLAB Foresight · Radar Audiovisual</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    :root {
      --icesi-blue: #5454e9;
      --icesi-blue-dark: #3e3ec9;
      --icesi-navy: #1d2340;
      --icesi-cyan: #009fe3;
      --icesi-tint: #eef0fd;
      --gris-1: #88898c;
      --bg: #f4f5f9;
      --surface: #ffffff;
      --surface-soft: #f7f8fc;
      --border: #dcdfec;
      --text: #161a2b;
      --muted: #565e75;
      --green: #0d8a4e;
      --green-soft: #e6f5ed;
      --danger: #c92a2a;
      --term-bg: #121626;
      --term-text: #e2e8f8;
      --radius: 14px;
    }

    *, *::before, *::after { box-sizing: border-box; }
    html { font-size: 16px; }

    body {
      margin: 0;
      font-family: "Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }

    .icesi-ribbon {
      max-width: 1680px;
      margin: 18px auto 0;
      padding: 0 32px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .ribbon-dot {
      width: 5px;
      height: 12px;
      background: var(--icesi-blue);
      border-radius: 2px;
    }
    .ribbon-bar {
      flex: 1;
      height: 12px;
      background: var(--icesi-blue);
      border-radius: 3px;
    }
    .ribbon-bar.short {
      flex: 0 0 110px;
      background: var(--icesi-cyan);
    }

    .header-wrap {
      max-width: 1680px;
      margin: 14px auto 0;
      padding: 0 32px;
    }
    .header-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 24px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 20px;
    }

    .innlab-cintillo {
      display: flex;
      align-items: center;
      gap: 28px;
      padding: 6px 0;
      background: transparent;
      color: var(--icesi-blue);
      max-width: 100%;
      flex-wrap: wrap;
    }
    .innlab-cintillo__brand {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }
    .innlab-cintillo__wheel {
      width: 58px;
      height: 58px;
      flex-shrink: 0;
    }
    .innlab-cintillo__icesi-stack {
      display: flex;
      flex-direction: column;
      line-height: 0.94;
    }
    .innlab-cintillo__u {
      font-size: 11px;
      font-weight: 600;
      margin-bottom: 3px;
      color: var(--icesi-blue);
    }
    .innlab-cintillo__icesi {
      font-size: 38px;
      font-weight: 700;
      letter-spacing: 0.02em;
      color: var(--icesi-blue);
    }
    .innlab-cintillo__divider {
      width: 1.5px;
      height: 56px;
      background: var(--gris-1);
      flex-shrink: 0;
    }
    .innlab-cintillo__innlab {
      display: flex;
      align-items: center;
      gap: 11px;
      flex-shrink: 0;
    }
    .innlab-cintillo__innlab-name {
      font-size: 30px;
      font-weight: 600;
      letter-spacing: 0.03em;
      line-height: 1;
      color: var(--icesi-blue);
    }
    .innlab-cintillo__innlab-sub {
      font-size: 12.5px;
      font-weight: 500;
      line-height: 1.2;
      color: var(--icesi-blue);
    }
    .innlab-cintillo__nodo {
      margin: 0;
      font-size: 13.5px;
      font-weight: 500;
      line-height: 1.3;
      color: var(--icesi-blue);
    }
    @media (max-width: 780px) {
      .innlab-cintillo { gap: 16px; }
      .innlab-cintillo__divider { display: none; }
      .innlab-cintillo__wheel { width: 48px; height: 48px; }
      .innlab-cintillo__icesi { font-size: 30px; }
      .innlab-cintillo__innlab-name { font-size: 24px; }
    }

    .logo-img {
      height: 56px;
      width: auto;
      display: block;
      object-fit: contain;
    }
    .header-title-block {
      margin-top: 18px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 16px;
      width: 100%;
    }
    .kicker {
      font-size: 13.5px;
      font-weight: 700;
      color: var(--icesi-blue);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin: 0 0 4px;
    }
    .header-title-block h1 {
      margin: 0;
      font-size: 28px;
      font-weight: 800;
      color: var(--icesi-navy);
      letter-spacing: -0.02em;
    }
    .header-title-block p {
      margin: 4px 0 0;
      font-size: 15px;
      color: var(--muted);
    }
    .foresight-pill {
      background: var(--icesi-blue);
      color: #ffffff;
      font-size: 14px;
      font-weight: 700;
      padding: 10px 18px;
      border-radius: 8px;
      letter-spacing: 0.01em;
    }

    .container {
      max-width: 1680px;
      margin: 22px auto 28px;
      padding: 0 32px;
      display: grid;
      grid-template-columns: 1.25fr 0.95fr;
      gap: 26px;
      align-items: start;
    }
    @media (max-width: 1180px) {
      .container { grid-template-columns: 1fr; }
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 26px 28px;
    }
    .card + .card { margin-top: 22px; }

    .card-title {
      margin: 0 0 6px;
      font-size: 19px;
      font-weight: 700;
      color: var(--icesi-navy);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .card-sub {
      margin: 0 0 20px;
      font-size: 14.5px;
      color: var(--muted);
    }

    .vertical-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
    }
    @media (max-width: 820px) {
      .vertical-grid { grid-template-columns: repeat(2, 1fr); }
    }
    .v-pill {
      border: 2px solid var(--border);
      background: var(--surface);
      border-radius: 12px;
      padding: 16px 18px;
      text-align: left;
      cursor: pointer;
      transition: border-color 0.15s ease, background 0.15s ease;
    }
    .v-pill:hover {
      border-color: var(--icesi-blue);
      background: var(--icesi-tint);
    }
    .v-pill.active {
      border-color: var(--icesi-blue);
      background: var(--icesi-tint);
    }
    .v-pill .v-name {
      display: block;
      font-weight: 700;
      font-size: 16px;
      color: var(--icesi-navy);
    }
    .v-pill .v-meta {
      display: block;
      font-size: 13px;
      color: var(--muted);
      margin-top: 4px;
    }

    .config-warning {
      margin-bottom: 14px;
      padding: 12px 14px;
      border-radius: 10px;
      background: #fff8e6;
      border: 1px solid #f2cf7e;
      color: #7a5200;
      font-size: 13.5px;
    }

    .uq-box {
      margin-top: 18px;
      padding: 18px;
      border-radius: 12px;
      background: var(--icesi-tint);
      border: 1.5px solid #c6cbf8;
      display: none;
    }
    .uq-box.visible { display: block; }
    .uq-box-title {
      font-size: 14.5px;
      font-weight: 700;
      color: var(--icesi-blue-dark);
      margin: 0 0 12px;
    }
    .ejes-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 9px;
    }
    .eje-btn {
      border: 1.5px solid #b8bff5;
      background: #ffffff;
      color: var(--icesi-navy);
      font-size: 13.5px;
      font-weight: 600;
      padding: 8px 14px;
      border-radius: 8px;
      cursor: pointer;
    }
    .eje-btn:hover, .eje-btn.active {
      background: var(--icesi-blue);
      color: #ffffff;
      border-color: var(--icesi-blue);
    }

    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 18px;
    }
    @media (max-width: 680px) {
      .form-grid { grid-template-columns: 1fr; }
    }
    .field { display: flex; flex-direction: column; gap: 7px; }
    .field.full { grid-column: 1 / -1; }

    label {
      font-size: 14px;
      font-weight: 600;
      color: var(--icesi-navy);
    }
    .field-hint {
      font-size: 12.5px;
      color: var(--muted);
      margin-top: -2px;
    }
    input[type="text"], input[type="number"], select {
      width: 100%;
      padding: 12px 14px;
      border: 1.5px solid var(--border);
      border-radius: 10px;
      font-size: 15px;
      color: var(--text);
      background: #ffffff;
      outline: none;
      font-family: inherit;
    }
    input:focus, select:focus {
      border-color: var(--icesi-blue);
    }

    .kw-input-row {
      display: flex;
      gap: 10px;
    }
    .chips-box {
      display: flex;
      flex-wrap: wrap;
      gap: 9px;
      min-height: 54px;
      padding: 14px;
      border: 1.5px solid var(--border);
      border-radius: 10px;
      background: var(--surface-soft);
      margin-top: 12px;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: #ffffff;
      border: 1.5px solid #c9cfe6;
      border-radius: 999px;
      padding: 6px 14px;
      font-size: 14px;
      font-weight: 500;
      color: var(--icesi-navy);
    }
    .chip button {
      border: none;
      background: none;
      color: var(--muted);
      font-weight: 700;
      cursor: pointer;
      padding: 0 2px;
      font-size: 17px;
      line-height: 1;
    }
    .chip button:hover { color: var(--danger); }

    .checkbox-row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 6px;
      font-size: 14.5px;
      cursor: pointer;
    }
    .checkbox-row input[type="checkbox"] {
      width: 18px;
      height: 18px;
      accent-color: var(--icesi-blue);
    }

    .btn {
      border: none;
      border-radius: 10px;
      padding: 13px 20px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 9px;
      text-decoration: none;
      font-family: inherit;
    }
    .btn-primary {
      background: var(--icesi-blue);
      color: #ffffff;
    }
    .btn-primary:hover:not(:disabled) { background: var(--icesi-blue-dark); }
    .btn-primary:disabled { opacity: 0.55; cursor: not-allowed; }

    .btn-secondary {
      background: var(--surface-soft);
      color: var(--icesi-navy);
      border: 1.5px solid var(--border);
    }
    .btn-secondary:hover { background: #e8ecf7; }

    .btn-success {
      background: var(--green);
      color: #ffffff;
    }
    .btn-success.disabled {
      pointer-events: none;
      opacity: 0.45;
    }

    .btn-pdf {
      background: var(--icesi-navy);
      color: #ffffff;
    }
    .btn-pdf:hover:not(.disabled) {
      background: #2b335c;
    }
    .btn-pdf.disabled {
      pointer-events: none;
      opacity: 0.45;
    }

    .cmd-preview {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13.5px;
      line-height: 1.55;
      background: var(--surface-soft);
      border: 1.5px solid var(--border);
      padding: 14px 16px;
      border-radius: 10px;
      color: var(--icesi-navy);
      word-break: break-all;
    }

    .terminal {
      background: var(--term-bg);
      color: var(--term-text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13.5px;
      line-height: 1.6;
      padding: 18px;
      border-radius: 12px;
      height: 540px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 13.5px;
      font-weight: 700;
      padding: 6px 14px;
      border-radius: 999px;
      background: var(--surface-soft);
      color: var(--muted);
      border: 1px solid var(--border);
    }
    .status-badge.running {
      background: #fff3e0;
      color: #a65300;
      border-color: #ffd599;
    }
    .status-badge.done {
      background: var(--green-soft);
      color: var(--green);
      border-color: #b7e4cc;
    }
    .status-badge.error {
      background: #fdecea;
      color: var(--danger);
      border-color: #f5c2c7;
    }

    .scancards-footer {
      max-width: 1680px;
      margin: 0 auto 56px;
      padding: 0 32px;
    }
    .scancards-footer-bar {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 22px 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 16px;
    }
    .scancards-footer-title {
      margin: 0 0 4px;
      font-size: 18px;
      font-weight: 700;
      color: var(--icesi-navy);
    }
    .scancards-footer-sub {
      margin: 0;
      font-size: 14px;
      color: var(--muted);
    }
    .scancards-footer-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(22, 26, 43, 0.55);
      align-items: center;
      justify-content: center;
      z-index: 999;
      padding: 20px;
    }
    .modal-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 14px;
      max-width: 480px;
      width: 100%;
      padding: 26px;
    }
  </style>
</head>
<body>
  <div class="icesi-ribbon" aria-hidden="true">
    <span class="ribbon-dot"></span>
    <span class="ribbon-dot"></span>
    <span class="ribbon-dot"></span>
    <span class="ribbon-dot"></span>
    <span class="ribbon-dot"></span>
    <span class="ribbon-bar"></span>
    <span class="ribbon-bar short"></span>
  </div>

  <div class="header-wrap">
    <header class="header-card">
      <div class="innlab-cintillo" role="region" aria-label="Universidad Icesi — INNLAB Centro de Innovación — Nodo de Innovación Basada en Inteligencia Artificial y Tecnologías Digitales">
        <div id="customLogosSlot" style="display:none; align-items:center; gap:12px;"></div>

        <div id="defaultBrandLockup" class="innlab-cintillo__brand">
          <svg class="innlab-cintillo__wheel" viewBox="0 0 84 84" fill="none" aria-hidden="true">
            <g fill="currentColor" transform="translate(42, 42)">
              <rect x="-2" y="-21.5" width="4" height="8.5" transform="rotate(0)" />
              <rect x="-1.9" y="-21.5" width="3.8" height="8.5" transform="rotate(15)" />
              <rect x="-1.8" y="-21.5" width="3.6" height="8.5" transform="rotate(30)" />
              <rect x="-1.7" y="-21.5" width="3.4" height="8.5" transform="rotate(45)" />
              <rect x="-1.6" y="-21.5" width="3.2" height="8.5" transform="rotate(60)" />
              <rect x="-1.5" y="-21.5" width="3" height="8.5" transform="rotate(75)" />
              <rect x="-1.4" y="-21.5" width="2.8" height="8.5" transform="rotate(90)" />
              <rect x="-1.3" y="-21.5" width="2.6" height="8.5" transform="rotate(105)" />
              <rect x="-1.2" y="-21.5" width="2.4" height="8.5" transform="rotate(120)" />
              <rect x="-1.1" y="-21.5" width="2.2" height="8.5" transform="rotate(135)" />
              <rect x="-1.05" y="-21.5" width="2.1" height="8.5" transform="rotate(150)" />
              <rect x="-1" y="-21.5" width="2" height="8.5" transform="rotate(165)" />
              <rect x="-0.95" y="-21.5" width="1.9" height="8.5" transform="rotate(180)" />
              <rect x="-1" y="-21.5" width="2" height="8.5" transform="rotate(195)" />
              <rect x="-1.1" y="-21.5" width="2.2" height="8.5" transform="rotate(210)" />
              <rect x="-1.2" y="-21.5" width="2.4" height="8.5" transform="rotate(225)" />
              <rect x="-1.35" y="-21.5" width="2.7" height="8.5" transform="rotate(240)" />
              <rect x="-1.5" y="-21.5" width="3" height="8.5" transform="rotate(255)" />
              <rect x="-1.65" y="-21.5" width="3.3" height="8.5" transform="rotate(270)" />
              <rect x="-1.8" y="-21.5" width="3.6" height="8.5" transform="rotate(285)" />
              <rect x="-1.95" y="-21.5" width="3.9" height="8.5" transform="rotate(300)" />
              <rect x="-2.1" y="-21.5" width="4.2" height="8.5" transform="rotate(315)" />
              <rect x="-2.2" y="-21.5" width="4.4" height="8.5" transform="rotate(330)" />
              <rect x="-2.3" y="-21.5" width="4.6" height="8.5" transform="rotate(345)" />

              <rect x="-3" y="-35" width="6" height="10.5" transform="rotate(0)" />
              <rect x="-1.3" y="-35" width="2.6" height="10.5" transform="rotate(15)" />
              <rect x="-1.4" y="-35" width="2.8" height="10.5" transform="rotate(30)" />
              <rect x="-1.5" y="-35" width="3" height="10.5" transform="rotate(45)" />
              <rect x="-1.6" y="-35" width="3.2" height="10.5" transform="rotate(60)" />
              <rect x="-1.7" y="-35" width="3.4" height="10.5" transform="rotate(75)" />
              <rect x="-1.8" y="-35" width="3.6" height="10.5" transform="rotate(90)" />
              <rect x="-1.9" y="-35" width="3.8" height="10.5" transform="rotate(105)" />
              <rect x="-2" y="-35" width="4" height="10.5" transform="rotate(120)" />
              <rect x="-2.1" y="-35" width="4.2" height="10.5" transform="rotate(135)" />
              <rect x="-2.2" y="-35" width="4.4" height="10.5" transform="rotate(150)" />
              <rect x="-2.3" y="-35" width="4.6" height="10.5" transform="rotate(165)" />
              <rect x="-2.4" y="-35" width="4.8" height="10.5" transform="rotate(180)" />
              <rect x="-2.5" y="-35" width="5" height="10.5" transform="rotate(195)" />
              <rect x="-2.6" y="-35" width="5.2" height="10.5" transform="rotate(210)" />
              <rect x="-2.7" y="-35" width="5.4" height="10.5" transform="rotate(225)" />
              <rect x="-2.8" y="-35" width="5.6" height="10.5" transform="rotate(240)" />
              <rect x="-2.9" y="-35" width="5.8" height="10.5" transform="rotate(255)" />
              <rect x="-3" y="-35" width="6" height="10.5" transform="rotate(270)" />
              <rect x="-3.1" y="-35" width="6.2" height="10.5" transform="rotate(285)" />
              <rect x="-3.2" y="-35" width="6.4" height="10.5" transform="rotate(300)" />
              <rect x="-3.3" y="-35" width="6.6" height="10.5" transform="rotate(315)" />
              <rect x="-3.4" y="-35" width="6.8" height="10.5" transform="rotate(330)" />
              <rect x="-3.5" y="-35" width="7" height="10.5" transform="rotate(345)" />
            </g>
          </svg>
          <div class="innlab-cintillo__icesi-stack">
            <span class="innlab-cintillo__u">Universidad</span>
            <span class="innlab-cintillo__icesi">icesi</span>
          </div>
        </div>

        <span class="innlab-cintillo__divider" aria-hidden="true"></span>

        <div class="innlab-cintillo__innlab">
          <span class="innlab-cintillo__innlab-name">INNLAB</span>
          <span class="innlab-cintillo__innlab-sub">Centro de<br />Innovación</span>
        </div>

        <span class="innlab-cintillo__divider" aria-hidden="true"></span>

        <p class="innlab-cintillo__nodo">
          Nodo de Innovación Basada en Inteligencia<br />
          Artificial y Tecnologías Digitales
        </p>
      </div>

      <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
        <span class="foresight-pill">Foresight · Anticipa el futuro, decide hoy.</span>
      </div>

      <div class="header-title-block">
        <div>
          <p class="kicker">Radar de señales · YouTube & Whisper AI</p>
          <h1>Panel de Vigilancia Tecnológica y Foresight a Medida</h1>
          <p>Selecciona la vertical o el encargo del cliente, ajusta las keywords bilingües y genera las ScanCards sin usar terminal.</p>
        </div>
      </div>
    </header>
  </div>

  <main class="container">
    <section>
      <div class="card">
        <h2 class="card-title">1. Vertical de vigilancia o encargo a medida</h2>
        <p class="card-sub">Carga automáticamente los canales en español e inglés y sus palabras clave base desde <code>config.yaml</code>.</p>
        <div id="configWarning" class="config-warning" style="display:none;"></div>
        <div id="verticalGrid" class="vertical-grid"></div>

        <div id="uqPanel" class="uq-box">
          <p class="uq-box-title">Presets por Eje · Brief Universidad del Quindío (Estatuto de Investigaciones 2030 / 2040)</p>
          <div id="ejesGrid" class="ejes-grid"></div>
        </div>
      </div>

      <div class="card">
        <h2 class="card-title">
          <span>2. Keywords de búsqueda (ES / EN)</span>
          <button type="button" id="resetKwBtn" class="btn btn-secondary" style="padding:8px 14px;font-size:13.5px;">
            Restaurar de la vertical
          </button>
        </h2>
        <p class="card-sub">Añade o elimina términos clave. Si dejas la caja vacía, analizará los videos más recientes del periodo.</p>

        <div class="kw-input-row">
          <input type="text" id="kwInput" placeholder="Escribe una keyword y presiona Enter (ej. open banking, descarga horaria, CRISPR)..." />
          <button type="button" id="addKwBtn" class="btn btn-secondary">Agregar</button>
        </div>
        <div id="chipsBox" class="chips-box"></div>
      </div>

      <div class="card">
        <h2 class="card-title">
          <span>3. Parámetros de extracción, anti-bloqueo y Whisper</span>
          <button type="button" id="resetDefaultsBtn" class="btn btn-secondary" style="padding:8px 14px;font-size:13.5px;">
            Valores recomendados
          </button>
        </h2>
        <p class="card-sub">Preconfigurado con valores equilibrados para resultados ágiles y bajo riesgo de bloqueo en YouTube.</p>

        <div class="form-grid">
          <div class="field">
            <label for="idiomaSelect">Idioma de los canales</label>
            <select id="idiomaSelect">
              <option value="todos" selected>Todos (Canales ES + EN)</option>
              <option value="es">Solo Español (Canales ES)</option>
              <option value="en">Solo Inglés (Canales EN)</option>
            </select>
          </div>

          <div class="field">
            <label for="modoSelect">Modo de búsqueda en YouTube</label>
            <select id="modoSelect">
              <option value="canales" selected>Por canales (Uploads + filtro local · Ahorra cuota)</option>
              <option value="busqueda">Búsqueda directa (search.list por keyword)</option>
            </select>
          </div>

          <div class="field">
            <label for="diasInput">Ventana de tiempo (días atrás)</label>
            <input type="number" id="diasInput" min="1" max="365" value="30" />
            <span class="field-hint">Recomendado: 30 días (45–60 para canales académicos).</span>
          </div>

          <div class="field">
            <label for="maxVideosInput">Máx. videos por canal</label>
            <input type="number" id="maxVideosInput" min="1" max="20" value="1" />
            <span class="field-hint">Recomendado: 1 para barridos rápidos, 2 para mayor cobertura.</span>
          </div>

          <div class="field">
            <label for="maxMinutosInput">Duración máxima por video (min)</label>
            <input type="number" id="maxMinutosInput" min="0" max="180" value="15" />
            <span class="field-hint">Recomendado: 15–20 min (evita charlas largas de 1–9 horas).</span>
          </div>

          <div class="field">
            <label for="maxCanalesInput">Tope de canales a revisar (0 = todos)</label>
            <input type="number" id="maxCanalesInput" min="0" max="200" value="12" />
            <span class="field-hint">Recomendado: 12 en pruebas ágiles; 0 para todos los configurados.</span>
          </div>

          <div class="field">
            <label for="pausaMinInput">Pausa mínima entre videos (seg)</label>
            <input type="number" id="pausaMinInput" min="0" max="60" step="0.5" value="1.5" />
            <span class="field-hint">Ayuda a evitar límites o bloqueos por ráfagas.</span>
          </div>

          <div class="field">
            <label for="pausaMaxInput">Pausa máxima entre videos (seg)</label>
            <input type="number" id="pausaMaxInput" min="0" max="120" step="0.5" value="3.5" />
            <span class="field-hint">Aplica variación aleatoria (jitter) sobre la pausa mínima.</span>
          </div>

          <div class="field">
            <label for="whisperSelect">Modelo local Whisper (sin subtítulos)</label>
            <select id="whisperSelect">
              <option value="tiny">tiny (ultra rápido)</option>
              <option value="base">base (rápido)</option>
              <option value="small" selected>small (equilibrado · recomendado)</option>
              <option value="medium">medium (alta precisión · más lento)</option>
            </select>
          </div>

          <div class="field">
            <label for="canalesInput">Filtrar canales puntuales (opcional)</label>
            <input type="text" id="canalesInput" placeholder="Ej. CNN, Portafolio, Bloomberg Línea" />
          </div>

          <div class="field">
            <label for="proxyInput">Proxy opcional (http/https/socks5)</label>
            <input type="text" id="proxyInput" placeholder="Ej. http://usuario:clave@host:puerto" />
            <span class="field-hint">Opcional: útil si tu IP recibe bloqueos temporales en YouTube.</span>
          </div>

          <div class="field">
            <label for="cookiesInput">Archivo cookies.txt o navegador (yt-dlp)</label>
            <input type="text" id="cookiesInput" placeholder="Ruta cookies.txt o navegador (ej. chrome, edge)" />
            <span class="field-hint">Ayuda en videos con HTTP 403 al descargar audio.</span>
          </div>

          <div class="field full">
            <label class="checkbox-row">
              <input type="checkbox" id="forzarCheck" />
              <span>Forzar reprocesamiento (ignorar caché SQLite de videos ya resumidos)</span>
            </label>
          </div>
        </div>
      </div>
    </section>

    <section>
      <div class="card">
        <h2 class="card-title">
          <span>Ejecución del radar</span>
          <span id="statusBadge" class="status-badge">Listo</span>
        </h2>
        <p class="card-sub">Extrae videos, transcribe audio con Whisper si no hay subtítulos, resume y genera las ScanCards.</p>

        <div style="display:flex; gap:12px; margin-bottom:18px; flex-wrap:wrap;">
          <button type="button" id="runBtn" class="btn btn-primary" style="flex:1; min-width:240px;">
            ▶ Ejecutar radar y generar ScanCards
          </button>
          <button type="button" id="clearKwBtn" class="btn btn-secondary">
            Limpiar keywords
          </button>
        </div>

        <label style="display:block; margin-bottom:8px;">Comando equivalente (referencia)</label>
        <div id="cmdPreview" class="cmd-preview"></div>
      </div>

      <div class="card">
        <h2 class="card-title">Progreso en tiempo real</h2>
        <p class="card-sub">Registro en vivo de canales consultados, Whisper local y generación del reporte HTML.</p>
        <div id="terminalBox" class="terminal">Esperando configuración y ejecución...</div>
      </div>
    </section>
  </main>

  <section id="scancardsFooter" class="scancards-footer">
    <div class="scancards-footer-bar">
      <div>
        <h2 class="scancards-footer-title">ScanCards y exportación (CSV + PDFs por ID)</h2>
        <p id="scancardsFooterStatus" class="scancards-footer-sub">
          Ejecuta el radar o exporta los resultados guardados en CSV homologado + PDFs individuales.
        </p>
      </div>
      <div class="scancards-footer-actions">
        <button type="button" id="exportCsvZipBtn" class="btn btn-primary">
          Exportar CSV + PDFs (.ZIP) ⭳
        </button>
        <a id="openReportBtn" href="/reporte" target="_blank" rel="noopener" class="btn btn-success disabled">
          Abrir ScanCards HTML ↗
        </a>
        <a id="downloadPdfBtn" href="/reporte?print=1" target="_blank" rel="noopener" class="btn btn-pdf disabled">
          Descargar PDF ⭳
        </a>
      </div>
    </div>
  </section>

  <div id="exportModal" class="modal-backdrop">
    <div class="modal-card">
      <h3 style="margin:0 0 8px; color:var(--icesi-navy);">Secuencia de IDs para CSV y PDFs</h3>
      <p style="margin:0 0 14px; font-size:14px; color:var(--muted);">
        Indica cuál fue el <strong>último ID</strong> en tu CSV base para continuar automáticamente desde el siguiente (por ejemplo, si escribes <code>REC-0179</code>, la exportación iniciará en <code>REC-0180</code>).
      </p>
      <div class="field" style="margin-bottom:18px;">
        <label for="ultimoIdInput">Último ID existente en el CSV</label>
        <input type="text" id="ultimoIdInput" value="REC-0179" placeholder="Ej. REC-0179" />
        <span class="field-hint">Cada fila del CSV tendrá su propio archivo <code><ID>.pdf</code> con la transcripción literal completa.</span>
      </div>
      <div style="display:flex; justify-content:flex-end; gap:10px;">
        <button type="button" id="cancelExportBtn" class="btn btn-secondary">Cancelar</button>
        <button type="button" id="confirmExportBtn" class="btn btn-primary">Generar y descargar .ZIP</button>
      </div>
    </div>
  </div>

  <script>
    const RECOMMENDED_DEFAULTS = {
      idioma: "todos",
      modo: "canales",
      dias: 30,
      max_videos: 1,
      max_minutos: 15,
      max_canales: 12,
      whisper_model: "small",
      pausa_min: 1.5,
      pausa_max: 3.5,
      proxy: "",
      cookies: ""
    };

    const UQ_EJES = [
      { label: "Todos los ejes UQ", keywords: [] },
      {
        label: "Eje 1 · Financiación CTeI y SGR",
        keywords: ["financiación universidad pública", "Sistema General de Regalías", "public university funding", "performance-based research funding", "full economic costing"]
      },
      {
        label: "Eje 2 · Evaluación (DORA / CoARA)",
        keywords: ["evaluación de la investigación", "métricas responsables", "investigación-creación", "research assessment reform", "CoARA", "DORA", "field-sensitive research evaluation"]
      },
      {
        label: "Eje 3 · IA e integridad científica",
        keywords: ["ética e integridad científica", "inteligencia artificial investigación", "university AI policy", "research integrity", "predatory journals", "AI authorship"]
      },
      {
        label: "Eje 4 · Territorio, Café y Agua",
        keywords: ["territorio y sostenibilidad", "paisaje cultural cafetero", "una sola salud", "place-based research", "water governance", "climate adaptation", "One Health"]
      },
      {
        label: "Eje 5 · Talento y descarga horaria",
        keywords: ["descarga horaria investigación", "relevo generacional investigación", "academic workload allocation", "teaching release", "research incentives", "doctoral training"]
      },
      {
        label: "Eje 6 · Estatuto, gobernanza y transferencia",
        keywords: ["estatuto de investigación", "gobernanza universitaria", "transferencia de conocimiento", "apropiación social del conocimiento", "university research policy", "research governance", "technology transfer"]
      },
      {
        label: "Eje 7 · Áreas emergentes",
        keywords: ["bioeconomía circular", "humanidades digitales", "salud mental", "emerging research fields", "circular bioeconomy", "digital humanities", "applied biotechnology"]
      }
    ];

    let appConfig = { verticales: {} };
    let selectedVertical = "territorio_sostenibilidad";
    let currentKeywords = [];
    let pollTimer = null;

    const LABELS = {
      territorio_sostenibilidad: "Territorio y Sostenibilidad",
      fintech: "Fintech",
      tecnologia: "Tecnología",
      agrotech: "Agrotech",
      biotech: "Biotech",
      uniquindio_estatuto: "Uniquindío · Estatuto (A medida)",
      personalizado: "Foresight a medida (Libre)"
    };

    function applyRecommendedDefaults(forVertical = selectedVertical) {
      document.getElementById("idiomaSelect").value = RECOMMENDED_DEFAULTS.idioma;
      document.getElementById("modoSelect").value = RECOMMENDED_DEFAULTS.modo;
      document.getElementById("diasInput").value = forVertical === "uniquindio_estatuto" ? 45 : RECOMMENDED_DEFAULTS.dias;
      document.getElementById("maxVideosInput").value = RECOMMENDED_DEFAULTS.max_videos;
      document.getElementById("maxMinutosInput").value = forVertical === "uniquindio_estatuto" ? 20 : RECOMMENDED_DEFAULTS.max_minutos;
      document.getElementById("maxCanalesInput").value = RECOMMENDED_DEFAULTS.max_canales;
      document.getElementById("pausaMinInput").value = RECOMMENDED_DEFAULTS.pausa_min;
      document.getElementById("pausaMaxInput").value = RECOMMENDED_DEFAULTS.pausa_max;
      document.getElementById("whisperSelect").value = RECOMMENDED_DEFAULTS.whisper_model;
      document.getElementById("proxyInput").value = RECOMMENDED_DEFAULTS.proxy;
      document.getElementById("cookiesInput").value = RECOMMENDED_DEFAULTS.cookies;
      document.getElementById("forzarCheck").checked = false;
    }

    function getKeywordsForVerticalAndLang(vert, lang) {
      if (vert === "personalizado") return [];
      const vObj = appConfig.verticales?.[vert];
      if (!vObj) return [];
      const kw = vObj.keywords || {};
      if (Array.isArray(kw)) return [...kw];
      const es = Array.isArray(kw.es) ? kw.es : [];
      const en = Array.isArray(kw.en) ? kw.en : [];
      if (lang === "es") return [...es];
      if (lang === "en") return [...en];
      return [...new Set([...es, ...en])];
    }

    function renderVerticalCards() {
      const grid = document.getElementById("verticalGrid");
      const warn = document.getElementById("configWarning");
      grid.innerHTML = "";
      const vertEntries = Object.entries(appConfig.verticales || {});

      if (!vertEntries.length) {
        warn.style.display = "block";
        warn.textContent = appConfig._config_error
          ? appConfig._config_error
          : "No se detectaron verticales en config.yaml. Revisa el archivo o usa 'Foresight a medida (Libre)'.";
      } else {
        warn.style.display = "none";
      }

      const keys = [...vertEntries.map(([k]) => k), "personalizado"];
      if (!keys.includes(selectedVertical)) {
        selectedVertical = keys[0] || "personalizado";
      }

      keys.forEach((key) => {
        const vObj = appConfig.verticales?.[key];
        const esCount = vObj?.canales_es?.length || 0;
        const enCount = vObj?.canales_en?.length || 0;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "v-pill" + (selectedVertical === key ? " active" : "");
        btn.innerHTML = `
          <span class="v-name">${LABELS[key] || key}</span>
          <span class="v-meta">${
            key === "personalizado"
              ? "Usa todos los canales + tus keywords"
              : `${esCount} canales ES · ${enCount} canales EN`
          }</span>
        `;
        btn.onclick = () => selectVertical(key);
        grid.appendChild(btn);
      });
    }

    function renderUQEjes() {
      const panel = document.getElementById("uqPanel");
      const grid = document.getElementById("ejesGrid");
      if (selectedVertical !== "uniquindio_estatuto") {
        panel.classList.remove("visible");
        return;
      }
      panel.classList.add("visible");
      grid.innerHTML = "";
      UQ_EJES.forEach((eje, idx) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "eje-btn";
        b.textContent = eje.label;
        b.onclick = () => {
          document.querySelectorAll(".eje-btn").forEach((el) => el.classList.remove("active"));
          b.classList.add("active");
          if (idx === 0) {
            const lang = document.getElementById("idiomaSelect").value;
            currentKeywords = getKeywordsForVerticalAndLang("uniquindio_estatuto", lang);
          } else {
            currentKeywords = [...eje.keywords];
          }
          renderChips();
          updateCommandPreview();
        };
        grid.appendChild(b);
      });
    }

    function selectVertical(key) {
      selectedVertical = key;
      const lang = document.getElementById("idiomaSelect").value;
      currentKeywords = getKeywordsForVerticalAndLang(key, lang);
      if (key === "uniquindio_estatuto") {
        if (parseInt(document.getElementById("diasInput").value || "30", 10) < 45) {
          document.getElementById("diasInput").value = 45;
        }
        if (parseInt(document.getElementById("maxMinutosInput").value || "15", 10) === 15) {
          document.getElementById("maxMinutosInput").value = 20;
        }
      }
      renderVerticalCards();
      renderUQEjes();
      renderChips();
      updateCommandPreview();
    }

    function renderChips() {
      const box = document.getElementById("chipsBox");
      box.innerHTML = "";
      if (!currentKeywords.length) {
        box.innerHTML = `<span style="font-size:14px;color:#565e75;">Sin filtro de keywords (analizará los videos más recientes del periodo).</span>`;
        return;
      }
      currentKeywords.forEach((kw, index) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.innerHTML = `<span>${kw}</span><button type="button" title="Quitar">×</button>`;
        chip.querySelector("button").onclick = () => {
          currentKeywords.splice(index, 1);
          renderChips();
          updateCommandPreview();
        };
        box.appendChild(chip);
      });
    }

    function addKeywordFromInput() {
      const input = document.getElementById("kwInput");
      const parts = input.value.split(",").map((s) => s.trim()).filter(Boolean);
      parts.forEach((p) => {
        if (!currentKeywords.some((k) => k.toLowerCase() === p.toLowerCase())) {
          currentKeywords.push(p);
        }
      });
      input.value = "";
      renderChips();
      updateCommandPreview();
    }

    function collectPayload() {
      const rawMin = document.getElementById("maxMinutosInput").value;
      const rawCan = document.getElementById("maxCanalesInput").value;
      const rawCookies = document.getElementById("cookiesInput").value.trim();
      const lowerCookies = rawCookies.toLowerCase();
      const browsers = ["chrome", "edge", "firefox", "brave", "opera", "chromium"];
      const isBrowser = browsers.includes(lowerCookies);

      return {
        vertical: selectedVertical === "personalizado" ? "todas" : selectedVertical,
        idioma: document.getElementById("idiomaSelect").value,
        modo: document.getElementById("modoSelect").value,
        dias: parseInt(document.getElementById("diasInput").value || String(RECOMMENDED_DEFAULTS.dias), 10),
        max_videos: parseInt(document.getElementById("maxVideosInput").value || String(RECOMMENDED_DEFAULTS.max_videos), 10),
        max_minutos: rawMin === "" ? RECOMMENDED_DEFAULTS.max_minutos : parseInt(rawMin, 10),
        max_canales: rawCan === "" ? RECOMMENDED_DEFAULTS.max_canales : parseInt(rawCan, 10),
        pausa_min: parseFloat(document.getElementById("pausaMinInput").value || String(RECOMMENDED_DEFAULTS.pausa_min)),
        pausa_max: parseFloat(document.getElementById("pausaMaxInput").value || String(RECOMMENDED_DEFAULTS.pausa_max)),
        whisper_model: document.getElementById("whisperSelect").value,
        proxy: document.getElementById("proxyInput").value.trim(),
        cookies: !isBrowser ? rawCookies : "",
        cookies_from_browser: isBrowser ? lowerCookies : "",
        canales: document.getElementById("canalesInput").value.trim(),
        forzar: document.getElementById("forzarCheck").checked,
        keywords: currentKeywords
      };
    }

    function updateCommandPreview() {
      const p = collectPayload();
      const parts = ["python src/youtube_news.py"];
      if (p.vertical && p.vertical !== "todas") parts.push(`--vertical ${p.vertical}`);
      parts.push(`--idioma ${p.idioma}`);
      parts.push(`--modo ${p.modo}`);
      parts.push(`--dias ${p.dias}`);
      parts.push(`--max-videos ${p.max_videos}`);
      parts.push(`--max-minutos ${p.max_minutos}`);
      if (p.max_canales > 0) parts.push(`--max-canales ${p.max_canales}`);
      parts.push(`--pausa-min ${p.pausa_min}`);
      parts.push(`--pausa-max ${p.pausa_max}`);
      parts.push(`--whisper-model ${p.whisper_model}`);
      if (p.proxy) parts.push(`--proxy "${p.proxy}"`);
      if (p.cookies) parts.push(`--cookies "${p.cookies}"`);
      if (p.cookies_from_browser) parts.push(`--cookies-from-browser ${p.cookies_from_browser}`);
      if (p.canales) parts.push(`--canal "${p.canales}"`);
      if (p.keywords.length) parts.push(`--keywords "${p.keywords.join(", ")}"`);
      if (p.forzar) parts.push("--forzar");
      document.getElementById("cmdPreview").textContent = parts.join(" ");
    }

    async function loadLogosIfAvailable() {
      try {
        const res = await fetch("/api/logos");
        const data = await res.json();
        if (data.icesi || data.innlab) {
          const slot = document.getElementById("customLogosSlot");
          const fallback = document.getElementById("defaultBrandLockup");
          let html = "";
          if (data.icesi) {
            html += `<img src="/assets/logo-icesi" alt="Universidad Icesi" class="logo-img" />`;
          }
          if (data.icesi && data.innlab) {
            html += `<span class="innlab-cintillo__divider" aria-hidden="true"></span>`;
          }
          if (data.innlab) {
            html += `<img src="/assets/logo-innlab" alt="INNLAB Centro de Innovación" class="logo-img" />`;
          }
          slot.innerHTML = html;
          slot.style.display = "flex";
          fallback.style.display = "none";
        }
      } catch (_) {}
    }

    async function pollStatus() {
      try {
        const res = await fetch("/api/status");
        const data = await res.json();
        const term = document.getElementById("terminalBox");
        const badge = document.getElementById("statusBadge");
        const runBtn = document.getElementById("runBtn");
        const reportBtn = document.getElementById("openReportBtn");
        const pdfBtn = document.getElementById("downloadPdfBtn");
        const footerStatus = document.getElementById("scancardsFooterStatus");

        if (data.logs && data.logs.length) {
          term.textContent = data.logs.join("\n");
          term.scrollTop = term.scrollHeight;
        }

        reportBtn.classList.toggle("disabled", !data.report_ready);
        pdfBtn.classList.toggle("disabled", !data.report_ready);
        if (footerStatus) {
          footerStatus.textContent = data.report_ready
            ? "ScanCards listas. Exporta CSV + PDFs por ID (.ZIP), abre el reporte HTML o descarga el PDF."
            : "Ejecuta el radar o exporta los resultados guardados en output/ como CSV + PDFs por ID.";
        }

        if (data.running) {
          badge.className = "status-badge running";
          badge.textContent = "Ejecutando...";
          runBtn.disabled = true;
        } else {
          runBtn.disabled = false;
          if (data.last_exit_code === 0) {
            badge.className = "status-badge done";
            badge.textContent = "Completado ✔";
          } else if (data.last_exit_code !== null) {
            badge.className = "status-badge error";
            badge.textContent = `Finalizó con código ${data.last_exit_code}`;
          }
          if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
        }
      } catch (err) {
        console.error(err);
      }
    }

    async function startExecution() {
      const payload = collectPayload();
      const term = document.getElementById("terminalBox");
      const badge = document.getElementById("statusBadge");
      const runBtn = document.getElementById("runBtn");

      runBtn.disabled = true;
      badge.className = "status-badge running";
      badge.textContent = "Iniciando...";
      term.textContent = "Preparando ejecución...\n";

      const res = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.message || "No se pudo iniciar la ejecución.");
        runBtn.disabled = false;
        return;
      }
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollStatus, 1500);
      pollStatus();
    }

    async function init() {
      await loadLogosIfAvailable();
      try {
        const res = await fetch("/api/config");
        const loadedCfg = await res.json();
        appConfig = (loadedCfg && typeof loadedCfg === "object") ? loadedCfg : { verticales: {} };
        if (!appConfig.verticales || typeof appConfig.verticales !== "object") {
          appConfig.verticales = {};
        }
      } catch (err) {
        console.error("Error cargando /api/config:", err);
        appConfig = { verticales: {} };
      }

      const firstKey = Object.keys(appConfig.verticales)[0] || "territorio_sostenibilidad";
      applyRecommendedDefaults(firstKey);
      selectVertical(firstKey);
      pollStatus();

      document.getElementById("addKwBtn").onclick = addKeywordFromInput;
      document.getElementById("kwInput").addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          addKeywordFromInput();
        }
      });
      document.getElementById("resetKwBtn").onclick = () => {
        const lang = document.getElementById("idiomaSelect").value;
        currentKeywords = getKeywordsForVerticalAndLang(selectedVertical, lang);
        renderChips();
        updateCommandPreview();
      };
      document.getElementById("resetDefaultsBtn").onclick = () => {
        applyRecommendedDefaults(selectedVertical);
        updateCommandPreview();
      };
      document.getElementById("clearKwBtn").onclick = () => {
        currentKeywords = [];
        renderChips();
        updateCommandPreview();
      };
      document.getElementById("idiomaSelect").onchange = () => {
        const lang = document.getElementById("idiomaSelect").value;
        currentKeywords = getKeywordsForVerticalAndLang(selectedVertical, lang);
        renderChips();
        updateCommandPreview();
      };
      [
        "modoSelect", "diasInput", "maxVideosInput", "maxMinutosInput",
        "maxCanalesInput", "pausaMinInput", "pausaMaxInput", "whisperSelect",
        "proxyInput", "cookiesInput", "canalesInput", "forzarCheck"
      ].forEach((id) => {
        document.getElementById(id).addEventListener("change", updateCommandPreview);
        document.getElementById(id).addEventListener("input", updateCommandPreview);
      });
      document.getElementById("runBtn").onclick = startExecution;

      const modal = document.getElementById("exportModal");
      document.getElementById("exportCsvZipBtn").onclick = () => {
        modal.style.display = "flex";
        document.getElementById("ultimoIdInput").focus();
        document.getElementById("ultimoIdInput").select();
      };
      document.getElementById("cancelExportBtn").onclick = () => {
        modal.style.display = "none";
      };
      modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.style.display = "none";
      });
      document.getElementById("confirmExportBtn").onclick = async () => {
        const ultimoId = document.getElementById("ultimoIdInput").value.trim();
        if (!ultimoId) {
          alert("Escribe el último ID del CSV (por ejemplo REC-0179).");
          return;
        }
        const confirmBtn = document.getElementById("confirmExportBtn");
        confirmBtn.disabled = true;
        confirmBtn.textContent = "Generando...";
        try {
          const res = await fetch("/api/export-zip", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              ultimo_id: ultimoId,
              keywords: currentKeywords
            })
          });
          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            alert(errData.message || "No se pudo generar la exportación.");
            return;
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "foresight_csv_pdfs.zip";
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
          modal.style.display = "none";
        } catch (err) {
          alert("Error de conexión al exportar el paquete ZIP.");
        } finally {
          confirmBtn.disabled = false;
          confirmBtn.textContent = "Generar y descargar .ZIP";
        }
      };
    }

    init();
  </script>
</body>
</html>
"""


class ForesightRequestHandler(BaseHTTPRequestHandler):
    """Servidor HTTP ligero para la UI local de Foresight."""

    ICESI_CANDIDATES = ["logo_icesi.png", "logo_icesi.svg", "icesi.png", "icesi.svg"]
    INNLAB_CANDIDATES = ["logo_innlab.png", "logo_innlab.svg", "innlab.png", "innlab.svg"]

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html_text: str, status: int = 200) -> None:
        raw = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path) -> None:
        mime, _ = mimetypes.guess_type(str(path))
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML_UI)
            return

        if parsed.path == "/api/logos":
            self._send_json(
                {
                    "icesi": _find_asset(self.ICESI_CANDIDATES) is not None,
                    "innlab": _find_asset(self.INNLAB_CANDIDATES) is not None,
                }
            )
            return

        if parsed.path == "/assets/logo-icesi":
            asset = _find_asset(self.ICESI_CANDIDATES)
            if asset:
                self._send_file(asset)
                return

        if parsed.path == "/assets/logo-innlab":
            asset = _find_asset(self.INNLAB_CANDIDATES)
            if asset:
                self._send_file(asset)
                return

        if parsed.path == "/api/config":
            self._send_json(_load_config_safe())
            return

        if parsed.path == "/api/status":
            with JOBS.lock:
                has_cards = (
                    ((_count_scancards(SCANCARDS_JSON_OUT) or 0) > 0)
                    or ((_count_scancards(JSON_OUT) or 0) > 0)
                    or (len(_extract_cards_from_html_fallback(HTML_OUT)) > 0)
                )
                self._send_json(
                    {
                        "running": JOBS.running,
                        "logs": JOBS.logs,
                        "last_exit_code": JOBS.last_exit_code,
                        "report_ready": HTML_OUT.exists() and has_cards,
                    }
                )
            return

        if parsed.path == "/reporte":
            if HTML_OUT.exists():
                html_content = HTML_OUT.read_text(encoding="utf-8")
                if "print=1" in parsed.query and "window.print()" not in html_content:
                    print_script = (
                        "<script>window.addEventListener('load',function(){"
                        "setTimeout(function(){window.print();},350);});</script>"
                    )
                    if "</body>" in html_content:
                        html_content = html_content.replace("</body>", print_script + "</body>")
                    else:
                        html_content += print_script
                self._send_html(html_content)
            else:
                self._send_html(
                    "<h3>Aún no se ha generado ningún reporte. Ejecuta primero el radar desde el panel.</h3>",
                    status=404,
                )
            return

        self._send_json({"error": "Ruta no encontrada"}, status=404)

    def _read_body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            try:
                params = self._read_body_json()
            except json.JSONDecodeError:
                self._send_json({"message": "JSON inválido."}, status=400)
                return

            started, msg = JOBS.start(params)
            self._send_json({"message": msg}, status=200 if started else 409)
            return

        if parsed.path == "/api/export-zip":
            if exportar_scancards_a_csv_y_pdfs is None:
                self._send_json(
                    {"message": "No se encontró src/exportador_foresight.py."},
                    status=500,
                )
                return

            # Prioriza resultados.json porque contiene la transcripción literal cruda de Whisper
            fuente_json: Path | None = None
            if JSON_OUT.exists() and (_count_scancards(JSON_OUT) or 0) > 0:
                fuente_json = JSON_OUT
            elif SCANCARDS_JSON_OUT.exists() and (_count_scancards(SCANCARDS_JSON_OUT) or 0) > 0:
                fuente_json = SCANCARDS_JSON_OUT
            elif HTML_OUT.exists():
                cards_html = _extract_cards_from_html_fallback(HTML_OUT)
                if cards_html:
                    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                    fallback_json = OUTPUT_DIR / "_fallback_from_html.json"
                    fallback_json.write_text(json.dumps(cards_html, ensure_ascii=False, indent=2), encoding="utf-8")
                    fuente_json = fallback_json

            if fuente_json is None:
                self._send_json(
                    {"message": "No hay resultados guardados en output/ para exportar."},
                    status=400,
                )
                return

            try:
                payload = self._read_body_json()
            except json.JSONDecodeError:
                self._send_json({"message": "JSON inválido."}, status=400)
                return

            ultimo_id = str(payload.get("ultimo_id") or "").strip() or None
            id_inicio = str(payload.get("id_inicio") or "").strip() or None
            keywords_filtro = payload.get("keywords")
            if not isinstance(keywords_filtro, list) or not keywords_filtro:
                keywords_filtro = JOBS.last_keywords

            try:
                resumen = exportar_scancards_a_csv_y_pdfs(
                    json_path=fuente_json,
                    csv_out=OUTPUT_DIR / "scancards_uniquindio_export.csv",
                    pdf_dir=OUTPUT_DIR / "pdfs_transcripciones",
                    id_inicio=id_inicio,
                    ultimo_id=ultimo_id,
                    keywords_filtro=keywords_filtro,
                    interactivo=False,
                )
            except Exception as exc:
                self._send_json({"message": f"Error al exportar: {exc}"}, status=400)
                return

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                csv_path = Path(resumen["csv_path"])
                if csv_path.exists():
                    zf.write(csv_path, arcname=csv_path.name)
                for pdf_str in resumen.get("pdfs_generados", []):
                    pdf_path = Path(pdf_str)
                    if pdf_path.exists():
                        zf.write(pdf_path, arcname=f"pdfs/{pdf_path.name}")
            zip_bytes = buf.getvalue()

            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="foresight_csv_pdfs.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)
            return

        self._send_json({"error": "Ruta no encontrada"}, status=404)


def main() -> None:
    cfg = _load_config_safe()
    verts = list((cfg.get("verticales") or {}).keys())
    server = ThreadingHTTPServer((HOST, PORT), ForesightRequestHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"Panel visual INNLAB Foresight · Universidad Icesi activo en: {url}")
    print(f"Configuración cargada desde: {CONFIG_PATH}")
    if cfg.get("_config_error"):
        print(f"⚠ {cfg['_config_error']}")
    else:
        print(f"Verticales detectadas ({len(verts)}): {', '.join(verts)}")
    print("Presiona Ctrl+C para detener el servidor.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
        server.server_close()


if __name__ == "__main__":
    main()