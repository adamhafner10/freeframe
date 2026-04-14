'use client'

import { useEffect } from 'react'

/**
 * Sets the browser tab title. Appends " – FileStream" suffix.
 * Pass null/undefined to reset to default "FileStream".
 */
export function usePageTitle(title: string | null | undefined) {
  useEffect(() => {
    document.title = title ? `${title} – FileStream` : 'FileStream'
    return () => { document.title = 'FileStream' }
  }, [title])
}
