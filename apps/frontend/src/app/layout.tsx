import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "MurrCloud AI Video Studio", description: "Internal MurrCloud workspace for model-selectable AI video production.", icons: { icon: "/icon.svg" } };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body>{children}</body></html>;
}
