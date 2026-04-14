'use client'

import * as React from 'react'
import { useUploadStore } from '@/stores/upload-store'

/**
 * Sends `/api/upload/abort` for every in-flight upload when the tab is closing.
 * Uses fetch with `keepalive: true` so the request survives tab unload in modern
 * browsers — same mechanism sendBeacon uses but allows the Authorization header.
 *
 * Backstop: a Celery beat task (`cleanup_stuck_uploads`) flips anything stuck
 * in `uploading` state for 30+ min to `failed` even if this beacon fails to
 * fire (e.g. browser killed before unload).
 */
export function UploadUnloadBeacon() {
  React.useEffect(() => {
    function onUnload() {
      const files = useUploadStore.getState().files
      const active = files.filter(
        (f) =>
          (f.status === 'uploading' || f.status === 'pending') &&
          f.uploadId && f.versionId && f.s3Key,
      )
      if (active.length === 0) return

      const token = localStorage.getItem('ff_access_token')
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (token) headers['Authorization'] = `Bearer ${token}`

      for (const f of active) {
        try {
          fetch('/api/upload/abort', {
            method: 'POST',
            keepalive: true,
            headers,
            body: JSON.stringify({
              s3_key: f.s3Key,
              upload_id: f.uploadId,
              version_id: f.versionId,
            }),
          }).catch(() => {})
        } catch {
          // page is unloading; nothing to do
        }
      }
    }
    window.addEventListener('beforeunload', onUnload)
    window.addEventListener('pagehide', onUnload)
    return () => {
      window.removeEventListener('beforeunload', onUnload)
      window.removeEventListener('pagehide', onUnload)
    }
  }, [])
  return null
}
