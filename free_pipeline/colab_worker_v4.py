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


def build_ugc_prompt(job: dict[str, Any], continuation: bool = False) -> str:
    product = job.get("product", "the product")
    icp = job.get("icp", "a natural everyday customer")
    features = job.get("product_features", "")
    setting = job.get("video_setting", "a realistic everyday environment")

    continuation_block = ""
    if continuation:
        continuation_block = """
CONTINUATION:
This is the second shot of the same UGC video.
Continue naturally from the exact starting frame.
Keep the same adult creator, wardrobe, product, location, lighting and camera style.
Do not reset the scene or introduce a new character.
Make one additional natural action with the product and keep the motion continuous.
""".strip()

    return f"""
Create a short vertical UGC product advertisement.

PRODUCT:
{product}

IDEAL CUSTOMER:
{icp}

KEY PRODUCT BENEFITS:
{features}

SETTING:
{setting}

STYLE:
Authentic creator-style selfie video, vertical 9:16.
A believable adult creator naturally presents and uses the exact product from the reference image.
Preserve the product's shape, color, packaging, label placement and recognizable details.
Natural handheld phone-camera motion, slight micro-shake, realistic exposure, imperfect framing,
natural skin texture, everyday environment, one simple action with the product.
The creator looks into the camera and briefly demonstrates the product instead of delivering a polished commercial.
Avoid studio lighting, subtitles, UI elements, watermarks, extra products, distorted hands and visible phone hardware.
Do not invent claims beyond the information supplied above.

{continuation_block}
""".strip()


def prepare_vertical_reference(image_path: Path) -> Image.Image:
    """Build a 9:16 reference while keeping the whole product visible."""
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


def generate_segment(
    session,
    job: dict[str, Any],
    image_path: Path,
    continuation: bool = False,
) -> Path:
    model_type = choose_wan_model(session)
    settings = session.get_default_settings(model_type)

    pil_image = prepare_vertical_reference(image_path)

    settings.update({
        "model_type": model_type,
        "image_mode": 0,
        "prompt": build_ugc_prompt(job, continuation=continuation),
        "image_prompt_type": "S",
        "image_start": [(pil_image, None)],
        "resolution": "480x832",
        # FastWan TI2V uses 121 frames; at 24 fps this is ~5.04 seconds.
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


def extract_last_frame(video_path: Path, frame_path: Path) -> Path:
    cmd = [
        "ffmpeg", "-y",
        "-sseof", "-0.05",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(frame_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return frame_path


def concatenate_vertical_clips(
    first_path: Path,
    second_path: Path,
    output_path: Path,
) -> Path:
    # Trim a tiny overlap from shot 2 because its first frame was conditioned
    # on the final frame of shot 1.
    cmd = [
        "ffmpeg", "-y",
        "-i", str(first_path),
        "-i", str(second_path),
        "-filter_complex",
        (
            "[1:v]trim=start=0.15,setpts=PTS-STARTPTS[v1];"
            "[0:v][v1]concat=n=2:v=1:a=0,"
            "scale=480:832:force_original_aspect_ratio=decrease,"
            "pad=480:832:(ow-iw)/2:(oh-ih)/2,"
            "setsar=1,setdar=9/16"
        ),
        "-r", "24",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output_path


def make_final_vertical(
    input_path: Path,
    output_path: Path,
    width: int = 480,
    height: int = 832,
) -> Path:
    """Force the final MP4 to 9:16 while preserving the full frame."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf",
        (
            f"split=2[bg][fg];"
            f"[bg]scale={width}:{height},boxblur=20:2[blur];"
            f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[small];"
            f"[blur][small]overlay=(W-w)/2:(H-h)/2,"
            "setsar=1,setdar=9/16"
        ),
        "-r", "24",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
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
        first = generate_segment(session, job, input_image, continuation=False)

        first_path = OUTPUT_DIR / f"seg1_{job_id}.mp4"
        if first.resolve() != first_path.resolve():
            shutil.copy2(first, first_path)

        last_frame = INPUT_DIR / f"{job_id}_last_frame.jpg"
        extract_last_frame(first_path, last_frame)

        second = generate_segment(session, job, last_frame, continuation=True)

        second_path = OUTPUT_DIR / f"seg2_{job_id}.mp4"
        if second.resolve() != second_path.resolve():
            shutil.copy2(second, second_path)

        raw_path = OUTPUT_DIR / f"raw_{output_name}"
        concatenate_vertical_clips(first_path, second_path, raw_path)

        final_path = OUTPUT_DIR / output_name
        make_final_vertical(raw_path, final_path)

        drive_file = upload_video(final_path, OUTPUT_FOLDER_ID, output_name)
        for temp_path in (first_path, second_path, raw_path, last_frame):
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
        # Remove the failed queue marker so the worker does not retry forever.
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
