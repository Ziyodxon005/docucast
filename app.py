import os
import json
import uuid
import time
import logging
import threading
import hashlib
import secrets
from pathlib import Path
from collections import defaultdict
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file, Response, send_from_directory, abort
from flask_cors import CORS

# ── Load environment variables ────────────────────────────
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ── Config from .env ──────────────────────────────────────
GEMINI_API_KEY       = os.getenv('GEMINI_API_KEY', '').strip()
GCLOUD_CREDS_JSON    = os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON', '').strip()
FLASK_SECRET_KEY     = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))
MAX_MB               = int(os.getenv('MAX_CONTENT_LENGTH_MB', '100'))
RATE_LIMIT_PER_HOUR  = int(os.getenv('RATE_LIMIT_PER_HOUR', '20'))

if not GEMINI_API_KEY:
    logger.warning("⚠️  GEMINI_API_KEY .env faylida topilmadi! Iltimos .env faylini to'ldiring.")

# ── Flask App ─────────────────────────────────────────────
app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = FLASK_SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024
CORS(app, resources={r"/api/*": {"origins": ["http://127.0.0.1:5000", "http://localhost:5000"]}})

TEMP_DIR   = Path("temp")
OUTPUT_DIR = Path("output")
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# ── In-memory stores ──────────────────────────────────────
jobs = {}
rate_store = defaultdict(list)  # ip -> [timestamps]

# ── Gemini Model ──────────────────────────────────────────
GEMINI_MODEL = "models/gemini-flash-latest"  # High-limit, fast, cost-efficient

SYSTEM_PROMPT = """Sen professional podkast ssenaristisan. Senga berilgan PDF hujjatdagi ma'lumotlarni chuqur tahlil qil. Ushbu ma'lumotlar asosida 2 kishi o'rtasidagi qiziqarli va jonli suhbat ssenariysini yarat.

Personajlar:
- Boshlovchi (Shaxnoza): Qiziquvchan, savollar beradi, mavzuni soddalashtirishni so'raydi, tinglovchilarga murojaat qiladi.
- Ekspert (Mustafo): Bilimdon, PDF-dagi raqamlar va faktlarni tushunarli tilda, hayotiy misollar bilan tushuntiradi.

Talablar:
- Til: Sof o'zbek tili (jonli nutq uslubida, "siz" shaklida).
- Davomiyligi: PDF hajmidan kelib chiqib, kamida 5-10 daqiqalik suhbat (30-50 ta turn).
- Har bir gapdagi so'zlar soni: 20-60 ta oralig'ida bo'lsin.
- Format: FAQAT JSON formatida javob ber. Hech qanday ortiqcha matn, markdown, kod bloki yoki izoh yo'q. Faqat sof JSON massivi.

JSON strukturasi (faqat bu format):
[{"speaker": "Shaxnoza", "text": "..."}, {"speaker": "Mustafo", "text": "..."}]

Muhim: Birinchi element Shaxnoza bo'lishi kerak. Suhbat tabiiy va qiziqarli bo'lsin. Haqiqiy raqamlar va faktlardan foydalaning."""


# ══════════════════════════════════════════════════════════
# SECURITY — Rate Limiter
# ══════════════════════════════════════════════════════════
def get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip = get_client_ip()
        now = time.time()
        hour_ago = now - 3600
        # Clean old entries
        rate_store[ip] = [t for t in rate_store[ip] if t > hour_ago]
        if len(rate_store[ip]) >= RATE_LIMIT_PER_HOUR:
            logger.warning(f"Rate limit exceeded for IP: {ip}")
            return jsonify({"error": f"Soatiga {RATE_LIMIT_PER_HOUR} ta so'rovdan ko'p yuborildi. Iltimos kuting."}), 429
        rate_store[ip].append(now)
        return f(*args, **kwargs)
    return decorated

def validate_job_owner(job_id, token):
    """Verify the request token matches the job's token."""
    job = jobs.get(job_id)
    if not job:
        return False
    return secrets.compare_digest(job.get('token', ''), token or '')


# ══════════════════════════════════════════════════════════
# CORE PIPELINE
# ══════════════════════════════════════════════════════════
def update_job(job_id, **kwargs):
    if job_id in jobs:
        jobs[job_id].update(kwargs)


def generate_podcast_task(job_id, pdf_path):
    try:
        import google.generativeai as genai

        if not GEMINI_API_KEY:
            raise Exception(".env faylida GEMINI_API_KEY topilmadi. Iltimos sozlang.")

        genai.configure(api_key=GEMINI_API_KEY)

        # Step 1: Upload PDF
        update_job(job_id, status='uploading', progress=5, message='PDF Gemini-ga yuklanmoqda...')
        logger.info(f"[{job_id}] Uploading PDF...")
        uploaded_file = genai.upload_file(path=str(pdf_path), mime_type="application/pdf")

        max_wait, waited = 120, 0
        while uploaded_file.state.name == "PROCESSING" and waited < max_wait:
            time.sleep(2); waited += 2
            uploaded_file = genai.get_file(uploaded_file.name)

        if uploaded_file.state.name != "ACTIVE":
            raise Exception(f"Fayl qayta ishlashda xato: {uploaded_file.state.name}")

        update_job(job_id, progress=15, message='PDF yuklandi. Ssenariy yaratilmoqda...')

        # Step 2: Generate script
        update_job(job_id, status='generating', progress=22, message='DocuCast AI ssenariyni yozmoqda...')
        logger.info(f"[{job_id}] Generating script with {GEMINI_MODEL}...")

        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT
        )
        response = model.generate_content([
            uploaded_file,
            "Ushbu PDF hujjatni chuqur o'qib, yuqoridagi talablarga asosan JSON formatida podkast ssenariysi yarat. Faqat sof JSON massivi qaytargin, boshqa hech narsa yo'q."
        ])

        raw_text = response.text.strip()
        # Strip markdown fences if present
        if raw_text.startswith("```"):
            lines = raw_text.split('\n')
            raw_text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
        raw_text = raw_text.strip()

        # Try to find JSON array even if there's extra text
        start = raw_text.find('[')
        end = raw_text.rfind(']')
        if start != -1 and end != -1:
            raw_text = raw_text[start:end+1]

        script = json.loads(raw_text)
        if not isinstance(script, list) or len(script) == 0:
            raise Exception("AI noto'g'ri format qaytardi")

        update_job(job_id, progress=36, message=f'Ssenariy tayyor! {len(script)} ta dialog. Ovozlashtirish boshlanmoqda...')
        logger.info(f"[{job_id}] Script: {len(script)} turns")

        # Step 3: TTS
        update_job(job_id, status='synthesizing', progress=40, message='Audio ovozlashtirish boshlanmoqda...')
        audio_paths = synthesize_all_turns(job_id, script)

        # Step 4: Merge
        update_job(job_id, status='merging', progress=92, message='Audio fayllar birlashtirilmoqda...')

        output_path = OUTPUT_DIR / f"{job_id}.mp3"
        merge_audio_files(audio_paths, str(output_path))

        # Cleanup temp chunks
        job_temp = TEMP_DIR / job_id
        if job_temp.exists():
            for f in job_temp.glob("*.mp3"):
                f.unlink(missing_ok=True)
            try:
                job_temp.rmdir()
            except Exception:
                pass

        file_size = output_path.stat().st_size
        # Estimate duration: MP3 at ~32kbps average for speech
        est_duration = file_size / (32000 / 8)

        update_job(job_id, status='done', progress=100,
                   message='Podkast tayyor! Yuklab olishingiz mumkin.',
                   output_file=str(output_path),
                   duration=est_duration)
        logger.info(f"[{job_id}] Done! Duration: ~{est_duration:.1f}s")

    except json.JSONDecodeError as e:
        logger.error(f"[{job_id}] JSON error: {e}")
        update_job(job_id, status='error', message=f'Ssenariy formatida xato: {str(e)}')
    except Exception as e:
        logger.error(f"[{job_id}] Error: {e}", exc_info=True)
        update_job(job_id, status='error', message=str(e))
    finally:
        if pdf_path.exists():
            pdf_path.unlink(missing_ok=True)


def synthesize_all_turns(job_id, script):
    """Returns list of MP3 file paths (no pydub/ffmpeg needed)."""
    paths = []
    total = len(script)
    job_temp = TEMP_DIR / job_id
    job_temp.mkdir(exist_ok=True)

    for i, turn in enumerate(script):
        speaker = turn.get('speaker', 'Shaxnoza')
        text = turn.get('text', '').strip()
        if not text:
            continue

        progress = 40 + int((i / total) * 50)
        update_job(job_id, progress=progress,
                   message=f'Ovozlashtirish: {i+1}/{total} — {speaker} gapirmoqda...')

        seg_path = str(job_temp / f"turn_{i:04d}.mp3")
        try:
            if GCLOUD_CREDS_JSON and GCLOUD_CREDS_JSON.strip():
                synthesize_google_tts(text, speaker, seg_path)
            else:
                synthesize_edge_tts(text, speaker, seg_path)

            if os.path.exists(seg_path) and os.path.getsize(seg_path) > 100:
                paths.append(seg_path)
            else:
                logger.warning(f"[{job_id}] Turn {i}: empty file, skipping")
        except Exception as e:
            logger.warning(f"[{job_id}] Turn {i} TTS xato: {e}")

    return paths


def synthesize_google_tts(text, speaker, output_path):
    """Google Cloud TTS — writes raw MP3 bytes directly, no ffmpeg needed."""
    import tempfile
    from google.cloud import texttospeech
    from google.oauth2 import service_account

    creds_data = json.loads(GCLOUD_CREDS_JSON)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(creds_data, f)
        creds_path = f.name

    credentials = service_account.Credentials.from_service_account_file(creds_path)
    client = texttospeech.TextToSpeechClient(credentials=credentials)
    os.unlink(creds_path)

    # Shaxnoza=MadinaNeural (ayol), Mustafo=SardorNeural (erkak)
    voice_name = "uz-UZ-MadinaNeural" if speaker == "Shaxnoza" else "uz-UZ-SardorNeural"
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(language_code="uz-UZ", name=voice_name)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=0.95,
        pitch=2.0 if speaker == "Shaxnoza" else -2.0
    )
    response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    # Write raw MP3 bytes — no pydub needed
    with open(output_path, 'wb') as f:
        f.write(response.audio_content)


def synthesize_edge_tts(text, speaker, output_path):
    """Microsoft Edge TTS — free, no API key, native Uzbek neural voices.
    Shaxnoza (Boshlovchi): uz-UZ-MadinaNeural (ayol ovoz)
    Mustafo  (Ekspert):    uz-UZ-SardorNeural (erkak ovoz)
    """
    import asyncio
    import edge_tts

    VOICES = {
        'Shaxnoza': 'uz-UZ-MadinaNeural',
        'Mustafo':  'uz-UZ-SardorNeural',
    }
    voice = VOICES.get(speaker, 'uz-UZ-MadinaNeural')

    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output_path)

    # Thread-safe: create a fresh event loop for each call
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
        raise Exception(f"edge-tts bo'sh fayl qaytardi: {output_path}")


def merge_audio_files(paths, output_path):
    """Merge MP3 files by raw byte concatenation — no ffmpeg needed!"""
    with open(output_path, 'wb') as out:
        for i, path in enumerate(paths):
            if not os.path.exists(path) or os.path.getsize(path) < 100:
                continue
            with open(path, 'rb') as f:
                out.write(f.read())
    logger.info(f"Merged {len(paths)} segments → {output_path} ({os.path.getsize(output_path):,} bytes)")


# ══════════════════════════════════════════════════════════
# SECURITY HEADERS MIDDLEWARE
# ══════════════════════════════════════════════════════════
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/') else 'public, max-age=3600'
    return response


# ══════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:filename>')
def static_files(filename):
    if filename.startswith('api/') or filename == '.env':
        abort(403)
    safe_exts = {'.css', '.js', '.ico', '.png', '.jpg', '.svg', '.woff', '.woff2'}
    if Path(filename).suffix not in safe_exts:
        abort(403)
    return send_from_directory('.', filename)


@app.route('/api/health')
def health():
    return jsonify({
        "status": "ok",
        "version": "2.0.0",
        "model": GEMINI_MODEL,
        "tts": "google-cloud" if GCLOUD_CREDS_JSON else "edge-tts (uz-UZ Neural)",
        "api_configured": bool(GEMINI_API_KEY)
    })


@app.route('/api/generate', methods=['POST'])
@rate_limit
def generate():
    # Validate PDF
    if 'pdf' not in request.files:
        return jsonify({"error": "PDF fayl topilmadi"}), 400
    pdf_file = request.files['pdf']
    if not pdf_file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "Faqat PDF formatidagi fayllar qabul qilinadi"}), 400

    # Content-type check
    mime = pdf_file.mimetype or ''
    if mime and 'pdf' not in mime and mime != 'application/octet-stream':
        return jsonify({"error": "Noto'g'ri fayl turi"}), 400

    if not GEMINI_API_KEY:
        return jsonify({"error": "Server sozlanmagan. Admin .env faylini to'ldirishi kerak."}), 503

    # Save PDF
    job_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)  # per-job download token
    pdf_path = TEMP_DIR / f"{job_id}.pdf"
    pdf_file.save(str(pdf_path))

    # Verify it's actually a PDF (magic bytes)
    with open(str(pdf_path), 'rb') as f:
        header = f.read(5)
    if header != b'%PDF-':
        pdf_path.unlink(missing_ok=True)
        return jsonify({"error": "Fayl haqiqiy PDF emas"}), 400

    jobs[job_id] = {
        "status": "queued", "progress": 0,
        "message": "Navbatga qo'shildi...",
        "output_file": None, "token": token,
        "created_at": time.time()
    }

    thread = threading.Thread(
        target=generate_podcast_task,
        args=(job_id, pdf_path),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "token": token})


@app.route('/api/status/<job_id>')
def status(job_id):
    if not jobs.get(job_id):
        return jsonify({"error": "Job topilmadi"}), 404

    def event_stream():
        timeout, start = 600, time.time()
        last_p = -1
        while time.time() - start < timeout:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job topilmadi'})}\n\n"
                break
            if job['progress'] != last_p or job['status'] in ('done', 'error'):
                last_p = job['progress']
                safe = {k: v for k, v in job.items() if k != 'token'}  # never send token
                yield f"data: {json.dumps(safe)}\n\n"
            if job['status'] in ('done', 'error'):
                break
            time.sleep(0.8)

    return Response(event_stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/download/<job_id>')
def download(job_id):
    token = request.args.get('token', '')
    if not validate_job_owner(job_id, token):
        return jsonify({"error": "Ruxsat yo'q"}), 403

    job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({"error": "Fayl tayyor emas"}), 404

    output_path = Path(job['output_file'])
    if not output_path.exists():
        return jsonify({"error": "Fayl topilmadi"}), 404

    return send_file(str(output_path), as_attachment=True,
                     download_name="docucast_podcast.mp3", mimetype="audio/mpeg")


@app.route('/api/stream/<job_id>')
def stream_audio(job_id):
    token = request.args.get('token', '')
    if not validate_job_owner(job_id, token):
        return jsonify({"error": "Ruxsat yo'q"}), 403

    job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({"error": "Fayl tayyor emas"}), 404

    output_path = Path(job['output_file'])
    if not output_path.exists():
        return jsonify({"error": "Fayl topilmadi"}), 404

    return send_file(str(output_path), mimetype="audio/mpeg")


# ── Cleanup old jobs (run periodically in background) ────
def cleanup_old_jobs():
    while True:
        time.sleep(3600)
        now = time.time()
        to_delete = [jid for jid, j in list(jobs.items()) if now - j.get('created_at', now) > 7200]
        for jid in to_delete:
            job = jobs.pop(jid, {})
            if job.get('output_file'):
                Path(job['output_file']).unlink(missing_ok=True)
        if to_delete:
            logger.info(f"Cleanup: {len(to_delete)} eski job o'chirildi")


if __name__ == '__main__':
    threading.Thread(target=cleanup_old_jobs, daemon=True).start()
    logger.info(f"🎙️  DocuCast v2.0 — http://127.0.0.1:5000")
    logger.info(f"📦  Model: {GEMINI_MODEL}")
    logger.info(f"🔑  Gemini API: {'✅ sozlangan' if GEMINI_API_KEY else '❌ SOZLANMAGAN (.env tekshiring)'}")
    logger.info(f"🔊  TTS: {'Google Cloud' if GCLOUD_CREDS_JSON else 'gTTS (bepul)'}")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
