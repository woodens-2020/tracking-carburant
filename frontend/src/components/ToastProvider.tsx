'use client'

import { Toaster } from 'react-hot-toast'

export default function ToastProvider() {
  return (
    <Toaster
      position="bottom-right"
      gutter={8}
      toastOptions={{
        duration: 3000,
        style: {
          background: '#ffffff',
          color: '#0f172a',
          border: '1px solid #e2e8f0',
          borderRadius: '10px',
          fontSize: '13px',
          fontWeight: 500,
          padding: '10px 16px',
          boxShadow: '0 8px 24px rgba(0,0,0,0.1)',
        },
        success: { iconTheme: { primary: '#16a34a', secondary: '#ffffff' } },
        error:   { iconTheme: { primary: '#dc2626', secondary: '#ffffff' } },
      }}
    />
  )
}
