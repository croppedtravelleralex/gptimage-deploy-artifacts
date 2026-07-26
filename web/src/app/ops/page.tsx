"use client";

import dynamic from "next/dynamic";

import { PageLoadingSpinner } from "@/components/page-loading-spinner";
import { useAuthGuard } from "@/lib/use-auth-guard";

const OpsDashboard = dynamic(() => import("./ops-dashboard"), {
  loading: () => <PageLoadingSpinner label="加载运维面板…" />,
});

export default function OpsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session) {
    return null;
  }
  return <OpsDashboard />;
}
