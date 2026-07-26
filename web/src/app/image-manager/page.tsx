"use client";

import dynamic from "next/dynamic";

import { PageLoadingSpinner } from "@/components/page-loading-spinner";
import { useAuthGuard } from "@/lib/use-auth-guard";

const ImageManagerContent = dynamic(() => import("./image-manager-content"), {
  loading: () => <PageLoadingSpinner label="加载图片管理…" />,
});

export default function ImageManagerPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session) {
    return null;
  }
  return <ImageManagerContent />;
}
