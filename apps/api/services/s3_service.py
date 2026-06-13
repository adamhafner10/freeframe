import boto3
from functools import lru_cache
from botocore.exceptions import ClientError
from ..config import settings

# S3 Content-Type and Cache-Control mappings
CONTENT_TYPE_MAP = {
    ".m3u8": ("application/vnd.apple.mpegurl", "no-cache"),
    ".ts": ("video/mp2t", "max-age=31536000"),
    ".jpg": ("image/jpeg", "max-age=86400"),
    ".jpeg": ("image/jpeg", "max-age=86400"),
    ".webp": ("image/webp", "max-age=86400"),
    ".mp3": ("audio/mpeg", "max-age=86400"),
    ".json": ("application/json", "max-age=86400"),
    ".png": ("image/png", "max-age=86400"),
}

def _is_aws_s3() -> bool:
    """Check if using AWS S3 (vs MinIO/local). Controlled by S3_STORAGE env var."""
    return settings.s3_storage.lower() == "s3"

@lru_cache(maxsize=1)
def get_s3_client():
    """
    Create S3 client. Auto-detects AWS vs MinIO:
    - If access_key starts with 'AKIA' -> use AWS S3 (no endpoint_url)
    - Otherwise -> use custom endpoint (MinIO or S3-compatible)

    Memoized: boto3 clients are thread-safe for service calls, and building one
    costs ~3ms — a real tax on the hot presign path. lru_cache (itself
    thread-safe) returns a single shared client for the process lifetime.
    Credentials/region are read from settings at first call and don't change at
    runtime, so a process-lifetime singleton is correct here.
    """
    if _is_aws_s3():
        # Real AWS S3 - don't pass endpoint_url
        return boto3.client(
            "s3",
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
    else:
        # MinIO or S3-compatible storage
        return boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )

@lru_cache(maxsize=1)
def _get_presign_client():
    """
    Client for generating presigned URLs. Uses s3_public_endpoint if set,
    so presigned URLs are accessible from the browser (e.g. localhost:9000
    instead of minio:9000 in Docker).

    Memoized for the same reason as get_s3_client(): presigning is the hottest
    server-side path during a multipart upload (one call per part), so we build
    the client once and reuse it.
    """
    endpoint = settings.s3_public_endpoint or (None if _is_aws_s3() else settings.s3_endpoint)
    kwargs = {
        "aws_access_key_id": settings.s3_access_key,
        "aws_secret_access_key": settings.s3_secret_key,
        "region_name": settings.s3_region,
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)

def ensure_bucket_exists():
    """Create the S3 bucket if it does not exist. Called on app startup."""
    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("404", "NoSuchBucket"):
            # For AWS S3 in non-us-east-1 regions, need LocationConstraint
            if _is_aws_s3() and settings.s3_region != "us-east-1":
                s3.create_bucket(
                    Bucket=settings.s3_bucket,
                    CreateBucketConfiguration={"LocationConstraint": settings.s3_region}
                )
            else:
                s3.create_bucket(Bucket=settings.s3_bucket)
        elif error_code == "403":
            # Bucket exists but we don't have access, or using wrong credentials
            # For AWS S3, bucket likely already exists - skip creation
            if _is_aws_s3():
                pass  # Assume bucket exists, will fail on actual operations if not
            else:
                raise
        else:
            raise

    # Set CORS for browser-based uploads (presigned PUT)
    if not _is_aws_s3():
        try:
            s3.put_bucket_cors(
                Bucket=settings.s3_bucket,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedHeaders": ["*"],
                            "AllowedMethods": ["GET", "PUT", "POST", "DELETE", "HEAD"],
                            "AllowedOrigins": [settings.frontend_url, "http://localhost:3000"],
                            "ExposeHeaders": ["ETag", "Content-Length", "x-amz-request-id"],
                            "MaxAgeSeconds": 3600,
                        }
                    ]
                },
            )
        except ClientError:
            pass  # CORS config failed, non-critical

        # NOTE: bucket stays fully private — no public-read policy. processed/ HLS
        # segments + thumbnails are served only via the HLS proxy + presigned URLs.


def get_content_type(key: str) -> tuple[str, str]:
    """Return (content_type, cache_control) for a given S3 key."""
    import os
    ext = os.path.splitext(key)[1].lower()
    return CONTENT_TYPE_MAP.get(ext, ("application/octet-stream", "no-cache"))

def create_multipart_upload(s3_key: str, content_type: str) -> str:
    """Initiate a multipart upload and return the upload_id."""
    s3 = get_s3_client()
    response = s3.create_multipart_upload(
        Bucket=settings.s3_bucket,
        Key=s3_key,
        ContentType=content_type,
    )
    return response["UploadId"]

def presign_upload_part(s3_key: str, upload_id: str, part_number: int, expires_in: int = 3600) -> str:
    """Return a presigned URL for uploading a single part."""
    s3 = _get_presign_client()
    return s3.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": s3_key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expires_in,
    )

def list_multipart_parts(s3_key: str, upload_id: str) -> list[dict]:
    """List parts already uploaded for an in-progress multipart upload.

    Returns a list of {"PartNumber": int, "ETag": str} for every part B2/S3 has
    received. Used to RESUME an interrupted upload: the client skips parts that
    already landed instead of re-PUTting gigabytes. Paginated (ListParts caps at
    1000 parts per page). Returns [] if the upload no longer exists.
    """
    s3 = get_s3_client()
    parts: list[dict] = []
    marker = 0
    while True:
        try:
            resp = s3.list_parts(
                Bucket=settings.s3_bucket,
                Key=s3_key,
                UploadId=upload_id,
                PartNumberMarker=marker,
            )
        except ClientError as e:
            # NoSuchUpload (expired/aborted) — nothing to resume.
            if e.response["Error"]["Code"] in ("NoSuchUpload", "404"):
                return []
            raise
        for p in resp.get("Parts", []):
            parts.append({"PartNumber": p["PartNumber"], "ETag": p["ETag"]})
        if resp.get("IsTruncated"):
            marker = resp["NextPartNumberMarker"]
        else:
            break
    return parts

def complete_multipart_upload(s3_key: str, upload_id: str, parts: list[dict]) -> None:
    """Complete a multipart upload. `parts` is a list of {"PartNumber": int, "ETag": str}."""
    s3 = get_s3_client()
    s3.complete_multipart_upload(
        Bucket=settings.s3_bucket,
        Key=s3_key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )

def abort_multipart_upload(s3_key: str, upload_id: str) -> None:
    """Abort a multipart upload and clean up uploaded parts."""
    s3 = get_s3_client()
    s3.abort_multipart_upload(
        Bucket=settings.s3_bucket,
        Key=s3_key,
        UploadId=upload_id,
    )

def generate_presigned_get_url(s3_key: str, expires_in: int = 3600) -> str:
    """Generate a presigned GET URL for an object."""
    s3 = _get_presign_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": s3_key},
        ExpiresIn=expires_in,
    )

def put_object(s3_key: str, body: bytes, content_type: str | None = None, cache_control: str | None = None) -> None:
    """Upload a small object directly (for processed files like thumbnails)."""
    s3 = get_s3_client()
    kwargs = {"Bucket": settings.s3_bucket, "Key": s3_key, "Body": body}
    if content_type:
        kwargs["ContentType"] = content_type
    if cache_control:
        kwargs["CacheControl"] = cache_control
    s3.put_object(**kwargs)

def delete_object(s3_key: str) -> None:
    s3 = get_s3_client()
    s3.delete_object(Bucket=settings.s3_bucket, Key=s3_key)

def delete_prefix(prefix: str) -> int:
    """Delete every object under a prefix. Paginated, idempotent, returns count deleted.

    Safe to re-run: a prefix with no objects deletes nothing and returns 0.
    """
    if not prefix:
        return 0
    s3 = get_s3_client()
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not objects:
            continue
        # delete_objects caps at 1000 keys per call; paginator pages are <=1000.
        s3.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": objects})
        deleted += len(objects)
    return deleted
