from flask import Flask, request, jsonify, render_template, send_from_directory, url_for
from yt_dlp import YoutubeDL
import os
import re
from pathlib import Path
from typing import Optional, Dict, Any, List

app = Flask(__name__)

# === Config ===
DOWNLOAD_FOLDER = os.getenv("DOWNLOAD_FOLDER", "./downloads")
DOWNLOAD_DIR = Path(DOWNLOAD_FOLDER).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_EXTS = {"m4a", "webm", "opus", "mp4", "m4b", "mp3"}  # sin FFmpeg, guardamos lo que venga

# --- Utilidades ---
INVALID_WIN_CHARS = r'<>:"/\\|?*\0'
def sanitize_filename(name: str) -> str:
    cleaned = re.sub(f"[{re.escape(INVALID_WIN_CHARS)}]", "_", name)
    cleaned = cleaned.strip().rstrip(". ")
    reserved = {
        "CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5",
        "COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4",
        "LPT5","LPT6","LPT7","LPT8","LPT9"
    }
    if cleaned.upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned or "audio"

def pick_best_audio(formats: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Elige el mejor formato de audio-only disponible:
    1) M4A/AAC si existe
    2) OPUS/WEBM
    3) Audio-only MP4/M4B
    Retorna el dict del formato elegido (con format_id).
    """
    if not formats:
        return None
    # Filtrar solo audio-only (vcodec = none)
    audios = [f for f in formats if (f.get("vcodec") in (None, "none"))]
    if not audios:
        return None

    def score(f: Dict[str, Any]) -> tuple:
        ext = (f.get("ext") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        abr = f.get("abr") or 0  # audio bitrate
        # preferencia: m4a/aac > opus/webm > otros
        if ext == "m4a" or acodec.startswith("mp4a") or "aac" in acodec:
            priority = 0
        elif ext in ("webm", "opus") or "opus" in acodec:
            priority = 1
        else:
            priority = 2
        # mayor abr primero
        return (priority, -abr)

    audios.sort(key=score)
    return audios[0] if audios else None

def find_audio_file(safe_title: str) -> Optional[Path]:
    # Busca archivos de audio que empiecen con el título (en toda la carpeta)
    candidates = [
        p for p in DOWNLOAD_DIR.rglob(f"{safe_title}.*")
        if p.is_file() and p.suffix.lstrip(".").lower() in AUDIO_EXTS
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]

# --- Rutas ---
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/downloads", methods=["POST"])
def download_audio():
    data = request.get_json(silent=True) or {}
    video_url = (data.get("url") or "").strip()

    if not video_url:
        return jsonify({"error": "URL is required"}), 400
    if not re.match(r"^https?://", video_url):
        return jsonify({"error": "Invalid URL format"}), 400

    base_opts = {
        "noplaylist": True,
        "write_pages": False,
        "ignoreconfig": True,     # ignora config global (evita .mhtml)
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "windowsfilenames": True,
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),  # ruta absoluta
        "overwrites": True,
        "rm_cache_dir": True,
    }

    try:
        # 1) SOLO listar info y formatos (sin descargar)
        with YoutubeDL({**base_opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(video_url, download=False)
        if not info:
            return jsonify({"error": "No se pudo obtener información del video."}), 500

        title = info.get("title") or "audio"
        safe_title = sanitize_filename(title)
        formats = info.get("formats") or []
        best_audio = pick_best_audio(formats)

        if not best_audio:
            # Sin pista de audio-only visible (puede ser restricción/cookies)
            return jsonify({
                "error": "No se encontró una pista de audio-only disponible para este video. "
                         "Puede requerir inicio de sesión/cookies o estar restringido por región/edad."
            }), 500

        fmt_id = best_audio.get("format_id")
        ext = (best_audio.get("ext") or "").lower()

        # 2) Descargar usando exactamente ese format_id
        ydl_opts = {
            **base_opts,
            "format": fmt_id,  # forzamos el formato exacto encontrado
        }
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(video_url, download=True)

        # 3) Ubicar el archivo final por título (y extensión elegida)
        exact = DOWNLOAD_DIR / f"{safe_title}.{ext}"
        final_path = exact if exact.exists() else find_audio_file(safe_title)

        # Higiene: si quedó un .mhtml homónimo, eliminar
        mhtml = DOWNLOAD_DIR / f"{safe_title}.mhtml"
        if mhtml.exists():
            try:
                mhtml.unlink()
            except Exception:
                pass

        if not final_path or not final_path.exists():
            return jsonify({
                "error": "Se eligió un formato de audio, pero no se encontró el archivo final. "
                         "Prueba actualizar yt-dlp, verificar conexión o usar otra URL."
            }), 500

        file_url = url_for("serve_file", filename=final_path.name, _external=True)
        return jsonify({
            "title": title,
            "filename": final_path.name,
            "file_url": file_url,
            "status": "Downloaded"
        }), 200

    except Exception as e:
        return jsonify({"error": f"Error descargando: {str(e)}"}), 500

@app.route("/files/<path:filename>")
def serve_file(filename):
    return send_from_directory(str(DOWNLOAD_DIR), filename, as_attachment=True)

@app.route("/files", methods=["GET"])
def list_files():
    files = sorted(
        [p.name for p in DOWNLOAD_DIR.rglob("*")
         if p.is_file() and p.suffix.lstrip(".").lower() in AUDIO_EXTS]
    )
    return jsonify({"files": files})

# Endpoint opcional de depuración: lista los formatos sin descargar
@app.route("/debug/formats", methods=["POST"])
def debug_formats():
    data = request.get_json(silent=True) or {}
    video_url = (data.get("url") or "").strip()
    if not re.match(r"^https?://", video_url):
        return jsonify({"error": "Invalid URL"}), 400
    with YoutubeDL({
        "noplaylist": True,
        "ignoreconfig": True,
        "quiet": True,
        "skip_download": True
    }) as ydl:
        info = ydl.extract_info(video_url, download=False)
    fmts = info.get("formats") or []
    # Solo devolvemos un resumen útil
    brief = [
        {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec"),
            "abr": f.get("abr"),
            "filesize": f.get("filesize") or f.get("filesize_approx")
        }
        for f in fmts
    ]
    return jsonify({"title": info.get("title"), "formats": brief})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
