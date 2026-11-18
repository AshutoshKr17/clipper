import os
import time
from pathlib import Path

import requests

YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
GRAPH = "https://graph.facebook.com/v21.0"


def _youtube_client():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    secret = Path(os.getenv("YOUTUBE_CLIENT_SECRET", "client_secret.json"))
    token = Path(os.getenv("YOUTUBE_TOKEN", "token.json"))
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), YT_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret.exists():
                raise FileNotFoundError(
                    f"{secret} not found. Download an OAuth 'Desktop app' client from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), YT_SCOPES)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def youtube_channel() -> str:
    """Log in if needed and return the connected channel's title."""
    resp = _youtube_client().channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    return items[0]["snippet"]["title"] if items else "(no channel on this account)"


def upload_youtube(
    file_path: Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "private",
    category_id: str = "20",
    publish_at: str | None = None,
) -> str:
    """Upload a Short. publish_at is ISO 8601 (e.g. 2026-11-20T18:00:00Z) and forces private until then."""
    from googleapiclient.http import MediaFileUpload

    youtube = _youtube_client()
    tags = tags or []
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tags[:5])
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at)
    body = {
        "snippet": {
            "title": f"{title} #Shorts"[:100],
            "description": f"{description}\n\n#Shorts {hashtags}".strip(),
            "tags": ["shorts", *tags][:30],
            "categoryId": category_id,
        },
        "status": status,
    }
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        progress, response = request.next_chunk()
        if progress:
            print(f"\r[youtube] uploading {progress.progress() * 100:.0f}%", end="", flush=True)
    print(f"\n[youtube] uploaded: https://youtube.com/shorts/{response['id']}")
    return response["id"]


def instagram_caption(meta: dict) -> str:
    tags = " ".join(f"#{t.replace(' ', '')}" for t in meta.get("tags", [])[:8])
    return f"{meta['title']}\n\n{meta.get('description', '')}\n\n{tags} #reels".strip()


def upload_instagram(file_path: Path, caption: str, share_to_feed: bool = True) -> str:
    """Publish a Reel via the Instagram Graph API (Business/Creator account linked to a FB Page)."""
    token = os.environ["IG_ACCESS_TOKEN"]
    user_id = os.environ["IG_USER_ID"]

    container = requests.post(
        f"{GRAPH}/{user_id}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption[:2200],
            "share_to_feed": str(share_to_feed).lower(),
            "access_token": token,
        },
        timeout=60,
    )
    container.raise_for_status()
    container_id = container.json()["id"]

    size = file_path.stat().st_size
    with open(file_path, "rb") as f:
        up = requests.post(
            f"https://rupload.facebook.com/ig-api-upload/v21.0/{container_id}",
            headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
            data=f,
            timeout=600,
        )
    up.raise_for_status()

    for _ in range(60):
        status = requests.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        ).json()
        code = status.get("status_code")
        print(f"[instagram] processing: {code}")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"Instagram processing failed: {status}")
        time.sleep(10)
    else:
        raise TimeoutError("Instagram processing timed out")

    pub = requests.post(
        f"{GRAPH}/{user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=60,
    )
    pub.raise_for_status()
    media_id = pub.json()["id"]
    print(f"[instagram] published reel: {media_id}")
    return media_id
