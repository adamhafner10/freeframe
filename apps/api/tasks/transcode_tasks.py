import uuid
import sys
import os
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

# Ensure the workspace root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from .celery_app import celery_app
from ..database import SessionLocal
from ..models.asset import AssetVersion, MediaFile, ProcessingStatus, AssetType, FileType
from ..models.asset import Asset
from ..services.s3_service import get_s3_client
from ..config import settings


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _set_failed(version_id: uuid.UUID) -> None:
    """Fresh-session DB write to mark a version failed. Resilient to stale pool connections."""
    with SessionLocal() as db:
        v = db.query(AssetVersion).filter(AssetVersion.id == version_id).first()
        if v:
            v.processing_status = ProcessingStatus.failed
            db.commit()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_asset(self, asset_id: str, version_id: str):
    """Full HLS transcode. Runs in the background AFTER upload is already marked ready —
    this is a streaming-optimization pass, not a prerequisite for playback.

    Uses fresh DB sessions at each write boundary so long ffmpeg runs don't hold a
    stale pool connection through the whole task.
    """
    asset_uuid = uuid.UUID(asset_id)
    version_uuid = uuid.UUID(version_id)

    # 1. Read inputs with a short-lived session
    with SessionLocal() as db:
        version = db.query(AssetVersion).filter(AssetVersion.id == version_uuid).first()
        if not version:
            return  # version already cleaned up
        asset = db.query(Asset).filter(Asset.id == asset_uuid).first()
        if not asset:
            _set_failed(version_uuid)
            return
        media_file = db.query(MediaFile).filter(MediaFile.version_id == version.id).first()
        if not media_file:
            _set_failed(version_uuid)
            return
        project_id = str(asset.project_id)
        asset_type = asset.asset_type
        mf_id = media_file.id
        # Nothing else needed from this session; close it before the long ffmpeg run
        # so we don't pin a pool connection that Neon might recycle under us.

    output_prefix = f"processed/{project_id}/{asset_id}/{version_id}"
    s3 = get_s3_client()

    # 2. Long operation — ffmpeg + uploads. No DB session held.
    try:
        result = _run_processing(asset_type, mf_id, output_prefix, s3)
    except Exception as exc:
        _set_failed(version_uuid)
        _publish_event(project_id, "transcode_failed", {
            "asset_id": asset_id,
            "error": str(exc),
        })
        raise self.retry(exc=exc)

    # 3. Commit success in a fresh session
    with SessionLocal() as db:
        mf = db.query(MediaFile).filter(MediaFile.id == mf_id).first()
        if mf:
            mf.s3_key_processed = result.get("hls_prefix")
            thumb = result.get("thumbnail_key")
            if thumb and not mf.s3_key_thumbnail:
                mf.s3_key_thumbnail = thumb
        # Keep status=ready (upload flow already sets this). We don't regress it here
        # even if it was somehow failed — fresh transcode success means we're good.
        v = db.query(AssetVersion).filter(AssetVersion.id == version_uuid).first()
        if v and v.processing_status != ProcessingStatus.ready:
            v.processing_status = ProcessingStatus.ready
        db.commit()

    _publish_event(project_id, "transcode_complete", {
        "asset_id": asset_id,
        "version_id": version_id,
    })


def _run_processing(asset_type: AssetType, mf_id: uuid.UUID, output_prefix: str, s3) -> dict:
    """Dispatches to the right processor. Returns a dict with hls_prefix/thumbnail_key."""
    # Re-fetch in a short session to get the raw key (fresh connection)
    with SessionLocal() as db:
        mf = db.query(MediaFile).filter(MediaFile.id == mf_id).first()
        if not mf:
            raise RuntimeError("media file vanished before processing")
        s3_key_raw = mf.s3_key_raw

    if asset_type == AssetType.video:
        return _process_video(s3_key_raw, output_prefix, s3)
    elif asset_type == AssetType.audio:
        return _process_audio(s3_key_raw, output_prefix, s3)
    elif asset_type in (AssetType.image, AssetType.image_carousel):
        return _process_image(s3_key_raw, output_prefix, s3)
    else:
        raise RuntimeError(f"unknown asset type: {asset_type}")


def _process_video(s3_key_raw: str, output_prefix: str, s3) -> dict:
    from packages.transcoder.ffmpeg_transcoder import FFmpegTranscoder
    from packages.transcoder.base import TranscodeJob

    transcoder = FFmpegTranscoder(s3, settings.s3_bucket, settings.s3_endpoint)
    job = TranscodeJob(
        media_id="",  # unused
        version_id=output_prefix.split("/")[-1],
        input_s3_key=s3_key_raw,
        output_s3_prefix=output_prefix,
        qualities=["1080p", "720p", "360p"],
    )
    result = _run_async(transcoder.transcode(job))
    if not result.success:
        raise RuntimeError(f"Transcode failed: {result.error}")
    return {
        "hls_prefix": result.hls_prefix,
        "thumbnail_key": result.thumbnail_keys[0] if result.thumbnail_keys else None,
    }


def _process_audio(s3_key_raw: str, output_prefix: str, s3) -> dict:
    from packages.transcoder.image_processor import process_audio
    result = process_audio(s3, settings.s3_bucket, s3_key_raw, output_prefix)
    return {
        "hls_prefix": result.get("mp3_key"),
        "thumbnail_key": result.get("waveform_key"),
    }


def _process_image(s3_key_raw: str, output_prefix: str, s3) -> dict:
    from packages.transcoder.image_processor import process_image
    result = process_image(s3, settings.s3_bucket, s3_key_raw, output_prefix)
    return {
        "hls_prefix": result.get("webp_key"),
        "thumbnail_key": result.get("thumbnail_key"),
    }


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def generate_thumbnail(self, asset_id: str, version_id: str):
    """Extract a single preview frame as fast as possible (~2-3s).
    Decoupled from full HLS transcode so users see a poster/preview image right after upload,
    long before the streaming ladder finishes."""
    version_uuid = uuid.UUID(version_id)

    with SessionLocal() as db:
        mf = db.query(MediaFile).filter(MediaFile.version_id == version_uuid).first()
        if not mf:
            return
        s3_key_raw = mf.s3_key_raw
        file_type = mf.file_type
        mf_id = mf.id
        if mf.s3_key_thumbnail:
            return  # already has one

    if file_type != FileType.video:
        return  # audio/image thumbnails handled by full-processing path

    s3 = get_s3_client()
    input_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": s3_key_raw},
        ExpiresIn=3600,
    )
    work_dir = Path(tempfile.mkdtemp(prefix=f"thumb_{version_id}_"))
    try:
        thumb = work_dir / "thumb.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "1", "-i", input_url,
                "-frames:v", "1",
                "-vf", "scale=640:-2",
                "-q:v", "2",
                str(thumb),
            ],
            check=True, capture_output=True, timeout=90,
        )
        thumbnail_key = f"processed/thumbnails/{asset_id}/{version_id}.jpg"
        s3.upload_file(
            str(thumb), settings.s3_bucket, thumbnail_key,
            ExtraArgs={"ContentType": "image/jpeg", "CacheControl": "max-age=86400"},
        )
        with SessionLocal() as db:
            mf = db.query(MediaFile).filter(MediaFile.id == mf_id).first()
            if mf and not mf.s3_key_thumbnail:
                mf.s3_key_thumbnail = thumbnail_key
                db.commit()
    except subprocess.CalledProcessError as exc:
        # Likely a weird input file — don't keep retrying, just skip thumbnail.
        # Full transcode task has its own retry chain for the real file.
        return
    except Exception as exc:
        self.retry(exc=exc)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _publish_event(project_id: str, event_type: str, payload: dict):
    """Publish SSE event via Redis from Celery worker context."""
    try:
        import redis as sync_redis
        r = sync_redis.from_url(settings.redis_url, decode_responses=True)
        message = json.dumps({"type": event_type, "payload": payload})
        r.publish(f"project:{project_id}", message)
        r.close()
    except Exception:
        pass  # SSE publish is best-effort
