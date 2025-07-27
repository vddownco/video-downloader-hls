from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for
from flask_socketio import SocketIO, emit
import os
import requests
import subprocess
import threading
import uuid
import time
import json
import re
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
import logging
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'

# Improved SocketIO configuration
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
    async_mode='threading'
)

UPLOAD_FOLDER = 'uploads'
HLS_FOLDER = 'hls_output'
CHUNK_SIZE = 8192 * 8  # Increased chunk size for better performance

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(HLS_FOLDER, exist_ok=True)

tasks = {}
active_connections = set()

# Progress update throttling
progress_cache = {}
PROGRESS_UPDATE_INTERVAL = 1.0  # Minimum seconds between progress updates

def throttled_progress_update(task_id, stage, progress, message):
    """Only send progress updates if enough time has passed"""
    current_time = time.time()
    cache_key = f"{task_id}_{stage}"

    # Check if we should send this update
    if cache_key in progress_cache:
        last_update_time, last_progress = progress_cache[cache_key]

        # Skip update if less than interval passed and progress change is small
        if (current_time - last_update_time < PROGRESS_UPDATE_INTERVAL and
                abs(progress - last_progress) < 5):
            return

    # Update cache and send progress
    progress_cache[cache_key] = (current_time, progress)

    socketio.emit('progress_update', {
        'task_id': task_id,
        'stage': stage,
        'progress': progress,
        'message': message
    })

def cleanup_old_tasks():
    """Clean up old completed/failed tasks to prevent memory leaks"""
    current_time = time.time()
    tasks_to_remove = []

    for task_id, task in tasks.items():
        # Remove tasks older than 24 hours
        if current_time - task.get('created_at', 0) > 86400:
            tasks_to_remove.append(task_id)

            # Clean up files
            if 'downloaded_file' in task and os.path.exists(task['downloaded_file']):
                try:
                    os.remove(task['downloaded_file'])
                except:
                    pass

            hls_dir = os.path.join(HLS_FOLDER, task_id)
            if os.path.exists(hls_dir):
                try:
                    import shutil
                    shutil.rmtree(hls_dir)
                except:
                    pass

    for task_id in tasks_to_remove:
        del tasks[task_id]
        if task_id in progress_cache:
            del progress_cache[task_id]

def parse_duration(duration_str):
    """Parse FFmpeg duration string to seconds"""
    if not duration_str or duration_str == 'N/A':
        return 0

    parts = duration_str.split(':')
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return 0

def extract_stream_info(filepath):
    """Extract detailed stream information using ffprobe"""
    try:
        cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-show_format',
            filepath
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"ffprobe failed: {result.stderr}")
            return None

        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        format_info = data.get('format', {})

        video_streams = []
        audio_streams = []
        subtitle_streams = []

        for stream in streams:
            codec_type = stream.get('codec_type')
            index = stream.get('index')

            if codec_type == 'video':
                video_info = {
                    'index': index,
                    'codec': stream.get('codec_name', 'unknown'),
                    'width': stream.get('width'),
                    'height': stream.get('height'),
                    'fps': None,
                    'bitrate': None
                }

                # Extract frame rate
                if 'r_frame_rate' in stream:
                    fps_str = stream['r_frame_rate']
                    if '/' in fps_str and fps_str != '0/0':
                        num, den = fps_str.split('/')
                        if int(den) != 0:
                            video_info['fps'] = f"{int(num)/int(den):.2f}"

                # Extract bitrate
                if 'bit_rate' in stream:
                    bitrate = int(stream['bit_rate'])
                    if bitrate > 1000000:
                        video_info['bitrate'] = f"{bitrate/1000000:.1f} Mbps"
                    else:
                        video_info['bitrate'] = f"{bitrate/1000:.0f} kbps"

                video_streams.append(video_info)

            elif codec_type == 'audio':
                audio_info = {
                    'index': index,
                    'codec': stream.get('codec_name', 'unknown'),
                    'language': None,
                    'title': None,
                    'channels': stream.get('channels'),
                    'sample_rate': stream.get('sample_rate'),
                    'bitrate': None
                }

                tags = stream.get('tags', {})
                if 'language' in tags:
                    audio_info['language'] = tags['language']

                if 'title' in tags:
                    audio_info['title'] = tags['title']

                if 'bit_rate' in stream:
                    bitrate = int(stream['bit_rate'])
                    audio_info['bitrate'] = f"{bitrate/1000:.0f} kbps"

                if audio_info['sample_rate']:
                    audio_info['sample_rate'] = f"{int(audio_info['sample_rate'])/1000:.1f}k"

                audio_streams.append(audio_info)

            elif codec_type == 'subtitle':
                subtitle_info = {
                    'index': index,
                    'codec': stream.get('codec_name', 'unknown'),
                    'language': None,
                    'title': None,
                    'forced': False,
                    'hearing_impaired': False
                }

                tags = stream.get('tags', {})
                if 'language' in tags:
                    subtitle_info['language'] = tags['language']

                if 'title' in tags:
                    subtitle_info['title'] = tags['title']

                disposition = stream.get('disposition', {})
                subtitle_info['forced'] = disposition.get('forced', 0) == 1
                subtitle_info['hearing_impaired'] = disposition.get('hearing_impaired', 0) == 1

                subtitle_streams.append(subtitle_info)

        return {
            'video': video_streams,
            'audio': audio_streams,
            'subtitle': subtitle_streams,
            'duration': format_info.get('duration')
        }

    except subprocess.TimeoutExpired:
        logger.error("ffprobe timeout")
        return None
    except Exception as e:
        logger.error(f"Error extracting stream info: {e}")
        return None

def download_file(url, task_id):
    try:
        if task_id not in tasks:
            return

        tasks[task_id]['status'] = 'downloading'
        tasks[task_id]['progress'] = 0

        # Get file size with timeout
        try:
            response = requests.head(url, timeout=10)
            total_size = int(response.headers.get('content-length', 0))
        except:
            total_size = 0

        parsed_url = urlparse(url)
        filename = f"{task_id}.mkv"
        filename = secure_filename(filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)

        # Download with improved error handling and progress
        try:
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise Exception(f"Failed to download: {str(e)}")

        downloaded_size = 0
        last_progress_time = time.time()

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

                    # Throttled progress updates
                    current_time = time.time()
                    if current_time - last_progress_time >= 1.0:  # Update every second
                        if total_size > 0:
                            progress = min(int((downloaded_size / total_size) * 100), 99)
                            throttled_progress_update(task_id, 'downloading', progress,
                                                      f'Downloaded: {downloaded_size // (1024*1024)} MB')
                        last_progress_time = current_time

        tasks[task_id]['downloaded_file'] = filepath
        tasks[task_id]['filename'] = filename
        tasks[task_id]['status'] = 'analyzing'
        tasks[task_id]['progress'] = 0

        # Extract stream information
        throttled_progress_update(task_id, 'analyzing', 50, 'Analyzing streams...')

        stream_info = extract_stream_info(filepath)

        if stream_info:
            tasks[task_id]['streams'] = stream_info
            tasks[task_id]['status'] = 'ready_for_conversion'

            socketio.emit('download_complete', {
                'task_id': task_id,
                'streams': stream_info,
                'message': 'Download complete. Please select streams to include.'
            })
        else:
            raise Exception('Failed to analyze video streams')

    except Exception as e:
        logger.error(f"Download error for task {task_id}: {e}")
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
        socketio.emit('error', {
            'task_id': task_id,
            'message': f'Download/Analysis error: {str(e)}'
        })

def parse_ffmpeg_progress(line, duration_seconds):
    """Parse FFmpeg progress from stderr output"""
    # Look for time= pattern
    time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2}\.\d{2})', line)
    if time_match:
        hours, minutes, seconds = time_match.groups()
        current_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

        if duration_seconds > 0:
            progress = min(int((current_seconds / duration_seconds) * 100), 99)
            return progress
    return None

def convert_to_hls(task_id, selected_streams, total_stream_counts):
    try:
        if task_id not in tasks:
            raise Exception('Task not found')

        task = tasks[task_id]
        input_file = task.get('downloaded_file')

        if not input_file or not os.path.exists(input_file):
            raise Exception('Downloaded file not found')

        tasks[task_id]['status'] = 'converting'
        tasks[task_id]['progress'] = 0

        hls_dir = os.path.join(HLS_FOLDER, task_id)
        os.makedirs(hls_dir, exist_ok=True)

        playlist_file = os.path.join(hls_dir, 'playlist.m3u8')

        # Build FFmpeg command with selected streams
        ffmpeg_cmd = ['ffmpeg', '-y', '-i', input_file, '-progress', 'pipe:2']

        # Add stream mappings based on selection
        map_args = []
        video_stream_count = total_stream_counts.get('video', 0)
        audio_stream_count = total_stream_counts.get('audio', 0)

        # Video streams
        for video_index in selected_streams.get('video', []):
            map_args.extend(['-map', f'0:v:{video_index}'])

        # Audio streams
        for audio_index in selected_streams.get('audio', []):
            map_args.extend(['-map', f'0:a:{audio_index-video_stream_count}'])

        # Subtitle streams
        for subtitle_index in selected_streams.get('subtitle', []):
            map_args.extend(['-map', f'0:s:{subtitle_index-video_stream_count-audio_stream_count}'])

        # If no streams selected, use defaults
        if not map_args:
            map_args = ['-map', '0:v', '-map', '0:a?']

        ffmpeg_cmd.extend(map_args)

        # Add encoding options
        ffmpeg_cmd.extend([
            '-c:v', 'copy',
            '-c:a', 'copy',
            '-c:s', 'webvtt',
            '-start_number', '0',
            '-hls_time', '10',
            '-hls_list_size', '0',
            '-hls_flags', 'delete_segments',
            '-f', 'hls',
            playlist_file
        ])

        # Get video duration for progress calculation
        duration_seconds = 0
        if 'streams' in task and 'duration' in task['streams']:
            try:
                duration_seconds = float(task['streams']['duration'])
            except:
                pass

        # Run conversion with real progress monitoring
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1
        )

        last_progress_time = time.time()

        # Monitor progress from stderr
        while True:
            line = process.stderr.readline()
            if not line:
                break

            # Parse progress
            if duration_seconds > 0:
                progress = parse_ffmpeg_progress(line, duration_seconds)
                if progress is not None:
                    current_time = time.time()
                    if current_time - last_progress_time >= 1.0:  # Update every second
                        throttled_progress_update(task_id, 'converting', progress,
                                                  f'Converting: {progress}%')
                        last_progress_time = current_time

        process.wait()

        if process.returncode == 0:
            # Clean up source file
            if os.path.exists(input_file):
                os.remove(input_file)

            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['hls_path'] = task_id
            tasks[task_id]['playlist_url'] = f'/hls/{task_id}/playlist.m3u8'

            generate_master_playlist(task_id)

            socketio.emit('conversion_complete', {
                'task_id': task_id,
                'playlist_url': f'/hls/{task_id}/playlist.m3u8',
                'message': 'Conversion completed successfully!'
            })

        else:
            # Get error output
            _, stderr = process.communicate()
            raise Exception(f"FFmpeg error: {stderr}")

    except Exception as e:
        logger.error(f"Conversion error for task {task_id}: {e}")
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
        socketio.emit('error', {
            'task_id': task_id,
            'message': f'Conversion error: {str(e)}'
        })

def generate_master_playlist(task_id):
    hls_dir = os.path.join(HLS_FOLDER, task_id)
    master_path = os.path.join(hls_dir, 'master.m3u8')

    bandwidth = 4500000

    content = (
        '#EXTM3U\n'
        f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}\n'
        'playlist.m3u8\n'
    )

    with open(master_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return master_path

# Routes remain the same but with better error handling
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/player/<task_id>')
def player(task_id):
    if task_id not in tasks or tasks[task_id]['status'] != 'completed':
        return redirect(url_for('index'))

    playlist_url = f'/hls/{task_id}/master.m3u8'
    return render_template('player.html', playlist_url=playlist_url, task_id=task_id)

@app.route('/download', methods=['POST'])
def download_video():
    try:
        data = request.json
        url = data.get('url')

        if not url:
            return jsonify({'error': 'No URL provided'}), 400

        # Clean up old tasks before creating new one
        cleanup_old_tasks()

        task_id = str(uuid.uuid4())

        tasks[task_id] = {
            'id': task_id,
            'url': url,
            'status': 'pending',
            'progress': 0,
            'created_at': time.time()
        }

        thread = threading.Thread(target=download_file, args=(url, task_id))
        thread.daemon = True
        thread.start()

        return jsonify({'task_id': task_id})

    except Exception as e:
        logger.error(f"Download endpoint error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/convert', methods=['POST'])
def convert_video():
    try:
        data = request.json
        task_id = data.get('task_id')
        selected_streams = data.get('selected_streams', {})
        total_stream_counts = data.get('total_stream_counts', {})

        if not task_id:
            return jsonify({'error': 'No task ID provided'}), 400

        if task_id not in tasks:
            return jsonify({'error': 'Task not found'}), 404

        if tasks[task_id]['status'] != 'ready_for_conversion':
            return jsonify({'error': 'Task not ready for conversion'}), 400

        thread = threading.Thread(target=convert_to_hls, args=(task_id, selected_streams, total_stream_counts))
        thread.daemon = True
        thread.start()

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Convert endpoint error: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/status/<task_id>')
def get_status(task_id):
    if task_id not in tasks:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(tasks[task_id])

@app.route('/hls/<task_id>/<path:filename>')
def serve_hls(task_id, filename):
    hls_dir = os.path.join(HLS_FOLDER, task_id)
    response = send_from_directory(hls_dir, filename)

    # Add CORS headers for HLS
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET'
    response.headers['Access-Control-Allow-Headers'] = 'Range'

    return response

# Improved WebSocket handlers
@socketio.on('connect')
def handle_connect():
    active_connections.add(request.sid)
    logger.info(f'Client connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    active_connections.discard(request.sid)
    logger.info(f'Client disconnected: {request.sid}')

@socketio.on('ping')
def handle_ping():
    emit('pong')

if __name__ == '__main__':
    print("Starting Enhanced Flask Video Converter...")
    print("Improvements:")
    print("- Throttled progress updates to prevent UI lag")
    print("- Better memory management and cleanup")
    print("- Improved WebSocket stability")
    print("- Real FFmpeg progress parsing")
    print("- Better error handling and logging")

    # Run cleanup periodically
    import atexit
    atexit.register(cleanup_old_tasks)

    socketio.run(app, debug=False, host='0.0.0.0', port=4500)