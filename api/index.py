from http.server import BaseHTTPRequestHandler
import json, urllib.request, subprocess, os, tempfile

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            length   = int(self.headers.get('Content-Length', 0))
            body     = json.loads(self.rfile.read(length))
            url      = body.get('url', '').strip()
            groq_key = body.get('groq_key', '').strip()

            if not url or not groq_key:
                return self._json(400, {'error': 'url and groq_key required'})

            # ===== 1. استخراج الصوت بـ yt-dlp =====
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, 'audio.%(ext)s')
                r = subprocess.run([
                    'yt-dlp',
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', '5',       # جودة متوسطة — ملف أصغر
                    '--max-filesize', '24m',       # حد Whisper API = 25MB
                    '--no-playlist',
                    '--quiet',
                    '-o', out,
                    url
                ], capture_output=True, text=True, timeout=180)

                mp3 = os.path.join(tmp, 'audio.mp3')
                if r.returncode != 0 or not os.path.exists(mp3):
                    return self._json(500, {'error': 'yt-dlp failed', 'detail': r.stderr[:400]})

                # ===== 2. إرسال لـ Groq Whisper =====
                with open(mp3, 'rb') as f:
                    audio_bytes = f.read()

                bd = b'Bound7MA4YWxk'
                def part(name, value, ctype=None):
                    hdr = f'Content-Disposition: form-data; name="{name}"'
                    if ctype:
                        hdr += f'; filename="audio.mp3"\r\nContent-Type: {ctype}'
                    return b'--' + bd + b'\r\n' + hdr.encode() + b'\r\n\r\n'

                multipart = (
                    b'--' + bd + b'\r\n'
                    b'Content-Disposition: form-data; name="file"; filename="audio.mp3"\r\n'
                    b'Content-Type: audio/mpeg\r\n\r\n' + audio_bytes + b'\r\n'
                    b'--' + bd + b'\r\n'
                    b'Content-Disposition: form-data; name="model"\r\n\r\n'
                    b'whisper-large-v3\r\n'
                    b'--' + bd + b'\r\n'
                    b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
                    b'text\r\n'
                    b'--' + bd + b'--\r\n'
                )

                req = urllib.request.Request(
                    'https://api.groq.com/openai/v1/audio/transcriptions',
                    data=multipart,
                    headers={
                        'Authorization': f'Bearer {groq_key}',
                        'Content-Type': f'multipart/form-data; boundary={bd.decode()}'
                    }
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    transcript = resp.read().decode('utf-8')

                return self._json(200, {'transcript': transcript})

        except subprocess.TimeoutExpired:
            return self._json(504, {'error': 'timeout — video too long'})
        except Exception as e:
            return self._json(500, {'error': str(e)})

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass
