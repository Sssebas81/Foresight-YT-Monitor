#!/usr/bin/env python3
"""
Módulo independiente de monitoreo y resumen de noticias en YouTube para Foresight.
Soporta múltiples canales en español e inglés por vertical, filtrado por keywords
(desde CLI o config.yaml), exclusión de Shorts, transmisiones en vivo y videos extensos,
transcripción local con faster-whisper cuando no hay subtítulos, caché SQLite y
resumen híbrido (Gemini -> Groq API -> Extractivo local).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

import requests
import yaml
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

# ---------------------------------------------------------------------------
# Configuración de Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("youtube_news")

T = TypeVar("T")


def _ensure_ffmpeg_in_path() -> None:
    """Agrega el binario de imageio-ffmpeg al PATH si no está ya disponible."""
    try:
        import imageio_ffmpeg

        ffmpeg_exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        ffmpeg_dir = str(ffmpeg_exe.parent)
        current_path = os.environ.get("PATH", "")
        if ffmpeg_dir not in current_path:
            os.environ["PATH"] = f"{ffmpeg_dir}{os.pathsep}{current_path}"
    except Exception:
        pass


_ensure_ffmpeg_in_path()


def run_with_heartbeat(
    label: str,
    func: Callable[..., T],
    *args: Any,
    interval: float = 8.0,
    **kwargs: Any,
) -> T:
    """Ejecuta `func` mostrando cada `interval` segundos cuánto tiempo lleva activo."""
    start_ts = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(func, *args, **kwargs)
        while True:
            try:
                return future.result(timeout=interval)
            except concurrent.futures.TimeoutError:
                elapsed = time.monotonic() - start_ts
                logger.info(
                    "  [EN CURSO · %02d:%02d] %s",
                    int(elapsed // 60),
                    int(elapsed % 60),
                    label,
                )


def sleep_with_jitter(delay_min: float, delay_max: float) -> None:
    """Pausa aleatoria acotada para suavizar el ritmo de peticiones hacia YouTube."""
    low = max(0.0, float(delay_min))
    high = max(low, float(delay_max))
    if high <= 0.0:
        return
    wait_s = random.uniform(low, high)
    if wait_s > 0:
        logger.debug("Pausa anti-bloqueo de %.2fs...", wait_s)
        time.sleep(wait_s)


# ---------------------------------------------------------------------------
# Excepciones personalizadas y utilidades de resiliencia
# ---------------------------------------------------------------------------
class QuotaExceededError(RuntimeError):
    """Se lanza cuando una API reporta agotamiento definitivo de cuota diaria."""


class GeminiQuotaExhausted(RuntimeError):
    """Se lanza cuando la API key de Gemini se queda sin solicitudes disponibles."""


def retry_with_backoff(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 2,
    base_delay: float = 2.0,
    max_delay: float = 15.0,
    **kwargs: Any,
) -> T:
    """Ejecuta una función con reintentos exponenciales ante fallos transitorios."""
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except (QuotaExceededError, GeminiQuotaExhausted):
            raise
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 403 and exc.response is not None:
                body = exc.response.text.lower()
                if "quotaexceeded" in body or "dailylimitexceeded" in body:
                    raise QuotaExceededError(
                        "Cuota diaria de YouTube Data API v3 agotada."
                    ) from exc
            if status not in (429, 500, 502, 503, 504) or attempt == max_retries:
                raise
            sleep_time = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, 1)
            logger.warning(
                "HTTP %s en %s. Reintentando en %.1fs (intento %d/%d)...",
                status,
                func.__name__,
                sleep_time,
                attempt + 1,
                max_retries,
            )
            time.sleep(sleep_time)
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(
                tok in err_msg
                for tok in ("429", "resource_exhausted", "quota", "404", "not_found")
            ):
                raise
            is_transient = any(
                token in err_msg
                for token in ("timeout", "connection", "503", "500", "unavailable", "timed out")
            )
            if not is_transient or attempt == max_retries:
                raise
            sleep_time = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, 1)
            logger.warning(
                "Error transitorio (%s). Reintentando en %.1fs (intento %d/%d)...",
                type(exc).__name__,
                sleep_time,
                attempt + 1,
                max_retries,
            )
            time.sleep(sleep_time)
    raise RuntimeError("Se agotaron los reintentos inesperadamente.")


# ---------------------------------------------------------------------------
# Modelos de datos y arquitectura extensible (NewsSource)
# ---------------------------------------------------------------------------
@dataclass
class NewsItem:
    """Representación estandarizada de una pieza informativa (video, RSS, web)."""

    item_id: str
    fuente: str
    vertical: str
    titulo: str
    url: str
    fecha_publicacion: str
    duracion: str = "N/A"
    tipo_fuente: str = "youtube"
    keywords_coincidentes: list[str] = field(default_factory=list)
    contenido_crudo: str | None = field(default=None, repr=False)
    resumen: str = "no disponible"
    puntos_clave: list[str] = field(default_factory=list)

    def to_export_dict(self) -> dict[str, Any]:
        """Serializa el objeto excluyendo el texto crudo de la transcripción."""
        data = asdict(self)
        data.pop("contenido_crudo", None)
        return data


class NewsSource(ABC):
    """Clase base abstracta para cualquier fuente de noticias."""

    def __init__(self, name: str, vertical: str, config: dict[str, Any]) -> None:
        """Guarda la identidad y configuración compartidas por los conectores de fuentes."""
        self.name = name
        self.vertical = vertical
        self.config = config

    @abstractmethod
    def fetch_recent_items(
        self,
        days: int,
        max_items: int,
        keywords: list[str] | None = None,
        search_mode: str = "canales",
    ) -> list[NewsItem]:
        """Obtiene metadatos de ítems publicados en los últimos `days` días."""

    @abstractmethod
    def extract_content(self, item: NewsItem) -> str | None:
        """Extrae el texto base (subtítulos, cuerpo de artículo) para resumir."""


# ---------------------------------------------------------------------------
# Transcriptor local con Whisper (faster-whisper + yt-dlp sin ffprobe)
# ---------------------------------------------------------------------------
class LocalWhisperTranscriber:
    """Descarga el audio directo con yt-dlp y lo transcribe localmente con faster-whisper."""

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        proxy: str | None = None,
        cookies_file: str | None = None,
        cookies_from_browser: str | None = None,
    ) -> None:
        """Configura el modelo, opciones de red/sesión y difiere la carga del modelo."""
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.proxy = (proxy or "").strip() or None
        self.cookies_file = (cookies_file or "").strip() or None
        self.cookies_from_browser = (cookies_from_browser or "").strip() or None
        self._model: WhisperModel | None = None

    def _get_model(self) -> WhisperModel:
        """Carga y reutiliza una única instancia local de faster-whisper."""
        if self._model is None:
            logger.info(
                "Cargando modelo local Whisper ('%s', device=%s, compute_type=%s)...",
                self.model_size,
                self.device,
                self.compute_type,
            )
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def _transcribe_audio_file(self, audio_path: str) -> str | None:
        """Transcribe un archivo de audio y devuelve sus segmentos como texto continuo."""
        model = self._get_model()
        segments, _ = model.transcribe(
            audio_path,
            beam_size=5,
            vad_filter=True,
        )
        lines = [
            re.sub(r"\s+", " ", seg.text).strip()
            for seg in segments
            if getattr(seg, "text", "").strip()
        ]
        full_text = " ".join(lines).strip()
        return full_text if full_text else None

    def _build_yt_dlp_cmd(self, out_template: str, video_url: str) -> list[str]:
        """Construye el comando de yt-dlp incluyendo proxy y cookies si están configurados."""
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "-f",
            "bestaudio[ext=m4a]/bestaudio/best",
            "-o",
            out_template,
        ]
        if self.proxy:
            cmd.extend(["--proxy", self.proxy])
        if self.cookies_file:
            cmd.extend(["--cookies", self.cookies_file])
        elif self.cookies_from_browser:
            cmd.extend(["--cookies-from-browser", self.cookies_from_browser])
        cmd.append(video_url)
        return cmd

    def transcribe_video(self, video_url: str, video_id: str) -> str | None:
        """Descarga audio directo sin conversión a mp3 y lo transcribe con Whisper."""
        with tempfile.TemporaryDirectory(prefix="yt_whisper_") as tmp_dir:
            out_template = str(Path(tmp_dir) / f"{video_id}.%(ext)s")
            cmd = self._build_yt_dlp_cmd(out_template=out_template, video_url=video_url)

            try:
                run_with_heartbeat(
                    f"Descargando audio de [{video_id}] con yt-dlp...",
                    subprocess.run,
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=240,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "yt-dlp falló descargando audio para [%s]: %s",
                    video_id,
                    (exc.stderr or "").strip().splitlines()[-1] if exc.stderr else exc,
                )
                return None
            except Exception as exc:
                logger.warning("Error ejecutando yt-dlp para [%s]: %s", video_id, exc)
                return None

            audio_files = list(Path(tmp_dir).glob(f"{video_id}.*"))
            if not audio_files:
                logger.warning("No se generó archivo de audio temporal para [%s].", video_id)
                return None

            audio_path = str(audio_files[0])

            try:
                return run_with_heartbeat(
                    f"Transcribiendo audio local con Whisper [{video_id}]...",
                    self._transcribe_audio_file,
                    audio_path,
                )
            except Exception as exc:
                logger.warning(
                    "Error transcribiendo audio con Whisper en [%s]: %s",
                    video_id,
                    exc,
                )
                return None


# ---------------------------------------------------------------------------
# Implementación concreta: Canal de YouTube (Data API v3 + Transcript API)
# ---------------------------------------------------------------------------
class YouTubeChannelSource(NewsSource):
    """Extractor de videos regulares desde un canal de YouTube con filtro de keywords."""

    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(
        self,
        name: str,
        vertical: str,
        config: dict[str, Any],
        api_key: str,
        preferred_languages: list[str] | None = None,
        exclude_shorts: bool = True,
        max_shorts_seconds: int = 60,
        exclude_live: bool = True,
        max_duration_minutes: int = 15,
        proxy: str | None = None,
    ) -> None:
        """Configura el canal, idiomas, filtros y proxy opcional."""
        super().__init__(name=name, vertical=vertical, config=config)
        self.api_key = api_key
        self.proxy = (proxy or "").strip() or None
        self.identifier = str(
            config.get("channel_id") or config.get("handle") or ""
        ).strip()
        if not self.identifier:
            raise ValueError(
                f"La fuente '{name}' debe definir 'channel_id' o 'handle' en config.yaml."
            )
        self.preferred_languages = preferred_languages or [
            "es",
            "es-419",
            "es-ES",
            "en",
            "en-US",
            "en-GB",
        ]
        self.exclude_shorts = exclude_shorts
        self.max_shorts_seconds = max_shorts_seconds
        self.exclude_live = exclude_live
        self.max_duration_minutes = max(0, int(max_duration_minutes))
        self.max_duration_seconds = self.max_duration_minutes * 60
        self.session = requests.Session()
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})

    def _yt_get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Realiza una petición GET autenticada a YouTube Data API v3."""
        req_params = {**params, "key": self.api_key}
        url = f"{self.BASE_URL}/{endpoint}"

        def _call() -> dict[str, Any]:
            """Realiza la solicitud HTTP y devuelve la respuesta como JSON."""
            resp = self.session.get(url, params=req_params, timeout=20)
            resp.raise_for_status()
            return resp.json()

        return retry_with_backoff(_call)

    def _resolve_channel_details(self) -> tuple[str, str, str]:
        """Devuelve (channel_id, uploads_playlist_id, canonical_title)."""
        params: dict[str, Any] = {"part": "snippet,contentDetails"}
        if self.identifier.startswith("UC") and len(self.identifier) == 24:
            params["id"] = self.identifier
        else:
            handle = (
                self.identifier
                if self.identifier.startswith("@")
                else f"@{self.identifier}"
            )
            params["forHandle"] = handle

        data = self._yt_get("channels", params)
        items = data.get("items", [])
        if not items:
            raise ValueError(
                f"No se encontró el canal de YouTube para '{self.identifier}'."
            )

        channel_info = items[0]
        channel_id = channel_info["id"]
        canonical_title = channel_info["snippet"]["title"]
        uploads_id = channel_info["contentDetails"]["relatedPlaylists"]["uploads"]
        return channel_id, uploads_id, canonical_title

    @staticmethod
    def _parse_iso_duration_seconds(iso_duration: str) -> int | None:
        """Convierte duración ISO 8601 (ej. PT1H4M22S) a segundos totales."""
        if not iso_duration or iso_duration == "P0D":
            return None
        match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso_duration)
        if not match:
            return None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        return hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def _format_seconds_duration(total_seconds: int | None) -> str:
        """Formatea segundos a MM:SS o HH:MM:SS."""
        if total_seconds is None:
            return "En vivo / N/A"
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _match_keywords(text: str, keywords: list[str]) -> list[str]:
        """Devuelve la lista de keywords que aparecen en `text` (case-insensitive)."""
        if not keywords:
            return []
        haystack = text.lower()
        matched: list[str] = []
        for kw in keywords:
            clean_kw = kw.strip()
            if not clean_kw:
                continue
            pattern = r"\b" + re.escape(clean_kw.lower()) + r"\b"
            if re.search(pattern, haystack) or clean_kw.lower() in haystack:
                matched.append(clean_kw)
        return matched

    def _fetch_videos_metadata(
        self, video_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Obtiene duración, descripción completa, etiquetas y estado en vivo."""
        meta: dict[str, dict[str, Any]] = {}
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            data = self._yt_get(
                "videos",
                {
                    "part": "contentDetails,snippet,liveStreamingDetails",
                    "id": ",".join(batch),
                },
            )
            for item in data.get("items", []):
                vid = item["id"]
                snippet = item.get("snippet", {})
                raw_dur = item.get("contentDetails", {}).get("duration", "")
                seconds = self._parse_iso_duration_seconds(raw_dur)
                broadcast = snippet.get("liveBroadcastContent", "none")
                has_live_details = "liveStreamingDetails" in item
                is_live = (
                    broadcast in ("live", "upcoming")
                    or raw_dur == "P0D"
                    or (has_live_details and seconds is None)
                )
                meta[vid] = {
                    "seconds": seconds,
                    "formatted_duration": self._format_seconds_duration(seconds),
                    "is_live": is_live,
                    "description": snippet.get("description", ""),
                    "tags": snippet.get("tags", []) or [],
                }
        return meta

    def _collect_via_uploads_playlist(
        self,
        uploads_playlist_id: str,
        cutoff_dt: datetime,
        search_cap: int,
    ) -> list[dict[str, str]]:
        """Recorre los videos más recientes del canal usando playlistItems.list (1 unidad/llamada)."""
        collected: list[dict[str, str]] = []
        next_page_token: str | None = None
        stop_pagination = False

        while not stop_pagination and len(collected) < search_cap:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": 50,
            }
            if next_page_token:
                params["pageToken"] = next_page_token

            data = self._yt_get("playlistItems", params)
            items = data.get("items", [])
            if not items:
                break

            for entry in items:
                snippet = entry.get("snippet", {})
                content_details = entry.get("contentDetails", {})
                video_id = content_details.get("videoId") or snippet.get(
                    "resourceId", {}
                ).get("videoId")
                published_str = content_details.get("videoPublishedAt") or snippet.get(
                    "publishedAt"
                )
                if not video_id or not published_str:
                    continue

                published_dt = datetime.fromisoformat(
                    published_str.replace("Z", "+00:00")
                )
                if published_dt < cutoff_dt:
                    stop_pagination = True
                    break

                collected.append(
                    {
                        "video_id": video_id,
                        "title": snippet.get("title", "Sin título").strip(),
                        "description": snippet.get("description", ""),
                        "published_iso": published_dt.isoformat(),
                    }
                )
                if len(collected) >= search_cap:
                    break

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return collected

    def _collect_via_channel_search(
        self,
        channel_id: str,
        cutoff_dt: datetime,
        keywords: list[str],
        max_results: int = 25,
    ) -> list[dict[str, str]]:
        """Busca videos por keywords dentro del canal usando search.list."""
        query_str = " | ".join(f'"{kw}"' if " " in kw else kw for kw in keywords[:8])
        params: dict[str, Any] = {
            "part": "snippet",
            "channelId": channel_id,
            "q": query_str,
            "type": "video",
            "order": "date",
            "publishedAfter": cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": min(50, max(10, max_results)),
        }
        data = self._yt_get("search", params)
        collected: list[dict[str, str]] = []
        for entry in data.get("items", []):
            vid = entry.get("id", {}).get("videoId")
            snippet = entry.get("snippet", {})
            published_str = snippet.get("publishedAt")
            if not vid or not published_str:
                continue
            published_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            collected.append(
                {
                    "video_id": vid,
                    "title": snippet.get("title", "Sin título").strip(),
                    "description": snippet.get("description", ""),
                    "published_iso": published_dt.isoformat(),
                }
            )
        return collected

    def fetch_recent_items(
        self,
        days: int,
        max_items: int,
        keywords: list[str] | None = None,
        search_mode: str = "canales",
    ) -> list[NewsItem]:
        """Lista videos regulares del canal filtrados por fecha, duración y keywords."""
        channel_id, uploads_playlist_id, _ = self._resolve_channel_details()
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        active_keywords = [k.strip() for k in (keywords or []) if k.strip()]

        if search_mode == "busqueda" and active_keywords:
            collected = self._collect_via_channel_search(
                channel_id=channel_id,
                cutoff_dt=cutoff_dt,
                keywords=active_keywords,
                max_results=max(max_items * 4, 20),
            )
        else:
            scan_cap = max(max_items * 15, 60) if active_keywords else max(max_items * 5, 25)
            collected = self._collect_via_uploads_playlist(
                uploads_playlist_id=uploads_playlist_id,
                cutoff_dt=cutoff_dt,
                search_cap=scan_cap,
            )

        if not collected:
            return []

        videos_meta = self._fetch_videos_metadata([v["video_id"] for v in collected])

        news_items: list[NewsItem] = []
        for v in collected:
            vid = v["video_id"]
            info = videos_meta.get(vid, {})
            seconds = info.get("seconds")
            is_live = bool(info.get("is_live", False))
            formatted_dur = info.get("formatted_duration", "N/A")

            if self.exclude_live and is_live:
                continue

            if (
                self.exclude_shorts
                and seconds is not None
                and seconds <= self.max_shorts_seconds
            ):
                continue

            if (
                self.max_duration_seconds > 0
                and seconds is not None
                and seconds > self.max_duration_seconds
            ):
                logger.info(
                    "  [OMITIDO - DURACIÓN %s > %d min] %s (%s)",
                    formatted_dur,
                    self.max_duration_minutes,
                    v["title"][:55],
                    vid,
                )
                continue

            matched_kws: list[str] = []
            if active_keywords:
                full_desc = info.get("description") or v.get("description", "")
                tags_text = " ".join(info.get("tags", []))
                searchable_blob = f"{v['title']}\n{full_desc}\n{tags_text}"
                matched_kws = self._match_keywords(searchable_blob, active_keywords)
                if not matched_kws:
                    continue

            news_items.append(
                NewsItem(
                    item_id=vid,
                    fuente=self.name,
                    vertical=self.vertical,
                    titulo=v["title"],
                    url=f"https://www.youtube.com/watch?v={vid}",
                    fecha_publicacion=v["published_iso"],
                    duracion=formatted_dur,
                    tipo_fuente="youtube",
                    keywords_coincidentes=matched_kws,
                )
            )
            if len(news_items) >= max_items:
                break

        return news_items

    def _build_transcript_api_client(self) -> Any:
        """Instancia YouTubeTranscriptApi aplicando proxy si está configurado."""
        if not self.proxy:
            return YouTubeTranscriptApi()

        try:
            from youtube_transcript_api.proxies import GenericProxyConfig

            return YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(
                    http_url=self.proxy,
                    https_url=self.proxy,
                )
            )
        except Exception:
            return YouTubeTranscriptApi()

    def extract_content(self, item: NewsItem) -> str | None:
        """
        Extrae los subtítulos del video priorizando español y luego inglés.
        Compatible tanto con youtube-transcript-api >= 1.0 como con versiones 0.6.x.
        """
        try:
            if hasattr(YouTubeTranscriptApi, "list_transcripts"):
                kwargs: dict[str, Any] = {}
                if self.proxy:
                    kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
                transcript_list = YouTubeTranscriptApi.list_transcripts(
                    item.item_id, **kwargs
                )
            else:
                api_client = self._build_transcript_api_client()
                transcript_list = api_client.list(item.item_id)

            transcript = transcript_list.find_transcript(self.preferred_languages)
            fetched = transcript.fetch()

            lines: list[str] = []
            for segment in fetched:
                text = (
                    segment["text"]
                    if isinstance(segment, dict)
                    else getattr(segment, "text", "")
                )
                cleaned = re.sub(r"\s+", " ", text).strip()
                if cleaned:
                    lines.append(cleaned)

            full_text = " ".join(lines).strip()
            return full_text if full_text else None

        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
            logger.warning(
                "Sin subtítulos en YouTube para [%s] '%s' (%s).",
                item.item_id,
                item.titulo[:60],
                type(exc).__name__,
            )
            return None
        except Exception as exc:
            logger.warning(
                "Error obteniendo subtítulos de [%s]: %s",
                item.item_id,
                exc,
            )
            return None


SOURCE_REGISTRY: dict[str, type[NewsSource]] = {
    "youtube": YouTubeChannelSource,
}


# ---------------------------------------------------------------------------
# Caché persistente en SQLite
# ---------------------------------------------------------------------------
class SummaryCache:
    """Almacena en SQLite los videos ya procesados para evitar consumo duplicado."""

    def __init__(self, db_path: Path) -> None:
        """Prepara el directorio y la tabla SQLite donde se guardan videos procesados."""
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Abre una conexión SQLite cuyas filas pueden consultarse por nombre de columna."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Crea la tabla de resultados si todavía no existe."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_items (
                    item_id TEXT PRIMARY KEY,
                    fuente TEXT NOT NULL,
                    vertical TEXT NOT NULL,
                    titulo TEXT NOT NULL,
                    url TEXT NOT NULL,
                    fecha_publicacion TEXT NOT NULL,
                    duracion TEXT NOT NULL,
                    resumen TEXT NOT NULL,
                    puntos_clave TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                )
                """
            )

    def get(self, item_id: str) -> NewsItem | None:
        """Recupera un ítem previamente resumido si existe en caché."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM processed_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if not row:
            return None
        return NewsItem(
            item_id=row["item_id"],
            fuente=row["fuente"],
            vertical=row["vertical"],
            titulo=row["titulo"],
            url=row["url"],
            fecha_publicacion=row["fecha_publicacion"],
            duracion=row["duracion"],
            resumen=row["resumen"],
            puntos_clave=json.loads(row["puntos_clave"]),
        )

    def save(self, item: NewsItem) -> None:
        """Guarda o actualiza el resultado de un ítem procesado."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processed_items (
                    item_id, fuente, vertical, titulo, url,
                    fecha_publicacion, duracion, resumen, puntos_clave, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    fuente = excluded.fuente,
                    vertical = excluded.vertical,
                    titulo = excluded.titulo,
                    resumen = excluded.resumen,
                    puntos_clave = excluded.puntos_clave,
                    processed_at = excluded.processed_at
                """,
                (
                    item.item_id,
                    item.fuente,
                    item.vertical,
                    item.titulo,
                    item.url,
                    item.fecha_publicacion,
                    item.duracion,
                    item.resumen,
                    json.dumps(item.puntos_clave, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )


# ---------------------------------------------------------------------------
# Motores de resumen: Gemini -> Groq API -> Extractivo Local
# ---------------------------------------------------------------------------
class SummaryOutput(BaseModel):
    """Esquema estructurado exigido al LLM para garantizar formato consistente."""

    resumen: str = Field(
        description="Resumen ejecutivo de 5 a 8 líneas en el idioma solicitado."
    )
    puntos_clave: list[str] = Field(
        description=(
            "Entre 3 y 5 viñetas analíticas diferenciadas del resumen "
            "(actores/territorios, datos o mecanismos clave, e implicaciones o señales estratégicas)."
        )
    )


class GroqFallbackSummarizer:
    """Respaldo usando Groq API (OpenAI-compatible) con auto-detección de modelo."""

    BASE_URL = "https://api.groq.com/openai/v1"
    ENDPOINT = f"{BASE_URL}/chat/completions"
    PREFERRED_MODELS = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "gemma2-9b-it",
    ]

    def __init__(self, api_key: str) -> None:
        """Inicializa el cliente HTTP y descubre los modelos disponibles en Groq."""
        self.api_key = api_key.strip().strip('"').strip("'")
        self.session = requests.Session()
        self.models = self._discover_models()

    def _discover_models(self) -> list[str]:
        """Prioriza modelos Groq recomendados y usa la lista base si falla el catálogo."""
        if not self.api_key:
            return list(self.PREFERRED_MODELS)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = self.session.get(f"{self.BASE_URL}/models", headers=headers, timeout=15)
            resp.raise_for_status()
            available = [
                m.get("id", "")
                for m in resp.json().get("data", [])
                if isinstance(m, dict) and m.get("id")
            ]
            ordered = [m for m in self.PREFERRED_MODELS if m in available]
            for m in available:
                if (
                    m not in ordered
                    and any(k in m for k in ("llama", "qwen", "mistral", "gemma"))
                    and "whisper" not in m
                    and "guard" not in m
                ):
                    ordered.append(m)
            return ordered[:4] if ordered else list(self.PREFERRED_MODELS)
        except Exception as exc:
            logger.warning("No se pudo listar modelos en Groq (%s). Usando lista base.", exc)
            return list(self.PREFERRED_MODELS)

    def generate_json(self, prompt: str) -> SummaryOutput:
        """Prueba los modelos disponibles hasta obtener un resumen con el esquema esperado."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None

        for model_name in self.models:
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Responde ÚNICAMENTE con un objeto JSON válido con las claves "
                            'exactas "resumen" (string) y "puntos_clave" (lista de strings).'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }

            def _call() -> SummaryOutput:
                """Envía el prompt a Groq y valida la respuesta JSON recibida."""
                resp = self.session.post(
                    self.ENDPOINT,
                    headers=headers,
                    json=payload,
                    timeout=35,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return SummaryOutput(**json.loads(content))

            try:
                res = run_with_heartbeat(
                    f"Resumiendo con Groq ({model_name})...",
                    retry_with_backoff,
                    _call,
                    max_retries=1,
                )
                time.sleep(1.0)
                return res
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Modelo Groq '%s' falló (%s). Probando siguiente...",
                    model_name,
                    exc,
                )

        assert last_exc is not None
        raise last_exc


class HybridSummarizer:
    """
    Intenta resumir con Gemini primero; si no hay cuota o falla, usa Groq API;
    y si ambos fallan, extrae un resumen base desde la transcripción disponible.
    """

    PREFERRED_GEMINI_ORDER = [
        "gemini-3.8-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-1.5-flash",
    ]

    def __init__(
        self,
        gemini_api_key: str | None,
        groq_api_key: str | None = None,
        target_language: str = "español",
        max_chars_single_pass: int = 24_000,
    ) -> None:
        """Configura Gemini y el respaldo Groq, si se proporcionaron sus claves."""
        self.target_language = target_language
        self.max_chars_single_pass = max_chars_single_pass
        self.gemini_quota_depleted = False
        self.gemini_client: genai.Client | None = None
        self.gemini_models: list[str] = []
        self.groq_client: GroqFallbackSummarizer | None = None

        clean_gemini = (gemini_api_key or "").strip().strip('"').strip("'")
        clean_groq = (groq_api_key or "").strip().strip('"').strip("'")

        if clean_gemini:
            self.gemini_client = genai.Client(
                api_key=clean_gemini,
                http_options=types.HttpOptions(api_version="v1beta", timeout=35_000),
            )
            self.gemini_models = self._discover_gemini_models()

        if clean_groq:
            self.groq_client = GroqFallbackSummarizer(api_key=clean_groq)
            logger.info("Respaldo Groq API habilitado (%s).", ", ".join(self.groq_client.models))

    def _discover_gemini_models(self) -> list[str]:
        """Filtra los modelos Gemini disponibles y los ordena según la prioridad del proyecto."""
        assert self.gemini_client is not None
        available: list[str] = []
        try:
            for m in self.gemini_client.models.list():
                name = (getattr(m, "name", "") or "").replace("models/", "")
                methods = getattr(m, "supported_actions", None) or getattr(
                    m, "supported_generation_methods", []
                )
                if "gemini" in name and (not methods or "generateContent" in methods):
                    available.append(name)
        except Exception as exc:
            logger.warning("No se pudo listar modelos Gemini (%s). Usando lista base.", exc)
            return list(self.PREFERRED_GEMINI_ORDER)

        ordered: list[str] = [m for m in self.PREFERRED_GEMINI_ORDER if m in available]
        for m in available:
            if (
                m not in ordered
                and ("flash" in m or "pro" in m)
                and "image" not in m
                and "tts" not in m
            ):
                ordered.append(m)

        logger.info("Modelos Gemini disponibles: %s", ordered[:5])
        return ordered[:5] if ordered else list(self.PREFERRED_GEMINI_ORDER)

    @staticmethod
    def _compact_transcript(text: str, max_chars: int) -> str:
        """Recorta transcripciones muy extensas conservando inicio, desarrollo y cierre."""
        if len(text) <= max_chars:
            return text
        part = max_chars // 3
        head = text[:part].strip()
        mid_start = max(part, (len(text) // 2) - (part // 2))
        middle = text[mid_start : mid_start + part].strip()
        tail = text[-part:].strip()
        return f"{head}\n[...]\n{middle}\n[...]\n{tail}"

    @staticmethod
    def _extractive_fallback(title: str, transcript: str) -> SummaryOutput:
        """Genera síntesis base y viñetas analíticas diferenciadas cuando no hay LLM."""
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", transcript).strip())
            if len(s.strip()) > 25
        ]
        if not sentences:
            snippet = transcript[:450].strip()
            return SummaryOutput(
                resumen=snippet or f"Registro audiovisual sin transcripción completa: {title}.",
                puntos_clave=[f"Tema central del registro: {title}."],
            )

        resumen = " ".join(sentences[:4])
        tail_candidates = sentences[4:] if len(sentences) > 4 else sentences
        step = max(1, len(tail_candidates) // 3)
        selected = [tail_candidates[i] for i in range(0, len(tail_candidates), step)][:3]
        puntos = [f"Lectura en transcripción: {s}" for s in selected]
        return SummaryOutput(resumen=resumen, puntos_clave=puntos[:5])

    def _try_gemini(self, prompt: str) -> SummaryOutput:
        """Intenta generar el resumen con los modelos Gemini antes de propagar el error."""
        if not self.gemini_client or self.gemini_quota_depleted:
            raise GeminiQuotaExhausted("Gemini no disponible o sin solicitudes restantes.")

        quota_errors = 0
        last_exc: Exception | None = None

        for model_name in self.gemini_models:
            def _invoke() -> SummaryOutput:
                """Solicita a Gemini una respuesta JSON validada contra `SummaryOutput`."""
                assert self.gemini_client is not None
                response = self.gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SummaryOutput,
                        temperature=0.2,
                    ),
                )
                payload = json.loads(response.text)
                return SummaryOutput(**payload)

            try:
                res = run_with_heartbeat(
                    f"Resumiendo con Gemini ({model_name})...",
                    retry_with_backoff,
                    _invoke,
                    max_retries=1,
                )
                time.sleep(1.5)
                return res
            except Exception as exc:
                last_exc = exc
                err_str = str(exc).lower()
                if "429" in err_str or "resource_exhausted" in err_str or "quota" in err_str:
                    quota_errors += 1
                    logger.warning(
                        "  [AVISO CUOTA GEMINI] El modelo '%s' reportó límite de solicitudes (429).",
                        model_name,
                    )
                else:
                    logger.warning(
                        "  Modelo Gemini '%s' no respondió (%s). Probando siguiente...",
                        model_name,
                        type(exc).__name__,
                    )

        if quota_errors > 0:
            self.gemini_quota_depleted = True
            logger.error(
                "====================================================================\n"
                "  [ALERTA] TU API KEY DE GEMINI SE QUEDÓ SIN SOLICITUDES DISPONIBLES\n"
                "  Cambia GEMINI_API_KEY en .env o usa GROQ_API_KEY como respaldo.\n"
                "===================================================================="
            )
            raise GeminiQuotaExhausted(
                "La API key de Gemini agotó sus solicitudes disponibles."
            ) from last_exc

        assert last_exc is not None
        raise last_exc

    def summarize(
        self,
        title: str,
        channel: str,
        transcript: str,
        vertical: str = "general",
        keywords: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Genera el resumen ejecutivo (5-8 líneas) y viñetas analíticas diferenciadas."""
        clean_transcript = self._compact_transcript(transcript, self.max_chars_single_pass)
        focus_line = (
            f"- Vertical de vigilancia: {vertical}\n"
            f"- Keywords de interés: {', '.join(keywords)}\n"
            if keywords
            else f"- Vertical de vigilancia: {vertical}\n"
        )

        final_prompt = f"""
Actúa como un analista senior de vigilancia tecnológica y prospectiva (foresight). Analiza la
siguiente transcripción (obtenida de subtítulos o reconocimiento de voz Whisper) y genera un
reporte estructurado completamente en {self.target_language}.

METADATOS Y ENFOQUE:
- Canal: {channel}
- Título: {title}
{focus_line}
REGLAS ESTRICTAS:
1. "resumen": Redacta un resumen claro, informativo y neutral de entre 5 y 8 líneas
   (aproximadamente 85 a 130 palabras), sin saludos ni frases de relleno como "En este video se habla de...".
   Sintetiza el núcleo del contenido y su relación con la vertical y keywords de interés.
2. "puntos_clave": NO copies ni repitas frases del "resumen". Entrega entre 3 y 5 viñetas
   analíticas breves y distintas entre sí que aporten lectura de análisis y foresight:
   - Actores, territorios, instituciones o tecnologías involucradas.
   - Datos, cifras, proyectos, prácticas o mecanismos específicos mencionados.
   - Implicaciones, riesgos, oportunidades o señales tempranas de cambio.

TRANSCRIPCIÓN DEL VIDEO:
{clean_transcript}
""".strip()

        try:
            result = self._try_gemini(final_prompt)
        except Exception as gemini_exc:
            if self.groq_client is not None:
                logger.info("  [RESPALDO GROQ] Generando resumen y puntos clave con Groq...")
                try:
                    result = self.groq_client.generate_json(final_prompt)
                except Exception as groq_exc:
                    logger.warning(
                        "  [RESPALDO LOCAL] Groq también falló (%s). Extrayendo síntesis base...",
                        groq_exc,
                    )
                    result = self._extractive_fallback(title, clean_transcript)
            else:
                logger.warning(
                    "  [RESPALDO LOCAL] Gemini no disponible (%s). Extrayendo síntesis base...",
                    gemini_exc,
                )
                result = self._extractive_fallback(title, clean_transcript)

        puntos = [p.strip() for p in result.puntos_clave if p.strip()][:5]
        return result.resumen.strip(), puntos


# ---------------------------------------------------------------------------
# Exportación de resultados (JSON y Markdown)
# ---------------------------------------------------------------------------
def export_results(
    items: list[NewsItem],
    json_path: Path,
    md_path: Path,
    days: int,
    vertical_filter: str | None = None,
    keywords_used: list[str] | None = None,
) -> None:
    """Genera resultados.json y resultados.md agrupados por canal y ordenados por fecha."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[NewsItem]] = {}
    for item in items:
        grouped.setdefault(item.fuente, []).append(item)

    for channel_items in grouped.values():
        channel_items.sort(key=lambda x: x.fecha_publicacion, reverse=True)

    json_payload = {
        "metadata": {
            "generado_en": datetime.now(timezone.utc).isoformat(),
            "ventana_dias": days,
            "vertical": vertical_filter or "todas",
            "keywords": keywords_used or [],
            "total_canales": len(grouped),
            "total_videos": len(items),
        },
        "canales": {
            canal: [v.to_export_dict() for v in videos]
            for canal, videos in grouped.items()
        },
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(json_payload, f, ensure_ascii=False, indent=2)

    md_lines: list[str] = [
        "# Monitoreo de Noticias en YouTube · Foresight",
        "",
        f"- **Fecha de generación:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Ventana analizada:** Últimos {days} días",
        f"- **Vertical:** {vertical_filter or 'Todas'}",
        f"- **Keywords:** {', '.join(keywords_used) if keywords_used else 'Sin filtro (todas)'}",
        f"- **Total de videos:** {len(items)}",
        "",
        "---",
        "",
    ]

    if not grouped:
        md_lines.append("_No se encontraron videos publicados que coincidan con los filtros._")
    else:
        for canal, videos in grouped.items():
            vertical = videos[0].vertical if videos else "general"
            md_lines.append(f"## {canal} (`{vertical}`)")
            md_lines.append("")

            for v in videos:
                fecha_corta = v.fecha_publicacion[:10]
                kw_badge = (
                    f" · Keywords: {', '.join(v.keywords_coincidentes)}"
                    if v.keywords_coincidentes
                    else ""
                )
                md_lines.append(f"### [{v.titulo}]({v.url})")
                md_lines.append(f"{v.fuente} · {fecha_corta} · {v.duracion}{kw_badge}")
                md_lines.append("")
                md_lines.append(f"**Resumen:** {v.resumen}")
                md_lines.append("")
                md_lines.append("**Puntos clave:**")
                if v.puntos_clave:
                    for punto in v.puntos_clave:
                        md_lines.append(f"- {punto}")
                else:
                    md_lines.append("- _No disponibles._")
                md_lines.append("")
                md_lines.append("---")
                md_lines.append("")

    with md_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(md_lines).strip() + "\n")


# ---------------------------------------------------------------------------
# Orquestación y CLI
# ---------------------------------------------------------------------------
def load_config(config_path: Path) -> dict[str, Any]:
    """Carga la configuración YAML y falla explícitamente si el archivo no existe."""
    if not config_path.exists():
        raise FileNotFoundError(f"No se encontró el archivo de configuración: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_keywords_arg(raw_keywords: list[str] | None) -> list[str]:
    """Permite pasar keywords separadas por comas o repetidas en CLI."""
    if not raw_keywords:
        return []
    parsed: list[str] = []
    for entry in raw_keywords:
        for piece in entry.split(","):
            clean = piece.strip()
            if clean and clean.lower() not in {p.lower() for p in parsed}:
                parsed.append(clean)
    return parsed


def parse_args() -> argparse.Namespace:
    """Define y analiza las opciones de línea de comandos del monitor."""
    parser = argparse.ArgumentParser(
        description="Extrae y resume noticias recientes desde canales de YouTube para Foresight."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Ruta al archivo config.yaml (por defecto: config.yaml).",
    )
    parser.add_argument(
        "--dias",
        type=int,
        default=None,
        help="Días hacia atrás a consultar (sobrescribe config.yaml).",
    )
    parser.add_argument(
        "--canal",
        type=str,
        default=None,
        help="Filtrar ejecución a uno o varios canales separados por coma (ej. CNN,Bloomberg Línea).",
    )
    parser.add_argument(
        "--vertical",
        type=str,
        default=None,
        help="Vertical de vigilancia (ej. fintech, agrotech, biotech, tecnologia).",
    )
    parser.add_argument(
        "--idioma",
        choices=["es", "en", "todos"],
        default="todos",
        help='Idioma de los canales a consultar: "es", "en" o "todos" (por defecto: todos).',
    )
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=None,
        help='Keywords personalizadas separadas por coma (sobrescriben las de config.yaml).',
    )
    parser.add_argument(
        "--modo",
        choices=["canales", "busqueda"],
        default=None,
        help='Modo de extracción: "canales" (uploads + filtro local) o "busqueda" (search.list).',
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=None,
        help="Máximo de videos por canal (sobrescribe config.yaml).",
    )
    parser.add_argument(
        "--max-minutos",
        type=int,
        default=None,
        help="Duración máxima de video en minutos (sobrescribe config.yaml; 0 = sin límite).",
    )
    parser.add_argument(
        "--max-canales",
        type=int,
        default=0,
        help="Tope opcional de canales a consultar en esta ejecución (0 = todos los filtrados).",
    )
    parser.add_argument(
        "--whisper-model",
        type=str,
        default="small",
        help="Modelo de faster-whisper en CPU (tiny, base, small, medium).",
    )
    parser.add_argument(
        "--pausa-min",
        type=float,
        default=None,
        help="Pausa mínima aleatoria en segundos entre videos (por defecto: 1.5 o YT_DELAY_MIN).",
    )
    parser.add_argument(
        "--pausa-max",
        type=float,
        default=None,
        help="Pausa máxima aleatoria en segundos entre videos (por defecto: 3.5 o YT_DELAY_MAX).",
    )
    parser.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="URL de proxy (ej. http://user:pass@host:port) o usar YT_PROXY en .env.",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=None,
        help="Ruta a archivo cookies.txt para yt-dlp o usar YT_COOKIES_FILE en .env.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        type=str,
        default=None,
        help="Navegador para extraer cookies en yt-dlp (ej. chrome, edge) o YT_COOKIES_BROWSER.",
    )
    parser.add_argument(
        "--forzar",
        action="store_true",
        help="Ignora el caché SQLite y vuelve a procesar todos los videos encontrados.",
    )
    return parser.parse_args()


def _build_sources_from_config(
    cfg: dict[str, Any],
    target_vert: str | None,
    target_lang: str,
    cli_keywords: list[str],
) -> list[dict[str, Any]]:
    """
    Construye la lista de fuentes a consultar combinando:
    - verticales.<nombre>.canales_es / canales_en + keywords.es / keywords.en
    - fuentes planas heredadas si existen en config.yaml
    """
    verticales_cfg = cfg.get("verticales", {}) or {}
    selected_verticals = (
        [target_vert] if target_vert else [str(v).lower() for v in verticales_cfg.keys()]
    )

    built: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    for vert_name in selected_verticals:
        vert_block = verticales_cfg.get(vert_name)
        if not isinstance(vert_block, dict):
            continue

        kw_block = vert_block.get("keywords", {})
        if isinstance(kw_block, dict):
            kw_es = [str(k).strip() for k in kw_block.get("es", []) if str(k).strip()]
            kw_en = [str(k).strip() for k in kw_block.get("en", []) if str(k).strip()]
        elif isinstance(kw_block, list):
            kw_es = [str(k).strip() for k in kw_block if str(k).strip()]
            kw_en = list(kw_es)
        else:
            kw_es, kw_en = [], []

        lang_groups: list[tuple[str, str, list[str]]] = []
        if target_lang in ("es", "todos"):
            lang_groups.append(("es", "canales_es", cli_keywords if cli_keywords else kw_es))
        if target_lang in ("en", "todos"):
            lang_groups.append(("en", "canales_en", cli_keywords if cli_keywords else kw_en))

        for lang_code, list_key, kws in lang_groups:
            for ch in vert_block.get(list_key, []) or []:
                if not isinstance(ch, dict):
                    continue
                nombre = str(ch.get("nombre", "")).strip()
                ident = str(ch.get("handle") or ch.get("channel_id") or "").strip()
                if not nombre or not ident:
                    continue
                dedup_key = (ident.lower(), vert_name)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                built.append(
                    {
                        "nombre": nombre,
                        "tipo": str(ch.get("tipo", "youtube")).lower(),
                        "handle": ch.get("handle"),
                        "channel_id": ch.get("channel_id"),
                        "vertical": vert_name,
                        "idioma_canal": lang_code,
                        "keywords_asignadas": kws,
                    }
                )

    for f in cfg.get("fuentes", []) or []:
        if not isinstance(f, dict):
            continue
        nombre = str(f.get("nombre", "")).strip()
        ident = str(f.get("handle") or f.get("channel_id") or "").strip()
        src_verts = [
            str(v).lower() for v in (f.get("verticales") or [f.get("vertical", "general")])
        ]
        if target_vert and target_vert not in src_verts:
            continue
        vert_label = target_vert or src_verts[0]
        dedup_key = (ident.lower(), vert_label)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        built.append(
            {
                **f,
                "vertical": vert_label,
                "idioma_canal": str(f.get("idioma", "en")).lower(),
                "keywords_asignadas": cli_keywords,
            }
        )

    return built


def main() -> None:
    """Coordina configuración, fuentes, transcripción, resúmenes y exportación de resultados."""
    run_start = time.monotonic()
    load_dotenv()
    args = parse_args()

    youtube_api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()

    if not youtube_api_key:
        raise EnvironmentError("Falta la variable de entorno YOUTUBE_API_KEY.")

    cfg = load_config(args.config)
    general_cfg = cfg.get("general", {})
    rutas_cfg = cfg.get("rutas", {})

    dias = args.dias if args.dias is not None else int(general_cfg.get("dias_atras", 14))
    max_videos = (
        args.max_videos
        if args.max_videos is not None
        else int(general_cfg.get("max_videos_por_canal", 2))
    )
    max_duracion_minutos = (
        args.max_minutos
        if args.max_minutos is not None
        else int(general_cfg.get("max_duracion_minutos", 15))
    )
    search_mode = args.modo or str(general_cfg.get("modo_busqueda", "canales")).lower()
    idioma = str(general_cfg.get("idioma_resumen", "español"))
    excluir_shorts = bool(general_cfg.get("excluir_shorts", True))
    max_shorts_segundos = int(general_cfg.get("max_shorts_segundos", 60))
    excluir_en_vivo = bool(general_cfg.get("excluir_en_vivo", True))
    idiomas_sub = general_cfg.get(
        "idiomas_subtitulos", ["es", "es-419", "es-ES", "en", "en-US", "en-GB"]
    )

    pausa_min = (
        args.pausa_min
        if args.pausa_min is not None
        else float(general_cfg.get("pausa_min_segundos", os.getenv("YT_DELAY_MIN", "1.5")))
    )
    pausa_max = (
        args.pausa_max
        if args.pausa_max is not None
        else float(general_cfg.get("pausa_max_segundos", os.getenv("YT_DELAY_MAX", "3.5")))
    )
    proxy_url = (
        args.proxy
        if args.proxy is not None
        else str(general_cfg.get("proxy", os.getenv("YT_PROXY", ""))).strip()
    ) or None
    cookies_file = (
        str(args.cookies)
        if args.cookies is not None
        else str(general_cfg.get("cookies_file", os.getenv("YT_COOKIES_FILE", ""))).strip()
    ) or None
    cookies_browser = (
        args.cookies_from_browser
        if args.cookies_from_browser is not None
        else str(
            general_cfg.get("cookies_from_browser", os.getenv("YT_COOKIES_BROWSER", ""))
        ).strip()
    ) or None

    cache_path = Path(rutas_cfg.get("cache_db", "cache/youtube_cache.db"))
    transcripts_dir = cache_path.parent / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    json_out = Path(rutas_cfg.get("salida_json", "output/resultados.json"))
    md_out = Path(rutas_cfg.get("salida_md", "output/resultados.md"))

    target_vert = args.vertical.strip().lower() if args.vertical else None
    cli_keywords = parse_keywords_arg(args.keywords)

    fuentes_filtradas = _build_sources_from_config(
        cfg=cfg,
        target_vert=target_vert,
        target_lang=args.idioma,
        cli_keywords=cli_keywords,
    )

    if args.canal:
        target_canales = {c.strip().lower() for c in args.canal.split(",") if c.strip()}
        fuentes_filtradas = [
            f for f in fuentes_filtradas if str(f.get("nombre", "")).lower() in target_canales
        ]

    if args.max_canales and args.max_canales > 0:
        fuentes_filtradas = fuentes_filtradas[: args.max_canales]

    if not fuentes_filtradas:
        logger.warning("No hay fuentes que coincidan con los filtros aplicados.")
        return

    logger.info(
        "Configuración Foresight -> Vertical: %s | Idioma canales: %s | Canales activos: %d | Modo: %s",
        target_vert or "todas",
        args.idioma,
        len(fuentes_filtradas),
        search_mode,
    )

    cache = SummaryCache(cache_path)
    summarizer = HybridSummarizer(
        gemini_api_key=gemini_api_key or None,
        groq_api_key=groq_api_key or None,
        target_language=idioma,
    )
    whisper_transcriber = LocalWhisperTranscriber(
        model_size=args.whisper_model,
        device="cpu",
        compute_type="int8",
        proxy=proxy_url,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_browser,
    )
    all_results: list[NewsItem] = []
    all_keywords_used: list[str] = []
    processed_live_count = 0

    for src_conf in fuentes_filtradas:
        nombre = str(src_conf.get("nombre", "Fuente sin nombre"))
        tipo = str(src_conf.get("tipo", "youtube")).lower()
        vertical_etiqueta = str(src_conf.get("vertical", "general"))
        canal_idioma = str(src_conf.get("idioma_canal", "en"))
        active_keywords = list(src_conf.get("keywords_asignadas", []))

        for kw in active_keywords:
            if kw not in all_keywords_used:
                all_keywords_used.append(kw)

        source_cls = SOURCE_REGISTRY.get(tipo)
        if not source_cls:
            logger.error("Tipo de fuente no soportado '%s' en '%s'.", tipo, nombre)
            continue

        logger.info(
            "Explorando fuente: %s [%s · vertical: %s]...",
            nombre,
            canal_idioma.upper(),
            vertical_etiqueta,
        )
        try:
            source = source_cls(
                name=nombre,
                vertical=vertical_etiqueta,
                config=src_conf,
                api_key=youtube_api_key,
                preferred_languages=idiomas_sub,
                exclude_shorts=excluir_shorts,
                max_shorts_seconds=max_shorts_segundos,
                exclude_live=excluir_en_vivo,
                max_duration_minutes=max_duracion_minutos,
                proxy=proxy_url,
            )
            recent_items = source.fetch_recent_items(
                days=dias,
                max_items=max_videos,
                keywords=active_keywords,
                search_mode=search_mode,
            )
            logger.info(
                "  → %d videos encontrados en los últimos %d días.",
                len(recent_items),
                dias,
            )
        except QuotaExceededError as exc:
            logger.error("Deteniendo ejecución por límite de cuota: %s", exc)
            break
        except Exception as exc:
            logger.error("Error consultando el canal '%s': %s", nombre, exc)
            continue

        for item in recent_items:
            if not args.forzar:
                cached_item = cache.get(item.item_id)
                if cached_item is not None and cached_item.resumen != "no disponible":
                    cached_item.vertical = vertical_etiqueta
                    cached_item.fuente = nombre
                    cached_item.keywords_coincidentes = item.keywords_coincidentes
                    logger.info("  [CACHÉ] %s (%s)", item.titulo[:65], item.item_id)
                    all_results.append(cached_item)
                    continue

            if processed_live_count > 0:
                sleep_with_jitter(pausa_min, pausa_max)
            processed_live_count += 1

            kw_info = (
                f" | Keywords: {', '.join(item.keywords_coincidentes)}"
                if item.keywords_coincidentes
                else ""
            )
            logger.info(
                "  [PROCESANDO] %s (%s)%s...",
                item.titulo[:60],
                item.item_id,
                kw_info,
            )
            transcript_file = transcripts_dir / f"{item.item_id}.txt"

            if transcript_file.exists() and not args.forzar:
                transcript = transcript_file.read_text(encoding="utf-8").strip()
                logger.info("  [TRANSCRIPCIÓN EN CACHÉ] [%s]", item.item_id)
            else:
                transcript = source.extract_content(item)

                if not transcript:
                    logger.info(
                        "  [WHISPER LOCAL] Transcribiendo audio de [%s]...",
                        item.item_id,
                    )
                    transcript = whisper_transcriber.transcribe_video(
                        video_url=item.url,
                        video_id=item.item_id,
                    )

                if transcript:
                    transcript_file.write_text(transcript, encoding="utf-8")

            if not transcript:
                logger.warning(
                    "  No se pudo obtener transcripción ni por subtítulos ni por Whisper para [%s].",
                    item.item_id,
                )
                item.resumen = "no disponible"
                item.puntos_clave = []
                cache.save(item)
                all_results.append(item)
                continue

            try:
                resumen, puntos = summarizer.summarize(
                    title=item.titulo,
                    channel=item.fuente,
                    transcript=transcript,
                    vertical=vertical_etiqueta,
                    keywords=item.keywords_coincidentes or active_keywords,
                )
                item.resumen = resumen
                item.puntos_clave = puntos
                cache.save(item)
                all_results.append(item)
            except Exception as exc:
                logger.error(
                    "  Error generando resumen para [%s]: %s",
                    item.item_id,
                    exc,
                )
                item.resumen = "no disponible"
                item.puntos_clave = []
                all_results.append(item)

    export_results(
        all_results,
        json_path=json_out,
        md_path=md_out,
        days=dias,
        vertical_filter=target_vert,
        keywords_used=all_keywords_used,
    )
    total_elapsed = time.monotonic() - run_start
    logger.info(
        "Proceso finalizado en %02d:%02d. Archivos generados: %s y %s",
        int(total_elapsed // 60),
        int(total_elapsed % 60),
        json_out,
        md_out,
    )


if __name__ == "__main__":
    main()