from http.server import BaseHTTPRequestHandler
import json, urllib.request, urllib.error, urllib.parse, os, tempfile, re, io

# youtube_transcript_api متاحة على Vercel عبر requirements.txt
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
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))

            url       = body.get('url', '').strip()
            groq_key  = body.get('groq_key', '').strip()
            action    = body.get('action', 'summary')

            if not url or not groq_key:
                return self._json(400, {'error': 'يرجى إدخال الرابط ومفتاح Groq'})

            video_id = self._extract_id(url)
            if not video_id:
                return self._json(400, {'error': 'رابط يوتيوب غير صالح'})

            # 1. بيانات الفيديو — بدون yt-dlp (نستخدم YouTube oEmbed API مجاناً)
            metadata = self._get_metadata_oembed(video_id)

            # 2. استخراج النص (Transcript API → Whisper)
            transcript_text = self._get_full_text(url, video_id, groq_key)

            if not transcript_text:
                return self._json(400, {'error': 'تعذّر استخراج نص من هذا الفيديو. تأكد أن الفيديو يحتوي على ترجمة أو أن مفتاح Groq صحيح.'})

            # 3. معالجة عبر Groq Llama 3.3
            final_result = self._process_with_llama(transcript_text, metadata, action, groq_key)

            return self._json(200, {'result': final_result, 'metadata': metadata})

        except Exception as e:
            return self._json(500, {'error': str(e)})

    # ─────────────────────────────────────────────
    # مساعدات
    # ─────────────────────────────────────────────

    def _extract_id(self, url):
        patterns = [
            r'(?:v=|youtu\.be/|embed/|shorts/|live/)([0-9A-Za-z_-]{11})',
        ]
        for p in patterns:
            m = re.search(p, url)
            if m:
                return m.group(1)
        return None

    def _get_metadata_oembed(self, video_id):
        """جلب عنوان القناة واسم الفيديو عبر YouTube oEmbed — لا يحتاج API key"""
        try:
            oembed_url = f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json'
            with urllib.request.urlopen(oembed_url, timeout=8) as r:
                data = json.loads(r.read().decode('utf-8'))
            return {
                'title':         data.get('title', 'فيديو يوتيوب'),
                'channel':       data.get('author_name', 'غير معروف'),
                'description':   '',
                'pinned_comment': ''
            }
        except:
            return {'title': 'فيديو يوتيوب', 'channel': 'غير معروف', 'description': '', 'pinned_comment': ''}

    def _get_full_text(self, url, video_id, groq_key):
        """محاولة YouTubeTranscriptApi أولاً ثم Whisper"""
        # ── محاولة 1: Transcript API ──
        if HAS_TRANSCRIPT_API:
            try:
                ts = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'ar-SA', 'en'])
                text = " ".join(i['text'] for i in ts)
                if text.strip():
                    return text
            except:
                pass

        # ── محاولة 2: Whisper عبر Groq ──
        return self._whisper_fallback(video_id, groq_key)

    def _whisper_fallback(self, video_id, groq_key):
        """
        تنزيل الصوت بدون yt-dlp:
        نستخدم cobalt.tools API (مجاني، بدون مفتاح) لجلب رابط الصوت المباشر
        ثم نرسله لـ Groq Whisper.
        """
        try:
            audio_url = self._get_audio_url_cobalt(video_id)
            if not audio_url:
                return None

            # تنزيل الصوت في الذاكرة (أقصى 25 MB لـ Whisper)
            audio_bytes = self._download_bytes(audio_url, max_mb=24)
            if not audio_bytes:
                return None

            # إرسال لـ Groq Whisper
            return self._transcribe_with_groq(audio_bytes, groq_key)

        except Exception as e:
            return None

    def _get_audio_url_cobalt(self, video_id):
        """Cobalt.tools — واجهة مفتوحة لجلب روابط الصوت من يوتيوب"""
        try:
            yt_url = f'https://www.youtube.com/watch?v={video_id}'
            payload = json.dumps({
                'url': yt_url,
                'downloadMode': 'audio',
                'audioFormat': 'mp3',
                'audioBitrate': '64'
            }).encode('utf-8')

            req = urllib.request.Request(
                'https://api.cobalt.tools/',
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'User-Agent': 'Mozilla/5.0'
                }
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode('utf-8'))

            # cobalt يعيد {"status":"stream","url":"..."} أو {"status":"redirect","url":"..."}
            status = data.get('status', '')
            if status in ('stream', 'redirect', 'tunnel'):
                return data.get('url')
            # قد يعيد {"status":"picker","picker":[...]}
            if status == 'picker':
                items = data.get('picker', [])
                if items:
                    return items[0].get('url')
        except:
            pass
        return None

    def _download_bytes(self, url, max_mb=24):
        """تنزيل بايتات مع حد أقصى للحجم"""
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                max_bytes = max_mb * 1024 * 1024
                chunks = []
                total = 0
                while True:
                    chunk = r.read(65536)  # 64 KB
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        break
                    chunks.append(chunk)
                return b''.join(chunks) if chunks else None
        except:
            return None

    def _transcribe_with_groq(self, audio_bytes, groq_key):
        """إرسال الصوت لـ Groq Whisper large-v3 وإرجاع النص"""
        # بناء multipart/form-data يدوياً
        boundary = b'----GroqWhisperBoundary7391'
        body_parts = []

        # حقل file
        body_parts.append(b'--' + boundary)
        body_parts.append(b'Content-Disposition: form-data; name="file"; filename="audio.mp3"')
        body_parts.append(b'Content-Type: audio/mpeg')
        body_parts.append(b'')
        body_parts.append(audio_bytes)

        # حقل model
        body_parts.append(b'--' + boundary)
        body_parts.append(b'Content-Disposition: form-data; name="model"')
        body_parts.append(b'')
        body_parts.append(b'whisper-large-v3-turbo')

        # حقل response_format
        body_parts.append(b'--' + boundary)
        body_parts.append(b'Content-Disposition: form-data; name="response_format"')
        body_parts.append(b'')
        body_parts.append(b'json')

        body_parts.append(b'--' + boundary + b'--')

        body = b'\r\n'.join(body_parts)

        req = urllib.request.Request(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            data=body,
            headers={
                'Authorization': f'Bearer {groq_key}',
                'Content-Type': f'multipart/form-data; boundary={boundary.decode()}'
            }
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            return result.get('text', '').strip() or None

    def _process_with_llama(self, text, meta, action, key):
        """معالجة النص عبر Groq Llama 3.3 70B"""
        prompts = {
            'summary': 'قم بتلخيص الدرس التالي بأسلوب نقاط شامل ومنظم مع عناوين فرعية واضحة:',
            'explain': 'اشرح المفاهيم والمعلومات في هذا الفيديو بأسلوب مبسط يناسب طالب الثانوية العامة، وأبرز ما يهم في الامتحانات:',
            'extract': 'استخرج المادة العلمية الكاملة: القوانين، التعريفات، المعادلات، خطوات الحل، والمعلومات الجوهرية مرتبةً بشكل منظم:',
            'mindmap': 'أنشئ خريطة ذهنية نصية منظمة بعناوين وتفرعات واضحة تغطي محتوى هذا الفيديو بالكامل:'
        }

        system_prompt = (
            f"أنت مساعد تعليمي ذكي لطالب الصف الثالث الثانوي المصري.\n"
            f"بيانات الفيديو — العنوان: «{meta['title']}»، القناة: {meta['channel']}.\n"
            f"أجب بالعربية دائماً بأسلوب منظم ومفيد."
        )

        user_content = f"{prompts.get(action, prompts['summary'])}\n\nالنص:\n{text[:14000]}"

        req_data = json.dumps({
            'model': 'llama-3.3-70b-versatile',
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user',   'content': user_content}
            ],
            'temperature': 0.4,
            'max_tokens': 4096
        }).encode('utf-8')

        req = urllib.request.Request(
            'https://api.groq.com/openai/v1/chat/completions',
            data=req_data,
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json'
            }
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            return res['choices'][0]['message']['content']

    # ─────────────────────────────────────────────
    # HTTP helpers
    # ─────────────────────────────────────────────

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
        
