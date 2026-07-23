"use client";

import { LoaderCircle } from "lucide-react";

import { useAuthGuard } from "@/lib/use-auth-guard";

import { ConversationWorkbench } from "./conversation-workbench";
import { SkillStatusLight } from "./skill-status-panel";

export default function ChatPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex h-[calc(100dvh-49px)] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="relative flex h-[calc(100dvh-49px)] w-full flex-col overflow-hidden px-0 pb-1 pt-1 md:px-1">
      <div className="pointer-events-none absolute right-3 top-2 z-10">
        <div className="pointer-events-auto">
          <SkillStatusLight />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        <ConversationWorkbench />
      </div>
    </div>
  );
}
