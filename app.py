import os
import subprocess
import json
import re
import shutil
import traceback
from flask import Flask, request, render_template, jsonify, Response, stream_with_context

# --- Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'downloads')

# Make host and port configurable
HOST = os.environ.get('FLASK_HOST', '127.0.0.1')
PORT = int(os.environ.get('FLASK_PORT', 5000))

# --- FFmpeg Check ---
FFMPEG_PATH = shutil.which('ffmpeg')

if not os.path.exists(DOWNLOAD_FOLDER):
    try:
        os.makedirs(DOWNLOAD_FOLDER)
        print(f"Created download folder: {DOWNLOAD_FOLDER}")
    except OSError as e:
        print(f"Error creating download folder {DOWNLOAD_FOLDER}: {e}")
        print("WARNING: Download folder could not be created. Downloads may fail.")

# --- Sanitize Filename Function ---
def sanitize_filename(filename):
    if not filename:
        return "downloaded_file"
    sanitized = re.sub(r'[\\/*?:"<>|]', "", filename)
    sanitized = re.sub(r'\s+', '_', sanitized)
    max_len = 200
    if len(sanitized) > max_len:
        name_part, ext_part = os.path.splitext(sanitized)
        sanitized = name_part[:max_len - len(ext_part) -1] + ext_part
    if not sanitized:
        sanitized = "downloaded_file"
    return sanitized

# ---------------------

app = Flask(__name__)

# --- Helper function to check if a string is likely a YouTube URL ---
# Corrected regex using raw string to fix SyntaxWarning
def is_youtube_url(url):
    youtube_regex = re.compile(
        r'(https?://)?(www\.)?'
        r'(youtube|youtu|youtube-nocookie)\.(com|be)/'
        r'(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})' # Using raw string for \?
    )
    return bool(youtube_regex.match(url))


# --- Routes ---

@app.route('/')
def index():
    """Serves the main HTML page."""
    return render_template('index.html')

@app.route('/get_info', methods=['POST'])
def get_info():
    """Fetches video information and available formats using yt-dlp."""
    url = request.json.get('youtube_url')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not is_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL format."}), 400

    ffmpeg_warning = None
    if not FFMPEG_PATH:
        ffmpeg_warning = "Warning: FFmpeg not found in PATH. Downloads requiring format merging (like high-quality MP4) might fail."
        print(ffmpeg_warning)

    try:
        command = ['yt-dlp', '--dump-json', '--no-warnings', '--allow-unplayable-formats', '--no-check-certificates', url]
        print(f"Executing get_info command: {' '.join(command)}")

        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=90,
            encoding='utf-8',
            errors='replace'
        )

        video_info = json.loads(process.stdout)

        title = video_info.get("title", "video")
        formats_list = []
        best_audio_format = None

        if 'formats' in video_info:
            for f in video_info['formats']:
                if not f.get("format_id") or not f.get('url') or f.get('manifest_url') or f.get("protocol") == "mhtml":
                    continue

                ext = f.get("ext")
                if ext in ['mhtml', 'jpg', 'webp', 'json', 'txt']:
                    continue

                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")

                is_video = vcodec != 'none'
                is_audio_only = (vcodec == 'none' and acodec != 'none')

                if not is_video and not is_audio_only:
                    continue

                if is_audio_only:
                     if best_audio_format is None or (ext in ['m4a', 'webm'] and 'audio' in f.get('format', '').lower() and f.get('abr', 0) > formats_list[formats_list.index(next((item for item in formats_list if item['format_id'] == best_audio_format), {'abr': 0}) )].get('abr', 0) ):
                           best_audio_format = f.get("format_id")

                format_data = {
                    "format_id": f.get("format_id"),
                    "ext": ext,
                    "resolution": f.get("resolution"),
                    "fps": f.get("fps"),
                    "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
                    "tbr": f.get('tbr'),
                    "vbr": f.get('vbr'),
                    "acodec": acodec,
                    "vcodec": vcodec,
                    "format_note": f.get("format_note"),
                    "type": "video" if is_video else "audio",
                    "_sort_height": f.get('height') or 0,
                    "_sort_tbr": f.get('tbr') or 0,
                    "_sort_audio_br": f.get('abr') or 0
                }
                formats_list.append(format_data)

            formats_list.sort(key=lambda x: (
                0 if x['type'] == 'video' else 1,
                -x['_sort_height'],
                -x['_sort_tbr'] if x['type'] == 'video' else -x['_sort_audio_br']
            ))

            highest_abr = 0
            for f in formats_list:
                if f['type'] == 'audio' and f['_sort_audio_br'] > highest_abr:
                    best_audio_format = f['format_id']
                    highest_abr = f['_sort_audio_br']


        has_high_res_video = any(f['type'] == 'video' and (f['_sort_height'] >= 720 or f.get('vbr', 0) > 1000) for f in formats_list)
        has_audio_only = any(f['type'] == 'audio' for f in formats_list)
        if not FFMPEG_PATH and has_high_res_video and has_audio_only:
             ffmpeg_warning = "Warning: FFmpeg not found. High-quality video downloads require merging with audio and will likely fail without FFmpeg."
             print(ffmpeg_warning)

        return jsonify({
            "title": title,
            "formats": formats_list,
            "best_audio_format": best_audio_format,
            "ffmpeg_warning": ffmpeg_warning
        }), 200

    except subprocess.CalledProcessError as e:
        print(f"!!! yt-dlp get_info error - Return Code: {e.returncode} !!!")
        print("--- STDOUT ---")
        print(e.stdout if e.stdout else "<No stdout>")
        print("--- STDERR ---")
        print(e.stderr if e.stderr else "<No stderr>")
        print("--------------")

        error_message = "Failed to get video info."
        if e.stderr:
             stderr_lower = e.stderr.lower()
             if "video unavailable" in stderr_lower or "private video" in stderr_lower:
                 error_message = "Video is unavailable or private."
             elif "confirm your age" in stderr_lower:
                 error_message = "Age-restricted video. Cannot download without login (not supported by this app)."
             elif "network error" in stderr_lower or "connection error" in stderr_lower:
                 error_message = "Network error while fetching video info."
             elif "unsupported url" in stderr_lower:
                  error_message = "Unsupported URL or video type."
             else:
                  error_message = f"Failed to get video info. Hint: {e.stderr.strip().splitlines()[0][:100]}..."

        return jsonify({"error": error_message, "ffmpeg_warning": ffmpeg_warning}), 500

    except subprocess.TimeoutExpired:
        print("!!! yt-dlp get_info command timed out !!!")
        return jsonify({"error": "Timeout getting video info. The request took too long.", "ffmpeg_warning": ffmpeg_warning}), 500

    except json.JSONDecodeError:
        print("!!! Failed to parse yt-dlp JSON output !!!")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Failed to parse video information from source. Unexpected yt-dlp output.", "ffmpeg_warning": ffmpeg_warning}), 500

    except Exception as e:
        print(f"!!! An unexpected server error occurred retrieving info: {e} !!!")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected server error occurred retrieving info.", "ffmpeg_warning": ffmpeg_warning}), 500


@app.route('/download_video', methods=['POST'])
def download_video():
    """Downloads the selected format TO THE SERVER and streams progress."""
    url = request.json.get('youtube_url')
    format_id = request.json.get('format_id')

    if not url or not format_id:
        return jsonify({"error": "URL or Format ID missing"}), 400

    if not os.path.exists(DOWNLOAD_FOLDER):
        try:
            os.makedirs(DOWNLOAD_FOLDER)
        except OSError as e:
            print(f"Error ensuring download folder exists before download: {e}")
            return jsonify({"error": f"Server configuration error: Cannot access download directory."}), 500

    output_template = os.path.join(DOWNLOAD_FOLDER, '%(title).100s [%(id)s] [%(format_id)s].%(ext)s')

    try:
        command = [
            'yt-dlp',
            '-f', format_id,
            '-o', output_template,
            '--no-warnings',
            '--no-mtime',
            '--progress',
            '--newline',
            '--no-check-certificates',
             url
        ]
        if FFMPEG_PATH:
             command.extend(['--ffmpeg-location', FFMPEG_PATH])

        print(f"Executing download command: {' '.join(command)}")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )

        def generate():
            yield json.dumps({"status": "start", "message": "Download started..."}) + "\n"
            yield json.dumps({"status": "command", "command": ' '.join(command)}) + "\n"

            try:
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        if line.startswith('[download]') or line.startswith('[ExtractAudio]') or line.startswith('[Merger]'):
                            yield json.dumps({"status": "progress", "message": line}) + "\n"
                        else:
                             yield json.dumps({"status": "info", "message": line}) + "\n"

                return_code = process.wait()

                if return_code == 0:
                    yield json.dumps({"status": "complete", "message": "Download finished successfully. Check server's downloads folder."}) + "\n"
                else:
                    remaining_output = process.stdout.read() if process.stdout else ""
                    error_output = remaining_output.strip()
                    print(f"!!! yt-dlp download failed with return code {return_code} !!!")
                    print(f"--- Remaining output/Error:\n{error_output}\n---")

                    error_message = "Download failed."
                    error_lower = error_output.lower()
                    if "ffmpeg" in error_lower and ("not found" in error_lower or "failed" in error_lower):
                         error_message = "Download failed: FFmpeg is required for this format/merge and was not found or failed."
                    elif "unable to download video data" in error_lower:
                         error_message = "Download failed: Network error or issue accessing video data."
                    elif "permission denied" in error_lower:
                         error_message = "Download failed: Permission error writing file on the server."
                    elif "disk full" in error_lower or "no space left on device" in error_lower:
                         error_message = "Download failed: Not enough disk space on the server."
                    elif "requested format not available" in error_lower or "no suitable format found" in error_lower:
                         error_message = f"Format ID '{format_id}' is not available or suitable. Refresh and try another."
                    elif error_output:
                         error_message = f"Download failed: {error_output.splitlines()[0][:150]}..."


                    yield json.dumps({"status": "error", "message": error_message}) + "\n"

            except Exception as e:
                print(f"!!! An unexpected error occurred during download streaming: {e} !!!")
                traceback.print_exc()
                yield json.dumps({"status": "error", "message": f"An unexpected server error occurred during download streaming: {e}"}) + "\n"
            finally:
                 if process.poll() is None:
                     print("Terminating hung yt-dlp process.")
                     try:
                         process.terminate()
                         process.wait(timeout=5)
                     except subprocess.TimeoutExpired:
                         print("Killing hung yt-dlp process.")
                         process.kill()


        return Response(stream_with_context(generate()), mimetype='application/json')

    except Exception as e:
        print(f"!!! An unexpected error occurred before starting download process: {e} !!!")
        traceback.print_exc()
        return jsonify({"error": f"An unexpected server error occurred starting the download: {e}"}), 500


if __name__ == '__main__':
    print(f"Starting Flask server on http://{HOST}:{PORT}")
    print(f"--- Downloads will be saved ON THE SERVER in: {DOWNLOAD_FOLDER} ---")
    if not FFMPEG_PATH:
        print("--- WARNING: FFmpeg executable not found in system PATH. ---")
        print("--- Downloads requiring format merging (e.g., high-quality MP4/WEBM) might fail. ---")
        print("--- Please install FFmpeg and ensure it's in the PATH for full functionality. ---")
    else:
        print(f"--- Found FFmpeg at: {FFMPEG_PATH} ---")

    app.run(host=HOST, port=PORT, debug=False, threaded=True)