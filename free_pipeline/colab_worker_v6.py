# Free UGC worker for Google Colab.
#
# Pipeline:
# Drive Queue -> image -> Wan2GP -> Drive Output -> Google Sheets.
# No paid APIs.

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps

from google.auth import default
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

CREDS, _ = default()
DRIVE = build("drive", "v3", credentials=CREDS)
SHEETS = build("sheets", "v4", credentials=CREDS)

SPREADSHEET_ID = os.environ.get("UGC_SPREADSHEET_ID", "").strip()
SHEET_NAME = ""
QUEUE_FOLDER_ID = ""
OUTPUT_FOLDER_ID = ""

POLL_SECONDS = int(os.environ.get("UGC_POLL_SECONDS", "20"))
WORKDIR = Path("/content/ugc_worker")
INPUT_DIR = WORKDIR / "inputs"
OUTPUT_DIR = WORKDIR / "outputs"
WAN_DIR = WORKDIR / "Wan2GP"

for p in (INPUT_DIR, OUTPUT_DIR):
    p.mkdir(parents=True, exist_ok=True)


def load_config_from_sheet() -> dict[str, str]:
    global SPREADSHEET_ID, SHEET_NAME, QUEUE_FOLDER_ID, OUTPUT_FOLDER_ID

    if not SPREADSHEET_ID:
        raise RuntimeError("Set UGC_SPREADSHEET_ID before starting the worker.")

    rows = SHEETS.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Config!A:B",
    ).execute().get("values", [])

    config: dict[str, str] = {}
    for row in rows[1:]:
        if len(row) >= 2:
            config[str(row[0]).strip()] = str(row[1]).strip()

    SPREADSHEET_ID = config.get("Spreadsheet ID", SPREADSHEET_ID)
    SHEET_NAME = config.get("Videos Sheet", "Videos")
    QUEUE_FOLDER_ID = config.get("Queue Folder ID", "")
    OUTPUT_FOLDER_ID = config.get("Output Folder ID", "")

    missing = [
        name for name, value in {
            "Spreadsheet ID": SPREADSHEET_ID,
            "Videos Sheet": SHEET_NAME,
            "Queue Folder ID": QUEUE_FOLDER_ID,
            "Output Folder ID": OUTPUT_FOLDER_ID,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError("Missing Config values: " + ", ".join(missing))

    return config


def list_job_files() -> list[dict[str, Any]]:
    q = (
        f"'{QUEUE_FOLDER_ID}' in parents and "
        "trashed = false and "
        "name contains 'job_'"
    )
    return DRIVE.files().list(
        q=q,
        pageSize=50,
        orderBy="createdTime",
        fields="files(id,name,createdTime,mimeType,size)",
    ).execute().get("files", [])


def download_drive_file(file_id: str, dest: Path) -> None:
    request = DRIVE.files().get_media(fileId=file_id)
    with dest.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def read_text_file(file_id: str) -> str:
    request = DRIVE.files().get_media(fileId=file_id)
    data = io.BytesIO()
    downloader = MediaIoBaseDownload(data, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return data.getvalue().decode("utf-8")


def upload_video(local_path: Path, folder_id: str, name: str) -> dict[str, Any]:
    metadata = {
        "name": name,
        "parents": [folder_id],
        "mimeType": "video/mp4",
    }
    media = MediaFileUpload(str(local_path), mimetype="video/mp4", resumable=True)
    return DRIVE.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink,webContentLink",
    ).execute()


def trash_drive_file(file_id: str) -> None:
    DRIVE.files().update(fileId=file_id, body={"trashed": True}).execute()


def extract_drive_id(value: str) -> str | None:
    value = value.strip()
    patterns = [
        r"/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]{20,})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return None


def download_product_image(photo_value: str, destination: Path) -> None:
    file_id = extract_drive_id(photo_value)
    if file_id:
        download_drive_file(file_id, destination)
        return

    import requests
    response = requests.get(photo_value, timeout=60)
    response.raise_for_status()
    destination.write_bytes(response.content)


def get_headers() -> list[str]:
    result = SHEETS.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!1:1",
    ).execute()
    return [str(x) for x in result.get("values", [[]])[0]]


def column_letter(n: int) -> str:
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def update_row(row_number: int, updates: dict[str, Any]) -> None:
    headers = get_headers()
    data = []

    for key, value in updates.items():
        if key not in headers:
            continue
        col = headers.index(key) + 1
        data.append({
            "range": f"{SHEET_NAME}!{column_letter(col)}{row_number}",
            "values": [[value]],
        })

    if data:
        SHEETS.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def install_wangp() -> None:
    if WAN_DIR.exists():
        return

    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "https://github.com/deepbeepmeep/Wan2GP.git",
            str(WAN_DIR),
        ],
        check=True,
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "-r",
            str(WAN_DIR / "requirements.txt"),
        ],
        check=True,
    )


def make_wangp_session():
    install_wangp()
    if str(WAN_DIR) not in sys.path:
        sys.path.insert(0, str(WAN_DIR))

    from shared.api import init  # type: ignore

    return init(
        root=WAN_DIR,
        cli_args=["--attention", "sdpa", "--profile", "4"],
        console_output=True,
    )


def choose_wan_model(session) -> str:
    models = session.list_model_metadata(
        main_output="video",
        inputs="image",
        include_availability=True,
    )

    candidates = []
    for model in models:
        text_blob = json.dumps(model).lower()
        if "wan" not in text_blob:
            continue
        if "5b" not in text_blob:
            continue
        if "22" not in text_blob and "2.2" not in text_blob:
            continue
        candidates.append(model)

    def score(model):
        text_blob = json.dumps(model).lower()
        score_value = 0
        if "fastwan" in text_blob:
            score_value += 100
        if "ti2v" in text_blob:
            score_value += 50
        if model.get("availability") == "available":
            score_value += 20
        return score_value

    candidates.sort(key=score, reverse=True)

    if not candidates:
        raise RuntimeError(
            "No Wan 2.2 5B image-capable model was found. "
            "Inspect session.list_model_metadata(main_output='video', inputs='image')."
        )

    selected = candidates[0]
    print("Selected model:", selected.get("model_type"))
    return str(selected["model_type"])


def build_ugc_prompt(job: dict[str, Any], shot_index: int) -> str:
    product = job.get("product", "the product")
    icp = job.get("icp", "a natural everyday customer")
    features = job.get("product_features", "")
    setting = job.get("video_setting", "a realistic everyday environment")

    shots = [
        """
CAMERA SHOT 1 — SELFIE MEDIUM:
Start with an authentic handheld smartphone selfie, medium framing from chest-up.
The creator quickly brings the product into the frame and demonstrates one simple action.
Energetic but natural movement, immediate visual hook in the first second.
""",
        """
CAMERA SHOT 2 — PRODUCT CLOSE-UP:
Use a tight close-up of the product and the creator's hands.
Small handheld push-in and slight tilt as the product is used.
The product is the visual focus; keep the exact packaging and details recognizable.
""",
        """
CAMERA SHOT 3 — THREE-QUARTER ANGLE:
Use a natural three-quarter side angle, as if a friend is holding the phone.
Show the creator using the product in the environment with a quick, believable movement.
Slight handheld reframing during the shot.
""",
        """
CAMERA SHOT 4 — HERO REVEAL:
Begin closer on the product, then naturally pull back to reveal the creator using it.
Dynamic handheld camera movement, subtle parallax, strong visual finish.
""",
    ]

    shot = shots[(shot_index - 1) % len(shots)]

    return f"""
Create one short silent UGC video shot for a fast-paced vertical social-media advertisement.

PRODUCT:
{product}

IDEAL CUSTOMER:
{icp}

KEY PRODUCT BENEFITS:
{features}

SETTING:
{setting}

{shot}

GLOBAL STYLE:
Authentic creator-generated social video, vertical 9:16.
Believable adult creator, natural skin and real-world lighting.
Preserve the exact product shape, color, packaging, label placement and recognizable details.
Natural handheld smartphone motion, imperfect framing, realistic exposure and subtle micro-shake.
No studio-commercial look.
NO SPEAKING, NO LIP-SYNC, NO DIALOGUE, NO VOICE, MOUTH NATURALLY CLOSED.
NO SUBTITLES, NO TEXT, NO LOGOS OR WATERMARKS ADDED BY THE MODEL.
NO EXTRA PRODUCTS.
Fast, visually interesting movement that reads immediately on a phone screen.
""".strip()


def prepare_vertical_reference(image_path: Path) -> Image.Image:
    img = Image.open(image_path).convert("RGB")
    target_w, target_h = 480, 832

    bg = ImageOps.fit(
        img,
        (target_w, target_h),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).filter(ImageFilter.GaussianBlur(radius=18))

    fg = ImageOps.contain(
        img,
        (target_w - 32, target_h - 80),
        method=Image.Resampling.LANCZOS,
    )

    canvas = bg.copy()
    x = (target_w - fg.width) // 2
    y = (target_h - fg.height) // 2
    canvas.paste(fg, (x, y))
    return canvas


def generate_shot(session, job: dict[str, Any], image_path: Path, shot_index: int) -> Path:
    model_type = choose_wan_model(session)
    settings = session.get_default_settings(model_type)

    pil_image = prepare_vertical_reference(image_path)

    settings.update({
        "model_type": model_type,
        "image_mode": 0,
        "prompt": build_ugc_prompt(job, shot_index),
        "image_prompt_type": "S",
        "image_start": [(pil_image, None)],
        "resolution": "480x832",
        # FastWan TI2V profile produces 121 frames; at 24 fps this is ~5.04 s.
        "video_length": 121,
        "force_fps": "24",
        "repeat_generation": 1,
        "seed": -1,
        "prompt_enhancer": "",
        "multi_prompts_gen_type": "FG",
    })

    job_handle = session.submit_task(settings)
    result = job_handle.result()

    if not result.success:
        messages = [getattr(err, "message", str(err)) for err in result.errors]
        raise RuntimeError("Wan2GP generation failed: " + " | ".join(messages))

    files = list(result.generated_files or [])
    if not files:
        raise RuntimeError("Wan2GP finished without a generated file.")

    return Path(files[0])


def make_quickcut_20s(
    sources: list[Path],
    output_path: Path,
    width: int = 720,
    height: int = 1280,
) -> Path:
    """
    Turn four ~5 s generations into eight ~2.5 s shots.
    Every second half gets a slightly different crop to create an editorial
    jump-cut feel without requiring eight separate model generations.
    """
    inputs = []
    for path in sources:
        inputs += ["-i", str(path)]

    filters = []
    labels = []

    for i in range(4):
        base = f"[{i}:v]"
        a = f"a{i}"
        b = f"b{i}"

        filters.append(
            f"{base}trim=start=0:end=2.5,setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:"
            f"(iw-ow)/2:(ih-oh)/2,[{a}]"
        )
        filters.append(
            f"{base}trim=start=2.5:end=5,setpts=PTS-STARTPTS,"
            f"scale=810:1440:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:"
            f"(iw-ow)/2+40:(ih-oh)/2-20,[{b}]"
        )
        labels.extend([a, b])

    concat_inputs = "".join(f"[{x}]" for x in labels)
    filters.append(
        f"{concat_inputs}concat=n=8:v=1:a=0,"
        "fps=24,format=yuv420p[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[outv]",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output_path


def create_dynamic_music(output_path: Path, duration: float = 20.0) -> Path:
    """
    Generate a short original instrumental beat locally.
    No external music API and no copyrighted track.
    """
    import math
    import wave
    import numpy as np

    sr = 44100
    n = int(sr * duration)
    audio = np.zeros(n, dtype=np.float32)

    bpm = 128.0
    beat = 60.0 / bpm
    bar = beat * 4

    def add_tone(start, freq, length, amp=0.12, decay=8.0):
        i0 = max(0, int(start * sr))
        i1 = min(n, i0 + int(length * sr))
        if i1 <= i0:
            return
        t = np.arange(i1 - i0, dtype=np.float32) / sr
        env = np.exp(-decay * t)
        tone = np.sin(2 * math.pi * freq * t) * env * amp
        audio[i0:i1] += tone

    def add_kick(start):
        i0 = int(start * sr)
        length = int(0.16 * sr)
        i1 = min(n, i0 + length)
        t = np.arange(max(0, i1 - i0), dtype=np.float32) / sr
        freq = 110.0 * np.exp(-18.0 * t) + 48.0
        env = np.exp(-24.0 * t)
        audio[i0:i1] += np.sin(2 * math.pi * freq * t) * env * 0.55

    def add_snare(start):
        i0 = int(start * sr)
        length = int(0.12 * sr)
        i1 = min(n, i0 + length)
        count = max(0, i1 - i0)
        rng = np.random.default_rng(int(start * 1000) + 123)
        noise = rng.standard_normal(count).astype(np.float32)
        t = np.arange(count, dtype=np.float32) / sr
        env = np.exp(-24.0 * t)
        audio[i0:i1] += noise * env * 0.10

    def add_hat(start):
        i0 = int(start * sr)
        length = int(0.045 * sr)
        i1 = min(n, i0 + length)
        count = max(0, i1 - i0)
        rng = np.random.default_rng(int(start * 1000) + 999)
        noise = rng.standard_normal(count).astype(np.float32)
        t = np.arange(count, dtype=np.float32) / sr
        env = np.exp(-80.0 * t)
        audio[i0:i1] += noise * env * 0.035

    roots = [110.0, 82.41, 98.0, 73.42]
    bars = int(math.ceil(duration / bar))

    for b in range(bars):
        base = b * bar
        root = roots[b % len(roots)]

        add_kick(base)
        add_kick(base + beat * 2)
        add_snare(base + beat)
        add_snare(base + beat * 3)

        for h in range(8):
            add_hat(base + h * beat / 2)

        for step in range(4):
            add_tone(
                base + step * beat,
                root * (2.0 if step in (1, 3) else 1.0),
                0.28,
                amp=0.08,
                decay=7.0,
            )

        # Small high-frequency pulse for energy without vocals.
        for step in (0, 2):
            add_tone(
                base + step * beat + beat * 0.5,
                root * 4.0,
                0.10,
                amp=0.045,
                decay=16.0,
            )

    # Gentle sidechain-style overall envelope on bar boundaries.
    audio = np.tanh(audio * 1.4)
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = audio / max(peak, 1.0) * 0.72

    pcm = np.int16(audio * 32767)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())

    return output_path


def upscale_final_with_wangp(
    session,
    input_path: Path,
    fallback_path: Path,
) -> Path:
    """
    Try WanGP's FlashVSR x2 video upsampler. If unavailable, fall back to
    high-quality Lanczos 720x1280 scaling so the pipeline remains reliable.
    """
    try:
        job = session.submit_media_postprocessing(
            str(input_path),
            spatial_upsampling="flashvsr*2",
            return_media=True,
        )
        result = job.result()
        files = list(result.generated_files or [])
        if files:
            enhanced = Path(files[0])
            # Normalize to a practical 720x1280 social output after x2.
            cmd = [
                "ffmpeg", "-y",
                "-i", str(enhanced),
                "-vf", "scale=720:1280:flags=lanczos,setsar=1,setdar=9/16",
                "-r", "24",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "18",
                "-an",
                "-movflags", "+faststart",
                str(fallback_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return fallback_path
    except Exception as exc:
        print("FlashVSR unavailable; using Lanczos fallback:", str(exc)[:300])

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", "scale=720:1280:flags=lanczos,setsar=1,setdar=9/16",
        "-r", "24",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-an",
        "-movflags", "+faststart",
        str(fallback_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return fallback_path


def mux_music(video_path: Path, music_path: Path, output_path: Path) -> Path:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(music_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "160k",
        "-t", "20",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output_path



def safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return value[:80] or "ugc"


def process_job(job_file: dict[str, Any], session) -> None:
    job_id = Path(job_file["name"]).stem.replace("job_", "")
    job = json.loads(read_text_file(job_file["id"]))
    row = int(job["row_number"])

    update_row(row, {
        "Status": "PROCESSING",
        "Error": "",
        "Updated At": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    input_image = INPUT_DIR / f"{job_id}_source"
    output_name = f"{job_id}_{safe_name(job.get('product', 'ugc'))}.mp4"

    try:
        download_product_image(str(job["product_photo"]), input_image)

        sources = []
        for shot_index in range(1, 5):
            print(f"Generating camera shot {shot_index}/4...")
            generated = generate_shot(session, job, input_image, shot_index)
            shot_path = OUTPUT_DIR / f"shot_{shot_index}_{job_id}.mp4"
            if generated.resolve() != shot_path.resolve():
                shutil.copy2(generated, shot_path)
            sources.append(shot_path)

        quickcut_path = OUTPUT_DIR / f"quickcut_{job_id}.mp4"
        make_quickcut_20s(sources, quickcut_path)

        enhanced_path = OUTPUT_DIR / f"enhanced_{job_id}.mp4"
        upscale_final_with_wangp(session, quickcut_path, enhanced_path)

        music_path = OUTPUT_DIR / f"music_{job_id}.wav"
        create_dynamic_music(music_path, duration=20.0)

        final_path = OUTPUT_DIR / output_name
        mux_music(enhanced_path, music_path, final_path)

        drive_file = upload_video(final_path, OUTPUT_FOLDER_ID, output_name)

        cleanup = sources + [quickcut_path, enhanced_path, music_path, final_path]
        for temp_path in cleanup:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

        update_row(row, {
            "Status": "FINISHED",
            "Finished Video": drive_file.get("webViewLink") or "",
            "Error": "",
            "Updated At": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        trash_drive_file(job_file["id"])

    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        print("\nERROR processing", job_file["name"])
        print(error_text)
        traceback.print_exc()
        update_row(row, {
            "Status": "ERROR",
            "Error": error_text[:5000],
            "Updated At": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        trash_drive_file(job_file["id"])

def run_forever() -> None:
    load_config_from_sheet()
    print("Config loaded.")
    session = make_wangp_session()
    print("Wan2GP session ready.")

    while True:
        jobs = list_job_files()
        if jobs:
            for job_file in jobs:
                print("Processing:", job_file["name"])
                process_job(job_file, session)
        else:
            print("Queue empty. Waiting", POLL_SECONDS, "seconds...")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if not SPREADSHEET_ID:
        raise SystemExit(
            "Set UGC_SPREADSHEET_ID before running. "
            "Example: os.environ['UGC_SPREADSHEET_ID'] = 'YOUR_SHEET_ID'"
        )

    run_forever()
