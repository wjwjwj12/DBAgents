import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "交数智航",
  description: "浙江综合交通大数据开发有限公司智能体平台",
  icons: {
    icon: "/platform-logo.png",
    shortcut: "/platform-logo.png",
    apple: "/platform-logo.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
