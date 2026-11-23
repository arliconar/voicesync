"""Motor de generación de clips, silencios exactos, time-stretch y ensamblaje final."""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydub import AudioSegment

from ass_processor import ParsedScript, format_ms, sanitize_filename
from voicebox_client import VoiceboxCancelledError, VoiceboxClient


class GenerationCancelledError(Exception):
    """Se lanza cuando el usuario cancela la generación en curso."""


@dataclass(frozen=True)
class GenerationConfig:
    """Parámetros de ejecución para construir la pista sincronizada."""

    script: ParsedScript
    voice_mapping: dict[str, str]
    default_profile_id: str
    profile_names: dict[str, str]
    language: str
    output_path: Path
    keep_clips: bool = False
    adjust_speed: bool = False
    max_speed_factor: float = 2.5


@dataclass(frozen=True)
class BuildResult:
    """Resumen del resultado tras ensamblar el audio final."""

    output_path: Path
    clips_dir: Path | None
    total_lines: int
    total_duration_ms: int
    overlaps_count: int
    stretched_count: int
    final_balance_ms: int = 0


def check_ffmpeg_available() -> str | None:
    """Verifica si ffmpeg está disponible en el PATH del sistema."""
    return shutil.which("ffmpeg")


def _build_atempo_filter(speed_factor: float) -> str:
    """Construye una cadena válida de filtros atempo de ffmpeg para cualquier factor > 0.5."""
    filters: list[str] = []
    remaining = speed_factor
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


def _time_stretch_wav(input_path: Path, speed_factor: float) -> Path:
    """Acelera un archivo WAV preservando el tono usando el filtro atempo de ffmpeg."""
    ffmpeg_bin = check_ffmpeg_available()
    if not ffmpeg_bin:
        raise RuntimeError("No se encontró 'ffmpeg' en el PATH para aplicar time-stretch.")

    temp_out = input_path.with_name(f"{input_path.stem}_stretched.wav")
    atempo_chain = _build_atempo_filter(speed_factor)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-filter:a",
        atempo_chain,
        str(temp_out),
    ]

    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        creationflags=creationflags,
        check=False,
    )
    if proc.returncode != 0 or not temp_out.is_file():
        if temp_out.exists():
            temp_out.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg falló al aplicar time-stretch ({atempo_chain}): {proc.stderr.strip()}"
        )

    temp_out.replace(input_path)
    return input_path


def _normalize_segment(
    segment: AudioSegment,
    frame_rate: int,
    channels: int,
    sample_width: int,
) -> AudioSegment:
    """Unifica frecuencia de muestreo, canales y ancho de muestra para evitar desajustes."""
    if segment.frame_rate != frame_rate:
        segment = segment.set_frame_rate(frame_rate)
    if segment.channels != channels:
        segment = segment.set_channels(channels)
    if segment.sample_width != sample_width:
        segment = segment.set_sample_width(sample_width)
    return segment


class AudioBuilder:
    """Orquesta la generación secuencial, silencios exactos y exportación final."""

    def __init__(
        self,
        client: VoiceboxClient,
        log_callback: Callable[[str], None] | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.client = client
        self._log = log_callback or (lambda _msg: None)
        self._progress = progress_callback or (lambda _val, _msg: None)
        self._cancel = cancel_event or threading.Event()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise GenerationCancelledError("Generación cancelada por el usuario.")

    def build(self, config: GenerationConfig) -> BuildResult:
        """Ejecuta todo el flujo de generación y ensamblaje de audio sincronizado."""
        if not check_ffmpeg_available():
            raise RuntimeError(
                "No se encontró 'ffmpeg' en el PATH del sistema. "
                "Instala FFmpeg y reinicia la aplicación."
            )

        lines = config.script.lines
        total_lines = len(lines)
        if total_lines == 0:
            raise ValueError("El guion .ass no contiene líneas válidas para generar.")

        output_path = Path(config.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if config.keep_clips:
            work_dir = output_path.parent / f"{output_path.stem}_clips"
        else:
            work_dir = output_path.parent / "temp_clips"

        # Preparar carpeta de trabajo limpia
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        self._log(f"Carpeta de trabajo: {work_dir}")
        self._log(
            f"Iniciando generación de {total_lines} líneas "
            f"(idioma: {config.language}, ajuste de velocidad: {'Sí' if config.adjust_speed else 'No'})."
        )

        ordered_segments: list[AudioSegment] = []
        tiempo_actual_ms = 0
        saldo_ms = 0  # Positivo = sobró tiempo (suma al silencio); Negativo = faltó tiempo (resta al silencio)
        target_frame_rate: int | None = None
        target_channels: int | None = None
        target_sample_width: int | None = None
        overlaps_count = 0
        stretched_count = 0

        try:
            for idx_zero, line in enumerate(lines):
                self._check_cancelled()

                profile_id = config.voice_mapping.get(line.character) or config.default_profile_id
                if not profile_id:
                    raise ValueError(
                        f"No hay una voz asignada para '{line.character}' ni una voz por defecto."
                    )

                voice_label = config.profile_names.get(profile_id, profile_id)
                safe_char = sanitize_filename(line.character)
                clip_filename = f"{line.index:04d}_{safe_char}_clip.wav"
                clip_path = work_dir / clip_filename

                progress_fraction = idx_zero / (total_lines + 1)
                self._progress(
                    progress_fraction,
                    f"Generando línea {line.index}/{total_lines} ({line.character})...",
                )

                preview_text = line.text if len(line.text) <= 70 else f"{line.text[:67]}..."
                self._log(
                    f"[{line.index:04d}/{total_lines:04d}] {format_ms(line.start_ms)} -> "
                    f"{format_ms(line.end_ms)} | {line.character} -> Voz: {voice_label} | \"{preview_text}\""
                )

                # 1. Generar clip con Voicebox (esperando si la generación es asíncrona)
                try:
                    self.client.generate_audio(
                        text=line.text,
                        profile_id=profile_id,
                        language=config.language,
                        output_path=clip_path,
                        cancel_event=self._cancel,
                    )
                except VoiceboxCancelledError as exc:
                    raise GenerationCancelledError(str(exc)) from exc
                self._check_cancelled()

                # 2. Cargar clip y determinar formato de audio de referencia
                clip_seg = AudioSegment.from_file(str(clip_path))
                if target_frame_rate is None:
                    target_frame_rate = clip_seg.frame_rate
                    target_channels = clip_seg.channels
                    target_sample_width = clip_seg.sample_width
                    self._log(
                        f"  Formato detectado de Voicebox: {target_frame_rate} Hz, "
                        f"{target_channels} canal(es), {target_sample_width * 8}-bit."
                    )
                else:
                    clip_seg = _normalize_segment(
                        clip_seg,
                        target_frame_rate,
                        target_channels,  # type: ignore[arg-type]
                        target_sample_width,  # type: ignore[arg-type]
                    )

                # 3. Calcular silencio en el espacio muerto aplicando el saldo (+/-) acumulado
                if idx_zero == 0:
                    espacio_guion_ms = line.start_ms
                    silencio_final_ms = espacio_guion_ms
                    saldo_aplicado_ms = 0
                else:
                    prev_line = lines[idx_zero - 1]
                    espacio_guion_ms = line.start_ms - prev_line.end_ms
                    saldo_aplicado_ms = saldo_ms
                    silencio_final_ms = espacio_guion_ms + saldo_ms

                if silencio_final_ms > 0:
                    silence_seg = AudioSegment.silent(
                        duration=silencio_final_ms,
                        frame_rate=target_frame_rate,
                    )
                    silence_seg = _normalize_segment(
                        silence_seg,
                        target_frame_rate,
                        target_channels,  # type: ignore[arg-type]
                        target_sample_width,  # type: ignore[arg-type]
                    )
                    silence_path = work_dir / f"{line.index:04d}_silence.wav"
                    silence_seg.export(str(silence_path), format="wav")
                    ordered_segments.append(silence_seg)
                    tiempo_actual_ms += len(silence_seg)
                    saldo_ms = 0

                    if idx_zero == 0:
                        self._log(
                            f"  + Silencio inicial: {silencio_final_ms / 1000:.2f}s "
                            f"({silencio_final_ms} ms, {silence_path.name})"
                        )
                    else:
                        self._log(
                            f"  + Silencio en espacio muerto: {silencio_final_ms / 1000:.2f}s "
                            f"(guion: {espacio_guion_ms / 1000:.2f}s, saldo aplicado: {saldo_aplicado_ms / 1000:+.2f}s "
                            f"-> saldo restante: 0.00s)"
                        )
                else:
                    # El espacio muerto no alcanza para absorber el saldo negativo:
                    # no se recorta el audio y el saldo restante pasa al próximo espacio en blanco.
                    saldo_ms = silencio_final_ms
                    if silencio_final_ms < 0:
                        overlaps_count += 1
                        self._log(
                            f"  ~ Sin silencio (espacio guion: {espacio_guion_ms / 1000:.2f}s, "
                            f"saldo previo: {saldo_aplicado_ms / 1000:+.2f}s -> saldo restante: "
                            f"{saldo_ms / 1000:+.2f}s; se compensará en el próximo espacio muerto)"
                        )

                # 4. Ajuste opcional de velocidad (solo si el usuario activó time-stretch explícitamente)
                clip_duration_ms = len(clip_seg)
                if config.adjust_speed and (idx_zero + 1) < total_lines:
                    next_start_ms = lines[idx_zero + 1].start_ms
                    available_ms = next_start_ms - tiempo_actual_ms
                    if available_ms > 0 and clip_duration_ms > available_ms:
                        desired_factor = clip_duration_ms / available_ms
                        applied_factor = min(desired_factor, config.max_speed_factor)
                        if applied_factor > 1.01:
                            self._log(
                                f"  ~ Ajustando velocidad x{applied_factor:.2f} "
                                f"(clip: {clip_duration_ms} ms > espacio: {available_ms} ms)..."
                            )
                            _time_stretch_wav(clip_path, applied_factor)
                            clip_seg = AudioSegment.from_file(str(clip_path))
                            clip_seg = _normalize_segment(
                                clip_seg,
                                target_frame_rate,
                                target_channels,  # type: ignore[arg-type]
                                target_sample_width,  # type: ignore[arg-type]
                            )
                            clip_duration_ms = len(clip_seg)
                            stretched_count += 1
                            self._log(f"    Nueva duración del clip: {clip_duration_ms} ms.")

                # 5. Agregar clip completo sin recortes y actualizar saldo (+/-) respecto al guion
                ordered_segments.append(clip_seg)
                tiempo_actual_ms += clip_duration_ms

                duracion_guion_ms = line.duration_ms
                delta_ms = duracion_guion_ms - clip_duration_ms
                saldo_ms += delta_ms

                self._log(
                    f"  OK Clip: {clip_filename} ({clip_duration_ms / 1000:.2f}s vs guion "
                    f"{duracion_guion_ms / 1000:.2f}s | delta: {delta_ms / 1000:+.2f}s | "
                    f"saldo acumulado: {saldo_ms / 1000:+.2f}s) | Puntero: {format_ms(tiempo_actual_ms)}"
                )

            # 6. Ensamblaje final en O(N) concatenando frames PCM ya normalizados
            self._check_cancelled()
            self._progress(total_lines / (total_lines + 1), "Ensamblando pista final (.wav)...")
            self._log("Ensamblando pista final sincronizada...")

            combined_raw = b"".join(seg.raw_data for seg in ordered_segments)
            final_audio = AudioSegment(
                data=combined_raw,
                sample_width=target_sample_width,  # type: ignore[arg-type]
                frame_rate=target_frame_rate,  # type: ignore[arg-type]
                channels=target_channels,  # type: ignore[arg-type]
            )
            final_audio.export(str(output_path), format="wav")

            self._progress(1.0, "¡Generación completada!")
            self._log(
                f"Pista final exportada en: {output_path} "
                f"(Duración total: {format_ms(len(final_audio))} | "
                f"Saldo final: {saldo_ms / 1000:+.2f}s)."
            )
            if config.keep_clips:
                self._log(f"Clips individuales y silencios conservados en: {work_dir}")

            return BuildResult(
                output_path=output_path,
                clips_dir=work_dir if config.keep_clips else None,
                total_lines=total_lines,
                total_duration_ms=len(final_audio),
                overlaps_count=overlaps_count,
                stretched_count=stretched_count,
                final_balance_ms=saldo_ms,
            )

        finally:
            if not config.keep_clips and work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
                self._log("Carpeta temporal de clips eliminada.")
