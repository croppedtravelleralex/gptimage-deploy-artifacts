"use client";

import dynamic from "next/dynamic";
import { LoaderCircle } from "lucide-react";

import { PageLoadingSpinner } from "@/components/page-loading-spinner";
import { useAuthGuard } from "@/lib/use-auth-guard";

const SettingsContent = dynamic(() => import("./settings-content"), {
  loading: () => <PageLoadingSpinner label="加载系统设置…" />,
});

export default function SettingsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <SettingsContent />;
}
