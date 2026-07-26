"use client";

import { LoaderCircle } from "lucide-react";

export function PageLoadingSpinner({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="flex min-h-[40vh] flex-col items-center justify-center gap-2 text-sm text-stone-500">
      <LoaderCircle className="size-5 animate-spin" />
      {label}
    </div>
  );
}
