"use client";

import dynamic from "next/dynamic";
import { LoaderCircle } from "lucide-react";

import { PageLoadingSpinner } from "@/components/page-loading-spinner";
import { useAuthGuard } from "@/lib/use-auth-guard";

const ImageWorkbench = dynamic(() => import("./image-workbench"), {
  loading: () => <PageLoadingSpinner label="加载生图工作台…" />,
});

export default function ImagePage() {
  const { isCheckingAuth, session } = useAuthGuard();

  if (isCheckingAuth || !session) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <ImageWorkbench isAdmin={session.role === "admin"} />;
}
