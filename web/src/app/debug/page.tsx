"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { LoaderCircle } from "lucide-react";

/** 旧「调试」入口重定向到「对话」 */
export default function DebugRedirectPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/chat");
  }, [router]);
  return (
    <div className="flex min-h-[calc(100vh-49px)] items-center justify-center gap-2 text-sm text-stone-500">
      <LoaderCircle className="size-4 animate-spin" />
      正在前往对话…
    </div>
  );
}
