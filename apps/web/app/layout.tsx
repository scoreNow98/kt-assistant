export const metadata = { title: "KT Assistant" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: "3rem" }}>{children}</body>
    </html>
  );
}
