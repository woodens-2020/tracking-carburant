import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import ToastProvider from '@/components/ToastProvider'
import './globals.css'

const inter = Inter({
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-inter',
})

export const metadata: Metadata = {
  title: 'Suivi des Meters — Station',
  description: 'Gestion des compteurs de carburant',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fr" className={inter.variable}>
      <body style={{ fontFamily: 'var(--font-inter), -apple-system, BlinkMacSystemFont, sans-serif' }}>
        {children}
        <ToastProvider />
      </body>
    </html>
  )
}
