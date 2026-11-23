#!/usr/bin/env python3
"""
Cortador y Compresor Visual de Grabaciones para WhatsApp (GPU NVENC).
Servidor web local sin dependencias externas que sirve la interfaz HTML5,
soporta streaming de video con HTTP Range Requests (206 Partial Content) y
procesa recortes/compresiones con FFmpeg (h264_nvenc con respaldo libx264).
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import http.server
import json
import math
import mimetypes
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

__version__ = "1.0.0"
APP_NAME = "WhatsApp Video Cutter"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
OUTPUT_SUBDIR = "WhatsApp_Ready"
WHATSAPP_MAX_MB = 180.0
IN_DOCKER = os.environ.get("WA_IN_DOCKER", "").strip().lower() in ("1", "true", "yes")
SUPPORTS_DESKTOP_ACTIONS = (os.name == "nt") and not IN_DOCKER


def get_resource_path(relative_path: str) -> Path:
    """Devuelve la ruta de un recurso compatible con ejecucion local y PyInstaller."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def resolve_runtime_paths(
    videos_dir_arg: str | None = None,
    default_folder_arg: str | None = None,
) -> tuple[Path, str]:
    """Determina la carpeta raiz de videos y la carpeta predeterminada."""
    base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    app_home = base_dir.parent if base_dir.name.lower() == "dist" else base_dir

    explicit_root = (videos_dir_arg or os.environ.get("WA_VIDEOS_ROOT", "")).strip()
    explicit_folder = (default_folder_arg or os.environ.get("WA_DEFAULT_FOLDER", "")).strip()

    if explicit_root:
        root = Path(explicit_root).expanduser().resolve()
        return root, (explicit_folder or ".")

    if app_home.parent.name.lower() == "videos" and app_home.parent.exists():
        return app_home.parent.resolve(), (explicit_folder or app_home.name)

    home_videos = Path.home() / "Videos"
    if home_videos.is_dir():
        return home_videos.resolve(), (explicit_folder or ".")

    return app_home.resolve(), (explicit_folder or ".")


VIDEOS_ROOT, DEFAULT_FOLDER = resolve_runtime_paths()

PROBE_CACHE_LOCK = threading.Lock()
PROBE_CACHE: OrderedDict[tuple[str, int, int], dict] = OrderedDict()
MAX_CACHE_ITEMS = 256

NVENC_AVAILABLE: bool | None = None
NVENC_LOCK = threading.Lock()


def check_nvenc_available() -> bool:
    """Verifica una sola vez si h264_nvenc funciona en este sistema."""
    global NVENC_AVAILABLE
    with NVENC_LOCK:
        if NVENC_AVAILABLE is not None:
            return NVENC_AVAILABLE
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=256x256:d=0.1",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:v",
                    "h264_nvenc",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            NVENC_AVAILABLE = proc.returncode == 0
        except Exception:
            NVENC_AVAILABLE = False
        return NVENC_AVAILABLE


def resolve_safe_path(rel_path: str, must_exist: bool = True) -> Path:
    """Resuelve una ruta relativa dentro de VIDEOS_ROOT bloqueando path traversal."""
    cleaned = (rel_path or "").strip().lstrip("/\\")
    target = (VIDEOS_ROOT / cleaned).resolve() if cleaned and cleaned != "." else VIDEOS_ROOT.resolve()
    root_resolved = VIDEOS_ROOT.resolve()
    if target != root_resolved and not target.is_relative_to(root_resolved):
        raise ValueError("Ruta fuera de la carpeta Videos permitida.")
    if must_exist and not target.exists():
        raise FileNotFoundError(f"No existe la ruta: {rel_path}")
    return target


def parse_fps(rate_str: str | None) -> float:
    """Convierte cadenas tipo '60/1' o '41203125/686702' a float."""
    if not rate_str or rate_str in ("0/0", "0"):
        return 0.0
    if "/" in rate_str:
        num_s, den_s = rate_str.split("/", 1)
        try:
            den = float(den_s)
            return round(float(num_s) / den, 2) if den > 0 else 0.0
        except ValueError:
            return 0.0
    try:
        return round(float(rate_str), 2)
    except ValueError:
        return 0.0


def probe_video(file_path: Path) -> dict:
    """Obtiene metadatos del video con ffprobe usando caché por archivo/mtime/tamaño."""
    stat = file_path.stat()
    cache_key = (str(file_path), stat.st_mtime_ns, stat.st_size)
    with PROBE_CACHE_LOCK:
        if cache_key in PROBE_CACHE:
            PROBE_CACHE.move_to_end(cache_key)
            return PROBE_CACHE[cache_key]

    info = {
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "bitrate_kbps": 0,
        "video_codec": "",
        "audio_codec": "",
        "has_audio": False,
    }
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            fmt = data.get("format", {})
            dur = float(fmt.get("duration") or 0.0)
            bit_rate = int(float(fmt.get("bit_rate") or 0))
            info["duration"] = round(dur, 3)
            info["bitrate_kbps"] = round(bit_rate / 1000) if bit_rate else 0

            for stream in data.get("streams", []):
                stype = stream.get("codec_type")
                if stype == "video" and not info["video_codec"]:
                    info["width"] = int(stream.get("width") or 0)
                    info["height"] = int(stream.get("height") or 0)
                    info["video_codec"] = stream.get("codec_name") or ""
                    fps = parse_fps(stream.get("avg_frame_rate")) or parse_fps(stream.get("r_frame_rate"))
                    info["fps"] = fps
                    if not info["duration"] and stream.get("duration"):
                        info["duration"] = round(float(stream["duration"]), 3)
                elif stype == "audio" and not info["has_audio"]:
                    info["has_audio"] = True
                    info["audio_codec"] = stream.get("codec_name") or ""
    except Exception:
        pass

    with PROBE_CACHE_LOCK:
        PROBE_CACHE[cache_key] = info
        if len(PROBE_CACHE) > MAX_CACHE_ITEMS:
            PROBE_CACHE.popitem(last=False)
    return info


def build_video_entry(file_path: Path, include_probe: bool = True) -> dict:
    """Construye el diccionario JSON descriptivo de un archivo de video."""
    stat = file_path.stat()
    size_bytes = stat.st_size
    size_mb = round(size_bytes / (1024 * 1024), 2)
    rel_path = file_path.relative_to(VIDEOS_ROOT).as_posix()
    meta = probe_video(file_path) if include_probe else {}
    return {
        "name": file_path.name,
        "path": rel_path,
        "size_bytes": size_bytes,
        "size_mb": size_mb,
        "over_limit": size_mb > WHATSAPP_MAX_MB,
        "mtime": stat.st_mtime,
        "duration": meta.get("duration", 0.0),
        "width": meta.get("width", 0),
        "height": meta.get("height", 0),
        "fps": meta.get("fps", 0.0),
        "bitrate_kbps": meta.get("bitrate_kbps", 0),
        "video_codec": meta.get("video_codec", ""),
        "has_audio": meta.get("has_audio", False),
    }


def format_time_tag(seconds: float) -> str:
    """Formatea segundos como 01m12s para nombres de archivo seguros."""
    total_sec = max(0, int(round(seconds)))
    hours, rem = divmod(total_sec, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}h{mins:02d}m{secs:02d}s"
    return f"{mins:02d}m{secs:02d}s"


def unique_output_path(output_dir: Path, base_name: str, ext: str = ".mp4") -> Path:
    """Genera una ruta de salida sin sobrescribir exportaciones previas en uso."""
    candidate = output_dir / f"{base_name}{ext}"
    if not candidate.exists():
        return candidate
    idx = 2
    while True:
        alt = output_dir / f"{base_name}_v{idx}{ext}"
        if not alt.exists():
            return alt
        idx += 1


def copy_files_to_clipboard(paths: list[Path]) -> tuple[bool, str]:
    """Copia uno o varios archivos al portapapeles de Windows (FileDropList)."""
    existing = [str(p.resolve()) for p in paths if p.exists()]
    if not existing:
        return False, "No se encontraron archivos para copiar."

    env = os.environ.copy()
    env["WA_CLIP_FILES"] = "\n".join(existing)
    ps_cmd = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$col = New-Object System.Collections.Specialized.StringCollection; "
        "($env:WA_CLIP_FILES -split \"`n\") | Where-Object { $_ -ne '' } | "
        "ForEach-Object { [void]$col.Add($_) }; "
        "if ($col.Count -gt 0) { [System.Windows.Forms.Clipboard]::SetFileDropList($col) }"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Sta", "-Command", ps_cmd],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode == 0:
            return True, ""
        return False, proc.stderr.strip() or "Error al copiar al portapapeles."
    except Exception as exc:
        return False, str(exc)


def compute_scale_filter(width: int, height: int, resolution: str) -> str | None:
    """Calcula un filtro scale seguro (dimensiones pares, sin upscaling)."""
    if resolution == "1080p":
        target_short = 1080
    elif resolution == "720p":
        target_short = 720
    else:
        return None

    if width <= 0 or height <= 0:
        return f"scale=-2:{target_short}"

    if width >= height:
        if height <= target_short:
            return None
        new_h = target_short
        new_w = max(2, int(round(width * (new_h / height) / 2)) * 2)
        return f"scale={new_w}:{new_h}"
    else:
        if width <= target_short:
            return None
        new_w = target_short
        new_h = max(2, int(round(height * (new_w / width) / 2)) * 2)
        return f"scale={new_w}:{new_h}"


def run_ffmpeg_copy(input_path: Path, output_path: Path, start: float, duration: float) -> tuple[bool, str]:
    """Ejecuta un recorte instantáneo sin recodificar (-c copy)."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(input_path),
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        return False, proc.stderr.strip() or "FFmpeg falló durante el corte directo."
    return True, ""


def run_ffmpeg_compress(
    input_path: Path,
    output_path: Path,
    start: float,
    duration: float,
    target_mb: float,
    meta: dict,
    resolution: str,
    fps_opt: str,
    margin_factor: float = 0.92,
) -> tuple[bool, str, str]:
    """Comprime el tramo con h264_nvenc (o libx264 fallback) ajustando bitrate al límite."""
    has_audio = bool(meta.get("has_audio", True))
    # 1 MB = 1,000,000 bytes es más conservador que MiB (1,048,576) para asegurar quedar bajo el límite
    safe_bytes = max(1.0, target_mb) * margin_factor * 1_000_000.0
    total_kbps = (safe_bytes * 8.0 / 1000.0) / max(0.5, duration)

    if has_audio:
        audio_kbps = 160 if total_kbps >= 1500 else (128 if total_kbps >= 750 else 96)
    else:
        audio_kbps = 0

    video_kbps = max(250, int(total_kbps - audio_kbps))
    maxrate_kbps = max(video_kbps, int(video_kbps * 1.15))
    bufsize_kbps = max(video_kbps, int(video_kbps * 1.50))

    vf_parts: list[str] = []
    scale_expr = compute_scale_filter(meta.get("width", 0), meta.get("height", 0), resolution)
    if scale_expr:
        vf_parts.append(scale_expr)
    if fps_opt in ("30", "60"):
        src_fps = meta.get("fps", 0.0)
        target_fps = float(fps_opt)
        if src_fps <= 0 or src_fps > target_fps + 0.5:
            vf_parts.append(f"fps={fps_opt}")

    use_nvenc = check_nvenc_available()
    encoders = ["h264_nvenc", "libx264"] if use_nvenc else ["libx264"]

    last_error = ""
    for enc in encoders:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(input_path),
        ]
        if vf_parts:
            cmd.extend(["-vf", ",".join(vf_parts)])

        if enc == "h264_nvenc":
            cmd.extend(
                [
                    "-c:v",
                    "h264_nvenc",
                    "-preset",
                    "p5",
                    "-rc",
                    "vbr",
                    "-b:v",
                    f"{video_kbps}k",
                    "-maxrate",
                    f"{maxrate_kbps}k",
                    "-bufsize",
                    f"{bufsize_kbps}k",
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
        else:
            cmd.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-b:v",
                    f"{video_kbps}k",
                    "-maxrate",
                    f"{maxrate_kbps}k",
                    "-bufsize",
                    f"{bufsize_kbps}k",
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                ]
            )

        if has_audio:
            cmd.extend(["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"])
        else:
            cmd.append("-an")

        cmd.extend(["-movflags", "+faststart", str(output_path)])

        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True, enc, ""
        last_error = proc.stderr.strip() or f"Error al codificar con {enc}"

    return False, "", last_error


def process_video_request(payload: dict) -> dict:
    """Procesa la petición de recorte/compresión/división y devuelve el resultado."""
    t_start = time.perf_counter()
    rel_path = payload.get("path", "")
    input_path = resolve_safe_path(rel_path, must_exist=True)
    if not input_path.is_file():
        raise ValueError("La ruta seleccionada no es un archivo válido.")

    meta = probe_video(input_path)
    total_duration = meta.get("duration") or 0.0
    if total_duration <= 0:
        raise ValueError("No se pudo leer la duración del video.")

    start_time = max(0.0, float(payload.get("start_time", 0.0)))
    raw_end = payload.get("end_time", total_duration)
    end_time = total_duration if raw_end is None else min(total_duration, float(raw_end))
    if end_time - start_time < 0.3:
        raise ValueError("El tramo seleccionado debe durar al menos 0.3 segundos.")

    clip_duration = end_time - start_time
    mode = payload.get("mode", "smart")
    if mode not in ("smart", "compress", "copy", "split"):
        mode = "smart"

    target_mb = float(payload.get("target_mb", 170.0))
    target_mb = max(5.0, min(179.0, target_mb))

    resolution = payload.get("resolution", "original")
    if resolution not in ("original", "1080p", "720p"):
        resolution = "original"

    fps_opt = str(payload.get("fps", "original"))
    if fps_opt not in ("original", "60", "30"):
        fps_opt = "original"

    auto_clipboard = bool(payload.get("auto_clipboard", True))

    original_size_mb = input_path.stat().st_size / (1024 * 1024)
    estimated_clip_mb = original_size_mb * (clip_duration / total_duration)
    needs_filters = (resolution != "original") or (fps_opt != "original")

    output_dir = input_path.parent / OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = input_path.stem
    time_tag = f"{format_time_tag(start_time)}-{format_time_tag(end_time)}"
    outputs: list[Path] = []
    used_engine = ""

    if mode == "split":
        parts = max(2, math.ceil(estimated_clip_mb / (target_mb * 0.88)))
        part_dur = clip_duration / parts
        for idx in range(parts):
            p_start = start_time + idx * part_dur
            p_dur = (end_time - p_start) if idx == parts - 1 else part_dur
            out_file = unique_output_path(output_dir, f"{stem}_WA_{time_tag}_parte{idx + 1}de{parts}")
            ok, err = run_ffmpeg_copy(input_path, out_file, p_start, p_dur)
            if not ok:
                raise RuntimeError(err)
            out_mb = out_file.stat().st_size / (1024 * 1024)
            if out_mb > WHATSAPP_MAX_MB:
                # Si una parte superó por VBR, la comprimimos automáticamente con GPU
                tmp_compressed = unique_output_path(output_dir, f"{out_file.stem}_gpu")
                ok2, enc, err2 = run_ffmpeg_compress(
                    input_path, tmp_compressed, p_start, p_dur, target_mb, meta, resolution, fps_opt
                )
                if ok2:
                    out_file.unlink(missing_ok=True)
                    tmp_compressed.rename(out_file)
                    used_engine = f"copy + {enc}"
                else:
                    raise RuntimeError(err2)
            outputs.append(out_file)
        if not used_engine:
            used_engine = f"copy (dividido en {parts} partes)"

    elif mode == "copy":
        out_file = unique_output_path(output_dir, f"{stem}_WA_{time_tag}")
        ok, err = run_ffmpeg_copy(input_path, out_file, start_time, clip_duration)
        if not ok:
            raise RuntimeError(err)
        outputs.append(out_file)
        used_engine = "copy (corte directo)"

    else:
        # smart o compress
        can_try_copy = (mode == "smart") and (not needs_filters) and (estimated_clip_mb <= target_mb * 0.96)
        out_file = unique_output_path(output_dir, f"{stem}_WA_{time_tag}")

        did_copy = False
        if can_try_copy:
            ok, err = run_ffmpeg_copy(input_path, out_file, start_time, clip_duration)
            if ok:
                actual_mb = out_file.stat().st_size / (1024 * 1024)
                if actual_mb <= target_mb:
                    outputs.append(out_file)
                    used_engine = "copy (corte directo sin pérdida)"
                    did_copy = True
                else:
                    # El tramo tenía mayor bitrate que el promedio y excedió el límite; recomprimir
                    out_file.unlink(missing_ok=True)

        if not did_copy:
            ok, enc, err = run_ffmpeg_compress(
                input_path,
                out_file,
                start_time,
                clip_duration,
                target_mb,
                meta,
                resolution,
                fps_opt,
                margin_factor=0.92,
            )
            if not ok:
                raise RuntimeError(err)
            # Verificación extra por si VBR se excedió ligeramente del target_mb
            actual_mb = out_file.stat().st_size / (1024 * 1024)
            if actual_mb > target_mb and actual_mb > 1.0:
                tighter_margin = max(0.72, 0.90 * (target_mb / actual_mb))
                retry_file = unique_output_path(output_dir, f"{stem}_WA_{time_tag}_fit")
                ok2, enc2, _ = run_ffmpeg_compress(
                    input_path,
                    retry_file,
                    start_time,
                    clip_duration,
                    target_mb,
                    meta,
                    resolution,
                    fps_opt,
                    margin_factor=tighter_margin,
                )
                if ok2:
                    out_file.unlink(missing_ok=True)
                    retry_file.rename(out_file)
                    enc = enc2
            outputs.append(out_file)
            used_engine = "h264_nvenc (GPU RTX)" if enc == "h264_nvenc" else "libx264 (CPU)"

    clipboard_ok = False
    clipboard_msg = ""
    if auto_clipboard and outputs and SUPPORTS_DESKTOP_ACTIONS:
        clipboard_ok, clipboard_msg = copy_files_to_clipboard(outputs)

    elapsed = round(time.perf_counter() - t_start, 2)
    output_entries = [build_video_entry(p, include_probe=True) for p in outputs]
    total_output_mb = round(sum(e["size_mb"] for e in output_entries), 2)

    return {
        "ok": True,
        "elapsed_seconds": elapsed,
        "engine": used_engine,
        "original_size_mb": round(original_size_mb, 2),
        "total_output_mb": total_output_mb,
        "clip_duration": round(clip_duration, 2),
        "clipboard_copied": clipboard_ok,
        "clipboard_error": clipboard_msg,
        "outputs": output_entries,
    }


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cortador WhatsApp • GPU RTX</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
  :root {
    --bg: #0b1117;
    --panel: #131c24;
    --panel-2: #19242e;
    --border: #253442;
    --text: #e8eef4;
    --muted: #8ea1b2;
    --wa: #25d366;
    --wa-dark: #199e4b;
    --wa-glow: rgba(37, 211, 102, 0.18);
    --danger: #ef4444;
    --warn: #f59e0b;
    --info: #38bdf8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.4;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 14px 22px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-badge {
    width: 38px;
    height: 38px;
    border-radius: 10px;
    background: linear-gradient(135deg, var(--wa), #128c7e);
    display: grid;
    place-items: center;
    font-size: 20px;
    font-weight: 700;
    color: #051b0e;
    box-shadow: 0 4px 14px var(--wa-glow);
  }
  .brand h1 {
    margin: 0;
    font-size: 17px;
    font-weight: 700;
  }
  .brand p {
    margin: 0;
    font-size: 12px;
    color: var(--muted);
  }
  .header-controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .pill {
    font-size: 12px;
    padding: 5px 10px;
    border-radius: 999px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    color: var(--muted);
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .pill.gpu-on {
    color: var(--wa);
    border-color: rgba(37, 211, 102, 0.35);
    background: rgba(37, 211, 102, 0.08);
  }
  select, input[type="number"] {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 7px 11px;
    font-size: 13px;
    outline: none;
  }
  select:focus, input[type="number"]:focus {
    border-color: var(--wa);
  }
  button {
    font-family: inherit;
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--panel-2);
    color: var(--text);
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13px;
    font-weight: 600;
    transition: all 0.14s ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }
  button:hover:not(:disabled) {
    border-color: #3d5469;
    background: #21303d;
  }
  button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  button.primary {
    background: linear-gradient(135deg, var(--wa), #1cb857);
    color: #041d0d;
    border: none;
    padding: 12px 18px;
    font-size: 15px;
    font-weight: 700;
    box-shadow: 0 6px 20px var(--wa-glow);
  }
  button.primary:hover:not(:disabled) {
    filter: brightness(1.07);
    background: linear-gradient(135deg, #2ee070, #1fc75f);
  }
  button.accent {
    background: rgba(37, 211, 102, 0.14);
    border-color: rgba(37, 211, 102, 0.45);
    color: var(--wa);
  }
  button.accent:hover:not(:disabled) {
    background: rgba(37, 211, 102, 0.24);
  }

  .layout {
    display: grid;
    grid-template-columns: 320px 1fr;
    gap: 18px;
    padding: 18px 22px;
    max-width: 1580px;
    margin: 0 auto;
    align-items: start;
  }
  @media (max-width: 1050px) {
    .layout { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px;
  }
  .card-title {
    margin: 0 0 12px 0;
    font-size: 14px;
    font-weight: 700;
    color: var(--text);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  /* Sidebar videos */
  .video-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 58vh;
    overflow-y: auto;
    padding-right: 4px;
  }
  .video-item {
    text-align: left;
    width: 100%;
    padding: 10px 12px;
    border-radius: 10px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 5px;
    align-items: stretch;
  }
  .video-item.active {
    border-color: var(--wa);
    background: rgba(37, 211, 102, 0.08);
    box-shadow: inset 0 0 0 1px rgba(37, 211, 102, 0.25);
  }
  .video-name {
    font-size: 13px;
    font-weight: 600;
    word-break: break-word;
    color: var(--text);
  }
  .video-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 11px;
    color: var(--muted);
  }
  .size-badge {
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 999px;
    font-size: 11px;
  }
  .size-over {
    background: rgba(239, 68, 68, 0.16);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.4);
  }
  .size-ok {
    background: rgba(37, 211, 102, 0.14);
    color: #86efac;
    border: 1px solid rgba(37, 211, 102, 0.35);
  }

  /* Main workspace */
  .main-col {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .player-wrap {
    position: relative;
    background: #000;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid var(--border);
    aspect-ratio: 16 / 9;
    max-height: 52vh;
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  video {
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: #000;
  }

  /* Dual timeline */
  .timeline-box {
    margin-top: 14px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px;
  }
  .time-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 12px;
    font-size: 13px;
  }
  .mono {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-weight: 600;
  }
  .range-track-wrap {
    position: relative;
    height: 34px;
    display: flex;
    align-items: center;
    user-select: none;
    margin: 4px 0 10px 0;
  }
  .range-rail {
    position: absolute;
    left: 0;
    right: 0;
    height: 10px;
    background: #0d151c;
    border: 1px solid var(--border);
    border-radius: 999px;
    cursor: pointer;
  }
  .range-selection {
    position: absolute;
    height: 10px;
    background: linear-gradient(90deg, var(--wa), #20b858);
    border-radius: 999px;
    pointer-events: none;
  }
  .range-playhead {
    position: absolute;
    top: 3px;
    bottom: 3px;
    width: 3px;
    background: #ffffff;
    border-radius: 2px;
    box-shadow: 0 0 8px rgba(255,255,255,0.8);
    pointer-events: none;
    transform: translateX(-50%);
  }
  .handle {
    position: absolute;
    top: 50%;
    width: 18px;
    height: 26px;
    border-radius: 6px;
    background: #f8fafc;
    border: 2px solid var(--wa-dark);
    transform: translate(-50%, -50%);
    cursor: ew-resize;
    box-shadow: 0 2px 8px rgba(0,0,0,0.6);
    z-index: 3;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .handle::after {
    content: "";
    width: 2px;
    height: 12px;
    background: var(--wa-dark);
    border-radius: 1px;
  }
  .handle-end {
    border-color: var(--info);
  }
  .handle-end::after {
    background: #0284c7;
  }

  .trim-actions {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 10px;
    margin-top: 8px;
  }
  .trim-group {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    background: var(--panel);
    padding: 8px 10px;
    border-radius: 10px;
    border: 1px solid var(--border);
  }
  .trim-group .label {
    font-size: 12px;
    font-weight: 700;
    color: var(--muted);
    min-width: 52px;
  }
  .mini-btn {
    padding: 4px 8px;
    font-size: 12px;
    border-radius: 6px;
  }

  /* Settings grid */
  .settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 14px;
  }
  .field label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .presets {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .preset-btn {
    padding: 5px 10px;
    font-size: 12px;
  }
  .preset-btn.active {
    background: rgba(37, 211, 102, 0.16);
    border-color: var(--wa);
    color: var(--wa);
  }

  .estimate-bar {
    margin-top: 14px;
    padding: 12px 14px;
    border-radius: 10px;
    background: rgba(56, 189, 248, 0.08);
    border: 1px solid rgba(56, 189, 248, 0.25);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    font-size: 13px;
  }
  .estimate-bar.copy-fast {
    background: rgba(37, 211, 102, 0.08);
    border-color: rgba(37, 211, 102, 0.3);
  }

  .cta-row {
    margin-top: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }
  .checkbox-row {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    cursor: pointer;
    user-select: none;
  }
  .checkbox-row input {
    width: 16px;
    height: 16px;
    accent-color: var(--wa);
  }

  /* Result card */
  .result-card {
    border-color: rgba(37, 211, 102, 0.45);
    background: linear-gradient(180deg, rgba(37, 211, 102, 0.07), var(--panel));
  }
  .result-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 12px;
  }
  .result-files {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .result-file-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 10px;
    background: var(--panel-2);
    border: 1px solid var(--border);
  }
  .kbd {
    display: inline-block;
    padding: 1px 5px;
    font-size: 11px;
    border-radius: 4px;
    background: #0d151c;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: ui-monospace, monospace;
  }
  .toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: #142b1d;
    border: 1px solid var(--wa);
    color: #dcfce7;
    padding: 11px 16px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    box-shadow: 0 10px 30px rgba(0,0,0,0.45);
    z-index: 99;
    display: none;
  }
  .toast.error {
    background: #2f1416;
    border-color: var(--danger);
    color: #fecaca;
  }
  .spinner {
    width: 16px;
    height: 16px;
    border: 2px solid rgba(4, 29, 13, 0.25);
    border-top-color: #041d0d;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-badge">✂</div>
    <div>
      <h1>Cortador & Compresor WhatsApp <span id="versionBadge" class="pill" style="font-size:11px; padding:2px 8px; vertical-align:middle;">v1.0.0</span></h1>
      <p>Recorta tramos exactos o comprime bajo 180 MB con aceleración GPU</p>
    </div>
  </div>
  <div class="header-controls">
    <span id="gpuBadge" class="pill">Comprobando GPU...</span>
    <label style="font-size:12px; color:var(--muted);">Carpeta:</label>
    <select id="folderSelect"></select>
    <button id="refreshBtn" title="Actualizar lista de videos">↻ Actualizar</button>
  </div>
</header>

<div class="layout">
  <aside class="card">
    <h2 class="card-title">
      <span>Grabaciones</span>
      <span id="videoCount" class="pill">0</span>
    </h2>
    <div id="videoList" class="video-list">
      <div style="color:var(--muted); font-size:13px;">Cargando grabaciones...</div>
    </div>

    <div style="margin-top:16px; border-top:1px solid var(--border); padding-top:14px;">
      <h3 class="card-title" style="font-size:13px;">
        <span>Listos en WhatsApp_Ready</span>
        <span id="readyCount" class="pill">0</span>
      </h3>
      <div id="readyList" class="video-list" style="max-height:24vh;">
        <div style="color:var(--muted); font-size:12px;">Sin exportaciones aún</div>
      </div>
    </div>
  </aside>

  <main class="main-col">
    <section class="card">
      <div class="card-title">
        <span id="currentVideoTitle">Selecciona una grabación para comenzar</span>
        <span id="currentVideoInfo" class="pill">—</span>
      </div>

      <div class="player-wrap">
        <video id="videoPlayer" controls preload="metadata"></video>
      </div>

      <div class="timeline-box">
        <div class="time-header">
          <div>
            Inicio: <span id="lblStart" class="mono" style="color:var(--wa);">00:00.0</span>
            &nbsp;•&nbsp;
            Fin: <span id="lblEnd" class="mono" style="color:var(--info);">00:00.0</span>
            &nbsp;•&nbsp;
            Selección: <span id="lblClipDur" class="mono">00:00.0</span>
          </div>
          <div style="color:var(--muted); font-size:12px;">
            Atajos: <span class="kbd">Espacio</span> Play &nbsp;
            <span class="kbd">I</span> Inicio &nbsp;
            <span class="kbd">O</span> Fin &nbsp;
            <span class="kbd">←</span>/<span class="kbd">→</span> ±1s
          </div>
        </div>

        <div id="rangeTrackWrap" class="range-track-wrap">
          <div id="rangeRail" class="range-rail"></div>
          <div id="rangeSelection" class="range-selection"></div>
          <div id="rangePlayhead" class="range-playhead"></div>
          <div id="handleStart" class="handle handle-start" title="Arrastrar inicio"></div>
          <div id="handleEnd" class="handle handle-end" title="Arrastrar fin"></div>
        </div>

        <div class="trim-actions">
          <div class="trim-group">
            <span class="label" style="color:var(--wa);">INICIO</span>
            <button id="btnMarkStart" class="mini-btn accent">Marcar aquí (I)</button>
            <button class="mini-btn" onclick="nudgeTime('start', -1)">-1s</button>
            <button class="mini-btn" onclick="nudgeTime('start', -0.1)">-0.1</button>
            <button class="mini-btn" onclick="nudgeTime('start', 0.1)">+0.1</button>
            <button class="mini-btn" onclick="nudgeTime('start', 1)">+1s</button>
          </div>
          <div class="trim-group">
            <span class="label" style="color:var(--info);">FIN</span>
            <button id="btnMarkEnd" class="mini-btn">Marcar aquí (O)</button>
            <button class="mini-btn" onclick="nudgeTime('end', -1)">-1s</button>
            <button class="mini-btn" onclick="nudgeTime('end', -0.1)">-0.1</button>
            <button class="mini-btn" onclick="nudgeTime('end', 0.1)">+0.1</button>
            <button class="mini-btn" onclick="nudgeTime('end', 1)">+1s</button>
          </div>
          <div class="trim-group" style="justify-content: space-between;">
            <button id="btnPlaySegment" class="mini-btn accent">▶ Reproducir tramo</button>
            <button id="btnResetRange" class="mini-btn">↺ Todo el video</button>
          </div>
        </div>
      </div>
    </section>

    <section class="card">
      <h2 class="card-title">Configuración para WhatsApp (Máx. 180 MB)</h2>
      <div class="settings-grid">
        <div class="field">
          <label>Peso máximo objetivo (MB)</label>
          <div class="presets">
            <button class="preset-btn active" data-mb="170">170 MB (Máx. calidad)</button>
            <button class="preset-btn" data-mb="100">100 MB (Rápido)</button>
            <button class="preset-btn" data-mb="60">60 MB (Grupo)</button>
            <button class="preset-btn" data-mb="30">30 MB (Liviano)</button>
          </div>
          <div style="display:flex; align-items:center; gap:8px;">
            <input id="targetMbInput" type="number" min="5" max="178" step="1" value="170" style="width:100px;">
            <span style="font-size:12px; color:var(--muted);">MB (límite WhatsApp: 180 MB)</span>
          </div>
        </div>

        <div class="field">
          <label>Modo de procesamiento</label>
          <select id="modeSelect" style="width:100%;">
            <option value="smart">Automático Inteligente (Recomendado)</option>
            <option value="compress">Comprimir siempre con GPU (RTX 3090)</option>
            <option value="copy">Corte instantáneo (Sin recodificar)</option>
            <option value="split">Dividir en partes menores al límite</option>
          </select>
          <p id="modeHelp" style="margin:6px 0 0 0; font-size:12px; color:var(--muted);">
            Si el tramo cabe bajo el límite, corta al instante sin recodificar; si supera el límite, comprime con GPU.
          </p>
        </div>

        <div class="field">
          <label>Resolución y FPS (opcional)</label>
          <div style="display:flex; gap:8px;">
            <select id="resolutionSelect" style="flex:1;">
              <option value="original">Resolución Original</option>
              <option value="1080p">1080p (Full HD)</option>
              <option value="720p">720p (Más liviano)</option>
            </select>
            <select id="fpsSelect" style="flex:1;">
              <option value="original">FPS Original</option>
              <option value="60">60 FPS</option>
              <option value="30">30 FPS</option>
            </select>
          </div>
          <p style="margin:6px 0 0 0; font-size:12px; color:var(--muted);">
            Bajar a 720p o 30 FPS da imagen más nítida si el video es largo o el límite muy bajo.
          </p>
        </div>
      </div>

      <div id="estimateBar" class="estimate-bar">
        <span id="estimateText">Selecciona un video para ver la estimación.</span>
        <span id="estimateBitrate" class="mono"></span>
      </div>

      <div class="cta-row">
        <label class="checkbox-row">
          <input id="autoClipboardChk" type="checkbox" checked>
          <span>Copiar automáticamente al portapapeles (listo para pegar con <span class="kbd">Ctrl+V</span> en WhatsApp)</span>
        </label>

        <button id="processBtn" class="primary" disabled>
          ⚡ Preparar video para WhatsApp
        </button>
      </div>
    </section>

    <section id="resultCard" class="card result-card" style="display:none;">
      <div class="result-summary">
        <div>
          <h2 class="card-title" style="margin-bottom:4px; color:var(--wa);">✔ Video listo para WhatsApp</h2>
          <div id="resultStats" style="font-size:13px; color:var(--text);"></div>
        </div>
        <span id="resultEngine" class="pill gpu-on"></span>
      </div>
      <div id="resultFiles" class="result-files"></div>
    </section>
  </main>
</div>

<div id="toast" class="toast"></div>

<script>
const state = {
  folders: [],
  currentFolder: "",
  videos: [],
  readyVideos: [],
  selectedVideo: null,
  startTime: 0,
  endTime: 0,
  duration: 0,
  playingSegment: false,
  dragging: null,
  processing: false,
  supportsDesktop: true,
};

const els = {
  versionBadge: document.getElementById("versionBadge"),
  gpuBadge: document.getElementById("gpuBadge"),
  folderSelect: document.getElementById("folderSelect"),
  refreshBtn: document.getElementById("refreshBtn"),
  videoList: document.getElementById("videoList"),
  readyList: document.getElementById("readyList"),
  videoCount: document.getElementById("videoCount"),
  readyCount: document.getElementById("readyCount"),
  currentVideoTitle: document.getElementById("currentVideoTitle"),
  currentVideoInfo: document.getElementById("currentVideoInfo"),
  videoPlayer: document.getElementById("videoPlayer"),
  lblStart: document.getElementById("lblStart"),
  lblEnd: document.getElementById("lblEnd"),
  lblClipDur: document.getElementById("lblClipDur"),
  rangeTrackWrap: document.getElementById("rangeTrackWrap"),
  rangeRail: document.getElementById("rangeRail"),
  rangeSelection: document.getElementById("rangeSelection"),
  rangePlayhead: document.getElementById("rangePlayhead"),
  handleStart: document.getElementById("handleStart"),
  handleEnd: document.getElementById("handleEnd"),
  btnMarkStart: document.getElementById("btnMarkStart"),
  btnMarkEnd: document.getElementById("btnMarkEnd"),
  btnPlaySegment: document.getElementById("btnPlaySegment"),
  btnResetRange: document.getElementById("btnResetRange"),
  targetMbInput: document.getElementById("targetMbInput"),
  modeSelect: document.getElementById("modeSelect"),
  modeHelp: document.getElementById("modeHelp"),
  resolutionSelect: document.getElementById("resolutionSelect"),
  fpsSelect: document.getElementById("fpsSelect"),
  estimateBar: document.getElementById("estimateBar"),
  estimateText: document.getElementById("estimateText"),
  estimateBitrate: document.getElementById("estimateBitrate"),
  autoClipboardChk: document.getElementById("autoClipboardChk"),
  processBtn: document.getElementById("processBtn"),
  resultCard: document.getElementById("resultCard"),
  resultStats: document.getElementById("resultStats"),
  resultEngine: document.getElementById("resultEngine"),
  resultFiles: document.getElementById("resultFiles"),
  toast: document.getElementById("toast"),
};

function showToast(msg, isError = false) {
  els.toast.textContent = msg;
  els.toast.className = "toast" + (isError ? " error" : "");
  els.toast.style.display = "block";
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { els.toast.style.display = "none"; }, 3800);
}

function fmtTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const mins = Math.floor(sec / 60);
  const secs = sec % 60;
  return `${String(mins).padStart(2, "0")}:${secs.toFixed(1).padStart(4, "0")}`;
}

async function apiGet(url) {
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Error de servidor");
  return data;
}

async function apiPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Error de servidor");
  return data;
}

async function init() {
  try {
    const info = await apiGet("/api/folders");
    state.folders = info.folders || [];
    state.currentFolder = info.default_folder || ".";
    state.supportsDesktop = info.supports_desktop_actions !== false;
    if (info.version && els.versionBadge) {
      els.versionBadge.textContent = `v${info.version}`;
    }
    if (!state.supportsDesktop && els.autoClipboardChk) {
      els.autoClipboardChk.checked = false;
      const row = els.autoClipboardChk.closest(".checkbox-row");
      if (row) row.style.display = "none";
    }
    if (info.nvenc_available) {
      els.gpuBadge.textContent = "⚡ GPU NVIDIA NVENC activa";
      els.gpuBadge.classList.add("gpu-on");
    } else {
      els.gpuBadge.textContent = "CPU libx264 (sin NVENC)";
    }

    els.folderSelect.innerHTML = "";
    for (const f of state.folders) {
      const opt = document.createElement("option");
      opt.value = f.path;
      opt.textContent = f.label;
      if (f.path === state.currentFolder) opt.selected = true;
      els.folderSelect.appendChild(opt);
    }
    await loadVideos(state.currentFolder);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function loadVideos(folder, autoSelectFirst = true) {
  els.videoList.innerHTML = '<div style="color:var(--muted); font-size:13px;">Escaneando videos...</div>';
  try {
    const data = await apiGet(`/api/videos?folder=${encodeURIComponent(folder)}`);
    state.videos = data.videos || [];
    state.readyVideos = data.ready_videos || [];
    renderVideoLists();

    if (autoSelectFirst && state.videos.length > 0) {
      const firstMatch = state.selectedVideo && state.videos.find(v => v.path === state.selectedVideo.path);
      selectVideo(firstMatch || state.videos[0]);
    } else if (state.videos.length === 0) {
      state.selectedVideo = null;
      els.currentVideoTitle.textContent = "No hay grabaciones en esta carpeta";
      els.currentVideoInfo.textContent = "—";
      els.videoPlayer.removeAttribute("src");
      els.videoPlayer.load();
      els.processBtn.disabled = true;
      updateEstimate();
    }
  } catch (err) {
    showToast(err.message, true);
  }
}

function renderVideoLists() {
  els.videoCount.textContent = state.videos.length;
  els.readyCount.textContent = state.readyVideos.length;

  if (state.videos.length === 0) {
    els.videoList.innerHTML = '<div style="color:var(--muted); font-size:13px;">No se encontraron videos aquí.</div>';
  } else {
    els.videoList.innerHTML = "";
    for (const v of state.videos) {
      const btn = document.createElement("button");
      btn.className = "video-item" + (state.selectedVideo && state.selectedVideo.path === v.path ? " active" : "");
      const badgeCls = v.over_limit ? "size-over" : "size-ok";
      const resTxt = v.height ? `${v.width}x${v.height}` : "";
      const fpsTxt = v.fps ? `${Math.round(v.fps)}fps` : "";
      btn.innerHTML = `
        <span class="video-name">${escapeHtml(v.name)}</span>
        <span class="video-meta">
          <span>${fmtTime(v.duration)} • ${resTxt} ${fpsTxt}</span>
          <span class="size-badge ${badgeCls}">${v.size_mb.toFixed(1)} MB</span>
        </span>
      `;
      btn.onclick = () => selectVideo(v);
      els.videoList.appendChild(btn);
    }
  }

  if (state.readyVideos.length === 0) {
    els.readyList.innerHTML = '<div style="color:var(--muted); font-size:12px;">Sin exportaciones aún</div>';
  } else {
    els.readyList.innerHTML = "";
    for (const rv of state.readyVideos) {
      const row = document.createElement("div");
      row.className = "video-item";
      row.innerHTML = `
        <span class="video-name" style="font-size:12px;">${escapeHtml(rv.name)}</span>
        <span class="video-meta">
          <span>${fmtTime(rv.duration)}</span>
          <span class="size-badge size-ok">${rv.size_mb.toFixed(1)} MB</span>
        </span>
        <div style="display:flex; gap:6px; margin-top:4px; flex-wrap:wrap;">
          ${state.supportsDesktop ? `<button class="mini-btn accent" style="flex:1;" onclick="copyPathToClipboard('${escapeAttr(rv.path)}')">📋 Copiar</button>` : ""}
          <button class="mini-btn" onclick="previewExport('${escapeAttr(rv.path)}')">▶ Ver</button>
          <a class="mini-btn" href="/api/download?path=${encodeURIComponent(rv.path)}" download style="text-decoration:none; border:1px solid var(--border); background:var(--panel-2); color:var(--text);">⬇</a>
          ${state.supportsDesktop ? `<button class="mini-btn" onclick="revealFile('${escapeAttr(rv.path)}')">📂</button>` : ""}
        </div>
      `;
      els.readyList.appendChild(row);
    }
  }
}

function selectVideo(v) {
  state.selectedVideo = v;
  state.playingSegment = false;
  state.duration = v.duration || 0;
  state.startTime = 0;
  state.endTime = state.duration;
  els.currentVideoTitle.textContent = v.name;
  els.currentVideoInfo.textContent = `${v.size_mb.toFixed(2)} MB • ${v.width}x${v.height} • ${Math.round(v.fps || 0)} fps`;
  els.videoPlayer.src = `/api/video?path=${encodeURIComponent(v.path)}`;
  els.processBtn.disabled = false;
  renderVideoLists();
  updateTimelineUI();
  updateEstimate();
}

function previewExport(path) {
  state.playingSegment = false;
  els.videoPlayer.src = `/api/video?path=${encodeURIComponent(path)}`;
  els.videoPlayer.play().catch(() => {});
  showToast("Reproduciendo archivo exportado");
}

function updateTimelineUI() {
  const dur = Math.max(0.01, state.duration);
  const sPct = (state.startTime / dur) * 100;
  const ePct = (state.endTime / dur) * 100;
  const curPct = Math.min(100, Math.max(0, ((els.videoPlayer.currentTime || 0) / dur) * 100));

  els.handleStart.style.left = `${sPct}%`;
  els.handleEnd.style.left = `${ePct}%`;
  els.rangeSelection.style.left = `${sPct}%`;
  els.rangeSelection.style.width = `${Math.max(0, ePct - sPct)}%`;
  els.rangePlayhead.style.left = `${curPct}%`;

  els.lblStart.textContent = fmtTime(state.startTime);
  els.lblEnd.textContent = fmtTime(state.endTime);
  els.lblClipDur.textContent = fmtTime(Math.max(0, state.endTime - state.startTime));
}

function nudgeTime(which, delta) {
  if (!state.selectedVideo) return;
  if (which === "start") {
    state.startTime = Math.max(0, Math.min(state.endTime - 0.3, state.startTime + delta));
    els.videoPlayer.currentTime = state.startTime;
  } else {
    state.endTime = Math.min(state.duration, Math.max(state.startTime + 0.3, state.endTime + delta));
    els.videoPlayer.currentTime = state.endTime;
  }
  updateTimelineUI();
  updateEstimate();
}

function updateEstimate() {
  const v = state.selectedVideo;
  if (!v || state.duration <= 0) {
    els.estimateText.textContent = "Selecciona un video para ver la estimación.";
    els.estimateBitrate.textContent = "";
    return;
  }
  const clipDur = Math.max(0.3, state.endTime - state.startTime);
  const rawEstMb = v.size_mb * (clipDur / state.duration);
  const targetMb = Math.max(5, Math.min(179, parseFloat(els.targetMbInput.value) || 170));
  const mode = els.modeSelect.value;
  const hasFilters = els.resolutionSelect.value !== "original" || els.fpsSelect.value !== "original";

  const modeDescriptions = {
    smart: "Si el tramo cabe bajo el límite, corta al instante sin recodificar; si supera el límite, comprime con GPU.",
    compress: "Recodifica siempre con GPU RTX 3090 para garantizar que el archivo pese menos del objetivo.",
    copy: "Corta al instante sin recomprimir ni perder calidad (puede superar el límite si el tramo es largo).",
    split: "Divide el tramo seleccionado en partes iguales menores al límite sin perder calidad.",
  };
  els.modeHelp.textContent = modeDescriptions[mode] || "";

  let willCopy = false;
  if (mode === "copy") {
    willCopy = true;
    els.estimateText.textContent = `Corte directo (~${rawEstMb.toFixed(1)} MB estimados). ${rawEstMb > 180 ? "⚠ Podría superar 180 MB." : "✔ Cabe directo."}`;
    els.estimateBitrate.textContent = "Sin recodificar";
  } else if (mode === "split") {
    const parts = Math.max(2, Math.ceil(rawEstMb / (targetMb * 0.88)));
    willCopy = true;
    els.estimateText.textContent = `Se dividirá en ${parts} partes de ~${(rawEstMb / parts).toFixed(1)} MB c/u (sin perder calidad).`;
    els.estimateBitrate.textContent = `${parts} archivos`;
  } else if (mode === "smart" && !hasFilters && rawEstMb <= targetMb * 0.96) {
    willCopy = true;
    els.estimateText.textContent = `✔ El tramo estimado pesa ~${rawEstMb.toFixed(1)} MB (bajo ${targetMb} MB). Se hará corte instantáneo sin pérdida.`;
    els.estimateBitrate.textContent = "Instantáneo (-c copy)";
  } else {
    const safeBytes = targetMb * 0.92 * 1_000_000;
    const totalKbps = (safeBytes * 8 / 1000) / clipDur;
    const audioKbps = totalKbps >= 1500 ? 160 : (totalKbps >= 750 ? 128 : 96);
    const videoKbps = Math.max(250, Math.round(totalKbps - audioKbps));
    const mbps = (videoKbps / 1000).toFixed(1);
    els.estimateText.textContent = `⚡ Compresión GPU NVENC: de ~${rawEstMb.toFixed(1)} MB → ≤ ${targetMb} MB en ~2-5 segundos.`;
    els.estimateBitrate.textContent = `Bitrate video: ~${mbps} Mbps`;
  }
  els.estimateBar.className = "estimate-bar" + (willCopy ? " copy-fast" : "");
}

// Timeline mouse interaction
function clientXToTime(clientX) {
  const rect = els.rangeRail.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
  return ratio * state.duration;
}

els.handleStart.addEventListener("mousedown", (e) => { e.stopPropagation(); state.dragging = "start"; });
els.handleEnd.addEventListener("mousedown", (e) => { e.stopPropagation(); state.dragging = "end"; });
els.rangeRail.addEventListener("mousedown", (e) => {
  if (!state.selectedVideo) return;
  state.dragging = "seek";
  const t = clientXToTime(e.clientX);
  els.videoPlayer.currentTime = t;
  updateTimelineUI();
});

window.addEventListener("mousemove", (e) => {
  if (!state.dragging || !state.selectedVideo) return;
  const t = clientXToTime(e.clientX);
  if (state.dragging === "start") {
    state.startTime = Math.max(0, Math.min(state.endTime - 0.3, t));
    els.videoPlayer.currentTime = state.startTime;
  } else if (state.dragging === "end") {
    state.endTime = Math.min(state.duration, Math.max(state.startTime + 0.3, t));
    els.videoPlayer.currentTime = state.endTime;
  } else if (state.dragging === "seek") {
    els.videoPlayer.currentTime = t;
  }
  updateTimelineUI();
  updateEstimate();
});
window.addEventListener("mouseup", () => { state.dragging = null; });

els.videoPlayer.addEventListener("loadedmetadata", () => {
  if (Number.isFinite(els.videoPlayer.duration) && els.videoPlayer.duration > 0) {
    if (state.duration <= 0) {
      state.duration = els.videoPlayer.duration;
      state.endTime = state.duration;
    }
    updateTimelineUI();
    updateEstimate();
  }
});

els.videoPlayer.addEventListener("timeupdate", () => {
  if (state.playingSegment && els.videoPlayer.currentTime >= state.endTime) {
    els.videoPlayer.pause();
    state.playingSegment = false;
  }
  updateTimelineUI();
});

els.btnMarkStart.onclick = () => {
  if (!state.selectedVideo) return;
  const cur = els.videoPlayer.currentTime || 0;
  state.startTime = Math.max(0, Math.min(state.endTime - 0.3, cur));
  updateTimelineUI();
  updateEstimate();
};

els.btnMarkEnd.onclick = () => {
  if (!state.selectedVideo) return;
  const cur = els.videoPlayer.currentTime || 0;
  state.endTime = Math.min(state.duration, Math.max(state.startTime + 0.3, cur));
  updateTimelineUI();
  updateEstimate();
};

els.btnPlaySegment.onclick = () => {
  if (!state.selectedVideo) return;
  els.videoPlayer.currentTime = state.startTime;
  state.playingSegment = true;
  els.videoPlayer.play().catch(() => {});
};

els.btnResetRange.onclick = () => {
  if (!state.selectedVideo) return;
  state.startTime = 0;
  state.endTime = state.duration;
  updateTimelineUI();
  updateEstimate();
};

document.querySelectorAll(".preset-btn").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".preset-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    els.targetMbInput.value = btn.dataset.mb;
    updateEstimate();
  };
});

els.targetMbInput.addEventListener("input", () => {
  document.querySelectorAll(".preset-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.mb === els.targetMbInput.value);
  });
  updateEstimate();
});
els.modeSelect.addEventListener("change", updateEstimate);
els.resolutionSelect.addEventListener("change", updateEstimate);
els.fpsSelect.addEventListener("change", updateEstimate);

els.folderSelect.addEventListener("change", () => {
  state.currentFolder = els.folderSelect.value;
  loadVideos(state.currentFolder, true);
});
els.refreshBtn.addEventListener("click", () => loadVideos(state.currentFolder, false));

// Keyboard shortcuts
window.addEventListener("keydown", (e) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if (!state.selectedVideo) return;
  if (e.code === "Space") {
    e.preventDefault();
    if (els.videoPlayer.paused) els.videoPlayer.play(); else els.videoPlayer.pause();
  } else if (e.key === "i" || e.key === "I") {
    e.preventDefault();
    els.btnMarkStart.click();
  } else if (e.key === "o" || e.key === "O") {
    e.preventDefault();
    els.btnMarkEnd.click();
  } else if (e.code === "ArrowLeft") {
    e.preventDefault();
    els.videoPlayer.currentTime = Math.max(0, els.videoPlayer.currentTime - (e.shiftKey ? 5 : 1));
  } else if (e.code === "ArrowRight") {
    e.preventDefault();
    els.videoPlayer.currentTime = Math.min(state.duration, els.videoPlayer.currentTime + (e.shiftKey ? 5 : 1));
  }
});

els.processBtn.onclick = async () => {
  if (!state.selectedVideo || state.processing) return;
  state.processing = true;
  els.processBtn.disabled = true;
  const origHtml = els.processBtn.innerHTML;
  els.processBtn.innerHTML = '<span class="spinner"></span> Procesando...';

  try {
    const res = await apiPost("/api/process", {
      path: state.selectedVideo.path,
      start_time: state.startTime,
      end_time: state.endTime,
      mode: els.modeSelect.value,
      target_mb: parseFloat(els.targetMbInput.value) || 170,
      resolution: els.resolutionSelect.value,
      fps: els.fpsSelect.value,
      auto_clipboard: els.autoClipboardChk.checked,
    });

    renderResult(res);
    await loadVideos(state.currentFolder, false);

    if (res.clipboard_copied) {
      showToast("✔ Listo y copiado al portapapeles. ¡Pega con Ctrl+V en WhatsApp!");
    } else {
      showToast("✔ Video listo en WhatsApp_Ready");
    }
  } catch (err) {
    showToast(err.message, true);
  } finally {
    state.processing = false;
    els.processBtn.disabled = false;
    els.processBtn.innerHTML = origHtml;
  }
};

function renderResult(res) {
  els.resultCard.style.display = "block";
  const pctSaved = res.original_size_mb > 0
    ? Math.max(0, Math.round((1 - res.total_output_mb / res.original_size_mb) * 100))
    : 0;
  els.resultStats.textContent =
    `${res.original_size_mb.toFixed(1)} MB → ${res.total_output_mb.toFixed(1)} MB (-${pctSaved}%) • Tramo: ${fmtTime(res.clip_duration)} • Tiempo proceso: ${res.elapsed_seconds}s`;
  els.resultEngine.textContent = res.engine;

  els.resultFiles.innerHTML = "";
  for (const out of res.outputs) {
    const row = document.createElement("div");
    row.className = "result-file-row";
    row.innerHTML = `
      <div>
        <div style="font-weight:700; font-size:13px;">${escapeHtml(out.name)}</div>
        <div style="font-size:12px; color:var(--muted);">
          ${out.size_mb.toFixed(2)} MB • ${fmtTime(out.duration)} • ${out.width}x${out.height}
          ${res.clipboard_copied ? ' • <span style="color:var(--wa);">Copiado al portapapeles (Ctrl+V listo)</span>' : ""}
        </div>
      </div>
      <div style="display:flex; gap:8px; flex-wrap:wrap;">
        ${state.supportsDesktop ? `<button class="accent" onclick="copyPathToClipboard('${escapeAttr(out.path)}')">📋 Copiar para WhatsApp</button>` : ""}
        <a class="accent" href="/api/download?path=${encodeURIComponent(out.path)}" download style="text-decoration:none; padding:7px 12px; border-radius:8px; border:1px solid rgba(37, 211, 102, 0.45); font-size:13px; font-weight:600; display:inline-flex; align-items:center; gap:6px;">⬇ Descargar</a>
        <button onclick="previewExport('${escapeAttr(out.path)}')">▶ Previsualizar</button>
        ${state.supportsDesktop ? `<button onclick="revealFile('${escapeAttr(out.path)}')">📂 Abrir carpeta</button>` : ""}
      </div>
    `;
    els.resultFiles.appendChild(row);
  }
  els.resultCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function copyPathToClipboard(path) {
  try {
    await apiPost("/api/clipboard", { paths: [path] });
    showToast("📋 Copiado. Ve a WhatsApp y presiona Ctrl+V");
  } catch (err) {
    showToast(err.message, true);
  }
}

async function revealFile(path) {
  try {
    await apiPost("/api/reveal", { path });
  } catch (err) {
    showToast(err.message, true);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}
function escapeAttr(s) {
  return String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

init();
</script>
</body>
</html>
"""


DISCONNECT_ERRORS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address) -> None:
        """Silencia desconexiones abruptas del navegador durante keep-alive o streaming."""
        exc = sys.exc_info()[1]
        if isinstance(exc, DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except DISCONNECT_ERRORS:
            self.close_connection = True

    def log_message(self, format: str, *args) -> None:
        # Silenciar logs ruidosos de rangos de video en consola
        pass

    def _safe_write(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except DISCONNECT_ERRORS:
            self.close_connection = True

    def _send_json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._safe_write(payload)

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._safe_write(payload)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/":
                self._send_html(HTML_PAGE)
            elif route == "/favicon.ico":
                self._handle_favicon()
            elif route == "/api/health":
                self._handle_health()
            elif route == "/api/folders":
                self._handle_folders()
            elif route == "/api/videos":
                folder = qs.get("folder", [DEFAULT_FOLDER])[0]
                self._handle_videos(folder)
            elif route == "/api/video":
                rel_path = qs.get("path", [""])[0]
                self._handle_stream_video(rel_path)
            elif route == "/api/download":
                rel_path = qs.get("path", [""])[0]
                self._handle_download(rel_path)
            else:
                self._send_json(404, {"error": "Ruta no encontrada"})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        try:
            body = self._read_json_body()
            if route == "/api/process":
                result = process_video_request(body)
                self._send_json(200, result)
            elif route == "/api/clipboard":
                if not SUPPORTS_DESKTOP_ACTIONS:
                    raise ValueError("Acción de escritorio solo disponible en Windows local.")
                raw_paths = body.get("paths", [])
                resolved = [resolve_safe_path(p, must_exist=True) for p in raw_paths]
                ok, msg = copy_files_to_clipboard(resolved)
                if not ok:
                    raise RuntimeError(msg or "No se pudo copiar al portapapeles.")
                self._send_json(200, {"ok": True})
            elif route == "/api/reveal":
                if not SUPPORTS_DESKTOP_ACTIONS:
                    raise ValueError("Acción de escritorio solo disponible en Windows local.")
                target = resolve_safe_path(body.get("path", ""), must_exist=True)
                subprocess.Popen(f'explorer.exe /select,"{target}"')
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"error": "Ruta no encontrada"})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_health(self) -> None:
        self._send_json(
            200,
            {
                "status": "ok",
                "version": __version__,
                "nvenc_available": check_nvenc_available(),
                "in_docker": IN_DOCKER,
                "supports_desktop_actions": SUPPORTS_DESKTOP_ACTIONS,
                "videos_root": str(VIDEOS_ROOT),
            },
        )

    def _handle_favicon(self) -> None:
        icon_path = get_resource_path("assets/app_icon.ico")
        if not icon_path.is_file():
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = icon_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/x-icon")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self._safe_write(data)

    def _handle_download(self, rel_path: str) -> None:
        file_path = resolve_safe_path(rel_path, must_exist=True)
        if not file_path.is_file():
            raise ValueError("El archivo solicitado no es válido.")
        file_size = file_path.stat().st_size
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = "application/octet-stream"

        ascii_name = file_path.name.encode("ascii", "ignore").decode("ascii").replace('"', "") or "video.mp4"
        utf8_name = urllib.parse.quote(file_path.name)
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}',
        )
        self.end_headers()

        chunk_size = 256 * 1024
        try:
            with file_path.open("rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except DISCONNECT_ERRORS:
            self.close_connection = True

    def _handle_folders(self) -> None:
        folders = []
        if VIDEOS_ROOT.exists():
            # Incluir subcarpetas de Videos que contengan archivos o sean la predeterminada
            for item in sorted(VIDEOS_ROOT.iterdir(), key=lambda p: p.name.lower()):
                if item.is_dir() and not item.name.startswith("."):
                    folders.append({"path": item.name, "label": item.name})
            # Incluir raíz de Videos si tiene videos sueltos
            has_root_videos = any(
                p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS for p in VIDEOS_ROOT.iterdir()
            )
            if has_root_videos or not folders:
                folders.insert(0, {"path": ".", "label": "(Carpeta Videos raíz)"})

        self._send_json(
            200,
            {
                "version": __version__,
                "folders": folders,
                "default_folder": DEFAULT_FOLDER,
                "nvenc_available": check_nvenc_available(),
                "supports_desktop_actions": SUPPORTS_DESKTOP_ACTIONS,
                "in_docker": IN_DOCKER,
            },
        )

    def _handle_videos(self, folder: str) -> None:
        folder_path = resolve_safe_path(folder, must_exist=True)
        if not folder_path.is_dir():
            raise ValueError("La ruta especificada no es una carpeta.")

        raw_files = [
            p
            for p in folder_path.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ]
        raw_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        videos = [build_video_entry(p, include_probe=True) for p in raw_files]

        ready_dir = folder_path / OUTPUT_SUBDIR
        ready_videos = []
        if ready_dir.is_dir():
            ready_files = [
                p
                for p in ready_dir.iterdir()
                if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
            ]
            ready_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            ready_videos = [build_video_entry(p, include_probe=True) for p in ready_files[:30]]

        self._send_json(200, {"videos": videos, "ready_videos": ready_videos})

    def _handle_stream_video(self, rel_path: str) -> None:
        file_path = resolve_safe_path(rel_path, must_exist=True)
        if not file_path.is_file():
            raise ValueError("El video solicitado no es un archivo.")

        file_size = file_path.stat().st_size
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = "video/mp4"

        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = 200

        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].strip().split(",")[0]
            if "-" in spec:
                s_part, e_part = spec.split("-", 1)
                if s_part == "" and e_part:
                    suffix_len = int(e_part)
                    start = max(0, file_size - suffix_len)
                else:
                    start = int(s_part) if s_part else 0
                    if e_part:
                        end = min(file_size - 1, int(e_part))
                if start >= file_size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", mime_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        chunk_size = 256 * 1024
        remaining = length
        try:
            with file_path.open("rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except DISCONNECT_ERRORS:
            self.close_connection = True


def start_server(
    preferred_port: int = 8765,
    open_browser: bool = True,
    host: str = "127.0.0.1",
) -> None:
    """Inicia el servidor HTTP local buscando un puerto disponible."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ERROR: No se encontró ffmpeg o ffprobe en el PATH.", file=sys.stderr)
        sys.exit(1)

    server = None
    bound_port = preferred_port
    for port in range(preferred_port, preferred_port + 15):
        try:
            server = ThreadingHTTPServer((host, port), RequestHandler)
            bound_port = port
            break
        except OSError:
            continue

    if server is None:
        server = ThreadingHTTPServer((host, 0), RequestHandler)
        bound_port = server.server_address[1]

    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{display_host}:{bound_port}"
    print(f"{APP_NAME} v{__version__} listo en: {url} (bind={host}:{bound_port})")
    print(f"Carpeta raíz: {VIDEOS_ROOT} (predeterminada: {DEFAULT_FOLDER})")
    print(f"GPU h264_nvenc: {'Disponible' if check_nvenc_available() else 'No disponible (usando libx264)'}")
    print("Presiona Ctrl+C para cerrar.")

    if open_browser and not IN_DOCKER:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


if __name__ == "__main__":
    default_host = os.environ.get("WA_HOST", "127.0.0.1")
    default_port = int(os.environ.get("WA_PORT", "8765"))
    parser = argparse.ArgumentParser(description="Cortador y compresor visual para WhatsApp")
    parser.add_argument("--host", default=default_host, help="Interfaz de red (ej. 127.0.0.1 o 0.0.0.0)")
    parser.add_argument("--port", type=int, default=default_port, help="Puerto HTTP local")
    parser.add_argument("--videos-dir", default=None, help="Carpeta raíz donde buscar grabaciones")
    parser.add_argument("--default-folder", default=None, help="Subcarpeta seleccionada por defecto")
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador automáticamente")
    parser.add_argument("--version", action="store_true", help="Mostrar la versión y salir")
    args = parser.parse_args()

    if args.version:
        print(f"{APP_NAME} {__version__}")
        sys.exit(0)

    VIDEOS_ROOT, DEFAULT_FOLDER = resolve_runtime_paths(args.videos_dir, args.default_folder)
    start_server(preferred_port=args.port, open_browser=not args.no_browser, host=args.host)
