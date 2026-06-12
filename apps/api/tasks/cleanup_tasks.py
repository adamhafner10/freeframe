from datetime import datetime, timezone, timedelta

from .celery_app import celery_app
from ..database import SessionLocal
from ..models.asset import Asset, AssetVersion, MediaFile, ProcessingStatus, HLSStatus
from ..services.s3_service import delete_object, delete_prefix

# How long a soft-deleted row sits before storage is reaped and the row is hard-deleted.
GRACE_DAYS = 7

# A version whose raw is ready but whose HLS is still pending/failed/processing past
# this window is presumed orphaned by a crashed worker and gets re-enqueued.
HLS_SELFHEAL_MINUTES = 20


@celery_app.task(name="reap_deleted_assets")
def reap_deleted_assets():
    """Hard-delete soft-deleted asset versions + their S3 objects after a grace window.

    A version is eligible when its own `deleted_at` is past the grace window, OR its
    parent asset was soft-deleted past the grace window. For each eligible version we
    delete the raw key, the processed HLS prefix, and the thumbnail from storage, then
    hard-delete the MediaFile + AssetVersion rows. Assets whose versions are all gone
    are hard-deleted too.

    Safe to re-run: S3 deletes are idempotent (missing keys = no-op), and rows that
    were already reaped on a prior run simply aren't found. Each version is wrapped in
    its own try/except so a single S3/DB failure doesn't abort the whole batch. Fresh
    sessions are used per phase and never held across S3 calls.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=GRACE_DAYS)

    # 1. Collect eligible version ids + their S3 keys with a short-lived read session.
    #    Don't hold this session across the S3 deletes below.
    targets = []  # list of dicts: version_id, asset_id, project_id, raw/processed/thumb keys
    with SessionLocal() as db:
        # Versions soft-deleted directly, or whose parent asset was soft-deleted.
        rows = (
            db.query(AssetVersion, Asset)
            .join(Asset, Asset.id == AssetVersion.asset_id)
            .filter(
                ((AssetVersion.deleted_at.isnot(None)) & (AssetVersion.deleted_at < cutoff))
                | ((Asset.deleted_at.isnot(None)) & (Asset.deleted_at < cutoff))
            )
            .all()
        )
        for version, asset in rows:
            media_files = (
                db.query(MediaFile).filter(MediaFile.version_id == version.id).all()
            )
            targets.append({
                "version_id": version.id,
                "asset_id": str(asset.id),
                "project_id": str(asset.project_id),
                "media": [
                    {
                        "id": mf.id,
                        "s3_key_raw": mf.s3_key_raw,
                        "s3_key_thumbnail": mf.s3_key_thumbnail,
                    }
                    for mf in media_files
                ],
            })

    reaped_versions = 0
    deleted_objects = 0

    # 2. Per version: delete S3 objects (no session held), then hard-delete rows.
    for t in targets:
        try:
            version_id = str(t["version_id"])
            asset_id = t["asset_id"]
            project_id = t["project_id"]

            # processed HLS prefix is built the same way transcode_tasks does:
            #   processed/{project_id}/{asset_id}/{version_id}
            deleted_objects += delete_prefix(
                f"processed/{project_id}/{asset_id}/{version_id}"
            )
            # NOTE: real thumbnails live UNDER processed/{project_id}/{asset_id}/{version_id}/
            # which the prefix delete above already covers, so no separate thumbnail
            # prefix delete is needed.

            for mf in t["media"]:
                if mf["s3_key_raw"]:
                    delete_object(mf["s3_key_raw"])
                    deleted_objects += 1
                if mf["s3_key_thumbnail"]:
                    delete_object(mf["s3_key_thumbnail"])
                    deleted_objects += 1

            # Hard-delete the rows in a fresh session, after storage is gone.
            with SessionLocal() as db:
                db.query(MediaFile).filter(
                    MediaFile.version_id == t["version_id"]
                ).delete(synchronize_session=False)
                db.query(AssetVersion).filter(
                    AssetVersion.id == t["version_id"]
                ).delete(synchronize_session=False)
                db.commit()
            reaped_versions += 1
        except Exception:
            # One bad asset shouldn't stop the batch; it'll be retried next run.
            continue

    # 3. Hard-delete soft-deleted assets that no longer have any versions.
    reaped_assets = 0
    with SessionLocal() as db:
        dead_assets = (
            db.query(Asset)
            .filter(Asset.deleted_at.isnot(None), Asset.deleted_at < cutoff)
            .all()
        )
        for asset in dead_assets:
            try:
                remaining = (
                    db.query(AssetVersion)
                    .filter(AssetVersion.asset_id == asset.id)
                    .count()
                )
                if remaining == 0:
                    db.delete(asset)
                    reaped_assets += 1
            except Exception:
                continue
        if reaped_assets:
            db.commit()

    return {
        "reaped_versions": reaped_versions,
        "reaped_assets": reaped_assets,
        "deleted_objects": deleted_objects,
    }


@celery_app.task(name="reconcile_hls_transcodes")
def reconcile_hls_transcodes():
    """Self-heal: re-enqueue HLS transcodes that never finished.

    A worker that crashes mid-transcode leaves a version with processing_status=ready
    (raw is playable) but hls_status stuck at pending/processing/failed. This beat
    finds those that are older than HLS_SELFHEAL_MINUTES and re-dispatches
    process_asset for them. process_asset is idempotent — if the HLS output already
    exists it simply reconciles hls_status to ready and returns, so this is safe to
    run on a loop and never duplicates work.

    Fresh session for the read; never holds a session across the dispatch.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=HLS_SELFHEAL_MINUTES)

    targets = []  # list of (asset_id, version_id)
    with SessionLocal() as db:
        rows = (
            db.query(AssetVersion)
            .filter(
                AssetVersion.deleted_at.is_(None),
                AssetVersion.processing_status == ProcessingStatus.ready,
                AssetVersion.hls_status.in_(
                    [HLSStatus.pending, HLSStatus.processing, HLSStatus.failed]
                ),
                AssetVersion.created_at < cutoff,
            )
            .all()
        )
        targets = [(str(v.asset_id), str(v.id)) for v in rows]

    if not targets:
        return {"requeued": 0}

    # Import locally to avoid a circular import at module load.
    from .transcode_tasks import process_asset
    from .celery_app import send_task_safe

    requeued = 0
    for asset_id, version_id in targets:
        try:
            send_task_safe(process_asset, asset_id, version_id)
            requeued += 1
        except Exception:
            continue

    return {"requeued": requeued}
