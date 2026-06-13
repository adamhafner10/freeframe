'use client'

import * as React from 'react'
import {
  ArrowLeft,
  Columns2,
  Download,
  Loader2,
  CheckCircle2,
  XCircle,
  MessageSquare,
  FileText,
  Video,
  Music,
  Image as ImageIcon,
  User,
  Clock,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { ReviewProvider, useReview } from '@/components/review/review-provider'
import { VideoPlayer } from '@/components/review/video-player'
import { AudioPlayer } from '@/components/review/audio-player'
import { ImageViewer } from '@/components/review/image-viewer'
import { AnnotationOverlay } from '@/components/review/annotation-overlay'
import { AnnotationCanvas } from '@/components/review/annotation-canvas'
import { CommentPanel } from '@/components/review/comment-panel'
import { CommentInput } from '@/components/review/comment-input'
import { useReviewStore } from '@/stores/review-store'
import type { ProjectBranding, SharePermission } from '@/types'

// ─── Constants ────────────────────────────────────────────────────────────────

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const GUEST_IDENTITY_KEY = 'ff_guest_identity'
const LEGACY_GUEST_IDENTITY_KEY = 'cadence_guest_identity'

// ─── Guest identity helpers (unified on ff_guest_identity) ────────────────────

interface GuestIdentity {
  name: string
  email: string
}

/**
 * Reads the guest identity, migrating the legacy `cadence_guest_identity`
 * key to the canonical `ff_guest_identity` key the ReviewProvider reads from.
 */
function loadGuestIdentity(): GuestIdentity | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = localStorage.getItem(GUEST_IDENTITY_KEY)
    if (raw) return JSON.parse(raw) as GuestIdentity
    // One-time migration from the legacy key so existing guests don't re-enter.
    const legacy = localStorage.getItem(LEGACY_GUEST_IDENTITY_KEY)
    if (legacy) {
      const parsed = JSON.parse(legacy) as GuestIdentity
      localStorage.setItem(GUEST_IDENTITY_KEY, legacy)
      localStorage.removeItem(LEGACY_GUEST_IDENTITY_KEY)
      return parsed
    }
  } catch {
    /* ignore */
  }
  return null
}

function saveGuestIdentity(identity: GuestIdentity) {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(GUEST_IDENTITY_KEY, JSON.stringify(identity))
    localStorage.removeItem(LEGACY_GUEST_IDENTITY_KEY)
  } catch {
    /* ignore */
  }
}

function isLoggedIn(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return !!localStorage.getItem('ff_access_token')
  } catch {
    return false
  }
}

// ─── Guest identity prompt (modal) ────────────────────────────────────────────

function GuestIdentityPrompt({
  onSave,
  onCancel,
}: {
  onSave: (identity: GuestIdentity) => void
  onCancel: () => void
}) {
  const [name, setName] = React.useState('')
  const [email, setEmail] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)

  function submit() {
    if (!name.trim() || !email.trim()) {
      setError('Both name and email are required.')
      return
    }
    if (!/\S+@\S+\.\S+/.test(email.trim())) {
      setError('Please enter a valid email address.')
      return
    }
    onSave({ name: name.trim(), email: email.trim() })
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="w-full max-w-sm rounded-xl border border-border bg-bg-secondary p-5 shadow-2xl">
        <div className="mb-1 flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/15">
            <User className="h-3.5 w-3.5 text-accent" />
          </div>
          <h3 className="text-sm font-semibold text-text-primary">Leave a comment</h3>
        </div>
        <p className="text-xs text-text-tertiary mb-4">
          Enter your name and email to comment. No account required.
        </p>
        <div className="space-y-3">
          <input
            type="text"
            placeholder="Your name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-md border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:border-accent"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit()
            }}
          />
          <input
            type="email"
            placeholder="your@email.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded-md border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:border-accent"
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit()
            }}
          />
          {error && <p className="text-xs text-status-error">{error}</p>}
        </div>
        <div className="flex items-center justify-end gap-2 mt-4">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-xs text-text-tertiary hover:text-text-primary transition-colors"
          >
            Cancel
          </button>
          <button
            disabled={!name.trim() || !email.trim()}
            onClick={submit}
            className="px-4 py-1.5 rounded-md bg-accent text-xs font-medium text-text-inverse hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            Continue
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Approval actions (approve / reject) ──────────────────────────────────────

function GuestApprovalActions({
  token,
  assetId,
  shareSession,
}: {
  token: string
  assetId: string
  shareSession?: string | null
}) {
  const [status, setStatus] = React.useState<'idle' | 'approved' | 'rejected'>('idle')
  const [loading, setLoading] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  // Identity-capture gate: when a guest with no stored identity clicks a
  // decision, stash it and show the prompt; replay once they identify.
  const [showGuestPrompt, setShowGuestPrompt] = React.useState(false)
  const pendingDecisionRef = React.useRef<'approved' | 'rejected' | null>(null)

  // Submit the decision to the backend. An approval must always be attributable,
  // so for unauthenticated guests we send the stored guest identity alongside
  // asset_id (the bearer token is still sent when present).
  const submitDecision = React.useCallback(
    async (decision: 'approved' | 'rejected') => {
      setLoading(true)
      setError(null)
      try {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' }
        const loggedIn = isLoggedIn()
        try {
          const t = localStorage.getItem('ff_access_token')
          if (t) headers['Authorization'] = `Bearer ${t}`
        } catch {
          /* ignore */
        }
        const payload: {
          asset_id: string
          guest_email?: string
          guest_name?: string
        } = { asset_id: assetId }
        if (!loggedIn) {
          const identity = loadGuestIdentity()
          if (identity) {
            payload.guest_name = identity.name
            payload.guest_email = identity.email
          }
        }
        const qs = shareSession ? `?share_session=${encodeURIComponent(shareSession)}` : ''
        const res = await fetch(
          `${API_URL}/share/${token}/${decision === 'approved' ? 'approve' : 'reject'}${qs}`,
          {
            method: 'POST',
            headers,
            body: JSON.stringify(payload),
          },
        )
        if (!res.ok) throw new Error('Failed to submit decision')
        setStatus(decision)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to submit')
      } finally {
        setLoading(false)
      }
    },
    [token, assetId, shareSession],
  )

  // Capture identity first (same pattern as the comment input) so the decision
  // is always attributable, then submit.
  function decide(decision: 'approved' | 'rejected') {
    if (!isLoggedIn() && !loadGuestIdentity()) {
      pendingDecisionRef.current = decision
      setShowGuestPrompt(true)
      return
    }
    submitDecision(decision)
  }

  function handleIdentitySaved(identity: GuestIdentity) {
    saveGuestIdentity(identity)
    setShowGuestPrompt(false)
    const pending = pendingDecisionRef.current
    pendingDecisionRef.current = null
    if (pending) setTimeout(() => submitDecision(pending), 50)
  }

  if (status === 'approved') {
    return (
      <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
        <CheckCircle2 className="h-4 w-4 text-emerald-400" />
        <span className="text-sm font-medium text-emerald-400">Approved</span>
      </div>
    )
  }
  if (status === 'rejected') {
    return (
      <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20">
        <XCircle className="h-4 w-4 text-red-400" />
        <span className="text-sm font-medium text-red-400">Rejected</span>
      </div>
    )
  }

  return (
    <>
      <div className="flex items-center gap-2">
        {error && <span className="text-xs text-red-400 mr-1">{error}</span>}
        <button
          onClick={() => decide('rejected')}
          disabled={loading}
          className="inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-xs font-medium border border-red-500/30 text-red-400 hover:border-red-500/60 hover:bg-red-500/10 disabled:opacity-50 transition-colors"
        >
          <XCircle className="h-4 w-4" />
          Reject
        </button>
        <button
          onClick={() => decide('approved')}
          disabled={loading}
          className="inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-xs font-medium bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50 transition-colors"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
          Approve
        </button>
      </div>
      {showGuestPrompt && (
        <GuestIdentityPrompt
          onSave={handleIdentitySaved}
          onCancel={() => {
            setShowGuestPrompt(false)
            pendingDecisionRef.current = null
          }}
        />
      )}
    </>
  )
}

// ─── Fields tab ───────────────────────────────────────────────────────────────

function FieldRow({
  label,
  value,
  capitalize,
}: {
  label: string
  value: string
  capitalize?: boolean
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-text-tertiary">{label}</span>
      <span
        className={cn(
          'text-xs text-text-primary font-medium truncate ml-4 max-w-[200px]',
          capitalize && 'capitalize',
        )}
      >
        {value}
      </span>
    </div>
  )
}

// ─── Processing / unavailable placeholder ─────────────────────────────────────

function MediaPlaceholder({
  assetType,
  hlsStatus,
}: {
  assetType: string
  hlsStatus?: string | null
}) {
  const processing = hlsStatus === 'pending' || hlsStatus === 'processing'
  const Icon = assetType === 'audio' ? Music : assetType.startsWith('image') ? ImageIcon : Video
  return (
    <div className="flex-1 flex items-center justify-center bg-black min-h-0">
      <div className="flex flex-col items-center gap-3 text-center px-6">
        {processing ? (
          <>
            <div className="h-12 w-12 rounded-full bg-accent/10 flex items-center justify-center">
              <Loader2 className="h-6 w-6 animate-spin text-accent" />
            </div>
            <div>
              <p className="text-sm font-medium text-text-primary">Processing…</p>
              <p className="text-xs text-text-tertiary mt-1">
                This asset is still being prepared. Check back in a moment.
              </p>
            </div>
          </>
        ) : (
          <>
            <Icon className="h-10 w-10 text-zinc-700" />
            <p className="text-sm text-zinc-500">Media unavailable</p>
          </>
        )}
      </div>
    </div>
  )
}

// ─── Inner review surface (inside ReviewProvider) ─────────────────────────────

interface ShareAssetReviewInnerProps {
  token: string
  shareSession?: string | null
  permission: SharePermission
  allowDownload: boolean
  branding: ProjectBranding | null
  shareName: string
  hlsStatus?: string | null
  onBack?: () => void
}

function ShareAssetReviewInner({
  token,
  shareSession,
  permission,
  allowDownload,
  branding,
  shareName,
  hlsStatus,
  onBack,
}: ShareAssetReviewInnerProps) {
  const { asset, isLoading, comments, addComment, refetchComments } = useReview()
  const { isDrawingMode, focusedCommentId, setActiveAnnotation, setFocusedCommentId } =
    useReviewStore()

  const [sidebarOpen, setSidebarOpen] = React.useState(true)
  const [activeTab, setActiveTab] = React.useState<'comments' | 'fields'>('comments')
  const [showGuestPrompt, setShowGuestPrompt] = React.useState(false)
  const pendingCommentRef = React.useRef<{
    body: string
    timecodeStart?: number
    timecodeEnd?: number
    annotationData?: Record<string, unknown>
    parentId?: string
  } | null>(null)

  const canComment = permission === 'comment' || permission === 'approve'

  const streamUrl = (asset as { stream_url?: string } | null)?.stream_url ?? null

  // ── Submit a comment through the share endpoints (ReviewProvider.addComment) ──
  const submitComment = React.useCallback(
    async (
      body: string,
      timecodeStart?: number,
      timecodeEnd?: number,
      annotationData?: Record<string, unknown>,
      parentId?: string,
    ) => {
      const payload: {
        body: string
        timecode_start?: number
        timecode_end?: number
        annotation?: { drawing_data: Record<string, unknown> }
        parent_id?: string
      } = { body }
      if (timecodeStart != null) payload.timecode_start = timecodeStart
      if (timecodeEnd != null) payload.timecode_end = timecodeEnd
      if (annotationData) payload.annotation = { drawing_data: annotationData }
      if (parentId) payload.parent_id = parentId
      await addComment(payload)
      // Refetch so nested replies / annotation indicators render from the tree.
      refetchComments().catch(() => {})
    },
    [addComment, refetchComments],
  )

  // ── Gate comment submission behind the guest identity capture ──
  const handleSubmitComment = React.useCallback(
    async (
      body: string,
      timecodeStart?: number,
      timecodeEnd?: number,
      annotationData?: Record<string, unknown>,
      parentId?: string,
    ) => {
      if (!isLoggedIn() && !loadGuestIdentity()) {
        pendingCommentRef.current = { body, timecodeStart, timecodeEnd, annotationData, parentId }
        setShowGuestPrompt(true)
        return
      }
      await submitComment(body, timecodeStart, timecodeEnd, annotationData, parentId)
    },
    [submitComment],
  )

  const handleSubmitReply = React.useCallback(
    async (parentId: string, body: string) => {
      if (!isLoggedIn() && !loadGuestIdentity()) {
        pendingCommentRef.current = { body, parentId }
        setShowGuestPrompt(true)
        return
      }
      await submitComment(body, undefined, undefined, undefined, parentId)
    },
    [submitComment],
  )

  const handleGuestIdentitySaved = React.useCallback(
    (identity: GuestIdentity) => {
      saveGuestIdentity(identity)
      setShowGuestPrompt(false)
      const pending = pendingCommentRef.current
      pendingCommentRef.current = null
      if (pending) {
        setTimeout(
          () =>
            submitComment(
              pending.body,
              pending.timecodeStart,
              pending.timecodeEnd,
              pending.annotationData,
              pending.parentId,
            ),
          50,
        )
      }
    },
    [submitComment],
  )

  if (isLoading || !asset) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-bg-primary">
        <Loader2 className="h-8 w-8 animate-spin text-text-tertiary" />
      </div>
    )
  }

  const assetType = asset.asset_type
  const playable = !!streamUrl
  const primaryColor = branding?.primary_color ?? '#7c3aed'

  const annotationOverlaySlot = (
    <>
      <AnnotationOverlay key={focusedCommentId ?? 'none'} />
      {isDrawingMode && <AnnotationCanvas />}
    </>
  )

  function renderMedia() {
    if (assetType === 'video') {
      if (!playable) return <MediaPlaceholder assetType="video" hlsStatus={hlsStatus} />
      return (
        <VideoPlayer
          assetId={asset!.id}
          comments={comments}
          className="flex-1 min-h-0"
          initialStreamUrl={streamUrl}
          overlay={annotationOverlaySlot}
        />
      )
    }
    if (assetType === 'audio') {
      if (!playable) return <MediaPlaceholder assetType="audio" hlsStatus={hlsStatus} />
      return (
        <AudioPlayer
          asset={asset!}
          version={asset!.latest_version ?? null}
          comments={comments}
          className="flex-1"
        />
      )
    }
    if (assetType === 'image' || assetType === 'image_carousel') {
      return (
        <div className="relative flex-1 flex items-center justify-center p-4 overflow-hidden bg-black">
          <ImageViewer
            asset={asset!}
            version={asset!.latest_version ?? null}
            annotationCanvas={annotationOverlaySlot}
          />
        </div>
      )
    }
    return <MediaPlaceholder assetType={assetType} hlsStatus={hlsStatus} />
  }

  return (
    <div className="absolute inset-0 flex flex-col bg-bg-primary text-text-primary overflow-hidden">
      {/* ── Top bar ── */}
      <div className="flex items-center justify-between border-b border-border px-3 h-12 bg-bg-secondary shrink-0">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          {onBack && (
            <button
              onClick={onBack}
              className="flex items-center justify-center h-7 w-7 rounded-md text-text-secondary hover:text-text-primary hover:bg-bg-hover transition-colors shrink-0"
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
          )}
          <div
            className="flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold text-white shrink-0 overflow-hidden"
            style={{ backgroundColor: primaryColor }}
          >
            {branding?.logo_s3_key ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={`${API_URL}/share/branding/logo`}
                alt=""
                className="h-full w-full rounded-full object-cover"
                onError={(e) => {
                  ;(e.target as HTMLImageElement).style.display = 'none'
                }}
              />
            ) : (
              'FF'
            )}
          </div>
          <nav className="flex items-center gap-1 text-[13px] min-w-0">
            <span className="text-text-tertiary shrink-0 truncate max-w-[200px]">{shareName}</span>
            <span className="text-text-tertiary/60">/</span>
            <span className="text-text-primary font-medium truncate">{asset.name}</span>
          </nav>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {allowDownload && streamUrl && (
            <a
              href={streamUrl}
              download
              className="inline-flex items-center gap-1.5 rounded-md bg-accent hover:bg-accent/90 px-3 py-1.5 text-xs font-medium text-text-inverse transition-colors"
            >
              <Download className="h-3.5 w-3.5" />
              Download
            </a>
          )}
          <button
            onClick={() => setSidebarOpen((p) => !p)}
            className={cn(
              'flex items-center justify-center h-8 w-8 rounded-md transition-colors',
              sidebarOpen
                ? 'bg-bg-hover text-text-primary'
                : 'text-text-tertiary hover:text-text-primary hover:bg-bg-hover',
            )}
            title="Toggle panel"
          >
            <Columns2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* ── Main ── */}
      <div className="flex flex-1 overflow-hidden min-h-0">
        {/* Viewer */}
        <div className="flex-1 flex flex-col bg-bg-primary overflow-hidden min-w-0">
          {renderMedia()}
        </div>

        {/* Sidebar */}
        {sidebarOpen && (
          <div className="w-[360px] flex flex-col border-l border-border bg-bg-secondary shrink-0 animate-in slide-in-from-right-2 duration-150">
            {/* Tabs */}
            <div className="px-4 pt-3 pb-2 shrink-0">
              <div className="flex items-center bg-bg-tertiary rounded-lg p-0.5">
                <button
                  onClick={() => setActiveTab('comments')}
                  className={cn(
                    'flex-1 py-1.5 text-[13px] font-medium rounded-md transition-all flex items-center justify-center gap-1.5',
                    activeTab === 'comments'
                      ? 'bg-bg-hover text-text-primary shadow-sm'
                      : 'text-text-tertiary hover:text-text-secondary',
                  )}
                >
                  <MessageSquare className="h-3.5 w-3.5" />
                  Comments
                </button>
                <button
                  onClick={() => setActiveTab('fields')}
                  className={cn(
                    'flex-1 py-1.5 text-[13px] font-medium rounded-md transition-all flex items-center justify-center gap-1.5',
                    activeTab === 'fields'
                      ? 'bg-bg-hover text-text-primary shadow-sm'
                      : 'text-text-tertiary hover:text-text-secondary',
                  )}
                >
                  <FileText className="h-3.5 w-3.5" />
                  Fields
                </button>
              </div>
            </div>

            <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
              {activeTab === 'comments' ? (
                <>
                  <CommentPanel
                    comments={comments as never}
                    onResolve={async () => {}}
                    onDelete={async () => {}}
                    onAddReaction={async () => {}}
                    onRemoveReaction={async () => {}}
                    onReply={() => {}}
                    onSubmitReply={handleSubmitReply}
                  />

                  {/* Approval actions */}
                  {permission === 'approve' && (
                    <div className="px-4 py-3 border-t border-border shrink-0">
                      <GuestApprovalActions token={token} assetId={asset.id} shareSession={shareSession} />
                    </div>
                  )}

                  {/* Comment input — rich (timecode + drawing) for comment/approve; read-only notice otherwise */}
                  {canComment ? (
                    <CommentInput
                      assetId={asset.id}
                      projectId=""
                      assetType={assetType}
                      onSubmit={handleSubmitComment}
                    />
                  ) : (
                    <div className="px-4 py-3 border-t border-border shrink-0">
                      <p className="text-xs text-text-tertiary text-center">
                        View-only access. Commenting is disabled for this link.
                      </p>
                    </div>
                  )}
                </>
              ) : (
                <div className="flex-1 overflow-y-auto p-4 space-y-3">
                  <FieldRow label="Name" value={asset.name} />
                  <FieldRow
                    label="Type"
                    value={assetType.replace('_', ' ')}
                    capitalize
                  />
                  {asset.description && (
                    <FieldRow label="Description" value={asset.description} />
                  )}
                  {asset.rating != null && (
                    <FieldRow label="Rating" value={`${asset.rating}/5`} />
                  )}
                  {asset.due_date && (
                    <FieldRow
                      label="Due date"
                      value={new Date(asset.due_date).toLocaleDateString()}
                    />
                  )}
                  {asset.keywords && asset.keywords.length > 0 && (
                    <div className="space-y-1">
                      <span className="text-xs text-text-tertiary">Keywords</span>
                      <div className="flex flex-wrap gap-1">
                        {asset.keywords.map((kw, i) => (
                          <span
                            key={i}
                            className="text-2xs bg-bg-tertiary text-text-secondary rounded px-1.5 py-0.5"
                          >
                            {kw}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Custom footer */}
      {branding?.custom_footer && (
        <div className="shrink-0 border-t border-border px-4 py-1.5 text-center">
          <p className="text-2xs text-text-tertiary">{branding.custom_footer}</p>
        </div>
      )}

      {/* Guest identity prompt */}
      {showGuestPrompt && (
        <GuestIdentityPrompt
          onSave={handleGuestIdentitySaved}
          onCancel={() => {
            setShowGuestPrompt(false)
            pendingCommentRef.current = null
          }}
        />
      )}
    </div>
  )
}

// ─── Public wrapper (mounts ReviewProvider) ───────────────────────────────────

export interface ShareAssetReviewProps {
  token: string
  assetId: string
  shareSession?: string | null
  permission: SharePermission
  allowDownload: boolean
  branding: ProjectBranding | null
  shareName: string
  /** HLS processing status, if the share payload exposes it. */
  hlsStatus?: string | null
  onBack?: () => void
}

/**
 * Frame.io-style review surface for a single shared asset. Reuses the full
 * internal review stack (VideoPlayer scrubber + markers + shortcuts, annotation
 * overlay/canvas, CommentPanel threads, rich CommentInput) but in guest/share
 * mode — routing every call through the public `/share/{token}/...` endpoints
 * and gating capabilities by the share link permission.
 */
export function ShareAssetReview({
  token,
  assetId,
  shareSession,
  permission,
  allowDownload,
  branding,
  shareName,
  hlsStatus,
  onBack,
}: ShareAssetReviewProps) {
  return (
    <ReviewProvider assetId={assetId} shareToken={token} shareSession={shareSession}>
      <ShareAssetReviewInner
        token={token}
        shareSession={shareSession}
        permission={permission}
        allowDownload={allowDownload}
        branding={branding}
        shareName={shareName}
        hlsStatus={hlsStatus}
        onBack={onBack}
      />
    </ReviewProvider>
  )
}
