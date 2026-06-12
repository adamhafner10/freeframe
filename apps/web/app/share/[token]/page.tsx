'use client'

import * as React from 'react'
import {
  Lock,
  AlertTriangle,
  Clock,
  Loader2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { FolderShareViewer } from '@/components/share/folder-share-viewer'
import { ShareAssetReview } from '@/components/share/share-asset-review'
import type { Asset, SharePermission, ProjectBranding, ShareLinkAppearance } from '@/types'

// ─── Types ────────────────────────────────────────────────────────────────────

interface ShareValidateResponse {
  asset?: (Asset & { thumbnail_url?: string; stream_url?: string; hls_status?: string | null }) | null
  asset_id?: string | null
  folder_id?: string | null
  project_id?: string | null
  folder_name?: string
  project_name?: string
  title?: string
  description?: string | null
  permission?: SharePermission
  allow_download?: boolean
  show_versions?: boolean
  show_watermark?: boolean
  appearance?: ShareLinkAppearance | null
  visibility?: string
  requires_password?: boolean
  requires_auth?: boolean
  share_session?: string | null
  expired?: boolean
  created_by_name?: string | null
  viewer_name?: string | null
  viewer_email?: string | null
  branding?: ProjectBranding | null
  error?: string
}

// ─── Utility ──────────────────────────────────────────────────────────────────

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

async function fetchShareInfo(
  token: string,
  password?: string,
  logOpen?: boolean,
): Promise<ShareValidateResponse> {
  const params = new URLSearchParams()
  if (password) params.set('password', password)
  if (logOpen) params.set('log_open', 'true')
  const qs = params.toString() ? `?${params.toString()}` : ''
  const url = `${API_URL}/share/${token}${qs}`

  // Include auth token if user is already logged in (for secure links)
  const headers: Record<string, string> = {}
  let accessToken: string | null = null
  try {
    if (typeof window !== 'undefined') {
      accessToken = localStorage.getItem('ff_access_token')
    }
  } catch {}
  if (accessToken) {
    headers['Authorization'] = `Bearer ${accessToken}`
  }

  const response = await fetch(url, { headers })
  if (!response.ok) {
    if (response.status === 403) {
      const data = await response.json().catch(() => ({}))
      if (data.detail === 'Incorrect password') {
        return { requires_password: true, error: 'Incorrect password' }
      }
      return { requires_password: true }
    }
    if (response.status === 410) return { expired: true }
    return {}
  }
  return response.json()
}

// ─── Password gate ────────────────────────────────────────────────────────────

interface PasswordGateProps {
  onSubmit: (password: string) => void
  error?: string | null
  loading?: boolean
}

function PasswordGate({ onSubmit, error, loading }: PasswordGateProps) {
  const [password, setPassword] = React.useState('')

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (password.trim()) onSubmit(password.trim())
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg-primary p-4">
      <div className="w-full max-w-sm rounded-xl border border-border bg-bg-secondary p-6 shadow-xl">
        <div className="mb-4 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-accent-muted">
            <Lock className="h-5 w-5 text-accent" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-text-primary">Password required</h1>
            <p className="text-xs text-text-tertiary">Enter the password to access this link</p>
          </div>
        </div>
        <form onSubmit={handleSubmit} className="space-y-3">
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter password…"
            autoFocus
            className="flex h-9 w-full rounded-md border border-border bg-bg-tertiary px-3 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:border-border-focus"
          />
          {error && <p className="text-xs text-status-error">{error}</p>}
          <Button type="submit" size="sm" className="w-full" loading={loading}>
            Access link
          </Button>
        </form>
      </div>
    </div>
  )
}

// ─── Error state ──────────────────────────────────────────────────────────────

interface ErrorStateProps {
  expired?: boolean
}

function ErrorState({ expired }: ErrorStateProps) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-bg-primary p-4">
      <div className="w-full max-w-sm rounded-xl border border-border bg-bg-secondary p-6 text-center shadow-xl">
        <div className="mb-4 flex justify-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-status-error/10">
            {expired ? (
              <Clock className="h-6 w-6 text-status-error" />
            ) : (
              <AlertTriangle className="h-6 w-6 text-status-error" />
            )}
          </div>
        </div>
        <h1 className="text-sm font-semibold text-text-primary">
          {expired ? 'Link expired' : 'Link not found'}
        </h1>
        <p className="mt-1 text-xs text-text-tertiary">
          {expired
            ? 'This share link has expired and is no longer accessible.'
            : 'This share link is invalid or has been removed.'}
        </p>
      </div>
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function SharePage({
  params,
}: {
  params: { token: string }
}) {
  const { token } = params

  type PageState =
    | { stage: 'loading' }
    | { stage: 'password_required'; error?: string; loading?: boolean }
    | { stage: 'expired' }
    | { stage: 'invalid' }
    | { stage: 'auth_required'; title?: string }
    | {
        stage: 'ready'
        asset: Asset & { thumbnail_url?: string; stream_url?: string; hls_status?: string | null }
        permission: SharePermission
        allowDownload: boolean
        showVersions: boolean
        branding: ProjectBranding | null
        shareName: string
      }
    | {
        stage: 'folder_ready'
        folderName: string
        title: string
        description: string | null
        createdByName: string | null
        viewerName: string | null
        permission: SharePermission
        allowDownload: boolean
        showVersions: boolean
        appearance: ShareLinkAppearance
        // FolderShareViewer expects a loose branding shape (logo_url/primary_color/…)
        branding: ProjectBranding | null
      }

  // FolderShareViewer's branding prop uses a looser shape than ProjectBranding.
  type FolderBranding = {
    logo_url?: string
    primary_color?: string
    custom_title?: string
    custom_footer?: string
  } | null

  const [state, setState] = React.useState<PageState>({ stage: 'loading' })
  const [shareSession, setShareSession] = React.useState<string | null>(null)
  const openLogged = React.useRef(false)

  async function validate(password?: string) {
    if (password) {
      setState({ stage: 'password_required', loading: true })
    }
    try {
      const shouldLogOpen = !password && !openLogged.current
      if (shouldLogOpen) openLogged.current = true
      const data = await fetchShareInfo(token, password, shouldLogOpen)
      if (data.requires_auth) {
        setState({ stage: 'auth_required', title: data.title })
        return
      }
      if (data.requires_password) {
        setState({ stage: 'password_required', error: data.error || undefined })
        return
      }
      if (data.expired) {
        setState({ stage: 'expired' })
        return
      }
      if (!data.permission) {
        setState({ stage: 'invalid' })
        return
      }

      // Store share session from password-protected link validation
      if (data.share_session) {
        setShareSession(data.share_session)
      }

      // Folder share mode OR project root share mode
      if ((data.folder_id || data.project_id) && !data.asset_id) {
        const defaultAppearance: ShareLinkAppearance = {
          layout: 'grid',
          theme: 'dark',
          accent_color: null,
          open_in_viewer: true,
          sort_by: 'created_at',
          card_size: 'm',
          aspect_ratio: 'landscape',
          thumbnail_scale: 'fill',
          show_card_info: true,
        }
        const folderName = data.folder_name ?? data.project_name ?? 'Shared'
        setState({
          stage: 'folder_ready',
          folderName,
          title: data.title ?? folderName,
          description: data.description ?? null,
          createdByName: data.created_by_name ?? null,
          viewerName: data.viewer_name ?? null,
          permission: data.permission,
          allowDownload: data.allow_download ?? false,
          showVersions: data.show_versions ?? true,
          appearance: { ...defaultAppearance, ...(data.appearance ?? {}) },
          branding: data.branding ?? null,
        })
        return
      }

      // Standard asset share mode
      if (!data.asset) {
        setState({ stage: 'invalid' })
        return
      }
      setState({
        stage: 'ready',
        asset: data.asset,
        permission: data.permission,
        allowDownload: data.allow_download ?? false,
        showVersions: data.show_versions ?? true,
        branding: data.branding ?? null,
        shareName: data.title || data.branding?.custom_title || 'FileStream',
      })
    } catch {
      setState({ stage: 'invalid' })
    }
  }

  React.useEffect(() => {
    validate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  if (state.stage === 'loading') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg-primary">
        <Loader2 className="h-8 w-8 animate-spin text-text-tertiary" />
      </div>
    )
  }

  if (state.stage === 'password_required') {
    return (
      <PasswordGate
        onSubmit={(pw) => validate(pw)}
        error={state.error}
        loading={state.loading}
      />
    )
  }

  if (state.stage === 'expired') {
    return <ErrorState expired />
  }

  if (state.stage === 'invalid') {
    return <ErrorState />
  }

  if (state.stage === 'auth_required') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg-primary p-4">
        <div className="w-full max-w-sm rounded-xl border border-border bg-bg-secondary p-6 shadow-xl text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-accent-muted">
            <Lock className="h-6 w-6 text-accent" />
          </div>
          <h1 className="text-lg font-semibold text-text-primary">
            {state.title || 'Secure Share Link'}
          </h1>
          <p className="mt-2 text-sm text-text-tertiary">
            This link is private. Please sign in to view the shared content.
          </p>
          <a
            href="/login"
            className="mt-4 inline-flex w-full items-center justify-center rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-white hover:bg-accent/90 transition-colors"
          >
            Sign in to continue
          </a>
        </div>
      </div>
    )
  }

  if (state.stage === 'folder_ready') {
    return (
      <FolderShareViewer
        token={token}
        shareSession={shareSession}
        folderName={state.folderName}
        title={state.title}
        description={state.description}
        createdByName={state.createdByName}
        viewerName={state.viewerName}
        permission={state.permission}
        allowDownload={state.allowDownload}
        showVersions={state.showVersions}
        appearance={state.appearance}
        branding={state.branding as unknown as FolderBranding}
      />
    )
  }

  return (
    <ShareAssetReview
      token={token}
      assetId={state.asset.id}
      shareSession={shareSession}
      permission={state.permission}
      allowDownload={state.allowDownload}
      branding={state.branding}
      shareName={state.shareName}
      hlsStatus={state.asset.hls_status}
    />
  )
}
