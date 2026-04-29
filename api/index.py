from http.server import BaseHTTPRequestHandler
import json, urllib.request, urllib.error, os, tempfile, re, time

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    HAS_TRANSCRIPT_API = True
except ImportError:
    HAS_TRANSCRIPT_API = False


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            length  = int(self.headers.get('Content-Length', 0))
            body    = json.loads(self.rfile.read(length))
            url     = body.get('url', '').strip()
            groq_key= body.get('groq_key', '').strip()
            action  = body.get('action', 'summary')

            if not url or not groq_key:
                return self._json(400, {'error': 'يرجى إدخال الرابط ومفتاح Groq'})

            video_id = self._extract_id(url)
            if not video_id:
                return self._json(400, {'error': 'رابط يوتيوب غير صالح'})

            metadata = self._get_metadata(video_id)
            transcript_text, source = self._get_text(video_id, groq_key)

            if not transcript_text:
                return self._json(400, {
                    'error': 'تعذّر استخراج نص الفيديو.\nالأسباب المحتملة:\n• لا توجد ترجمة مدمجة\n• الفيديو محمي أو خاص\n• تحقق من مفتاح Groq'
                })

            result = self._process_with_llama(transcript_text, metadata, action, groq_key)
            return self._json(200, {'result': result, 'metadata': metadata, 'source': source})

        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore')
            return self._json(500, {'error': f'HTTP {e.code}: {body[:300]}'})
        except Exception as e:
            return self._json(500, {'error': str(e)})

    def _extract_id(self, url):
        m = re.search(r'(?:v=|youtu\.be/|embed/|shorts/|live/)([0-9A-Za-z_-]{11})', url)
        return m.group(1) if m else None

    def _get_metadata(self, video_id):
        try:
            oembed = f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json'
            with urllib.request.urlopen(oembed, timeout=8) as r:
                d = json.loads(r.read())
            return {'title': d.get('title','فيديو'), 'channel': d.get('author_name','غير معروف'), 'description':''}
        except:
            return {'title': 'فيديو يوتيوب', 'channel': 'غير معروف', 'description': ''}

    def _get_text(self, video_id, groq_key):
        # المسار 1: YouTube Transcript API
        if HAS_TRANSCRIPT_API:
            try:
                ts = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=['ar','ar-SA','ar-EG','en']
                )
                text = ' '.join(i['text'] for i in ts).strip()
                if len(text) > 100:
                    return text, 'transcript_api'
            except:
                pass

        # المسار 2: Cobalt.tools
        audio_bytes = self._cobalt_download(video_id)
        if audio_bytes:
            text = self._whisper(audio_bytes, groq_key)
            if text:
                return text, 'whisper_cobalt'

        # المسار 3: Invidious
        audio_bytes = self._invidious_download(video_id)
        if audio_bytes:
            text = self._whisper(audio_bytes, groq_key)
            if text:
                return text, 'whisper_invidious'

        return None, None

    def _cobalt_download(self, video_id):
        endpoints = [
            'https://api.cobalt.tools/v1/request',
            'https://api.cobalt.tools/',
        ]
        payload = json.dumps({
            'url': f'https://www.youtube.com/watch?v={video_id}',
            'downloadMode': 'audio',
            'audioFormat': 'mp3',
            'audioBitrate': '64'
        }).encode()

        for endpoint in endpoints:
            try:
                req = urllib.request.Request(
                    endpoint, data=payload,
                    headers={
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'User-Agent': 'Mozilla/5.0',
                        'Origin': 'https://cobalt.tools'
                    }
                )
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read())

                audio_url = None
                status = data.get('status','')
                if status in ('stream','redirect','tunnel'):
                    audio_url = data.get('url')
                elif status == 'picker':
                    items = data.get('picker', [])
                    if items: audio_url = items[0].get('url')

                if audio_url:
                    result = self._download_bytes(audio_url)
                    if result:
                        return result
            except:
                continue
        return None

    def _invidious_download(self, video_id):
        instances = [
            'https://invidious.privacyredirect.com',
            'https://inv.nadeko.net',
            'https://invidious.nerdvpn.de',
            'https://yt.cdaut.de',
        ]
        for base in instances:
            try:
                api_url = f'{base}/api/v1/videos/{video_id}?fields=adaptiveFormats'
                req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = json.loads(r.read())

                formats = [f for f in data.get('adaptiveFormats', []) if f.get('type','').startswith('audio/')]
                if not formats:
                    continue
                formats.sort(key=lambda x: x.get('bitrate', 9999999))
                audio_url = formats[0].get('url')
                if audio_url:
                    result = self._download_bytes(audio_url, max_mb=20)
                    if result:
                        return result
            except:
                continue
        return None

    def _download_bytes(self, url, max_mb=22):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                limit = max_mb * 1024 * 1024
                chunks, total = [], 0
                while True:
                    chunk = r.read(65536)
                    if not chunk: break
                    total += len(chunk)
                    if total > limit: break
                    chunks.append(chunk)
                data = b''.join(chunks)
                return data if len(data) > 10000 else None
        except:
            return None

    def _whisper(self, audio_bytes, groq_key):
        try:
            boundary = b'WB' + str(int(time.time())).encode()
            crlf = b'\r\n'
            parts = [
                b'--' + boundary,
                b'Content-Disposition: form-data; name="file"; filename="audio.mp3"',
                b'Content-Type: audio/mpeg', b'', audio_bytes,
                b'--' + boundary,
                b'Content-Disposition: form-data; name="model"', b'', b'whisper-large-v3-turbo',
                b'--' + boundary,
                b'Content-Disposition: form-data; name="response_format"', b'', b'json',
                b'--' + boundary,
                b'Content-Disposition: form-data; name="language"', b'', b'ar',
                b'--' + boundary + b'--',
            ]
            body = crlf.join(parts)

            req = urllib.request.Request(
                'https://api.groq.com/openai/v1/audio/transcriptions',
                data=body,
                headers={
                    'Authorization': f'Bearer {groq_key}',
                    'Content-Type': f'multipart/form-data; boundary={boundary.decode()}'
                }
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
                text = result.get('text', '').strip()
                return text if len(text) > 20 else None
        except:
            return None

    def _process_with_llama(self, text, meta, action, key):
        prompts = {
            'summary': 'لخّص هذا الدرس بشكل شامل ومنظم مع عناوين فرعية واضحة:',
            'explain': 'اشرح مفاهيم هذا الفيديو بأسلوب مبسط يناسب طالب الثانوية، وأبرز ما يهم في الامتحان:',
            'extract': 'استخرج المادة العلمية الكاملة: تعريفات، قوانين، معادلات، خطوات حل، مرتبة بشكل واضح:',
            'mindmap': 'أنشئ خريطة ذهنية نصية منظمة بعناوين رئيسية وفرعية تغطي محتوى الفيديو كاملاً:'
        }
        sys_prompt = (
            f'أنت مساعد تعليمي لطالب الثانوية العامة المصرية.\n'
            f'الفيديو: «{meta["title"]}» — القناة: {meta["channel"]}.\n'
            f'أجب بالعربية بأسلوب منظم ومفيد.'
        )
        user_content = f'{prompts.get(action, prompts["summary"])}\n\nالنص:\n{text[:14000]}'
        payload = json.dumps({
            'model': 'llama-3.3-70b-versatile',
            'messages': [
                {'role': 'system', 'content': sys_prompt},
                {'role': 'user',   'content': user_content}
            ],
            'temperature': 0.4,
            'max_tokens': 4096
        }).encode()
        req = urllib.request.Request(
            'https://api.groq.com/openai/v1/chat/completions',
            data=payload,
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            res = json.loads(resp.read())
            return res['choices'][0]['message']['content']

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

    def log_message(self, *args):
        pass
            
