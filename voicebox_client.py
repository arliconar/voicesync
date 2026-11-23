"""Cliente REST para comunicarse con el servidor FastAPI de Voicebox."""

from __future__ import annotations

import base64
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


class VoiceboxError(Exception):
    """Error base para operaciones con la API de Voicebox."""


class VoiceboxConnectionError(VoiceboxError):
    """Error al conectar con el servidor de Voicebox."""


class VoiceboxCancelledError(VoiceboxError):
    """Error lanzado cuando la generación es cancelada."""


@dataclass(frozen=True)
class VoiceProfile:
    """Representa un perfil de voz disponible en Voicebox."""

    id: str
    name: str
    language: str = ""
    description: str = ""

    @property
    def display_label(self) -> str:
        """Etiqueta legible para mostrar en menús desplegables."""
        short_id = self.id[:8] if len(self.id) > 8 else self.id
        return f"{self.name} [{short_id}]"


class VoiceboxClient:
    """Cliente HTTP para los endpoints REST de Voicebox."""

    def __init__(self, host: str = "127.0.0.1", port: int | str = 17493) -> None:
        self.host, self.port = self._validate_address(host, port)
        self.base_url = f"http://{self.host}:{self.port}"
        self.session = requests.Session()

    @staticmethod
    def _validate_address(host: str, port: int | str) -> tuple[str, int]:
        cleaned_host = host.strip()
        if not cleaned_host:
            raise VoiceboxConnectionError("La dirección IP o host no puede estar vacía.")

        # Quitar esquema si el usuario pegó http:// o https://
        for prefix in ("http://", "https://"):
            if cleaned_host.lower().startswith(prefix):
                cleaned_host = cleaned_host[len(prefix):]
        cleaned_host = cleaned_host.rstrip("/")

        if not cleaned_host or any(c.isspace() for c in cleaned_host):
            raise VoiceboxConnectionError(f"Dirección IP o host inválido: '{host}'.")

        try:
            port_num = int(str(port).strip())
        except (TypeError, ValueError) as exc:
            raise VoiceboxConnectionError(
                f"Puerto inválido: '{port}'. Debe ser un número entre 1 y 65535."
            ) from exc

        if not (1 <= port_num <= 65535):
            raise VoiceboxConnectionError(
                f"Puerto fuera de rango: {port_num}. Debe estar entre 1 y 65535."
            )

        return cleaned_host, port_num

    def check_health(self, timeout: float = 5.0) -> dict[str, Any]:
        """Verifica que el servidor Voicebox esté activo usando GET /health."""
        url = f"{self.base_url}/health"
        try:
            response = self.session.get(url, timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            raise VoiceboxConnectionError(
                f"No se pudo conectar con Voicebox en {self.base_url}. "
                "Verifica que el servidor esté encendido y la IP/puerto sean correctos."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise VoiceboxConnectionError(
                f"Tiempo de espera agotado al conectar con {url}."
            ) from exc
        except requests.exceptions.HTTPError as exc:
            raise VoiceboxConnectionError(
                f"El servidor respondió con error HTTP {response.status_code} en /health."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise VoiceboxConnectionError(f"Error de red al consultar /health: {exc}") from exc

        try:
            data = response.json()
            return data if isinstance(data, dict) else {"status": str(data)}
        except ValueError:
            return {"status": "ok", "raw": response.text.strip()}

    def get_profiles(self, timeout: float = 10.0) -> list[VoiceProfile]:
        """Obtiene la lista de perfiles de voz configurados en GET /profiles."""
        url = f"{self.base_url}/profiles"
        try:
            response = self.session.get(url, timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            raise VoiceboxConnectionError(
                f"No se pudo conectar con {url} para obtener los perfiles."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise VoiceboxConnectionError(
                f"Tiempo de espera agotado al obtener perfiles desde {url}."
            ) from exc
        except requests.exceptions.HTTPError as exc:
            raise VoiceboxError(
                f"Error HTTP {response.status_code} al consultar /profiles: {response.text[:200]}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise VoiceboxError(f"Error de red al consultar /profiles: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise VoiceboxError(
                "La respuesta de /profiles no es un JSON válido."
            ) from exc

        if isinstance(payload, dict):
            raw_profiles = payload.get("profiles") or payload.get("items") or payload.get("data") or []
        elif isinstance(payload, list):
            raw_profiles = payload
        else:
            raise VoiceboxError("Formato inesperado en la respuesta de /profiles.")

        profiles: list[VoiceProfile] = []
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id") or item.get("profile_id") or "").strip()
            name = str(item.get("name") or item.get("title") or profile_id).strip()
            if not profile_id:
                continue
            profiles.append(
                VoiceProfile(
                    id=profile_id,
                    name=name,
                    language=str(item.get("language") or ""),
                    description=str(item.get("description") or ""),
                )
            )

        return profiles

    def generate_audio(
        self,
        text: str,
        profile_id: str,
        language: str,
        output_path: str | Path,
        timeout: float = 600.0,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        """Genera un clip de audio con POST /generate y lo guarda como .wav.

        Soporta tanto respuestas directas con bytes de audio como respuestas JSON
        síncronas o asíncronas (status='generating') con ID de generación.
        """
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)

        url = f"{self.base_url}/generate"
        payload = {
            "text": text,
            "profile_id": profile_id,
            "language": language,
        }

        try:
            response = self.session.post(url, json=payload, timeout=min(timeout, 180.0))
            response.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            raise VoiceboxConnectionError(
                f"Se perdió la conexión con Voicebox en {self.base_url} durante la generación."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise VoiceboxError(
                "Tiempo de espera agotado esperando respuesta de POST /generate."
            ) from exc
        except requests.exceptions.HTTPError as exc:
            detail = self._extract_error_detail(response)
            raise VoiceboxError(
                f"Error HTTP {response.status_code} en POST /generate: {detail}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise VoiceboxError(f"Error de red en POST /generate: {exc}") from exc

        content_type = (response.headers.get("Content-Type") or "").lower()
        raw_bytes = response.content

        # Caso 1: La API devolvió bytes de audio directamente
        if self._looks_like_audio(content_type, raw_bytes):
            out_file.write_bytes(raw_bytes)
            return out_file

        # Caso 2: La API devolvió JSON con metadata (id, status, audio_url, audio_path, etc.)
        try:
            data = response.json()
        except ValueError as exc:
            raise VoiceboxError(
                "POST /generate devolvió una respuesta que no es audio ni JSON válido "
                f"(Content-Type: {content_type or 'desconocido'})."
            ) from exc

        if not isinstance(data, dict):
            raise VoiceboxError(f"Respuesta JSON inesperada de POST /generate: {data!r}")

        return self._resolve_json_audio(
            data,
            out_file,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _looks_like_audio(content_type: str, data: bytes) -> bool:
        if any(
            token in content_type
            for token in ("audio/", "application/octet-stream", "audio/wav", "audio/x-wav")
        ):
            return len(data) > 0
        if len(data) >= 4 and data[:4] in (b"RIFF", b"OggS", b"fLaC", b"ID3\x03", b"ID3\x04"):
            return True
        return False

    @staticmethod
    def _extract_error_detail(response: requests.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict) and "detail" in body:
                return str(body["detail"])
            return str(body)[:250]
        except ValueError:
            return response.text[:250].strip() or "Sin detalles"

    def _cancel_remote_generation(self, gen_id: str) -> None:
        """Intenta cancelar la generación en curso en el servidor Voicebox."""
        try:
            self.session.post(f"{self.base_url}/generate/{gen_id}/cancel", timeout=5.0)
        except requests.exceptions.RequestException:
            pass

    def _wait_for_generation(
        self,
        gen_id: str,
        initial_data: dict[str, Any],
        timeout: float,
        cancel_event: threading.Event | None,
    ) -> dict[str, Any]:
        """Espera a que una generación asíncrona de Voicebox termine consultando GET /history/{id}."""
        in_progress_states = {
            "generating",
            "pending",
            "processing",
            "queued",
            "loading",
            "running",
            "in_progress",
            "starting",
        }
        failed_states = {"failed", "error"}
        cancelled_states = {"cancelled", "canceled"}

        current = initial_data
        status = str(current.get("status") or "").lower()
        if status not in in_progress_states:
            if status in failed_states:
                err_msg = current.get("error") or "Error desconocido en Voicebox."
                raise VoiceboxError(f"Voicebox falló al generar el audio: {err_msg}")
            if status in cancelled_states:
                raise VoiceboxCancelledError("La generación fue cancelada en Voicebox.")
            return current

        deadline = time.monotonic() + timeout
        history_url = f"{self.base_url}/history/{gen_id}"

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self._cancel_remote_generation(gen_id)
                raise VoiceboxCancelledError("Generación cancelada por el usuario.")

            time.sleep(0.75)

            if cancel_event is not None and cancel_event.is_set():
                self._cancel_remote_generation(gen_id)
                raise VoiceboxCancelledError("Generación cancelada por el usuario.")

            try:
                resp = self.session.get(history_url, timeout=10.0)
            except requests.exceptions.ConnectionError as exc:
                raise VoiceboxConnectionError(
                    f"Se perdió la conexión con Voicebox al consultar el estado de {gen_id}."
                ) from exc
            except requests.exceptions.RequestException:
                continue

            if resp.status_code == 404:
                # Servidor sin endpoint /history/{id}: salir y dejar que se intente /audio/{id}
                return current

            if resp.status_code != 200:
                continue

            try:
                polled = resp.json()
            except ValueError:
                continue

            if not isinstance(polled, dict):
                continue

            current = polled
            status = str(current.get("status") or "").lower()

            if status in failed_states:
                err_msg = current.get("error") or "Error desconocido en Voicebox."
                raise VoiceboxError(f"Voicebox falló al generar el audio: {err_msg}")
            if status in cancelled_states:
                raise VoiceboxCancelledError("La generación fue cancelada en Voicebox.")
            if status not in in_progress_states:
                return current

        self._cancel_remote_generation(gen_id)
        raise VoiceboxError(
            f"Tiempo de espera agotado ({timeout:.0f}s) esperando a que Voicebox "
            f"complete la generación {gen_id} (último estado: '{status}')."
        )

    def _resolve_json_audio(
        self,
        data: dict[str, Any],
        out_file: Path,
        timeout: float,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        # 2a. Audio embebido en base64
        for key in ("audio_base64", "audio_data", "audio_bytes"):
            if data.get(key):
                audio_bytes = base64.b64decode(data[key])
                out_file.write_bytes(audio_bytes)
                return out_file

        # 2b. Si tiene ID de generación, esperar si aún está en curso (status='generating', etc.)
        gen_id = data.get("id") or data.get("generation_id") or data.get("audio_id")
        if gen_id:
            gen_id_str = str(gen_id).strip()
            data = self._wait_for_generation(
                gen_id=gen_id_str,
                initial_data=data,
                timeout=timeout,
                cancel_event=cancel_event,
            )

        # 2c. URL explícita de descarga
        for key in ("audio_url", "download_url", "url"):
            audio_url = data.get(key)
            if isinstance(audio_url, str) and audio_url.strip():
                full_url = urljoin(f"{self.base_url}/", audio_url.lstrip("/"))
                if self._try_download_audio(full_url, out_file, timeout=30.0):
                    return out_file

        # 2d. Descargar desde los endpoints de audio de Voicebox por ID
        if gen_id:
            gen_id_str = str(gen_id).strip()
            candidate_endpoints = [
                f"{self.base_url}/audio/{gen_id_str}",
                f"{self.base_url}/history/{gen_id_str}/export-audio",
                f"{self.base_url}/generations/{gen_id_str}/audio",
                f"{self.base_url}/history/{gen_id_str}/audio",
            ]
            # Breves reintentos por si el archivo termina de escribirse en disco justo al completar
            for attempt in range(5):
                if cancel_event is not None and cancel_event.is_set():
                    raise VoiceboxCancelledError("Generación cancelada por el usuario.")
                for endpoint in candidate_endpoints:
                    if self._try_download_audio(endpoint, out_file, timeout=30.0):
                        return out_file
                if attempt < 4:
                    time.sleep(0.5)

        # 2e. Ruta local en disco (fallback si el servidor está en el mismo equipo)
        audio_path = data.get("audio_path") or data.get("file_path") or data.get("path")
        if isinstance(audio_path, str) and audio_path.strip():
            local_candidate = Path(audio_path)
            if local_candidate.is_file():
                shutil.copy2(local_candidate, out_file)
                return out_file

            remote_candidate = urljoin(f"{self.base_url}/", audio_path.lstrip("/"))
            if self._try_download_audio(remote_candidate, out_file, timeout=30.0):
                return out_file

        raise VoiceboxError(
            "POST /generate respondió JSON, pero no se pudo descargar ni localizar "
            f"el archivo de audio. Respuesta recibida: {data}"
        )

    def _try_download_audio(
        self,
        url: str,
        out_file: Path,
        timeout: float,
    ) -> bool:
        try:
            resp = self.session.get(url, timeout=timeout)
            if resp.status_code != 200:
                return False
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if self._looks_like_audio(content_type, resp.content):
                out_file.write_bytes(resp.content)
                return True
            return False
        except requests.exceptions.RequestException:
            return False
