# ASS2Voicebox

Aplicación de escritorio en Python (`customtkinter`, modo oscuro) que toma un archivo de subtítulos `.ass`, se conecta a un servidor local o remoto de **Voicebox** (API FastAPI), permite asignar una voz distinta a cada personaje del guion y genera una pista de audio `.wav` sincronizada respetando los tiempos con silencios exactos en los tiempos muertos.

## Características

- **Detección automática de personajes** por `Actor (Name)` o `Estilo (Style)`, limpiando etiquetas de formato ASS (`{\i1}`, `{\pos(...)}`, `\N`, etc.) y filtrando comentarios o líneas vacías.
- **Conexión con Voicebox REST API**: validación con `GET /health`, carga de perfiles con `GET /profiles` y generación con `POST /generate` (soporta tanto respuesta directa en bytes de audio como respuesta JSON con `id` / URL de descarga).
- **Sincronización exacta con silencios**: calcula el espacio muerto antes de cada línea (incluyendo el silencio inicial), genera archivos de silencio con la misma frecuencia de muestreo y canales que Voicebox y advierte en el log si hay solapamientos.
- **Ajuste opcional de velocidad (time-stretch)**: acelera automáticamente con `ffmpeg` (`atempo`, preservando el tono) los clips que excedan el espacio disponible hasta el siguiente subtítulo.
- **Interfaz responsiva**: toda la conexión y generación corre en hilos secundarios con barra de progreso, botón de cancelación y consola de logs en tiempo real.

## Requisitos previos

- **Python 3.10+**
- **FFmpeg** instalado y disponible en el `PATH` del sistema (la aplicación verifica esto al iniciar).
  - En Windows puedes instalarlo con: `winget install Gyan.FFmpeg`
- Servidor de **Voicebox** ejecutándose (por defecto en `http://127.0.0.1:17493`) con al menos un perfil de voz creado.

## Instalación

```bash
# 1. (Opcional pero recomendado) Crear y activar entorno virtual
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
# source .venv/bin/activate

# 2. Instalar dependencias
pip install -r requirements.txt
```

## Uso

```bash
python main.py
```

1. **Servidor Voicebox**: verifica la IP (`127.0.0.1`) y el puerto (`17493`), elige el idioma global (ej. `es`) y haz clic en **"Conectar / Recargar voces"**.
2. **Guion (.ass)**: selecciona tu archivo `.ass` y elige si quieres detectar personajes por **Actor (Name)** o **Estilo (Style)**.
3. **Asignación de voces**: asigna una voz de Voicebox a cada personaje detectado (puedes usar **"Voz por defecto"** + **"Aplicar a todos"** para agilizar).
4. **Salida y opciones**: elige la ruta del `.wav` final y marca si deseas conservar los clips/silencios individuales o activar el ajuste de velocidad (*time-stretch*).
5. Haz clic en **"Generar Audio Sincronizado"** y sigue el progreso en la consola en tiempo real.

## Estructura del proyecto

- `main.py`: Interfaz gráfica en `customtkinter` y gestión de hilos secundarios.
- `voicebox_client.py`: Cliente HTTP para los endpoints `/health`, `/profiles` y `/generate` de Voicebox.
- `ass_processor.py`: Parseo de `.ass` con `pysubs2`, limpieza de texto y agrupación de personajes.
- `audio_builder.py`: Generación secuencial de clips, cálculo de silencios exactos, *time-stretch* con `ffmpeg` y ensamblaje final del `.wav`.
- `requirements.txt`: Dependencias de Python del proyecto.
