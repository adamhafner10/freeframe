import os
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
import uuid
from datetime import datetime, timezone
from ..database import get_db
from ..middleware.auth import get_current_user
from ..models.user import User
from ..models.asset import Asset, AssetVersion, MediaFile, AssetType, ProcessingStatus, FileType
from ..models.project import Project
from ..services.s3_service import (
    create_multipart_upload, presign_upload_part,
    complete_multipart_upload, abort_multipart_upload,
    list_multipart_parts,
)
from ..services.permissions import get_project_member, require_project_role
from ..models.project import ProjectRole
from ..schemas.upload import (
    InitiateUploadRequest, InitiateUploadResponse,
    PresignPartRequest, PresignPartResponse,
    CompleteUploadRequest, CompleteUploadResponse, AbortUploadRequest,
    ALLOWED_MIME_TYPES, MAX_FILE_SIZE_BYTES, mime_to_asset_type,
)

router = APIRouter(prefix="/upload", tags=["upload"])

@router.post("/initiate", response_model=InitiateUploadResponse)
def initiate_upload(
    body: InitiateUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate mime type
    if body.mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {body.mime_type}")
    if body.file_size_bytes > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 10GB limit")

    # Verify project access (editor or above)
    project = db.query(Project).filter(Project.id == body.project_id, Project.deleted_at.is_(None)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    require_project_role(db, body.project_id, current_user, ProjectRole.editor)

    # Get or create asset
    if body.asset_id:
        asset = db.query(Asset).filter(Asset.id == body.asset_id, Asset.deleted_at.is_(None)).first()
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        if asset.project_id != body.project_id:
            raise HTTPException(status_code=400, detail="Asset does not belong to the specified project")
    else:
        asset_type = mime_to_asset_type(body.mime_type)
        asset = Asset(
            project_id=body.project_id,
            name=body.asset_name,
            asset_type=asset_type,
            created_by=current_user.id,
            folder_id=body.folder_id,
        )
        db.add(asset)
        db.flush()

    # Get next version number
    last_version = db.query(AssetVersion).filter(
        AssetVersion.asset_id == asset.id,
        AssetVersion.deleted_at.is_(None),
    ).order_by(AssetVersion.version_number.desc()).first()
    next_version_number = (last_version.version_number + 1) if last_version else 1

    # Build S3 key: raw/{project_id}/{asset_id}/{version_id}/{filename}
    version = AssetVersion(
        asset_id=asset.id,
        version_number=next_version_number,
        processing_status=ProcessingStatus.uploading,
        created_by=current_user.id,
    )
    db.add(version)
    db.flush()

    ext = os.path.splitext(body.original_filename)[1].lower()
    s3_key = f"raw/{body.project_id}/{asset.id}/{version.id}/original{ext}"

    # Initiate S3 multipart upload
    upload_id = create_multipart_upload(s3_key, body.mime_type)

    # Create MediaFile record
    file_type_map = {AssetType.image: FileType.image, AssetType.audio: FileType.audio, AssetType.video: FileType.video, AssetType.image_carousel: FileType.image}
    media_file = MediaFile(
        version_id=version.id,
        file_type=file_type_map.get(asset.asset_type, FileType.video),
        original_filename=body.original_filename,
        mime_type=body.mime_type,
        file_size_bytes=body.file_size_bytes,
        s3_key_raw=s3_key,
    )
    db.add(media_file)
    db.commit()

    return InitiateUploadResponse(
        upload_id=upload_id,
        s3_key=s3_key,
        asset_id=asset.id,
        version_id=version.id,
    )


# --- Batch presign + resume schemas (inline; this router owns them) ---

class PresignPartsBatchRequest(BaseModel):
    s3_key: str
    upload_id: str
    part_numbers: list[int]  # 1-indexed part numbers to presign in one round-trip

class PresignedPart(BaseModel):
    part_number: int
    url: str

class PresignPartsBatchResponse(BaseModel):
    parts: list[PresignedPart]

class ListPartsRequest(BaseModel):
    s3_key: str
    upload_id: str

class ListedPart(BaseModel):
    part_number: int
    etag: str

class ListPartsResponse(BaseModel):
    parts: list[ListedPart]


def _verify_upload_owner(db: Session, s3_key: str, current_user: User) -> AssetVersion:
    """Verify the s3_key belongs to an upload initiated by this user.

    Returns the owning AssetVersion or raises 404/403.
    """
    media_file = db.query(MediaFile).filter(MediaFile.s3_key_raw == s3_key).first()
    if not media_file:
        raise HTTPException(status_code=404, detail="Upload not found")
    version = db.query(AssetVersion).filter(AssetVersion.id == media_file.version_id).first()
    if not version or version.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this upload")
    return version


@router.post("/presign-part", response_model=PresignPartResponse)
def presign_part(
    body: PresignPartRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.part_number < 1 or body.part_number > 10000:
        raise HTTPException(status_code=400, detail="Part number must be between 1 and 10000")

    _verify_upload_owner(db, body.s3_key, current_user)

    url = presign_upload_part(body.s3_key, body.upload_id, body.part_number)
    return PresignPartResponse(presigned_url=url, part_number=body.part_number)


@router.post("/presign-parts", response_model=PresignPartsBatchResponse)
def presign_parts_batch(
    body: PresignPartsBatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Presign a LIST of part numbers in a single round-trip.

    Replaces N sequential /presign-part calls (one per ~10MB chunk) with one
    request for the whole upload, so the client can fan out parallel PUTs
    without a presign latency tax per part.
    """
    if not body.part_numbers:
        raise HTTPException(status_code=400, detail="part_numbers must not be empty")
    if len(body.part_numbers) > 10000:
        raise HTTPException(status_code=400, detail="Too many parts (max 10000)")
    if any(n < 1 or n > 10000 for n in body.part_numbers):
        raise HTTPException(status_code=400, detail="Part number must be between 1 and 10000")

    _verify_upload_owner(db, body.s3_key, current_user)

    parts = [
        PresignedPart(
            part_number=n,
            url=presign_upload_part(body.s3_key, body.upload_id, n),
        )
        for n in body.part_numbers
    ]
    return PresignPartsBatchResponse(parts=parts)


@router.post("/list-parts", response_model=ListPartsResponse)
def list_parts(
    body: ListPartsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List parts already uploaded to B2/S3 for an in-progress multipart upload.

    The client calls this when RESUMING a previously-failed/interrupted upload
    so it can skip parts that already landed instead of re-PUTting them.
    """
    _verify_upload_owner(db, body.s3_key, current_user)

    parts = list_multipart_parts(body.s3_key, body.upload_id)
    return ListPartsResponse(
        parts=[ListedPart(part_number=p["PartNumber"], etag=p["ETag"]) for p in parts]
    )


@router.post("/complete", response_model=CompleteUploadResponse)
def complete_upload(
    body: CompleteUploadRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate DB first
    version = db.query(AssetVersion).filter(
        AssetVersion.id == body.version_id,
        AssetVersion.deleted_at.is_(None),
    ).first()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    if version.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this upload")

    # Then complete S3 multipart
    complete_multipart_upload(body.s3_key, body.upload_id, [p.model_dump() for p in body.parts])

    # Mark ready *immediately* — the original file is already in B2 and the asset
    # endpoint falls back to s3_key_raw when s3_key_processed is empty, so playback
    # works as soon as this commits. The HLS ladder + thumbnail are quality
    # optimizations that run in the background without blocking the user.
    version.processing_status = ProcessingStatus.ready
    db.commit()

    background_tasks.add_task(_trigger_processing, body.asset_id, body.version_id)

    return CompleteUploadResponse(status="ready", asset_id=body.asset_id, version_id=body.version_id)


def _trigger_processing(asset_id: uuid.UUID, version_id: uuid.UUID):
    """Dispatch background Celery tasks:
       - generate_thumbnail: fast single-frame extraction (~3s) — user sees poster quickly
       - process_asset: full HLS ladder for adaptive streaming (slower)
    Neither blocks playback of the original file."""
    from ..tasks.transcode_tasks import process_asset, generate_thumbnail
    from ..tasks.celery_app import send_task_safe
    send_task_safe(generate_thumbnail, str(asset_id), str(version_id))
    send_task_safe(process_asset, str(asset_id), str(version_id))


@router.post("/abort", status_code=status.HTTP_204_NO_CONTENT)
def abort_upload(
    body: AbortUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = db.query(AssetVersion).filter(
        AssetVersion.id == body.version_id,
        AssetVersion.deleted_at.is_(None),
    ).first()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    if version.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this upload")

    abort_multipart_upload(body.s3_key, body.upload_id)
    version.processing_status = ProcessingStatus.failed
    db.commit()
