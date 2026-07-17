"use client";

export default function AccountsLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // 突破根布局 max-w-[1440px]，账号表左右平铺
  return (
    <div className="relative left-1/2 w-screen max-w-[100vw] -translate-x-1/2 px-2 sm:px-3 lg:px-4">
      {children}
    </div>
  );
}
