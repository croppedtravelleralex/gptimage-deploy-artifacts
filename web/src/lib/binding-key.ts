import type { Account } from "@/lib/api";

function proxyEndpointKey(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "default";
  try {
    const parsed = new URL(trimmed.includes("://") ? trimmed : `http://${trimmed}`);
    const host = parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.hostname;
    return `proxy:${host || "unknown"}`;
  } catch {
    const stripped = trimmed.replace(/^[a-z]+:\/\//i, "").replace(/^[^@]+@/, "").split("/")[0];
    return `proxy:${stripped || "unknown"}`;
  }
}

/** 与 services/account_identity.binding_key_for_account 对齐（优先用 API 下发的 hash）。 */
export function bindingKeyForAccount(account: Account): string {
  const hash = String(account.proxy_binding_hash ?? "").trim();
  if (hash) return hash;
  const egress = String(account.proxy_egress_ip ?? "").trim();
  if (egress) return `egress:${egress}`;
  const raw = String(account.proxy ?? "").trim();
  if (raw) return proxyEndpointKey(raw);
  return "default";
}
