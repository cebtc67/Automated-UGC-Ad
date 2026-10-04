# Free Google Sheets to UGC Video Pipeline

Architecture:

Google Sheets -> Apps Script -> Google Drive Queue -> Google Colab -> Wan2GP/Wan 2.2 -> Google Drive Output -> Google Sheets.

No n8n. No paid video API. No paid LLM API.

## Limitation

Compute is not guaranteed. Google Colab Free has dynamic GPU availability and runtime limits. The worker is therefore a pull queue: when a GPU runtime is available, start the worker and let it process queued jobs.

Wan2GP is used because it is designed for low-VRAM generation. This prototype targets a conservative short vertical run.

## Google Sheet

Run free_pipeline/Code.gs in the Apps Script editor attached to your Google Sheet.

The setup creates:

Videos columns:
- No
- Product
- Product Photo
- ICP
- Product Features
- Video Setting
- Model
- Status
- Finished Video
- Job ID
- Error
- Updated At

Config tab:
- Spreadsheet ID
- Videos Sheet
- Queue Folder ID
- Output Folder ID

Product Photo can be a Google Drive file ID, Google Drive URL containing a file ID, or a directly downloadable public image URL.

Set Status to READY to queue a job.

## Apps Script

1. Open Extensions -> Apps Script.
2. Replace the default code with free_pipeline/Code.gs.
3. Save.
4. Run setup() manually once.
5. Accept the authorization prompts.
6. Reload the Sheet.
7. Use the UGC Automation menu.

## Colab

Open free_pipeline/colab_worker.py in Google Colab.

Select a GPU runtime when available.

Set UGC_SPREADSHEET_ID to the ID of your Sheet, then run the worker.

The worker:
1. Authenticates your Google account.
2. Reads Config.
3. Polls the Queue folder.
4. Downloads each product photo.
5. Builds a deterministic UGC prompt from the row.
6. Generates a short vertical video with Wan2GP.
7. Uploads the MP4 to Output.
8. Updates the row to FINISHED.
9. Writes the Drive URL into Finished Video.

## Why this differs from the original project

The original project uses hosted Kie.AI, OpenAI and OpenRouter services.

This branch intentionally avoids those services.

Prompt generation is deterministic and based directly on product metadata in the Sheet. Video generation is local to the Colab runtime with open-source tooling.

The first target is a functional free prototype, not maximum visual quality.

## Next upgrade

Once the end-to-end queue works, add a local open-source image-composition stage that creates a human-holding-product reference before video generation. This should improve UGC realism and product fidelity without introducing a paid API.
