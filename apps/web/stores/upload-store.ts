import { create, type StateCreator } from 'zustand'
import { persist } from 'zustand/middleware'
import { api } from '@/lib/api'
import type { AssetResponse } from '@/types'

// Part size for multipart upload. Bigger parts = fewer parts = fewer round-trips
// (presign + PUT) per upload. B2/S3 cap multipart at 10000 parts, so 64MB keeps
// even a 10GB file (~160 parts) well under the limit. MUST stay >= 5MB (S3 min
// part size for non-final parts).
const CHUNK_SIZE = 64 * 1024 * 1024 // 64 MB
// Number of parts uploaded in parallel. Drains a shared worklist of part numbers.
const UPLOAD_CONCURRENCY = 5
// Per-part PUT retry policy (exponential backoff). A single transient failure no
// longer aborts the whole upload.
const PART_MAX_ATTEMPTS = 4
const PART_RETRY_BASE_MS = 500
const HISTORY_PAGE_SIZE = 20

export type UploadStatus = 'pending' | 'uploading' | 'processing' | 'complete' | 'failed' | 'cancelled'

export interface UploadFile {
  id: string
  fileName: string
  fileSize: number
  fileType: string
  projectId: string
  projectName?: string
  assetName: string
  folderId?: string | null
  progress: number
  processingProgress: number
  status: UploadStatus
  error?: string
  assetId?: string
  versionId?: string
  uploadId?: string
  s3Key?: string
  createdAt: number // timestamp for grouping
}

interface InitiateResponse {
  upload_id: string
  s3_key: string
  asset_id: string
  version_id: string
}

interface VersionInitiateResponse {
  upload_id: string
  s3_key: string
  asset_id: string
  version_id: string
}

// AbortControllers for cancellation
const abortControllers: Record<string, AbortController> = {}

interface CompletedPart {
  PartNumber: number
  ETag: string
}

interface BatchPresignResponse {
  parts: Array<{ part_number: number; url: string }>
}

interface ListPartsResponse {
  parts: Array<{ part_number: number; etag: string }>
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/**
 * Upload all parts of a file to B2/S3 with bounded concurrency, per-part retry,
 * and resume. Shared by the new-asset and new-version paths.
 *
 *  - Presigns every part up front in ONE batch request (no per-part round-trip).
 *  - Calls list-parts to skip parts that already landed (resume an interrupted
 *    upload instead of re-PUTting gigabytes).
 *  - Runs UPLOAD_CONCURRENCY workers draining a shared part-number worklist.
 *  - Each part PUT retries with exponential backoff (PART_MAX_ATTEMPTS).
 *  - A single AbortController (signal) aborts ALL in-flight parts on cancel.
 *
 * Returns the full {PartNumber, ETag} list (already-uploaded + freshly uploaded)
 * sorted by part number, ready for /upload/complete.
 */
async function uploadParts(opts: {
  file: File
  s3Key: string
  uploadId: string
  signal: AbortSignal
  onProgress: (uploadedBytes: number) => void
}): Promise<CompletedPart[]> {
  const { file, s3Key, uploadId, signal, onProgress } = opts
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE)

  // 1. Resume: find parts already uploaded so we can skip them.
  const etagByPart = new Map<number, string>()
  try {
    const listed = await api.post<ListPartsResponse>('/upload/list-parts', {
      s3_key: s3Key,
      upload_id: uploadId,
    })
    for (const p of listed.parts) etagByPart.set(p.part_number, p.etag)
  } catch {
    // No resumable state (fresh upload, expired upload, or endpoint hiccup) —
    // proceed as if nothing was uploaded yet.
  }

  const pending: number[] = []
  for (let partNumber = 1; partNumber <= totalChunks; partNumber++) {
    if (!etagByPart.has(partNumber)) pending.push(partNumber)
  }

  // 2. Batch-presign all pending parts in a single round-trip.
  const urlByPart = new Map<number, string>()
  if (pending.length > 0) {
    const presigned = await api.post<BatchPresignResponse>('/upload/presign-parts', {
      s3_key: s3Key,
      upload_id: uploadId,
      part_numbers: pending,
    })
    for (const p of presigned.parts) urlByPart.set(p.part_number, p.url)
  }

  // 3. Track per-part uploaded bytes for accurate aggregate progress. Seed with
  //    already-uploaded parts so a resumed upload doesn't start the bar at 0.
  const bytesForPart = (partNumber: number) =>
    Math.min(partNumber * CHUNK_SIZE, file.size) - (partNumber - 1) * CHUNK_SIZE
  const uploadedBytesByPart = new Map<number, number>()
  Array.from(etagByPart.keys()).forEach((partNumber) => {
    uploadedBytesByPart.set(partNumber, bytesForPart(partNumber))
  })
  const reportProgress = () => {
    let sum = 0
    Array.from(uploadedBytesByPart.values()).forEach((b) => {
      sum += b
    })
    onProgress(sum)
  }
  reportProgress()

  // 4. Upload one part with retry + exponential backoff.
  const putPart = async (partNumber: number): Promise<string> => {
    const start = (partNumber - 1) * CHUNK_SIZE
    const end = Math.min(start + CHUNK_SIZE, file.size)
    const chunk = file.slice(start, end)
    const url = urlByPart.get(partNumber)
    if (!url) throw new Error(`No presigned URL for part ${partNumber}`)

    let lastErr: unknown
    for (let attempt = 1; attempt <= PART_MAX_ATTEMPTS; attempt++) {
      if (signal.aborted) throw new DOMException('Upload cancelled', 'AbortError')
      try {
        const res = await fetch(url, { method: 'PUT', body: chunk, signal })
        if (!res.ok) throw new Error(`Part ${partNumber} failed: ${res.statusText}`)
        return res.headers.get('ETag') ?? ''
      } catch (err) {
        // Cancellation is terminal — never retry an aborted PUT.
        if (signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
          throw new DOMException('Upload cancelled', 'AbortError')
        }
        lastErr = err
        if (attempt < PART_MAX_ATTEMPTS) {
          await sleep(PART_RETRY_BASE_MS * 2 ** (attempt - 1))
        }
      }
    }
    throw lastErr instanceof Error ? lastErr : new Error(`Part ${partNumber} failed`)
  }

  // 5. Bounded-concurrency pool: N workers drain the shared `pending` worklist.
  let cursor = 0
  const worker = async () => {
    while (true) {
      if (signal.aborted) throw new DOMException('Upload cancelled', 'AbortError')
      const idx = cursor++
      if (idx >= pending.length) return
      const partNumber = pending[idx]
      const etag = await putPart(partNumber)
      etagByPart.set(partNumber, etag)
      uploadedBytesByPart.set(partNumber, bytesForPart(partNumber))
      reportProgress()
    }
  }
  const workerCount = Math.min(UPLOAD_CONCURRENCY, pending.length) || 1
  await Promise.all(Array.from({ length: workerCount }, () => worker()))

  // 6. Assemble the full part list (resumed + uploaded), sorted by part number.
  const parts: CompletedPart[] = []
  for (let partNumber = 1; partNumber <= totalChunks; partNumber++) {
    parts.push({ PartNumber: partNumber, ETag: etagByPart.get(partNumber) ?? '' })
  }
  return parts
}

interface UploadStore {
  files: UploadFile[]
  panelOpen: boolean
  historyLoaded: boolean
  historyHasMore: boolean
  historyLoading: boolean
  historySkip: number
  setPanelOpen: (open: boolean) => void
  togglePanel: () => void
  startUpload: (file: File, projectId: string, assetName: string, projectName?: string, folderId?: string | null) => string
  startVersionUpload: (file: File, assetId: string, assetName: string, projectId: string) => string
  cancelUpload: (fileId: string) => void
  removeFile: (fileId: string) => void
  clearCompleted: () => void
  fetchHistory: () => Promise<void>
  fetchMoreHistory: () => Promise<void>
  // SSE-driven processing updates
  updateProcessingProgress: (assetId: string, percent: number) => void
  markProcessingComplete: (assetId: string) => void
  markProcessingFailed: (assetId: string, error: string) => void
  // Fallback poll: re-check processing items from backend (catches missed SSE events)
  refreshProcessingItems: () => Promise<void>
}

function mapProcessingStatus(status: string): UploadStatus {
  switch (status) {
    case 'uploading': return 'uploading'
    case 'processing': return 'processing'
    case 'ready': return 'complete'
    case 'failed': return 'failed'
    default: return 'complete'
  }
}

function mimeFromAssetType(assetType: string): string {
  switch (assetType) {
    case 'video': return 'video/mp4'
    case 'audio': return 'audio/mpeg'
    case 'image':
    case 'image_carousel': return 'image/jpeg'
    default: return 'application/octet-stream'
  }
}

function mergeHistoryAssets(existing: UploadFile[], assets: AssetResponse[]): UploadFile[] {
  const existingAssetIds = new Set(existing.map((f) => f.assetId).filter(Boolean))
  const newFiles: UploadFile[] = assets
    .filter((a) => a.latest_version && !existingAssetIds.has(a.id))
    .map((a) => {
      const v = a.latest_version!
      const file = v.files?.[0]
      const status = mapProcessingStatus(v.processing_status)
      return {
        id: `history-${a.id}`,
        fileName: file?.original_filename ?? a.name,
        fileSize: file?.file_size_bytes ?? 0,
        fileType: file?.mime_type ?? mimeFromAssetType(a.asset_type),
        projectId: a.project_id,
        assetName: a.name,
        progress: status === 'failed' ? 0 : 100,
        processingProgress: status === 'complete' ? 100 : 0,
        status,
        // For items whose status flipped server-side (eg the cleanup beat task
        // flipped a stuck 'uploading' → 'failed'), surface a default reason.
        error: status === 'failed' ? 'Upload interrupted. Re-upload to continue.' : undefined,
        assetId: a.id,
        versionId: v.id,
        createdAt: new Date(v.created_at).getTime(),
      }
    })
  return [...existing, ...newFiles]
}

const storeCreator: StateCreator<UploadStore, [['zustand/persist', unknown]]> = (set, get) => ({
  files: [],
  panelOpen: false,
  historyLoaded: false,
  historyHasMore: true,
  historyLoading: false,
  historySkip: 0,

  setPanelOpen: (open) => set({ panelOpen: open }),
  togglePanel: () => set((s) => ({ panelOpen: !s.panelOpen })),

  startUpload: (file, projectId, assetName, projectName, folderId) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`

    const entry: UploadFile = {
      id,
      fileName: file.name,
      fileSize: file.size,
      fileType: file.type,
      projectId,
      projectName,
      assetName,
      folderId: folderId ?? null,
      progress: 0,
      processingProgress: 0,
      status: 'pending',
      createdAt: Date.now(),
    }

    set((s) => ({ files: [entry, ...s.files], panelOpen: true }))

    const updateFile = (fileId: string, patch: Partial<UploadFile>) => {
      set((s) => ({
        files: s.files.map((f) => (f.id === fileId ? { ...f, ...patch } : f)),
      }))
    }

    // Start async upload
    ;(async () => {
      const controller = new AbortController()
      abortControllers[id] = controller

      // Track initiate response fields so catch block can call /upload/abort
      let upload_id: string | undefined
      let s3_key: string | undefined
      let version_id: string | undefined

      try {
        updateFile(id, { status: 'uploading' })

        const initRes = await api.post<InitiateResponse>(
          '/upload/initiate',
          {
            project_id: projectId,
            asset_name: assetName,
            original_filename: file.name,
            file_size_bytes: file.size,
            mime_type: file.type,
            folder_id: folderId ?? null,
          },
        )
        upload_id = initRes.upload_id
        s3_key = initRes.s3_key
        version_id = initRes.version_id
        const asset_id = initRes.asset_id

        updateFile(id, { uploadId: upload_id, assetId: asset_id, versionId: version_id, s3Key: s3_key })

        const parts = await uploadParts({
          file,
          s3Key: s3_key,
          uploadId: upload_id,
          signal: controller.signal,
          onProgress: (uploadedBytes) => {
            // Reserve 95–100% for the complete/processing handoff.
            const pct = file.size > 0 ? Math.round((uploadedBytes / file.size) * 95) : 95
            updateFile(id, { progress: pct })
          },
        })

        await api.post('/upload/complete', {
          s3_key,
          upload_id,
          asset_id,
          version_id,
          parts,
        })

        // Upload done — backend now processes (transcode/convert).
        // For non-processable types (or if SSE isn't wired), mark complete directly.
        const isMedia = file.type.startsWith('video/') || file.type.startsWith('audio/') || file.type.startsWith('image/')
        if (isMedia) {
          updateFile(id, { progress: 100, status: 'processing', processingProgress: 0 })
        } else {
          updateFile(id, { progress: 100, status: 'complete' })
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          updateFile(id, { status: 'cancelled', progress: 0 })
        } else {
          const message = err instanceof Error ? err.message : 'Upload failed'
          updateFile(id, { status: 'failed', error: message })
        }
        // Notify backend so the version is marked failed (not stuck at uploading).
        // This ensures post-refresh history shows the item in "Failed", not "Active".
        if (upload_id && s3_key && version_id) {
          api.post('/upload/abort', { s3_key, upload_id, version_id }).catch(() => {})
        }
      } finally {
        delete abortControllers[id]
      }
    })()

    return id
  },

  startVersionUpload: (file, assetId, assetName, projectId) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    const entry: UploadFile = {
      id,
      fileName: file.name,
      fileSize: file.size,
      fileType: file.type,
      projectId,
      assetName,
      progress: 0,
      processingProgress: 0,
      status: 'pending',
      assetId,
      createdAt: Date.now(),
    }
    set((s) => ({ files: [entry, ...s.files], panelOpen: true }))

    const updateFile = (fileId: string, patch: Partial<UploadFile>) => {
      set((s) => ({ files: s.files.map((f) => (f.id === fileId ? { ...f, ...patch } : f)) }))
    }

    ;(async () => {
      const controller = new AbortController()
      abortControllers[id] = controller
      let upload_id: string | undefined
      let s3_key: string | undefined
      let version_id: string | undefined
      try {
        updateFile(id, { status: 'uploading' })
        const initRes = await api.post<VersionInitiateResponse>(
          `/assets/${assetId}/versions`,
          {
            project_id: projectId,
            asset_name: assetName,
            original_filename: file.name,
            file_size_bytes: file.size,
            mime_type: file.type,
          },
        )
        upload_id = initRes.upload_id
        s3_key = initRes.s3_key
        version_id = initRes.version_id
        updateFile(id, { uploadId: upload_id, versionId: version_id, s3Key: s3_key })

        const parts = await uploadParts({
          file,
          s3Key: s3_key,
          uploadId: upload_id,
          signal: controller.signal,
          onProgress: (uploadedBytes) => {
            const pct = file.size > 0 ? Math.round((uploadedBytes / file.size) * 95) : 95
            updateFile(id, { progress: pct })
          },
        })

        await api.post('/upload/complete', { s3_key, upload_id, asset_id: assetId, version_id, parts })
        const isMedia = file.type.startsWith('video/') || file.type.startsWith('audio/') || file.type.startsWith('image/')
        updateFile(id, { progress: 100, status: isMedia ? 'processing' : 'complete', processingProgress: 0 })
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          updateFile(id, { status: 'cancelled', progress: 0 })
        } else {
          updateFile(id, { status: 'failed', error: err instanceof Error ? err.message : 'Upload failed' })
        }
        if (upload_id && s3_key && version_id) {
          api.post('/upload/abort', { s3_key, upload_id, version_id }).catch(() => {})
        }
      } finally {
        delete abortControllers[id]
      }
    })()

    return id
  },

  cancelUpload: (fileId) => {
    abortControllers[fileId]?.abort()
    set((s) => ({
      files: s.files.map((f) =>
        f.id === fileId ? { ...f, status: 'cancelled' as const, progress: 0 } : f,
      ),
    }))
  },

  removeFile: (fileId) => {
    set((s) => ({ files: s.files.filter((f) => f.id !== fileId) }))
  },

  clearCompleted: () => {
    set((s) => ({ files: s.files.filter((f) => f.status !== 'complete') }))
  },

  fetchHistory: async () => {
    if (get().historyLoaded) return
    set({ historyLoading: true })
    try {
      const assets = await api.get<AssetResponse[]>(`/me/assets?skip=0&limit=${HISTORY_PAGE_SIZE}`)
      const merged = mergeHistoryAssets(get().files, assets)
      set({
        historyLoaded: true,
        historyLoading: false,
        historySkip: HISTORY_PAGE_SIZE,
        historyHasMore: assets.length >= HISTORY_PAGE_SIZE,
        files: merged,
      })
    } catch {
      set({ historyLoaded: true, historyLoading: false })
    }
  },

  fetchMoreHistory: async () => {
    const { historyHasMore, historyLoading, historySkip } = get()
    if (!historyHasMore || historyLoading) return
    set({ historyLoading: true })
    try {
      const assets = await api.get<AssetResponse[]>(`/me/assets?skip=${historySkip}&limit=${HISTORY_PAGE_SIZE}`)
      const merged = mergeHistoryAssets(get().files, assets)
      set((s) => ({
        historyLoading: false,
        historySkip: s.historySkip + HISTORY_PAGE_SIZE,
        historyHasMore: assets.length >= HISTORY_PAGE_SIZE,
        files: merged,
      }))
    } catch {
      set({ historyLoading: false })
    }
  },

  updateProcessingProgress: (assetId, percent) => {
    set((s) => ({
      files: s.files.map((f) =>
        f.assetId === assetId && f.status === 'processing'
          ? { ...f, processingProgress: percent }
          : f,
      ),
    }))
  },

  markProcessingComplete: (assetId) => {
    set((s) => ({
      files: s.files.map((f) =>
        f.assetId === assetId && f.status === 'processing'
          ? { ...f, status: 'complete' as const, processingProgress: 100 }
          : f,
      ),
    }))
  },

  markProcessingFailed: (assetId, error) => {
    set((s) => ({
      files: s.files.map((f) =>
        f.assetId === assetId && f.status === 'processing'
          ? { ...f, status: 'failed' as const, error }
          : f,
      ),
    }))
  },

  refreshProcessingItems: async () => {
    const processingFiles = get().files.filter((f) => f.status === 'processing' && f.assetId)
    if (!processingFiles.length) return
    try {
      const results = await Promise.all(
        processingFiles.map((f) =>
          api.get<AssetResponse>(`/assets/${f.assetId}`).catch(() => null),
        ),
      )
      set((s) => ({
        files: s.files.map((f) => {
          if (f.status !== 'processing' || !f.assetId) return f
          const idx = processingFiles.findIndex((pf) => pf.assetId === f.assetId)
          const asset = idx >= 0 ? results[idx] : null
          if (!asset?.latest_version) return f
          const status = mapProcessingStatus(asset.latest_version.processing_status)
          if (status === 'processing') return f
          return { ...f, status, processingProgress: status === 'complete' ? 100 : 0 }
        }),
      }))
    } catch {
      // SSE is the primary mechanism; ignore poll errors
    }
  },
})

export const useUploadStore = create<UploadStore>()(
  persist(storeCreator, {
    name: 'ff-uploads',
    // Only persist failed/cancelled items — in-progress uploads can't be resumed
    // and successful ones are fetched from the API history on panel open.
    partialize: (state: UploadStore) => ({
      files: state.files.filter(
        (f: UploadFile) => f.status === 'failed' || f.status === 'cancelled',
      ),
    }),
  }),
)
