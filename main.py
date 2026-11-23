"""ASS2Voicebox — Aplicación de escritorio para doblaje sincronizado desde .ass usando Voicebox."""

from __future__ import annotations

import datetime
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ass_processor import GroupByMode, ParsedScript, load_ass_script
from audio_builder import (
    AudioBuilder,
    BuildResult,
    GenerationCancelledError,
    GenerationConfig,
    check_ffmpeg_available,
)
from voicebox_client import (
    VoiceboxClient,
    VoiceboxConnectionError,
    VoiceboxError,
    VoiceProfile,
)

SUPPORTED_LANGUAGES = ["es", "en", "ja", "fr", "de", "it", "pt", "zh", "ko", "ru"]
NO_VOICES_OPTION = "(Sin voces cargadas)"


class ASS2VoiceboxApp(ctk.CTk):
    """Ventana principal de ASS2Voicebox."""

    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("ASS2Voicebox — Generador de Audio Sincronizado")
        self.geometry("1040x860")
        self.minsize(880, 720)

        # Estado interno
        self.ffmpeg_path: str | None = check_ffmpeg_available()
        self.profiles: list[VoiceProfile] = []
        self.label_to_profile: dict[str, VoiceProfile] = {}
        self.parsed_script: ParsedScript | None = None
        self.character_dropdowns: dict[str, ctk.CTkOptionMenu] = {}
        self.character_vars: dict[str, tk.StringVar] = {}

        self.is_generating = False
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None

        # Variables de interfaz
        self.ip_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="17493")
        self.language_var = tk.StringVar(value="es")
        self.ass_path_var = tk.StringVar(value="")
        self.group_mode_var = tk.StringVar(value="Actor (Name)")
        self.default_voice_var = tk.StringVar(value=NO_VOICES_OPTION)
        self.output_path_var = tk.StringVar(value="")
        self.keep_clips_var = tk.BooleanVar(value=False)
        self.adjust_speed_var = tk.BooleanVar(value=False)
        self.progress_text_var = tk.StringVar(value="Listo.")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Validación de FFmpeg al inicio
        self.after(150, self._verify_ffmpeg_on_startup)

    # ------------------------------------------------------------------
    # Construcción de la interfaz
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=2)  # Tabla de voces expandible
        self.grid_rowconfigure(5, weight=2)  # Consola de logs expandible

        # Banner de advertencia de FFmpeg (solo visible si falta)
        self.ffmpeg_banner = ctk.CTkFrame(self, fg_color="#7f1d1d", corner_radius=8)
        self.ffmpeg_banner_label = ctk.CTkLabel(
            self.ffmpeg_banner,
            text=(
                "ADVERTENCIA: No se detectó 'ffmpeg' en el PATH del sistema. "
                "Instala FFmpeg y reinicia ASS2Voicebox para poder procesar audio."
            ),
            text_color="#fecaca",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.ffmpeg_banner_label.pack(padx=12, pady=8)
        if not self.ffmpeg_path:
            self.ffmpeg_banner.grid(row=0, column=0, padx=14, pady=(10, 0), sticky="ew")

        self._build_server_section(row=1)
        self._build_script_section(row=2)
        self._build_voices_section(row=3)
        self._build_output_section(row=4)
        self._build_execution_section(row=5)

    def _build_server_section(self, row: int) -> None:
        frame = ctk.CTkFrame(self, corner_radius=10)
        frame.grid(row=row, column=0, padx=14, pady=(10, 6), sticky="ew")
        frame.grid_columnconfigure(7, weight=1)

        ctk.CTkLabel(
            frame,
            text="1. Servidor Voicebox",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, columnspan=8, padx=12, pady=(8, 4), sticky="w")

        ctk.CTkLabel(frame, text="IP:").grid(row=1, column=0, padx=(12, 4), pady=(0, 10), sticky="w")
        self.ip_entry = ctk.CTkEntry(frame, textvariable=self.ip_var, width=140)
        self.ip_entry.grid(row=1, column=1, padx=(0, 10), pady=(0, 10), sticky="w")

        ctk.CTkLabel(frame, text="Puerto:").grid(row=1, column=2, padx=(0, 4), pady=(0, 10), sticky="w")
        self.port_entry = ctk.CTkEntry(frame, textvariable=self.port_var, width=80)
        self.port_entry.grid(row=1, column=3, padx=(0, 12), pady=(0, 10), sticky="w")

        self.connect_btn = ctk.CTkButton(
            frame,
            text="Conectar / Recargar voces",
            command=self._on_connect_clicked,
            width=190,
        )
        self.connect_btn.grid(row=1, column=4, padx=(0, 14), pady=(0, 10), sticky="w")

        # Indicador visual de estado
        status_container = ctk.CTkFrame(frame, fg_color="transparent")
        status_container.grid(row=1, column=5, padx=(0, 14), pady=(0, 10), sticky="w")

        self.status_dot = ctk.CTkLabel(
            status_container,
            text="●",
            text_color="#e74c3c",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.status_dot.pack(side="left", padx=(0, 6))

        self.status_label = ctk.CTkLabel(
            status_container,
            text="Desconectado",
            text_color="#e74c3c",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.status_label.pack(side="left")

        ctk.CTkLabel(frame, text="Idioma global:").grid(
            row=1, column=6, padx=(8, 6), pady=(0, 10), sticky="e"
        )
        self.language_combo = ctk.CTkComboBox(
            frame,
            values=SUPPORTED_LANGUAGES,
            variable=self.language_var,
            width=85,
        )
        self.language_combo.grid(row=1, column=7, padx=(0, 12), pady=(0, 10), sticky="w")

    def _build_script_section(self, row: int) -> None:
        frame = ctk.CTkFrame(self, corner_radius=10)
        frame.grid(row=row, column=0, padx=14, pady=6, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            frame,
            text="2. Guion de Subtítulos (.ass)",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, columnspan=4, padx=12, pady=(8, 4), sticky="w")

        self.select_ass_btn = ctk.CTkButton(
            frame,
            text="Seleccionar archivo .ass",
            command=self._on_select_ass,
            width=180,
        )
        self.select_ass_btn.grid(row=1, column=0, padx=(12, 8), pady=(0, 8), sticky="w")

        self.ass_entry = ctk.CTkEntry(
            frame,
            textvariable=self.ass_path_var,
            placeholder_text="Ningún archivo .ass seleccionado...",
            state="readonly",
        )
        self.ass_entry.grid(row=1, column=1, padx=(0, 12), pady=(0, 8), sticky="ew")

        ctk.CTkLabel(frame, text="Detectar personaje por:").grid(
            row=1, column=2, padx=(0, 6), pady=(0, 8), sticky="e"
        )
        self.group_selector = ctk.CTkSegmentedButton(
            frame,
            values=["Actor (Name)", "Estilo (Style)"],
            variable=self.group_mode_var,
            command=self._on_group_mode_changed,
        )
        self.group_selector.grid(row=1, column=3, padx=(0, 12), pady=(0, 8), sticky="e")

        self.script_stats_label = ctk.CTkLabel(
            frame,
            text="Carga un archivo .ass para detectar líneas y personajes.",
            text_color="#9ca3af",
            font=ctk.CTkFont(size=12),
        )
        self.script_stats_label.grid(
            row=2, column=0, columnspan=4, padx=12, pady=(0, 8), sticky="w"
        )

    def _build_voices_section(self, row: int) -> None:
        frame = ctk.CTkFrame(self, corner_radius=10)
        frame.grid(row=row, column=0, padx=14, pady=6, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, padx=12, pady=(8, 6), sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header,
            text="3. Asignación de Voces por Personaje",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        default_controls = ctk.CTkFrame(header, fg_color="transparent")
        default_controls.grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(default_controls, text="Voz por defecto:").pack(side="left", padx=(0, 6))
        self.default_voice_menu = ctk.CTkOptionMenu(
            default_controls,
            values=[NO_VOICES_OPTION],
            variable=self.default_voice_var,
            width=230,
        )
        self.default_voice_menu.pack(side="left", padx=(0, 8))

        self.apply_all_btn = ctk.CTkButton(
            default_controls,
            text="Aplicar a todos",
            command=self._on_apply_default_to_all,
            width=125,
            fg_color="#374151",
            hover_color="#4b5563",
        )
        self.apply_all_btn.pack(side="left")

        self.voices_scroll = ctk.CTkScrollableFrame(frame, corner_radius=8)
        self.voices_scroll.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="nsew")
        self.voices_scroll.grid_columnconfigure(0, weight=1)

        self._render_empty_voices_placeholder()

    def _build_output_section(self, row: int) -> None:
        frame = ctk.CTkFrame(self, corner_radius=10)
        frame.grid(row=row, column=0, padx=14, pady=6, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            frame,
            text="4. Opciones y Archivo de Salida",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=12, pady=(8, 4), sticky="w")

        ctk.CTkLabel(frame, text="Salida (.wav):").grid(
            row=1, column=0, padx=(12, 8), pady=(0, 6), sticky="w"
        )
        self.output_entry = ctk.CTkEntry(
            frame,
            textvariable=self.output_path_var,
            placeholder_text="Ruta del archivo .wav sincronizado final...",
        )
        self.output_entry.grid(row=1, column=1, padx=(0, 8), pady=(0, 6), sticky="ew")

        self.browse_output_btn = ctk.CTkButton(
            frame,
            text="Examinar...",
            command=self._on_select_output,
            width=110,
        )
        self.browse_output_btn.grid(row=1, column=2, padx=(0, 12), pady=(0, 6), sticky="e")

        self.keep_clips_chk = ctk.CTkCheckBox(
            frame,
            text="Conservar clips individuales y silencios en carpeta aparte",
            variable=self.keep_clips_var,
        )
        self.keep_clips_chk.grid(row=2, column=0, columnspan=3, padx=12, pady=(2, 4), sticky="w")

        self.adjust_speed_chk = ctk.CTkCheckBox(
            frame,
            text=(
                "Forzar ajuste de velocidad (time-stretch con ffmpeg) si excede el tiempo "
                "(desactivado = compensar con saldo +/- en espacios muertos sin acelerar voces)"
            ),
            variable=self.adjust_speed_var,
        )
        self.adjust_speed_chk.grid(row=3, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="w")

    def _build_execution_section(self, row: int) -> None:
        frame = ctk.CTkFrame(self, corner_radius=10)
        frame.grid(row=row, column=0, padx=14, pady=(6, 12), sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        controls = ctk.CTkFrame(frame, fg_color="transparent")
        controls.grid(row=0, column=0, padx=12, pady=(10, 6), sticky="ew")
        controls.grid_columnconfigure(2, weight=1)

        self.generate_btn = ctk.CTkButton(
            controls,
            text="Generar Audio Sincronizado",
            command=self._on_generate_clicked,
            height=38,
            width=230,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#16a34a",
            hover_color="#15803d",
        )
        self.generate_btn.grid(row=0, column=0, padx=(0, 10), sticky="w")

        self.cancel_btn = ctk.CTkButton(
            controls,
            text="Cancelar",
            command=self._on_cancel_clicked,
            height=38,
            width=110,
            fg_color="#dc2626",
            hover_color="#b91c1c",
            state="disabled",
        )
        self.cancel_btn.grid(row=0, column=1, padx=(0, 14), sticky="w")

        self.progress_label = ctk.CTkLabel(
            controls,
            textvariable=self.progress_text_var,
            anchor="w",
            text_color="#d1d5db",
        )
        self.progress_label.grid(row=0, column=2, sticky="ew")

        self.clear_log_btn = ctk.CTkButton(
            controls,
            text="Limpiar log",
            command=self._clear_log,
            width=90,
            height=28,
            fg_color="#374151",
            hover_color="#4b5563",
        )
        self.clear_log_btn.grid(row=0, column=3, padx=(8, 0), sticky="e")

        self.progress_bar = ctk.CTkProgressBar(frame)
        self.progress_bar.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        self.progress_bar.set(0.0)

        self.log_textbox = ctk.CTkTextbox(
            frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
            state="disabled",
        )
        self.log_textbox.grid(row=2, column=0, padx=12, pady=(0, 10), sticky="nsew")

    # ------------------------------------------------------------------
    # Utilidades de log y estado UI
    # ------------------------------------------------------------------
    def _verify_ffmpeg_on_startup(self) -> None:
        if self.ffmpeg_path:
            self._append_log(f"FFmpeg detectado correctamente: {self.ffmpeg_path}")
        else:
            self._append_log(
                "ERROR: No se encontró 'ffmpeg' en el PATH. "
                "Instala FFmpeg antes de generar audio."
            )
            messagebox.showerror(
                "FFmpeg no encontrado",
                "No se detectó 'ffmpeg' en el PATH del sistema.\n\n"
                "ASS2Voicebox necesita FFmpeg para manipular audio con pydub y aplicar "
                "time-stretch.\n\nInstala FFmpeg, agrégalo al PATH y reinicia la aplicación.",
            )

    def _append_log(self, message: str) -> None:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"

        def _insert() -> None:
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", line)
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")

        self.after(0, _insert)

    def _clear_log(self) -> None:
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    def _set_connection_status(self, connected: bool, detail: str) -> None:
        color = "#2ecc71" if connected else "#e74c3c"
        self.status_dot.configure(text_color=color)
        self.status_label.configure(text=detail, text_color=color)

    def _set_progress(self, fraction: float, message: str) -> None:
        clamped = max(0.0, min(1.0, fraction))

        def _update() -> None:
            self.progress_bar.set(clamped)
            self.progress_text_var.set(f"{int(clamped * 100)}% — {message}")

        self.after(0, _update)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.ip_entry.configure(state=state)
        self.port_entry.configure(state=state)
        self.connect_btn.configure(state=state)
        self.language_combo.configure(state=state)
        self.select_ass_btn.configure(state=state)
        self.group_selector.configure(state=state)
        self.default_voice_menu.configure(state=state)
        self.apply_all_btn.configure(state=state)
        self.output_entry.configure(state=state)
        self.browse_output_btn.configure(state=state)
        self.keep_clips_chk.configure(state=state)
        self.adjust_speed_chk.configure(state=state)
        self.generate_btn.configure(state=state)
        self.cancel_btn.configure(state="disabled" if enabled else "normal")

        for dropdown in self.character_dropdowns.values():
            dropdown.configure(state=state)

    # ------------------------------------------------------------------
    # Conexión con Voicebox
    # ------------------------------------------------------------------
    def _on_connect_clicked(self) -> None:
        host = self.ip_var.get().strip()
        port = self.port_var.get().strip()

        self.connect_btn.configure(state="disabled", text="Conectando...")
        self._append_log(f"Conectando con Voicebox en http://{host}:{port} ...")

        thread = threading.Thread(
            target=self._connect_worker,
            args=(host, port),
            daemon=True,
        )
        thread.start()

    def _connect_worker(self, host: str, port: str) -> None:
        try:
            client = VoiceboxClient(host=host, port=port)
            client.check_health()
            profiles = client.get_profiles()
        except (VoiceboxConnectionError, VoiceboxError) as exc:
            self.after(0, lambda err=str(exc): self._on_connect_failed(err))
            return
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda err=str(exc): self._on_connect_failed(f"Error inesperado: {err}"))
            return

        self.after(0, lambda p=profiles: self._on_connect_success(p))

    def _on_connect_failed(self, error_msg: str) -> None:
        self.connect_btn.configure(state="normal", text="Conectar / Recargar voces")
        self._set_connection_status(False, "Desconectado")
        self._append_log(f"ERROR de conexión: {error_msg}")
        messagebox.showerror("Error de conexión con Voicebox", error_msg)

    def _on_connect_success(self, profiles: list[VoiceProfile]) -> None:
        self.connect_btn.configure(state="normal", text="Conectar / Recargar voces")
        self.profiles = profiles
        self.label_to_profile = {p.display_label: p for p in profiles}

        if not profiles:
            self._set_connection_status(True, "Conectado (0 voces)")
            self._refresh_voice_dropdowns()
            msg = (
                "Se estableció conexión con Voicebox, pero no hay perfiles de voz creados.\n"
                "Crea al menos un perfil de voz en Voicebox y pulsa 'Conectar / Recargar voces'."
            )
            self._append_log(f"ADVERTENCIA: {msg.replace(chr(10), ' ')}")
            messagebox.showwarning("Sin perfiles en Voicebox", msg)
            return

        self._set_connection_status(True, f"Conectado ({len(profiles)} voces)")
        self._refresh_voice_dropdowns()
        self._append_log(
            f"Conectado a Voicebox. {len(profiles)} perfiles de voz cargados correctamente."
        )

    def _get_voice_options(self) -> list[str]:
        if not self.profiles:
            return [NO_VOICES_OPTION]
        return [p.display_label for p in self.profiles]

    def _refresh_voice_dropdowns(self) -> None:
        options = self._get_voice_options()
        current_default = self.default_voice_var.get()
        if current_default not in options:
            self.default_voice_var.set(options[0])
        self.default_voice_menu.configure(values=options)

        for char_name, dropdown in self.character_dropdowns.items():
            var = self.character_vars[char_name]
            current = var.get()
            dropdown.configure(values=options)
            if current not in options:
                var.set(self.default_voice_var.get())

    # ------------------------------------------------------------------
    # Carga de archivo .ass y tabla de personajes
    # ------------------------------------------------------------------
    def _current_group_by(self) -> GroupByMode:
        return "style" if "Estilo" in self.group_mode_var.get() else "name"

    def _on_select_ass(self) -> None:
        selected = filedialog.askopenfilename(
            title="Seleccionar archivo de subtítulos (.ass)",
            filetypes=[("Subtítulos ASS", "*.ass"), ("Todos los archivos", "*.*")],
        )
        if not selected:
            return

        self.ass_path_var.set(selected)
        self._load_script_from_path(Path(selected))

        # Sugerir automáticamente la ruta de salida .wav junto al .ass
        ass_path = Path(selected)
        suggested_out = ass_path.with_name(f"{ass_path.stem}_voicebox.wav")
        self.output_path_var.set(str(suggested_out))

    def _on_group_mode_changed(self, _value: str) -> None:
        path_str = self.ass_path_var.get().strip()
        if path_str:
            self._load_script_from_path(Path(path_str))

    def _load_script_from_path(self, path: Path) -> None:
        group_by = self._current_group_by()
        try:
            script = load_ass_script(path, group_by=group_by)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"ERROR al cargar .ass: {exc}")
            messagebox.showerror("Error al leer archivo .ass", str(exc))
            return

        self.parsed_script = script
        mode_label = "Actor (Name)" if group_by == "name" else "Estilo (Style)"
        self.script_stats_label.configure(
            text=(
                f"{script.total_lines} líneas válidas  |  "
                f"{script.total_characters} personajes únicos  (criterio: {mode_label})"
            ),
            text_color="#93c5fd",
        )
        self._populate_character_table(script)
        self._append_log(
            f"Guion cargado: '{path.name}' -> {script.total_lines} líneas válidas, "
            f"{script.total_characters} personajes ({mode_label})."
        )

    def _render_empty_voices_placeholder(self) -> None:
        for widget in self.voices_scroll.winfo_children():
            widget.destroy()
        self.character_dropdowns.clear()
        self.character_vars.clear()

        ctk.CTkLabel(
            self.voices_scroll,
            text="No hay personajes cargados todavía. Selecciona un archivo .ass arriba.",
            text_color="#9ca3af",
        ).grid(row=0, column=0, padx=10, pady=20)

    def _populate_character_table(self, script: ParsedScript) -> None:
        previous_selections = {k: v.get() for k, v in self.character_vars.items()}

        for widget in self.voices_scroll.winfo_children():
            widget.destroy()
        self.character_dropdowns.clear()
        self.character_vars.clear()

        if not script.character_counts:
            self._render_empty_voices_placeholder()
            return

        options = self._get_voice_options()
        default_choice = (
            self.default_voice_var.get()
            if self.default_voice_var.get() in options
            else options[0]
        )

        for row_idx, (character, count) in enumerate(script.character_counts.items()):
            row_frame = ctk.CTkFrame(self.voices_scroll, fg_color="#1f2937", corner_radius=6)
            row_frame.grid(row=row_idx, column=0, padx=4, pady=3, sticky="ew")
            row_frame.grid_columnconfigure(0, weight=1)

            plural = "línea" if count == 1 else "líneas"
            ctk.CTkLabel(
                row_frame,
                text=f"{character} ({count} {plural})",
                anchor="w",
                font=ctk.CTkFont(size=13),
            ).grid(row=0, column=0, padx=12, pady=6, sticky="ew")

            prev = previous_selections.get(character)
            initial_val = prev if prev in options else default_choice
            var = tk.StringVar(value=initial_val)
            dropdown = ctk.CTkOptionMenu(
                row_frame,
                values=options,
                variable=var,
                width=260,
            )
            dropdown.grid(row=0, column=1, padx=12, pady=6, sticky="e")

            self.character_vars[character] = var
            self.character_dropdowns[character] = dropdown

    def _on_apply_default_to_all(self) -> None:
        default_label = self.default_voice_var.get()
        if not self.profiles or default_label == NO_VOICES_OPTION:
            messagebox.showinfo(
                "Sin voces cargadas",
                "Conéctate primero a Voicebox para cargar los perfiles de voz.",
            )
            return
        for var in self.character_vars.values():
            var.set(default_label)
        self._append_log(f"Voz por defecto '{default_label}' aplicada a todos los personajes.")

    # ------------------------------------------------------------------
    # Salida y ejecución de generación
    # ------------------------------------------------------------------
    def _on_select_output(self) -> None:
        initial_name = "audio_sincronizado.wav"
        current = self.output_path_var.get().strip()
        if current:
            initial_name = Path(current).name

        selected = filedialog.asksaveasfilename(
            title="Guardar pista de audio sincronizada",
            defaultextension=".wav",
            initialfile=initial_name,
            filetypes=[("Audio WAV", "*.wav")],
        )
        if selected:
            self.output_path_var.set(selected)

    def _on_generate_clicked(self) -> None:
        if self.is_generating:
            return

        # Validaciones previas
        if not check_ffmpeg_available():
            messagebox.showerror(
                "FFmpeg requerido",
                "No se encontró 'ffmpeg' en el PATH. Instálalo y reinicia la aplicación.",
            )
            return

        if not self.profiles:
            messagebox.showwarning(
                "Sin voces conectadas",
                "Conéctate primero al servidor de Voicebox y asegúrate de tener perfiles creados.",
            )
            return

        if not self.parsed_script or self.parsed_script.total_lines == 0:
            messagebox.showwarning(
                "Guion vacío o no cargado",
                "Selecciona un archivo .ass válido con al menos una línea de diálogo.",
            )
            return

        output_str = self.output_path_var.get().strip()
        if not output_str:
            messagebox.showwarning(
                "Ruta de salida faltante",
                "Indica la ruta y nombre del archivo .wav de salida.",
            )
            return

        output_path = Path(output_str)
        if output_path.suffix.lower() != ".wav":
            output_path = output_path.with_suffix(".wav")
            self.output_path_var.set(str(output_path))

        default_profile = self.label_to_profile.get(self.default_voice_var.get())
        if not default_profile:
            default_profile = self.profiles[0]

        voice_mapping: dict[str, str] = {}
        for character, var in self.character_vars.items():
            chosen_profile = self.label_to_profile.get(var.get(), default_profile)
            voice_mapping[character] = chosen_profile.id

        profile_names = {p.id: p.name for p in self.profiles}
        language = self.language_var.get().strip() or "es"

        try:
            client = VoiceboxClient(host=self.ip_var.get(), port=self.port_var.get())
        except VoiceboxConnectionError as exc:
            messagebox.showerror("Configuración de servidor inválida", str(exc))
            return

        config = GenerationConfig(
            script=self.parsed_script,
            voice_mapping=voice_mapping,
            default_profile_id=default_profile.id,
            profile_names=profile_names,
            language=language,
            output_path=output_path,
            keep_clips=self.keep_clips_var.get(),
            adjust_speed=self.adjust_speed_var.get(),
        )

        self.is_generating = True
        self.cancel_event.clear()
        self._set_controls_enabled(False)
        self._set_progress(0.0, "Preparando generación...")

        self.worker_thread = threading.Thread(
            target=self._generation_worker,
            args=(client, config),
            daemon=True,
        )
        self.worker_thread.start()

    def _generation_worker(self, client: VoiceboxClient, config: GenerationConfig) -> None:
        builder = AudioBuilder(
            client=client,
            log_callback=self._append_log,
            progress_callback=self._set_progress,
            cancel_event=self.cancel_event,
        )
        try:
            result = builder.build(config)
        except GenerationCancelledError:
            self.after(0, self._on_generation_cancelled)
            return
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda err=str(exc): self._on_generation_error(err))
            return

        self.after(0, lambda res=result: self._on_generation_finished(res))

    def _on_cancel_clicked(self) -> None:
        if not self.is_generating:
            return
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self._append_log("Solicitud de cancelación enviada. Esperando a que termine el paso actual...")
        self.progress_text_var.set("Cancelando...")

    def _on_generation_finished(self, result: BuildResult) -> None:
        self.is_generating = False
        self._set_controls_enabled(True)

        summary_lines = [
            f"Archivo generado:\n{result.output_path}",
            f"\nLíneas procesadas: {result.total_lines}",
            f"Desfases compensados/acumulados: {result.overlaps_count}",
            f"Saldo final de tiempo: {result.final_balance_ms / 1000:+.2f} s",
        ]
        if result.stretched_count > 0:
            summary_lines.append(f"Clips ajustados con time-stretch: {result.stretched_count}")
        if result.clips_dir:
            summary_lines.append(f"\nClips conservados en:\n{result.clips_dir}")

        messagebox.showinfo("Generación completada", "\n".join(summary_lines))

    def _on_generation_cancelled(self) -> None:
        self.is_generating = False
        self._set_controls_enabled(True)
        self.progress_bar.set(0.0)
        self.progress_text_var.set("Cancelado.")
        self._append_log("Proceso cancelado por el usuario.")

    def _on_generation_error(self, error_msg: str) -> None:
        self.is_generating = False
        self._set_controls_enabled(True)
        self.progress_text_var.set("Error en la generación.")
        self._append_log(f"ERROR FATAL: {error_msg}")
        messagebox.showerror("Error durante la generación", error_msg)

    def _on_close(self) -> None:
        if self.is_generating:
            should_exit = messagebox.askyesno(
                "Generación en curso",
                "Hay una generación de audio en curso. ¿Deseas cancelarla y cerrar la aplicación?",
            )
            if not should_exit:
                return
            self.cancel_event.set()
        self.destroy()


def main() -> None:
    app = ASS2VoiceboxApp()
    app.mainloop()


if __name__ == "__main__":
    main()
