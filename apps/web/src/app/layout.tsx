import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Media Downloader",
  description: "Prepare downloads from media you are authorized to use.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

