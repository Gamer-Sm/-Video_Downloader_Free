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

# Extensiones de audio que aceptaremos (sin conversión FFmpeg)
AUDIO_EXTS = {"m4a", "webm", "opus", "mp4", "m4b", "mp3"}

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
    Prioriza M4A/AAC; si no hay, OPUS/WEBM; luego el resto. Elige mayor bitrate.
    """
    if not formats:
        return None
    audios = [f for f in formats if (f.get("vcodec") in (None, "none"))]
    if not audios:
        return None

    def score(f: Dict[str, Any]) -> tuple:
        ext = (f.get("ext") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        abr = f.get("abr") or 0
        if ext == "m4a" or acodec.startswith("mp4a") or "aac" in acodec:
            priority = 0
        elif ext in ("webm", "opus") or "opus" in acodec:
            priority = 1
        else:
            priority = 2
        return (priority, -abr)

    audios.sort(key=score)
    return audios[0]

def find_audio_file(safe_title: str) -> Optional[Path]:
    """Busca recursivamente archivos de audio cuyo nombre empiece con el título saneado."""
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

# Vista previa (sin descargar)
@app.route("/preview", methods=["POST"])
def preview():
    data = request.get_json(silent=True) or {}
    video_url = (data.get("url") or "").strip()
    if not re.match(r"^https?://", video_url):
        return jsonify({"error": "Invalid URL"}), 400
    try:
        with YoutubeDL({
            "noplaylist": True,
            "ignoreconfig": True,   # evita configs externas (como --write-pages)
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }) as ydl:
            info = ydl.extract_info(video_url, download=False)
        if not info:
            return jsonify({"error": "No se pudo obtener información"}), 500

        best = pick_best_audio(info.get("formats") or [])
        return jsonify({
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or video_url,
            "best_audio": {
                "format_id": best.get("format_id") if best else None,
                "ext": best.get("ext") if best else None,
                "abr": best.get("abr") if best else None,
                "acodec": best.get("acodec") if best else None,
            }
        })
    except Exception as e:
        return jsonify({"error": f"Error en preview: {str(e)}"}), 500

# Descargar (elige formato exacto y guarda sin convertir)
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
        "ignoreconfig": True,   # ignora config global de yt-dlp
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "windowsfilenames": True,
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),  # ruta absoluta
        "overwrites": True,
        "rm_cache_dir": True,
    }

    try:
        # 1) Info previa para elegir formato
        with YoutubeDL({**base_opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(video_url, download=False)
        if not info:
            return jsonify({"error": "No se pudo obtener información del video."}), 500

        title = info.get("title") or "audio"
        safe_title = sanitize_filename(title)
        best = pick_best_audio(info.get("formats") or [])
        if not best:
            return jsonify({"error": "No hay pista de audio disponible (puede requerir login/cookies)."}), 500

        fmt_id = best.get("format_id")
        ext = (best.get("ext") or "").lower() or "m4a"

        # 2) Descargar usando ese format_id exacto
        with YoutubeDL({**base_opts, "format": fmt_id}) as ydl:
            ydl.extract_info(video_url, download=True)

        # 3) Localizar archivo final (exacto o por búsqueda)
        exact = DOWNLOAD_DIR / f"{safe_title}.{ext}"
        final_path = exact if exact.exists() else find_audio_file(safe_title)

        # Limpieza de posibles .mhtml
        mhtml = DOWNLOAD_DIR / f"{safe_title}.mhtml"
        if mhtml.exists():
            try:
                mhtml.unlink()
            except Exception:
                pass

        if not final_path or not final_path.exists():
            return jsonify({"error": "No se generó el archivo de audio."}), 500

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
