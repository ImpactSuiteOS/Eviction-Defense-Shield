import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'EvictionShield — System Workflow',
  description: 'Automated Eviction Defense Identification · Google Cloud + Vertex AI',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
