import csv
import io
import os
import time
import hashlib
import logging
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from boxsdk import OAuth2, Client
from boxsdk.exception import BoxAPIException

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')

DRIVE_SOURCE_FOLDER_ID = os.environ['DRIVE_SOURCE_FOLDER_ID']
BOX_DESTINATION_FOLDER_ID = os.environ['BOX_DESTINATION_FOLDER_ID']
DRY_RUN = os.getenv('DRY_RUN', 'true').lower() == 'true'
MAX_FILES = int(os.getenv('MAX_FILES', '0'))
EXPORT_GOOGLE_DOCS = os.getenv('EXPORT_GOOGLE_DOCS', 'true').lower() == 'true'
SKIP_SENSITIVE_NAMES = os.getenv('SKIP_SENSITIVE_NAMES', 'true').lower() == 'true'
CHUNK_SIZE = int(os.getenv('CHUNK_SIZE', str(1024 * 1024 * 8)))

SENSITIVE_NAME_PARTS = [
    'token', 'secret', 'password', 'credential', 'credentials',
    'key', 'api-key', 'apikey', 'openai-api-key', 'private_key', '.pem', '.p12', '.pfx'
]

GOOGLE_EXPORT_MAP = {
    'application/vnd.google-apps.document': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx'),
    'application/vnd.google-apps.spreadsheet': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
    'application/vnd.google-apps.presentation': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx'),
    'application/vnd.google-apps.drawing': ('application/pdf', '.pdf'),
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def sensitive_name(name):
    lower = name.lower()
    return any(part in lower for part in SENSITIVE_NAME_PARTS)

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()

def get_google_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
        scopes=['https://www.googleapis.com/auth/drive.readonly'],
    )
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

def get_box_client():
    oauth = OAuth2(
        client_id=os.environ['BOX_CLIENT_ID'],
        client_secret=os.environ['BOX_CLIENT_SECRET'],
        access_token=os.environ['BOX_ACCESS_TOKEN'],
        refresh_token=os.environ.get('BOX_REFRESH_TOKEN'),
        store_tokens=lambda access_token, refresh_token: logging.warning('Box token refreshed; update secret storage if required.'),
    )
    return Client(oauth)

def list_drive_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None
    count = 0
    while True:
        resp = service.files().list(
            q=query,
            spaces='drive',
            fields='nextPageToken, files(id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,parents,webViewLink)',
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for item in resp.get('files', []):
            yield item
            count += 1
            if MAX_FILES and count >= MAX_FILES:
                return
        page_token = resp.get('nextPageToken')
        if not page_token:
            break

def list_box_names(client, folder_id):
    existing = {}
    offset = 0
    limit = 1000
    while True:
        batch = list(client.folder(folder_id).get_items(limit=limit, offset=offset, fields=['id', 'name', 'size', 'sha1', 'type']))
        if not batch:
            break
        for item in batch:
            existing[item.name] = {
                'id': item.id,
                'name': item.name,
                'size': getattr(item, 'size', None),
                'sha1': getattr(item, 'sha1', None),
                'type': item.type,
            }
        if len(batch) < limit:
            break
        offset += limit
    return existing

def download_drive_file(service, meta):
    file_id = meta['id']
    name = meta['name']
    mime_type = meta.get('mimeType', '')

    if mime_type in GOOGLE_EXPORT_MAP:
        if not EXPORT_GOOGLE_DOCS:
            raise RuntimeError('GOOGLE_WORKSPACE_EXPORT_DISABLED')
        export_mime, ext = GOOGLE_EXPORT_MAP[mime_type]
        if not name.lower().endswith(ext):
            name = name + ext
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=CHUNK_SIZE)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            logging.info('Downloading %s: %.1f%%', name, status.progress() * 100)

    data = buffer.getvalue()
    return name, data, sha256_bytes(data)

def upload_to_box(client, folder_id, filename, data):
    return client.folder(folder_id).upload_stream(io.BytesIO(data), filename)

def main():
    os.makedirs('reports', exist_ok=True)
    report_path = f"reports/drive_to_box_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    drive = get_google_service()
    box = get_box_client()
    existing_box = list_box_names(box, BOX_DESTINATION_FOLDER_ID)
    logging.info('Existing Box destination items: %s', len(existing_box))

    fields = ['timestamp', 'status', 'drive_id', 'drive_name', 'box_name', 'box_file_id', 'drive_mime_type', 'drive_size', 'sha256', 'reason', 'drive_url']
    counters = {'copied': 0, 'skipped': 0, 'box_error': 0, 'error': 0}

    with open(report_path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()

        for meta in list_drive_files(drive, DRIVE_SOURCE_FOLDER_ID):
            base = {
                'timestamp': now_iso(),
                'drive_id': meta['id'],
                'drive_name': meta['name'],
                'box_name': '',
                'box_file_id': '',
                'drive_mime_type': meta.get('mimeType', ''),
                'drive_size': meta.get('size', ''),
                'sha256': '',
                'reason': '',
                'drive_url': meta.get('webViewLink', ''),
            }

            if SKIP_SENSITIVE_NAMES and sensitive_name(meta['name']):
                counters['skipped'] += 1
                writer.writerow({**base, 'status': 'SKIPPED', 'reason': 'SENSITIVE_NAME'})
                continue

            try:
                box_name, data, digest = download_drive_file(drive, meta)
                base['box_name'] = box_name
                base['sha256'] = digest

                if box_name in existing_box:
                    counters['skipped'] += 1
                    writer.writerow({**base, 'status': 'SKIPPED', 'reason': 'BOX_NAME_EXISTS'})
                    continue

                if DRY_RUN:
                    counters['skipped'] += 1
                    writer.writerow({**base, 'status': 'DRY_RUN', 'reason': 'NOT_UPLOADED'})
                    continue

                uploaded = None
                for attempt in range(1, 4):
                    try:
                        uploaded = upload_to_box(box, BOX_DESTINATION_FOLDER_ID, box_name, data)
                        break
                    except BoxAPIException:
                        if attempt == 3:
                            raise
                        time.sleep(2 ** attempt)

                counters['copied'] += 1
                existing_box[box_name] = {'id': uploaded.id, 'name': uploaded.name, 'size': uploaded.size}
                writer.writerow({**base, 'status': 'COPIED', 'box_file_id': uploaded.id, 'reason': 'OK'})

            except BoxAPIException as exc:
                counters['box_error'] += 1
                writer.writerow({**base, 'status': 'BOX_ERROR', 'reason': str(exc)[:500]})
            except Exception as exc:
                counters['error'] += 1
                writer.writerow({**base, 'status': 'ERROR', 'reason': str(exc)[:500]})

    logging.info('Finished migration: %s report=%s', counters, report_path)

if __name__ == '__main__':
    main()
