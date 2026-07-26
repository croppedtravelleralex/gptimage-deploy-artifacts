"use client";

import dynamic from "next/dynamic";
import { LoaderCircle } from "lucide-react";

import { PageLoadingSpinner } from "@/components/page-loading-spinner";
import { useAuthGuard } from "@/lib/use-auth-guard";

const LogsContent = dynamic(() => import("./logs-content"), {
  loading: () => <PageLoadingSpinner label="加载日志管理…" />,
});

export default function LogsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }
  return <LogsContent />;
}
