from .celery_app import celery_app
from ..database import SessionLocal
from ..models.asset import Asset, AssetVersion, ProcessingStatus
from ..models.activity import Notification, NotificationType
from datetime import datetime, timezone, timedelta


@celery_app.task(name="cleanup_stuck_uploads")
def cleanup_stuck_uploads():
    """Mark as failed any AssetVersion that's been in `uploading` state for >30 min.

    Reasons a version gets stuck `uploading`:
    - User refreshed/closed the tab mid-upload (the /upload/abort cleanup never ran)
    - Network died between the last part PUT and POST /upload/complete
    - B2 presigned URL expired mid-upload

    Frame.io-style UX: rather than leaving the row in silent limbo, flip it to
    `failed` so the UploadsPanel shows it clearly with a Retry button.
    """
    with SessionLocal() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        stuck = db.query(AssetVersion).filter(
            AssetVersion.processing_status == ProcessingStatus.uploading,
            AssetVersion.created_at < cutoff,
            AssetVersion.deleted_at.is_(None),
        ).all()
        for v in stuck:
            v.processing_status = ProcessingStatus.failed
        if stuck:
            db.commit()
        return {"cleaned": len(stuck)}


@celery_app.task(name="send_due_date_reminders")
def send_due_date_reminders():
    """Send notifications for assets due within 24 hours."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=24)
        assets = db.query(Asset).filter(
            Asset.due_date >= now,
            Asset.due_date <= window_end,
            Asset.assignee_id.isnot(None),
            Asset.deleted_at.is_(None),
        ).all()
        for asset in assets:
            # Avoid duplicate reminders: check if one was sent in the last hour
            recent = db.query(Notification).filter(
                Notification.user_id == asset.assignee_id,
                Notification.type == NotificationType.due_soon,
                Notification.asset_id == asset.id,
                Notification.created_at >= now - timedelta(hours=1),
            ).first()
            if not recent:
                db.add(Notification(
                    user_id=asset.assignee_id,
                    type=NotificationType.due_soon,
                    asset_id=asset.id,
                ))
        db.commit()
    finally:
        db.close()
