# Changelog

Todos los cambios notables en este proyecto serán documentados en este archivo.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## [1.0.0] - 2026-11-23

### Added
- Interfaz web local interactiva con reproductor HTML5 y línea de tiempo de doble cabezal (`Inicio` / `Fin`).
- Streaming HTTP/1.1 con soporte completo de `Range` (`206 Partial Content` y `416 Range Not Satisfiable`).
- Cuatro modos de procesamiento con FFmpeg: `smart`, `compress` (GPU `h264_nvenc` con respaldo `libx264`), `copy` (sin recodificar) y `split` (división automática en partes `< 180 MB`).
- Presets de peso objetivo (`170 MB`, `100 MB`, `60 MB`, `30 MB`) y selector de resolución/FPS opcional.
- Integración de escritorio Windows: copia automática al portapapeles (`FileDropList` para `Ctrl+V` en WhatsApp) y apertura en Explorador.
- Soporte para compilación a ejecutable Windows (`WhatsAppVideoCutter.exe`) con icono personalizado (`assets/app_icon.ico`) y metadatos de versión `1.0.0.0`.
- Soporte completo para Docker (`Dockerfile` y `docker-compose.yml`), endpoint `GET /api/health` y descarga directa `GET /api/download`.
- Suite de tests unitarios y de integración con `pytest`.
- Lanzador `Iniciar_WhatsApp_Cutter.bat` ejecutado dentro de `.venv`.

### Fixed
- Manejo silencioso de desconexiones abruptas del navegador (`ConnectionAbortedError [WinError 10053]`, `ConnectionResetError`, `BrokenPipeError`) durante buffering/scrubbing HTTP/1.1.
