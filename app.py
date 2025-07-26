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

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

UPLOAD_FOLDER = 'uploads'
HLS_FOLDER = 'hls_output'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(HLS_FOLDER, exist_ok=True)

tasks = {}

def parse_duration(duration_str):
    """Parse FFmpeg duration string to seconds"""
    if not duration_str or duration_str == 'N/A':
        return 0

    # Handle format like "01:23:45.67"
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
            filepath
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        streams = data.get('streams', [])

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
                    if '/' in fps_str:
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

                # Extract language
                tags = stream.get('tags', {})
                if 'language' in tags:
                    audio_info['language'] = tags['language']

                # Extract title
                if 'title' in tags:
                    audio_info['title'] = tags['title']

                # Extract bitrate
                if 'bit_rate' in stream:
                    bitrate = int(stream['bit_rate'])
                    audio_info['bitrate'] = f"{bitrate/1000:.0f} kbps"

                # Format sample rate
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

                # Extract metadata from tags
                tags = stream.get('tags', {})
                if 'language' in tags:
                    subtitle_info['language'] = tags['language']

                if 'title' in tags:
                    subtitle_info['title'] = tags['title']

                # Check for forced/SDH indicators
                disposition = stream.get('disposition', {})
                subtitle_info['forced'] = disposition.get('forced', 0) == 1
                subtitle_info['hearing_impaired'] = disposition.get('hearing_impaired', 0) == 1

                subtitle_streams.append(subtitle_info)

        return {
            'video': video_streams,
            'audio': audio_streams,
            'subtitle': subtitle_streams
        }

    except Exception as e:
        print(f"Error extracting stream info: {e}")
        return None

def download_file(url, task_id):
    try:
        tasks[task_id]['status'] = 'downloading'
        tasks[task_id]['progress'] = 0

        response = requests.head(url)
        total_size = int(response.headers.get('content-length', 0))

        parsed_url = urlparse(url)
        #filename = os.path.basename(parsed_url.path)
        filename = None
        if not filename:
            filename = f"{task_id}.mkv"

        filename = secure_filename(filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)

        response = requests.get(url, stream=True)
        downloaded_size = 0

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

                    if total_size > 0:
                        progress = int((downloaded_size / total_size) * 100)
                        tasks[task_id]['progress'] = progress
                        socketio.emit('progress_update', {
                            'task_id': task_id,
                            'stage': 'downloading',
                            'progress': progress,
                            'message': f'Downloading: {progress}%'
                        })

        tasks[task_id]['downloaded_file'] = filepath
        tasks[task_id]['filename'] = filename
        tasks[task_id]['status'] = 'analyzing'
        tasks[task_id]['progress'] = 0

        # Extract stream information
        socketio.emit('progress_update', {
            'task_id': task_id,
            'stage': 'analyzing',
            'progress': 50,
            'message': 'Analyzing streams...'
        })

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
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
        socketio.emit('error', {
            'task_id': task_id,
            'message': f'Download/Analysis error: {str(e)}'
        })

def generate_master_playlist(task_id):
    hls_dir = os.path.join(HLS_FOLDER, task_id)
    master_path = os.path.join(hls_dir, 'master.m3u8')

    bandwidth = 4500000

    content = (
        '#EXTM3U\n'
        f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},SUBTITLES="subs"\n'
        'playlist.m3u8\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="default",DEFAULT=YES,AUTOSELECT=YES,URI="playlist_vtt.m3u8"\n'
    )

    with open(master_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return master_path

def generate_first_subtitle_segment(task_id):
    hls_dir = os.path.join(HLS_FOLDER, task_id)
    playlist_path = os.path.join(hls_dir, 'playlist0.vtt')

    content = (
        "WEBVTT\n\n"
        "00:00.000 --> 00:05.000\n"
        "Streaming...\n"
    )

    with open(playlist_path, 'w', encoding='utf-8') as f:
        f.write(content)

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
        ffmpeg_cmd = ['ffmpeg', '-i', input_file]

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
            '-c:s', 'webvtt',  # Convert subtitles to WebVTT for web compatibility
            '-start_number', '0',
            '-hls_time', '10',
            '-hls_list_size', '0',
            '-f', 'hls',
            playlist_file
        ])

        # Run conversion with progress monitoring
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        # Simulate progress updates (FFmpeg progress parsing can be complex)
        for i in range(101):
            time.sleep(0.1)
            tasks[task_id]['progress'] = i
            socketio.emit('progress_update', {
                'task_id': task_id,
                'stage': 'converting',
                'progress': i,
                'message': f'Converting: {i}%'
            })

            if process.poll() is not None:
                break

        stdout, stderr = process.communicate()

        if process.returncode == 0:
            # Clean up source file
            if os.path.exists(input_file):
                os.remove(input_file)

            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['hls_path'] = task_id
            tasks[task_id]['playlist_url'] = f'/hls/{task_id}/playlist.m3u8'

            socketio.emit('conversion_complete', {
                'task_id': task_id,
                'playlist_url': f'/hls/{task_id}/playlist.m3u8',
                'message': 'Conversion completed successfully!'
            })

            generate_first_subtitle_segment(task_id)
            generate_master_playlist(task_id)
        else:
            raise Exception(f"FFmpeg error: {stderr}")

    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
        socketio.emit('error', {
            'task_id': task_id,
            'message': f'Conversion error: {str(e)}'
        })

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
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

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

@app.route('/convert', methods=['POST'])
def convert_video():
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

@app.route('/status/<task_id>')
def get_status(task_id):
    if task_id not in tasks:
        return jsonify({'error': 'Task not found'}), 404

    return jsonify(tasks[task_id])

@app.route('/streams/<task_id>')
def get_streams(task_id):
    if task_id not in tasks:
        return jsonify({'error': 'Task not found'}), 404

    task = tasks[task_id]
    if 'streams' not in task:
        return jsonify({'error': 'Stream information not available'}), 404

    return jsonify(task['streams'])

@app.route('/hls/<task_id>/<path:filename>')
def serve_hls(task_id, filename):
    hls_dir = os.path.join(HLS_FOLDER, task_id)
    return send_from_directory(hls_dir, filename)

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

if __name__ == '__main__':
    print("Starting Enhanced Flask Video Converter...")
    print("Required dependencies:")
    print("- pip install flask flask-socketio requests")
    print("- ffmpeg and ffprobe (available in PATH)")
    print("\nFeatures:")
    print("- Stream analysis and selection")
    print("- Video, audio, and subtitle stream detection")
    print("- WebVTT subtitle conversion")
    print("- Real-time progress updates")

    socketio.run(app, debug=True, host='0.0.0.0', port=4500)