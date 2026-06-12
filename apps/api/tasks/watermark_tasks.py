import uuid
import tempfile
import os
import subprocess
import json
import sys

# Ensure the workspace root is on the path (same pattern as transcode_tasks)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from .celery_app import celery_app
from ..database import SessionLocal
from ..models.asset import Asset, MediaFile, AssetVersion
from ..config import settings


def _publish_event(project_id: str, event_type: str, payload: dict):
    """Publish SSE event via Redis from Celery worker context (best-effort)."""
    try:
        import redis as sync_redis
        r = sync_redis.from_url(settings.redis_url, decode_responses=True)
        message = json.dumps({"type": event_type, "payload": payload})
        r.publish(f"project:{project_id}", message)
        r.close()
    except Exception:
        pass


def _watermarked_exists(wm_key: str, s3) -> bool:
    """True if the burned-watermark output already exists in S3.

    Makes re-enqueues (share create/update, retry) a safe no-op rather than
    re-running a ~600s ffmpeg pass + re-upload.
    """
    if not wm_key:
        return False
    try:
        s3.head_object(Bucket=settings.s3_bucket, Key=wm_key)
        return True
    except Exception:
        return False


@celery_app.task(name="apply_watermark", bind=True, max_retries=3, default_retry_delay=60)
def apply_watermark(
    self,
    asset_id: str,
    watermark_text: str,
    position: str,
    opacity: float,
    image_key: str | None,
):
    """Burn a text watermark into a video/image asset, upload the result to S3,
    and persist its key on the media file.

    Uses fresh DB sessions at each boundary: inputs are read into locals and the
    session is CLOSED before the long ffmpeg run, so a recycled pool connection
    (pool_recycle) can't break the final commit. A separate fresh session persists
    the result. Mirrors the pattern in transcode_tasks.process_asset.
    """
    from ..services.s3_service import get_s3_client, put_object

    asset_uuid = uuid.UUID(asset_id)

    s3 = get_s3_client()

    # 1. Read inputs with a short-lived session, then close it before ffmpeg.
    with SessionLocal() as db:
        asset = db.query(Asset).filter(
            Asset.id == asset_uuid,
            Asset.deleted_at.is_(None),
        ).first()
        if not asset:
            return

        # Find the first media file for this asset (via latest version)
        latest_version = (
            db.query(AssetVersion)
            .filter(
                AssetVersion.asset_id == asset.id,
                AssetVersion.deleted_at.is_(None),
            )
            .order_by(AssetVersion.version_number.desc())
            .first()
        )
        if not latest_version:
            return

        source = db.query(MediaFile).filter(
            MediaFile.version_id == latest_version.id
        ).first()
        if not source:
            return

        project_id = str(asset.project_id)
        version_id = str(latest_version.id)
        mf_id = source.id
        s3_key_raw = source.s3_key_raw
        original_filename = source.original_filename
        existing_watermarked = source.s3_key_watermarked

    output_ext = ".mp4"
    # Stable, version-scoped key so a new version (or a re-run) lands predictably.
    wm_key = f"watermarked/{project_id}/{asset_id}/{version_id}/output{output_ext}"

    # 2. IDEMPOTENCY: if the burned output already exists in S3, this is a no-op.
    #    Reconcile the DB pointer if it drifted, then return without re-encoding.
    if _watermarked_exists(wm_key, s3):
        if existing_watermarked != wm_key:
            with SessionLocal() as db:
                mf = db.query(MediaFile).filter(MediaFile.id == mf_id).first()
                if mf:
                    mf.s3_key_watermarked = wm_key
                    db.commit()
        return

    # 3. Long operation — download + ffmpeg + upload. NO DB session held.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Determine file extension from original filename
            _, ext = os.path.splitext(original_filename)
            ext = ext.lower() or ".mp4"
            local_path = os.path.join(tmp, f"source{ext}")

            # Download source from S3
            s3.download_file(settings.s3_bucket, s3_key_raw, local_path)

            output_path = os.path.join(tmp, f"watermarked_{asset_id}{output_ext}")

            # Build ffmpeg drawtext filter if we have watermark text
            vf_filters = []
            if watermark_text:
                escaped = watermark_text.replace("'", r"'\''").replace(":", r"\:")
                fontsize = 24
                if position == "center":
                    x, y = "(w-text_w)/2", "(h-text_h)/2"
                elif position == "tiled":
                    x, y = "w/4", "h/4"
                else:  # corner / bottom_right
                    x, y = "w-text_w-10", "h-text_h-10"
                vf_filters.append(
                    f"drawtext=text='{escaped}':fontsize={fontsize}"
                    f":fontcolor=white@{opacity}:x={x}:y={y}"
                )

            if vf_filters:
                cmd = [
                    "ffmpeg", "-y",
                    "-i", local_path,
                    "-vf", ",".join(vf_filters),
                    "-c:a", "copy",
                    output_path,
                ]
                subprocess.run(cmd, check=True, timeout=600)
            else:
                # No watermark text — copy as-is so the share still serves a file
                # under the watermarked key (consistent fallback for show_watermark).
                output_path = local_path

            # Upload watermarked file back to S3
            with open(output_path, "rb") as f:
                put_object(wm_key, f.read(), "video/mp4")
    except Exception as exc:
        raise self.retry(exc=exc)

    # 4. Persist the burned key in a FRESH session (idempotent — only set if unset
    #    or drifted). Never holds a connection across the ffmpeg run.
    with SessionLocal() as db:
        mf = db.query(MediaFile).filter(MediaFile.id == mf_id).first()
        if mf and mf.s3_key_watermarked != wm_key:
            mf.s3_key_watermarked = wm_key
            db.commit()

    # Publish SSE event (best-effort)
    _publish_event(
        project_id,
        "watermark_complete",
        {"asset_id": asset_id, "key": wm_key},
    )
