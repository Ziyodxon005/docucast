import os
import re
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


def _clean_env_value(val):
    return (val or '').strip().strip('"').strip("'")


def load_gemini_api_keys():
    """Load all Gemini API keys from .env (GEMINI_API_KEY, GEMINI_API_KEY2..N, GEMINI_API_KEYS)."""
    keys = []
    seen = set()

    def _add(raw):
        for part in str(raw or '').split(','):
            cleaned = _clean_env_value(part)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                keys.append(cleaned)

    # Support comma-separated GEMINI_API_KEYS
    _add(os.getenv('GEMINI_API_KEYS', ''))
    # Support primary GEMINI_API_KEY
    _add(os.getenv('GEMINI_API_KEY', ''))

    # Collect numbered keys (GEMINI_API_KEY1, GEMINI_API_KEY_1, GEMINI_API_KEY2, ...)
    numbered = []
    for env_name, env_val in os.environ.items():
        m = re.match(r'^GEMINI_API_KEY_?(\d+)$', env_name)
        if m:
            numbered.append((int(m.group(1)), env_name, env_val))
    for _, _, env_val in sorted(numbered, key=lambda x: x[0]):
        _add(env_val)

    return keys


class GeminiKeyPool:
    """Thread-safe Gemini API key pool with automatic quota/error failover."""

    def __init__(self, keys):
        self._keys = list(keys)
        self._lock = threading.Lock()
        self._current_idx = 0
        self._cooldown_until = {}

    @property
    def total(self):
        return len(self._keys)

    def get_key(self, idx):
        return self._keys[idx]

    @staticmethod
    def mask_key(key):
        if len(key) <= 8:
            return '***'
        return f"...{key[-4:]}"

    def ordered_indices(self):
        """Return key indices to try, starting from current active key and skipping cooled-down keys first."""
        with self._lock:
            n = len(self._keys)
            if n == 0:
                return []
            now = time.time()
            rotated = [(self._current_idx + offset) % n for offset in range(n)]
            ready = [i for i in rotated if self._cooldown_until.get(i, 0) <= now]
            cooling = sorted(
                [i for i in rotated if self._cooldown_until.get(i, 0) > now],
                key=lambda i: self._cooldown_until[i]
            )
            return ready + cooling

    def mark_success(self, idx):
        with self._lock:
            self._cooldown_until.pop(idx, None)
            self._current_idx = idx

    def mark_failed(self, idx, permanent=False):
        with self._lock:
            if not self._keys:
                return
            cooldown = 3600 if permanent else 120
            self._cooldown_until[idx] = time.time() + cooldown
            if self._current_idx == idx:
                self._current_idx = (idx + 1) % len(self._keys)


# ── Config from .env ──────────────────────────────────────
GEMINI_API_KEYS      = load_gemini_api_keys()
GEMINI_API_KEY       = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ''
gemini_key_pool      = GeminiKeyPool(GEMINI_API_KEYS)
GCLOUD_CREDS_JSON    = os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON', '').strip()
FLASK_SECRET_KEY     = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))
MAX_MB               = int(os.getenv('MAX_CONTENT_LENGTH_MB', '100'))
RATE_LIMIT_PER_HOUR  = int(os.getenv('RATE_LIMIT_PER_HOUR', '20'))

if not GEMINI_API_KEYS:
    logger.warning("⚠️  GEMINI_API_KEY .env faylida topilmadi! Iltimos .env faylini to'ldiring.")
else:
    logger.info(f"🔑  {len(GEMINI_API_KEYS)} ta Gemini API kalit yuklandi.")

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
GEMINI_MODEL = "gemini-3.5-flash-lite"  # Tezkor, arzon, barqaror model

SUPPORTED_LANGUAGES = {
    "uz": {
        "name": "O'zbek",
        "native": "O'zbekcha",
        "prompt_name": "Uzbek (O'zbek tili)",
        "style_note": "Sof o'zbek tilida, jonli nutq uslubida, 'siz' shaklida yozing.",
        "user_instruction": (
            "Ushbu PDF hujjatni chuqur o'qib, yuqoridagi talablarga asosan "
            "FAQAT O'ZBEK TILIDA JSON formatida podkast ssenariysi yarat. "
            "PDF boshqa tilda bo'lsa ham, barcha dialoglarni sof o'zbek tiliga tarjima qilib yoz. "
            "Faqat sof JSON massivi qaytargin, boshqa hech narsa yo'q."
        ),
        "edge_voices": {
            "Shaxnoza": "uz-UZ-MadinaNeural",
            "Mustafo": "uz-UZ-SardorNeural",
        },
        "gcloud_language_code": "uz-UZ",
        "gcloud_voices": {
            "Shaxnoza": "uz-UZ-MadinaNeural",
            "Mustafo": "uz-UZ-SardorNeural",
        },
    },
    "en": {
        "name": "Ingliz",
        "native": "English",
        "prompt_name": "English",
        "style_note": "Write in natural, engaging, conversational English suitable for a modern podcast.",
        "user_instruction": (
            "Read this PDF document thoroughly and create a podcast script in JSON format "
            "STRICTLY IN ENGLISH according to the instructions above. "
            "Even if the PDF is in another language, translate and adapt the entire conversation into natural English. "
            "Return ONLY a raw JSON array, nothing else."
        ),
        "edge_voices": {
            "Shaxnoza": "en-US-AriaNeural",
            "Mustafo": "en-US-GuyNeural",
        },
        "gcloud_language_code": "en-US",
        "gcloud_voices": {
            "Shaxnoza": "en-US-Neural2-F",
            "Mustafo": "en-US-Neural2-D",
        },
    },
    "ru": {
        "name": "Rus",
        "native": "Русский",
        "prompt_name": "Russian (Русский язык)",
        "style_note": "Пишите на живом, естественном разговорном русском языке, с обращением на «вы».",
        "user_instruction": (
            "Внимательно изучи этот PDF-документ и создай сценарий подкаста в формате JSON "
            "СТРОГО НА РУССКОМ ЯЗЫКЕ согласно указанным выше требованиям. "
            "Даже если PDF написан на другом языке, переведи и адаптируй весь диалог на живой русский язык. "
            "Верни ТОЛЬКО чистый JSON-массив, без лишнего текста."
        ),
        "edge_voices": {
            "Shaxnoza": "ru-RU-SvetlanaNeural",
            "Mustafo": "ru-RU-DmitryNeural",
        },
        "gcloud_language_code": "ru-RU",
        "gcloud_voices": {
            "Shaxnoza": "ru-RU-Wavenet-A",
            "Mustafo": "ru-RU-Wavenet-B",
        },
    },
    "de": {
        "name": "Nemis",
        "native": "Deutsch",
        "prompt_name": "German (Deutsch)",
        "style_note": "Schreiben Sie in natürlichem, lebendigem Gesprächsdeutsch (Sie-Form).",
        "user_instruction": (
            "Analysiere dieses PDF-Dokument gründlich und erstelle ein Podcast-Skript im JSON-Format "
            "AUSSCHLIESSLICH AUF DEUTSCH gemäß den obigen Anforderungen. "
            "Selbst wenn das PDF in einer anderen Sprache verfasst ist, übersetze und adaptiere den gesamten Dialog ins Deutsche. "
            "Gib NUR ein reines JSON-Array zurück, sonst nichts."
        ),
        "edge_voices": {
            "Shaxnoza": "de-DE-KatjaNeural",
            "Mustafo": "de-DE-ConradNeural",
        },
        "gcloud_language_code": "de-DE",
        "gcloud_voices": {
            "Shaxnoza": "de-DE-Neural2-F",
            "Mustafo": "de-DE-Neural2-B",
        },
    },
}


def build_system_prompt(language_code):
    """Build Gemini system instruction for the requested podcast language."""
    lang = SUPPORTED_LANGUAGES.get(language_code, SUPPORTED_LANGUAGES["uz"])
    return f"""You are a professional podcast scriptwriter. Deeply analyze the provided PDF document and create an engaging, lively two-person podcast conversation based on its facts and insights.

Characters (keep these exact JSON "speaker" names):
- Shaxnoza (Host): Curious, asks thoughtful questions, asks to simplify complex topics, addresses listeners warmly.
- Mustafo (Expert): Knowledgeable, explains numbers and facts from the PDF clearly using relatable real-life examples.

Requirements:
- Language: STRICTLY {lang['prompt_name']}. {lang['style_note']}
  CRITICAL: Regardless of the language of the PDF document, every single "text" value MUST be written 100% in {lang['prompt_name']}. Do NOT mix in other languages unless citing a specific proper noun or technical term.
- Duration: Depending on the PDF length, create a 5-10 minute conversation (30-50 turns).
- Length per turn: 20-60 words per turn.
- Format: Respond ONLY in valid JSON format. No extra text, no markdown, no code fences, no explanations. Only a raw JSON array.

JSON structure (strictly this format):
[{{"speaker": "Shaxnoza", "text": "..."}}, {{"speaker": "Mustafo", "text": "..."}}]

Important: The first element must have "speaker": "Shaxnoza". Make the dialogue natural, engaging, and grounded in real numbers and facts from the PDF."""


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



def classify_gemini_error(exc):
    """Return (should_rotate_key, is_permanent_key_error) for a Gemini exception."""
    code = getattr(exc, 'code', None)
    status = str(getattr(exc, 'status', '') or '').upper()
    msg = str(exc).lower()

    permanent_markers = (
        'api_key_invalid', 'api key not valid', 'permission_denied',
        'unauthenticated', 'leaked', 'expired', 'suspended', 'billing',
        'consumer_suspended', 'project_deleted'
    )
    if code in (401, 403) or status in ('PERMISSION_DENIED', 'UNAUTHENTICATED') or any(m in msg for m in permanent_markers):
        return True, True

    transient_markers = (
        '429', 'resource_exhausted', 'quota', 'rate limit', 'rate_limit',
        'too many requests', 'exceeded', '500', '502', '503', '504',
        'unavailable', 'deadline_exceeded', 'overloaded', 'timeout', 'timed out'
    )
    if code in (429, 500, 502, 503, 504) or status in ('RESOURCE_EXHAUSTED', 'UNAVAILABLE', 'DEADLINE_EXCEEDED', 'INTERNAL') or any(m in msg for m in transient_markers):
        return True, False

    return False, False


def generate_script_with_failover(job_id, pdf_path, lang_cfg, system_prompt):
    """Upload PDF and generate script, rotating Gemini API keys on quota/key errors."""
    from google import genai
    from google.genai import types

    indices = gemini_key_pool.ordered_indices()
    if not indices:
        raise Exception(".env faylida GEMINI_API_KEY topilmadi.")

    total_keys = gemini_key_pool.total
    last_error = None

    for attempt_num, key_idx in enumerate(indices, start=1):
        api_key = gemini_key_pool.get_key(key_idx)
        masked = gemini_key_pool.mask_key(api_key)
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=120_000)
        )
        uploaded_file = None

        try:
            # Step 1: Upload PDF (must use the same API key/client that generates the script)
            update_job(
                job_id,
                status='uploading',
                progress=5,
                message=f"PDF Gemini-ga yuklanmoqda ({lang_cfg['name']} tili, kalit #{key_idx + 1}/{total_keys})..."
            )
            logger.info(f"[{job_id}] Uploading PDF with key #{key_idx + 1}/{total_keys} ({masked}), language={lang_cfg['name']}...")

            uploaded_file = client.files.upload(file=str(pdf_path))

            max_wait, waited = 120, 0
            while uploaded_file.state.name == "PROCESSING" and waited < max_wait:
                time.sleep(2)
                waited += 2
                uploaded_file = client.files.get(name=uploaded_file.name)

            if uploaded_file.state.name != "ACTIVE":
                raise Exception(f"Fayl qayta ishlashda xato: {uploaded_file.state.name}")

            update_job(
                job_id,
                progress=15,
                message='PDF yuklandi. Ssenariy yaratilmoqda...'
            )

            # Step 2: Generate script
            update_job(
                job_id,
                status='generating',
                progress=22,
                message=f"DocuCast AI {lang_cfg['name'].lower()} tilida ssenariy yozmoqda..."
            )
            logger.info(f"[{job_id}] Generating script with {GEMINI_MODEL} (key #{key_idx + 1}/{total_keys}, {masked})...")

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    uploaded_file,
                    lang_cfg["user_instruction"],
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt
                )
            )

            gemini_key_pool.mark_success(key_idx)
            logger.info(f"[{job_id}] Gemini javobi olindi (kalit #{key_idx + 1}/{total_keys})!")
            return (response.text or "").strip()

        except Exception as e:
            last_error = e
            should_rotate, is_permanent = classify_gemini_error(e)
            has_next = attempt_num < len(indices)

            if should_rotate and has_next:
                gemini_key_pool.mark_failed(key_idx, permanent=is_permanent)
                next_idx = indices[attempt_num]
                reason = "kalit yaroqsiz/bloklangan" if is_permanent else "limiti tugadi yoki vaqtinchalik xato"
                logger.warning(
                    f"[{job_id}] Kalit #{key_idx + 1}/{total_keys} ({masked}) {reason}: {e}. "
                    f"Keyingi kalitga (#{next_idx + 1}/{total_keys}) o'tilmoqda..."
                )
                update_job(
                    job_id,
                    status='uploading',
                    progress=8,
                    message=f"{key_idx + 1}-API kalit {reason}. {next_idx + 1}-kalitga o'tilmoqda..."
                )
                continue

            if should_rotate:
                gemini_key_pool.mark_failed(key_idx, permanent=is_permanent)

            raise

        finally:
            if uploaded_file is not None:
                try:
                    client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass

    raise last_error or Exception("Barcha Gemini API kalitlarida xatolik yuz berdi.")


def generate_podcast_task(job_id, pdf_path, language="uz"):
    try:
        lang_cfg = SUPPORTED_LANGUAGES.get(language, SUPPORTED_LANGUAGES["uz"])
        system_prompt = build_system_prompt(language)

        raw_text = generate_script_with_failover(job_id, pdf_path, lang_cfg, system_prompt)
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

        script_pdf_path = OUTPUT_DIR / f"{job_id}_script.pdf"
        script_ready = False
        try:
            generate_script_pdf(script, str(script_pdf_path), language)
            script_ready = script_pdf_path.exists() and script_pdf_path.stat().st_size > 0
            logger.info(f"[{job_id}] Script PDF created: {script_pdf_path}")
        except Exception as pdf_err:
            logger.warning(f"[{job_id}] Script PDF yaratishda xato: {pdf_err}")

        update_job(
            job_id,
            progress=36,
            script=script,
            current_turn=0,
            script_ready=script_ready,
            script_turns=len(script),
            script_pdf=str(script_pdf_path) if script_ready else None,
            message=f'Ssenariy tayyor! {len(script)} ta dialog. Ovozlashtirish boshlanmoqda...'
        )
        logger.info(f"[{job_id}] Script: {len(script)} turns")

        # Step 3: TTS
        update_job(job_id, status='synthesizing', progress=40, current_turn=0, message='Audio ovozlashtirish boshlanmoqda...')
        audio_paths = synthesize_all_turns(job_id, script, language)

        # Step 4: Merge
        update_job(job_id, status='merging', progress=92, current_turn=len(script), message='Audio fayllar birlashtirilmoqda...')

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


def synthesize_all_turns(job_id, script, language="uz"):
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
        update_job(job_id, progress=progress, current_turn=i,
                   message=f'Ovozlashtirish: {i+1}/{total} — {speaker} gapirmoqda...')

        seg_path = str(job_temp / f"turn_{i:04d}.mp3")
        try:
            if GCLOUD_CREDS_JSON and GCLOUD_CREDS_JSON.strip():
                synthesize_google_tts(text, speaker, seg_path, language)
            else:
                synthesize_edge_tts(text, speaker, seg_path, language)

            if os.path.exists(seg_path) and os.path.getsize(seg_path) > 100:
                paths.append(seg_path)
            else:
                logger.warning(f"[{job_id}] Turn {i}: empty file, skipping")
        except Exception as e:
            logger.warning(f"[{job_id}] Turn {i} TTS xato: {e}")

    return paths


def synthesize_google_tts(text, speaker, output_path, language="uz"):
    """Google Cloud TTS — writes raw MP3 bytes directly, no ffmpeg needed."""
    import tempfile
    from google.cloud import texttospeech
    from google.oauth2 import service_account

    lang_cfg = SUPPORTED_LANGUAGES.get(language, SUPPORTED_LANGUAGES["uz"])

    creds_data = json.loads(GCLOUD_CREDS_JSON)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(creds_data, f)
        creds_path = f.name

    credentials = service_account.Credentials.from_service_account_file(creds_path)
    client = texttospeech.TextToSpeechClient(credentials=credentials)
    os.unlink(creds_path)

    gcloud_voices = lang_cfg["gcloud_voices"]
    voice_name = gcloud_voices.get(speaker, gcloud_voices["Shaxnoza"])
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=lang_cfg["gcloud_language_code"],
        name=voice_name
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=0.95,
        pitch=2.0 if speaker == "Shaxnoza" else -2.0
    )
    response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    # Write raw MP3 bytes — no pydub needed
    with open(output_path, 'wb') as f:
        f.write(response.audio_content)


def synthesize_edge_tts(text, speaker, output_path, language="uz"):
    """Microsoft Edge TTS — free, no API key, native neural voices for uz/en/ru/de."""
    import asyncio
    import edge_tts

    lang_cfg = SUPPORTED_LANGUAGES.get(language, SUPPORTED_LANGUAGES["uz"])
    voices = lang_cfg["edge_voices"]
    voice = voices.get(speaker, voices["Shaxnoza"])

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


def _load_pdf_fonts():
    """Load Unicode TrueType fonts (supporting Uzbek, English, Russian Cyrillic, German)."""
    from PIL import ImageFont

    regular_candidates = [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    bold_candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]

    reg_path = next((p for p in regular_candidates if os.path.exists(p)), None)
    bold_path = next((p for p in bold_candidates if os.path.exists(p)), reg_path)

    if reg_path:
        return {
            "title": ImageFont.truetype(bold_path, 38),
            "subtitle": ImageFont.truetype(reg_path, 21),
            "speaker": ImageFont.truetype(bold_path, 22),
            "badge": ImageFont.truetype(bold_path, 18),
            "body": ImageFont.truetype(reg_path, 22),
            "footer": ImageFont.truetype(reg_path, 17),
        }

    default_font = ImageFont.load_default()
    return {k: default_font for k in ("title", "subtitle", "speaker", "badge", "body", "footer")}


def _wrap_text(draw, text, font, max_width):
    """Wrap text to fit within max_width pixels using exact font measurement."""
    lines = []
    for paragraph in (text or "").splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        # Break unusually long single tokens if needed
        while draw.textlength(current, font=font) > max_width and len(current) > 1:
            cut = len(current) - 1
            while cut > 1 and draw.textlength(current[:cut], font=font) > max_width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
        lines.append(current)
    return lines


def generate_script_pdf(script, output_path, language="uz"):
    """Render the podcast script as a clean multi-page A4 PDF."""
    from PIL import Image, ImageDraw

    PAGE_W, PAGE_H = 1240, 1754  # A4 at 150 DPI
    MARGIN_X = 76
    TOP_Y = 74
    BOTTOM_LIMIT = PAGE_H - 96
    CONTENT_W = PAGE_W - (MARGIN_X * 2)

    fonts = _load_pdf_fonts()
    lang_cfg = SUPPORTED_LANGUAGES.get(language, SUPPORTED_LANGUAGES["uz"])
    pages = []

    def _new_page(is_first=False):
        img = Image.new("RGB", (PAGE_W, PAGE_H), "#FFFFFF")
        draw = ImageDraw.Draw(img)
        # Top accent bar
        draw.rectangle([0, 0, PAGE_W, 12], fill="#7C3AED")
        if is_first:
            draw.rounded_rectangle(
                [MARGIN_X, TOP_Y, PAGE_W - MARGIN_X, TOP_Y + 126],
                radius=18,
                fill="#F5F3FF",
                outline="#DDD6FE",
                width=2,
            )
            draw.text((MARGIN_X + 28, TOP_Y + 22), "DocuCast — Podkast Ssenariysi", fill="#1E1B4B", font=fonts["title"])
            meta = f"Til: {lang_cfg['native']}  •  {len(script)} ta dialog  •  Shaxnoza & Mustafo"
            draw.text((MARGIN_X + 28, TOP_Y + 76), meta, fill="#5B21B6", font=fonts["subtitle"])
            return img, draw, TOP_Y + 156
        return img, draw, TOP_Y

    img, draw, y = _new_page(is_first=True)
    pages.append(img)

    line_h = 33
    card_pad_x = 24
    card_pad_top = 18
    card_pad_bottom = 20
    text_max_w = CONTENT_W - (card_pad_x * 2) - 8

    for idx, turn in enumerate(script, start=1):
        speaker = (turn.get("speaker") or "Shaxnoza").strip()
        text = (turn.get("text") or "").strip()
        if not text:
            continue

        is_host = speaker.lower() == "shaxnoza"
        accent = "#7C3AED" if is_host else "#0891B2"
        bg = "#FAF5FF" if is_host else "#ECFEFF"
        border = "#E9D5FF" if is_host else "#CFFAFE"
        role_label = f"#{idx:02d}  {speaker}"

        wrapped = _wrap_text(draw, text, fonts["body"], text_max_w)
        card_h = card_pad_top + 36 + len(wrapped) * line_h + card_pad_bottom

        if y + card_h > BOTTOM_LIMIT:
            img, draw, y = _new_page(is_first=False)
            pages.append(img)

        draw.rounded_rectangle(
            [MARGIN_X, y, PAGE_W - MARGIN_X, y + card_h],
            radius=14,
            fill=bg,
            outline=border,
            width=2,
        )
        # Left speaker accent stripe
        draw.rounded_rectangle(
            [MARGIN_X, y, MARGIN_X + 8, y + card_h],
            radius=4,
            fill=accent,
        )
        draw.text((MARGIN_X + card_pad_x + 4, y + card_pad_top), role_label, fill=accent, font=fonts["speaker"])

        text_y = y + card_pad_top + 38
        for line in wrapped:
            draw.text((MARGIN_X + card_pad_x + 4, text_y), line, fill="#1E293B", font=fonts["body"])
            text_y += line_h

        y += card_h + 16

    total_pages = len(pages)
    for page_num, page_img in enumerate(pages, start=1):
        d = ImageDraw.Draw(page_img)
        d.line([MARGIN_X, PAGE_H - 68, PAGE_W - MARGIN_X, PAGE_H - 68], fill="#E2E8F0", width=1)
        d.text((MARGIN_X, PAGE_H - 52), "DocuCast AI", fill="#64748B", font=fonts["footer"])
        page_label = f"{page_num} / {total_pages}"
        pw = d.textlength(page_label, font=fonts["footer"])
        d.text((PAGE_W - MARGIN_X - pw, PAGE_H - 52), page_label, fill="#64748B", font=fonts["footer"])

    pages[0].save(
        str(output_path),
        "PDF",
        resolution=150.0,
        save_all=True,
        append_images=pages[1:],
    )


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
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if (
        request.path.startswith('/api/')
        or request.path in ('/', '/index.html')
        or request.path.endswith(('.js', '.css'))
    ):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    else:
        response.headers['Cache-Control'] = 'public, max-age=3600'
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
        "tts": "google-cloud" if GCLOUD_CREDS_JSON else "edge-tts (Neural)",
        "api_configured": gemini_key_pool.total > 0,
        "api_keys_count": gemini_key_pool.total,
        "languages": [
            {"code": code, "name": cfg["name"], "native": cfg["native"]}
            for code, cfg in SUPPORTED_LANGUAGES.items()
        ],
    })


@app.route('/api/generate', methods=['POST'])
@rate_limit
def generate():
    # Validate language
    language = (request.form.get('language') or 'uz').strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        supported = ", ".join(SUPPORTED_LANGUAGES.keys())
        return jsonify({"error": f"Noto'g'ri til tanlandi ({language}). Ruxsat etilgan tillar: {supported}"}), 400

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

    if gemini_key_pool.total == 0:
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
        "language": language,
        "filename": pdf_file.filename,
        "script": None,
        "current_turn": -1,
        "script_ready": False,
        "script_turns": 0,
        "script_pdf": None,
        "output_file": None, "token": token,
        "created_at": time.time()
    }

    thread = threading.Thread(
        target=generate_podcast_task,
        args=(job_id, pdf_path, language),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "token": token, "language": language})


@app.route('/api/status/<job_id>')
def status(job_id):
    if not jobs.get(job_id):
        return jsonify({"error": "Job topilmadi"}), 404

    def event_stream():
        timeout, start = 600, time.time()
        last_state = None
        sent_script = False
        while time.time() - start < timeout:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job topilmadi'})}\n\n"
                break
            curr_state = (
                job['progress'],
                job.get('script_ready'),
                job.get('current_turn'),
                job['status'],
                job.get('message'),
            )
            if curr_state != last_state or job['status'] in ('done', 'error'):
                last_state = curr_state
                exclude = {'token', 'script_pdf'}
                if sent_script or not job.get('script'):
                    exclude.add('script')
                else:
                    sent_script = True
                safe = {k: v for k, v in job.items() if k not in exclude}
                yield f"data: {json.dumps(safe)}\n\n"
            if job['status'] in ('done', 'error'):
                break
            time.sleep(0.5)

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


@app.route('/api/script-pdf/<job_id>')
def view_script_pdf(job_id):
    token = request.args.get('token', '')
    if not validate_job_owner(job_id, token):
        return jsonify({"error": "Ruxsat yo'q"}), 403

    job = jobs.get(job_id)
    if not job or not job.get('script_ready') or not job.get('script_pdf'):
        return jsonify({"error": "Ssenariy PDF hali tayyor emas"}), 404

    pdf_path = Path(job['script_pdf'])
    if not pdf_path.exists():
        return jsonify({"error": "Ssenariy PDF topilmadi"}), 404

    return send_file(str(pdf_path), mimetype="application/pdf",
                     as_attachment=False, download_name="docucast_script.pdf")


@app.route('/api/download-pdf/<job_id>')
def download_script_pdf(job_id):
    token = request.args.get('token', '')
    if not validate_job_owner(job_id, token):
        return jsonify({"error": "Ruxsat yo'q"}), 403

    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job topilmadi"}), 404

    pdf_path = Path(job['script_pdf']) if job.get('script_pdf') else (OUTPUT_DIR / f"{job_id}_script.pdf")
    if not pdf_path.exists() and job.get('script'):
        try:
            generate_script_pdf(job['script'], str(pdf_path), job.get('language', 'uz'))
            update_job(job_id, script_ready=True, script_pdf=str(pdf_path))
        except Exception as e:
            logger.error(f"[{job_id}] On-demand PDF error: {e}")
            return jsonify({"error": "Ssenariy PDF yaratishda xato"}), 500

    if not pdf_path.exists():
        return jsonify({"error": "Ssenariy PDF hali tayyor emas"}), 404

    resp = send_file(str(pdf_path), as_attachment=True,
                     download_name="docucast_script.pdf", mimetype="application/pdf")
    resp.headers['Content-Disposition'] = 'attachment; filename="docucast_script.pdf"'
    return resp


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
            if job.get('script_pdf'):
                Path(job['script_pdf']).unlink(missing_ok=True)
        if to_delete:
            logger.info(f"Cleanup: {len(to_delete)} eski job o'chirildi")


if __name__ == '__main__':
    threading.Thread(target=cleanup_old_jobs, daemon=True).start()
    logger.info(f"🎙️  DocuCast v2.0 — http://127.0.0.1:5000")
    logger.info(f"📦  Model: {GEMINI_MODEL}")
    logger.info(f"🔑  Gemini API: {f'✅ {gemini_key_pool.total} ta kalit sozlangan' if gemini_key_pool.total else '❌ SOZLANMAGAN (.env tekshiring)'}")
    logger.info(f"🔊  TTS: {'Google Cloud' if GCLOUD_CREDS_JSON else 'edge-tts (uz/en/ru/de Neural)'}")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
