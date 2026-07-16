import { httpRequest, request } from "@/lib/request";

export type AccountType = string;
export type AccountStatus = "正常" | "限流" | "异常" | "禁用";
export type ImageModel = string;
export type AuthRole = "admin" | "user";
export type ImageStorageMode = "local" | "webdav" | "both";

export type ImageStorageSettings = {
  enabled: boolean;
  mode: ImageStorageMode;
  webdav_url: string;
  webdav_username: string;
  webdav_password: string;
  webdav_root_path: string;
  public_base_url: string;
};

export type Account = {
  access_token: string;
  type: AccountType;
  source_type?: string | null;
  status: AccountStatus;
  quota: number;
  image_quota_unknown?: boolean;
  email?: string | null;
  user_id?: string | null;
  limits_progress?: Array<{
    feature_name?: string;
    remaining?: number;
    reset_after?: string;
  }>;
  default_model_slug?: string | null;
  restore_at?: string | null;
  success: number;
  fail: number;
  /** 当前图片在途数(正在生成、尚未结束的图片数)。号池空闲时持续 > 0 表示并发槽位泄漏。 */
  image_inflight?: number;
  panda_sync_state?: string | null;
  panda_probe_count?: number;
  panda_probe_next_at?: string | null;
  panda_probe_last_at?: string | null;
  panda_probe_last_error?: string | null;
  panda_ready_at?: string | null;
  panda_synced_at?: string | null;
  panda_receive_state?: string | null;
  panda_imported_at?: string | null;
  panda_verified_at?: string | null;
  panda_rejected_at?: string | null;
  panda_verify_last_error?: string | null;
  last_quota_refresh_at?: string | null;
  last_quota_refresh_error?: string | null;
  quota_refresh_fail_count?: number;
  quota_refresh_failure_kind?: string | null;
  last_used_at?: string | null;
  proxy?: string | null;
  proxy_provider?: string | null;
  proxy_scope?: string | null;
  proxy_node_id?: string | null;
  proxy_egress_ip?: string | null;
  proxy_egress_hash?: string | null;
  traffic_total_bytes?: number | null;
  traffic_uploaded_bytes?: number | null;
  traffic_downloaded_bytes?: number | null;
  traffic_updated_at?: string | null;
  created_at?: string | null;
  outlook_recovery_state?: string | null;
  outlook_recovery_terminal_reason?: string | null;
  outlook_recovery_terminal_at?: string | null;
  outlook_recovery_last_attempt_at?: string | null;
  outlook_recovery_last_error?: string | null;
};

export type AccountImportPayload = {
  access_token: string;
  accessToken?: string;
  type?: string;
  export_type?: string;
  source_type?: string;
  [key: string]: unknown;
};

export type Model = {
  id: string;
  object: string;
  created: number;
  owned_by: string;
  permission: unknown[];
  root: string;
  parent: string | null;
};

type AccountListResponse = {
  items: Account[];
  total?: number;
  offset?: number;
  limit?: number;
    stats?: {
      total: number;
      cumulative_total?: number;
      active: number;
      limited: number;
      abnormal: number;
      disabled: number;
      total_quota: number;
      unlimited_quota_count?: number;
      unknown_quota_count?: number;
      panda_staging_count?: number;
      panda_ready_count?: number;
      panda_synced_count?: number;
      panda_upload_queue_count?: number;
      panda_upload_eligible_count?: number;
      panda_upload_unsynced_eligible_count?: number;
      panda_upload_blocked_count?: number;
      panda_upload_retained_count?: number;
      panda_upload_remote_pending_count?: number;
      panda_upload_remote_verified_count?: number;
      panda_upload_remote_rejected_count?: number;
      panda_incoming_count?: number;
      panda_verified_count?: number;
      panda_rejected_count?: number;
      schedulable?: number;
      tainted_count?: number;
      total_success?: number;
      total_fail?: number;
      by_type?: Record<string, number>;
    };
};

type ModelListResponse = {
  object: string;
  data: Model[];
};

type AccountMutationResponse = {
  items?: Account[];
  added?: number;
  skipped?: number;
  removed?: number;
  refreshed?: number;
  relogined?: number;
  errors?: Array<{ access_token: string; error: string }>;
};

export type PandaAccountSyncResponse = {
  ok: boolean;
  exit_code: number | null;
  error: string;
  summary: string;
  stdout_tail: string;
  stderr_tail: string;
  batch_size: number;
  max_accounts_per_run: number;
  timeout_seconds: number;
  synced?: number;
  failed?: number;
  queued?: number;
  details?: {
    scanned?: number;
    eligible?: number;
    remote_missing_reupload?: number;
    already_remote?: number;
    blocked_by_config?: number;
    blocked_by_watermark?: number;
    blocked_by_state?: number;
    blocked_by_quota_or_status?: number;
    blocked_by_failure_evidence?: number;
    blocked_by_missing_quota_refresh?: number;
    blocked_by_probe_error?: number;
    deleted_local?: number;
    remote_token_snapshot?: string;
  };
  stats?: AccountListResponse["stats"];
};

export type AccountActivityDailyResponse = {
  days: number;
  sync_label: string;
  items: Array<{
    date: string;
    registered: number;
    uploaded: number;
    received: number;
    deleted: number;
  }>;
};

export type PandaSyncPublicSettings = {
  enabled: boolean;
  base_url: string;
  batch_size: number;
  timeout_seconds: number;
  remove_local_on_success: boolean;
  queue_on_failure: boolean;
  staging_enabled: boolean;
  upload_max_batch?: number;
  public_import_min_interval_sec?: number;
  has_auth_key?: boolean;
};

export type AccountRefreshResponse = {
  items?: Account[];
  refreshed: number;
  relogined?: number;
  errors: Array<{ access_token: string; error: string }>;
};

export type RefreshProgressResponse = {
  total: number;
  processed: number;
  done: boolean;
  error: string | null;
  status_counts?: Record<string, number>;
  total_quota?: number;
  result?: AccountRefreshResponse | null;
  results?: Array<{ token: string; status: string; error?: string | null }>;
};

export type AccountRefreshAllStatus = {
  job_id: string;
  state: "idle" | "running" | "paused" | "stopping" | "stopped" | "completed" | string;
  running: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  last_update_at?: string | null;
  total: number;
  processed: number;
  refreshed: number;
  available: number;
  became_available: number;
  quota_total?: number;
  unlimited_quota?: number;
  unknown_quota?: number;
  failed: number;
  removed?: number;
  expired_removed?: number;
  synced_to_panda?: number;
  sync_failed?: number;
  queued_for_panda?: number;
  skipped: number;
  pause_reason: string;
  current_token: string;
  recent?: Array<{
    index?: number;
    token?: string;
    status?: string;
    quota?: number;
    quota_unknown?: boolean;
    available?: boolean;
    error?: string;
  }>;
  options?: Record<string, unknown>;
  resource?: {
    available_memory_mb?: number | null;
    memory_current_mb?: number | null;
    memory_limit_mb?: number | null;
    load_1m?: number | null;
  };
};

export type AccountMaintenanceLoopStatus = {
  state: string;
  enabled: boolean;
  started_at?: string | null;
  last_update_at?: string | null;
  next_run_at?: string | null;
  pause_reason?: string;
  current_batch_id?: string;
  current_batch?: {
    batch_id?: string;
    started_at?: string;
    total?: number;
    status?: AccountRefreshAllStatus | null;
  } | null;
  last_batch?: Record<string, unknown> | null;
  totals?: Record<string, number>;
  recent?: Array<Record<string, unknown>>;
  resource?: {
    available_memory_mb?: number | null;
    memory_current_mb?: number | null;
    memory_limit_mb?: number | null;
    load_1m?: number | null;
    image_inflight?: number | null;
  };
  settings?: AccountMaintenanceLoopSettings;
};

export type OutlookAutoRecoverySettings = {
  enabled?: boolean;
  interval_sec?: number;
  max_per_cycle?: number;
  startup_delay_sec?: number;
  progress_poll_sec?: number;
};

export type OutlookAutoRecoveryStatus = {
  state: string;
  enabled: boolean;
  started_at?: string | null;
  last_update_at?: string | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  seconds_until_next_run?: number | null;
  pause_reason?: string;
  candidate_count?: number;
  terminal_count?: number;
  current?: {
    email?: string;
    stage?: string;
    message?: string;
    progress_id?: string;
  } | null;
  last_result?: {
    at?: string;
    ok?: boolean;
    email?: string;
    stage?: string;
    error?: string;
    quota?: number | null;
    skipped?: boolean;
    reason?: string;
    attempted?: number;
    candidate_count?: number;
  } | null;
  totals?: Record<string, number>;
  recent?: Array<Record<string, unknown>>;
  settings?: OutlookAutoRecoverySettings;
};

export type AccountRefreshAllStartOptions = {
  concurrency?: number;
  max_concurrency?: number;
  batch_size?: number;
  delay_between_accounts_sec?: number;
  delay_between_batches_sec?: number;
  stale_after_hours?: number;
  include_recent?: boolean;
  min_available_memory_mb?: number;
  max_load_1m?: number;
  resource_pause_enabled?: boolean;
  limit?: number;
  delete_invalid?: boolean;
  delete_after_failures?: number;
  expired_grace_hours?: number;
  panda_sync_enabled?: boolean;
  panda_sync_base_url?: string;
  panda_sync_batch_size?: number;
  panda_sync_timeout_seconds?: number;
  panda_sync_remove_local_on_success?: boolean;
};

export type AccountRefreshAllSettings = {
  concurrency?: number;
  max_concurrency?: number;
  batch_size?: number;
  delay_between_accounts_sec?: number;
  delay_between_batches_sec?: number;
  stale_after_hours?: number;
  include_recent?: boolean;
  min_available_memory_mb?: number;
  max_load_1m?: number;
  resource_pause_enabled?: boolean;
  resource_check_interval_sec?: number;
  delete_invalid?: boolean;
  delete_after_failures?: number;
  expired_grace_hours?: number;
};

export type AccountMaintenanceLoopSettings = {
  enabled?: boolean;
  batch_limit?: number;
  concurrency?: number;
  batch_size?: number;
  delay_between_accounts_sec?: number;
  delay_between_batches_sec?: number;
  cooldown_sec?: number;
  stale_after_hours?: number;
  include_recent?: boolean;
  min_available_memory_mb?: number;
  slow_min_available_memory_mb?: number;
  max_load_1m?: number;
  resource_pause_enabled?: boolean;
  resource_check_interval_sec?: number;
  slow_when_image_inflight?: number;
  pause_when_image_inflight?: number;
  slow_batch_limit?: number;
  slow_delay_between_accounts_sec?: number;
  slow_cooldown_sec?: number;
  startup_delay_sec?: number;
  delete_invalid?: boolean;
  delete_after_failures?: number;
  expired_grace_hours?: number;
};

type AccountUpdateResponse = {
  item: Account;
  items?: Account[];
};

export type ProxyRuntimeEgressMode = "direct" | "single_proxy";
export type ProxyRuntimeClearanceMode = "none" | "manual" | "flaresolverr";

export type ProxyRuntimeClearanceSettings = {
  enabled: boolean;
  mode: ProxyRuntimeClearanceMode;
  cf_cookies: string;
  cf_clearance: string;
  user_agent: string;
  browser: string;
  flaresolverr_url: string;
  timeout_sec: number | string;
  refresh_interval: number | string;
  warm_up_on_start: boolean;
  has_cf_cookies?: boolean;
  has_cf_clearance?: boolean;
};

export type ProxyRuntimeSettings = {
  enabled: boolean;
  egress_mode: ProxyRuntimeEgressMode;
  proxy_url: string;
  resource_proxy_url: string;
  skip_ssl_verify: boolean;
  reset_session_status_codes: number[];
  clearance: ProxyRuntimeClearanceSettings;
};

export type ProxyRuntimeStatus = {
  enabled: boolean;
  egress_mode: ProxyRuntimeEgressMode | string;
  proxy_source: string;
  has_proxy: boolean;
  clearance_enabled: boolean;
  clearance_mode: ProxyRuntimeClearanceMode | string;
  has_clearance_bundle: boolean;
  cached_clearance_hosts: string[];
};

export type ProxyRuntimeResponse = {
  runtime: ProxyRuntimeSettings;
  status: ProxyRuntimeStatus;
};

export type ThirdPartyAppsSettings = {
  infinite_canvas: {
    enabled: boolean;
    url: string;
  };
};

export type SettingsConfig = {
  proxy: string;
  base_url?: string;
  global_system_prompt?: string;
  sensitive_words?: string[];
  ai_review?: {
    enabled?: boolean;
    base_url?: string;
    api_key?: string;
    model?: string;
    prompt?: string;
  };
  refresh_account_interval_minute?: number | string;
  image_retention_days?: number | string;
  image_poll_timeout_secs?: number | string;
  image_account_concurrency?: number | string;
  image_parallel_generation?: boolean;
  image_settle_enabled?: boolean;
  image_check_before_hit_enabled?: boolean;
  image_settle_secs?: number | string;
  image_timeout_retry_secs?: number | string;
  auto_remove_invalid_accounts?: boolean;
  auto_remove_rate_limited_accounts?: boolean;
  auto_relogin_after_refresh?: boolean;
  log_levels?: string[];
  image_storage?: ImageStorageSettings;
  proxy_runtime?: ProxyRuntimeSettings;
  third_party_apps?: ThirdPartyAppsSettings;
  account_refresh_all?: AccountRefreshAllSettings;
  account_maintenance_loop?: AccountMaintenanceLoopSettings;
  backup?: BackupSettings;
  backup_state?: BackupState;
  [key: string]: unknown;
};

export type BackupInclude = {
  config: boolean;
  register: boolean;
  cpa: boolean;
  sub2api: boolean;
  logs: boolean;
  image_tasks: boolean;
  accounts_snapshot: boolean;
  auth_keys_snapshot: boolean;
  images: boolean;
};

export type BackupSettings = {
  enabled: boolean;
  provider: "cloudflare_r2" | string;
  account_id: string;
  access_key_id: string;
  secret_access_key: string;
  bucket: string;
  prefix: string;
  interval_minutes: number | string;
  rotation_keep: number | string;
  encrypt: boolean;
  passphrase: string;
  include: BackupInclude;
};

export type BackupState = {
  running: boolean;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  last_status?: string;
  last_error?: string | null;
  last_object_key?: string | null;
};

export type BackupItem = {
  key: string;
  name: string;
  size: number;
  updated_at?: string | null;
  encrypted: boolean;
};

export type BackupDetail = {
  key: string;
  name: string;
  encrypted: boolean;
  created_at?: string | null;
  trigger?: string | null;
  app_version?: string | null;
  storage_backend?: Record<string, unknown> | null;
  files: Array<{
    name: string;
    exists: boolean;
    content_type?: string;
    size: number;
    sha256?: string;
  }>;
  snapshots: Array<{
    name: string;
    count: number;
  }>;
};

export type ManagedImage = {
  rel: string;
  path?: string;
  name: string;
  date: string;
  size: number;
  url: string;
  thumbnail_url?: string;
  created_at: string;
  width?: number;
  height?: number;
  tags?: string[];
};

export type SystemLog = {
  id: string;
  time: string;
  type: "call" | "account" | string;
  summary?: string;
  detail?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ImageResponse = {
  created: number;
  data: Array<{ b64_json?: string; url?: string; revised_prompt?: string }>;
};

export type ImageTask = {
  id: string;
  status: "queued" | "running" | "success" | "error";
  mode: "generate" | "edit";
  model?: ImageModel;
  size?: string;
  quality?: string;
  created_at: string;
  updated_at: string;
  conversation_id?: string;
  data?: Array<{ b64_json?: string; url?: string; revised_prompt?: string }>;
  error?: string;
  progress?: string;
  elapsed_secs?: number;
  duration_ms?: number;
};

type ImageTaskListResponse = {
  items: ImageTask[];
  missing_ids: string[];
};

export type LoginResponse = {
  ok: boolean;
  version: string;
  role: AuthRole;
  subject_id: string;
  name: string;
};

export type UserKey = {
  id: string;
  name: string;
  role: "user";
  enabled: boolean;
  created_at: string | null;
  last_used_at: string | null;
};

export type OutlookPoolStats = {
  unused: number;
  in_use: number;
  used: number;
  token_invalid: number;
  failed: number;
};

export type RegisterConfig = {
  enabled: boolean;
  mail: {
    request_timeout: number;
    wait_timeout: number;
    wait_interval: number;
    providers: Array<Record<string, unknown>>;
  };
  proxy: string;
  proxy_count?: number;
  proxy_preview?: string[];
  total: number;
  threads: number;
  mode: "total" | "quota" | "available";
  target_quota: number;
  target_available: number;
  check_interval: number;
  stats: {
    job_id?: string;
    success: number;
    fail: number;
    skipped?: number;
    done: number;
    running: number;
    threads: number;
    elapsed_seconds?: number;
    avg_seconds?: number;
    success_rate?: number;
    current_quota?: number;
    current_available?: number;
    started_at?: string;
    updated_at?: string;
    finished_at?: string;
  };
  logs?: Array<{
    time: string;
    text: string;
    level: string;
  }>;
};

export async function login(authKey: string) {
  const normalizedAuthKey = String(authKey || "").trim();
  return httpRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: {},
    headers: {
      Authorization: `Bearer ${normalizedAuthKey}`,
    },
    redirectOnUnauthorized: false,
  });
}

export async function fetchAccounts(options: { offset?: number; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (typeof options.offset === "number") params.set("offset", String(options.offset));
  if (typeof options.limit === "number") params.set("limit", String(options.limit));
  const query = params.toString();
  return httpRequest<AccountListResponse>(`/api/accounts${query ? `?${query}` : ""}`);
}

export async function syncAccountsToPanda() {
  return httpRequest<PandaAccountSyncResponse>("/api/accounts/sync/panda", {
    method: "POST",
    body: {},
  });
}

export async function fetchAccountActivityDaily(days = 14) {
  return httpRequest<AccountActivityDailyResponse>(`/api/accounts/activity/daily?days=${days}`);
}

export async function fetchPandaSyncSettings() {
  return httpRequest<{ panda_sync: PandaSyncPublicSettings }>("/api/accounts/panda-sync");
}

export async function updatePandaSyncSettings(updates: Partial<Pick<PandaSyncPublicSettings, "enabled">>) {
  return httpRequest<{ panda_sync: PandaSyncPublicSettings }>("/api/accounts/panda-sync", {
    method: "POST",
    body: updates,
  });
}

export async function fetchModels() {
  return httpRequest<ModelListResponse>("/v1/models");
}

export async function createAccounts(tokens: string[], accounts: AccountImportPayload[] = []) {
  return httpRequest<AccountMutationResponse>("/api/accounts?include_items=false", {
    method: "POST",
    body: { tokens, accounts },
  });
}

export type OAuthLoginStartResponse = {
  session_id: string;
  authorize_url: string;
  expires_in: string;
  redirect_uri_prefix: string;
};

export async function startOAuthLogin(emailHint?: string) {
  return httpRequest<OAuthLoginStartResponse>("/api/accounts/oauth/start", {
    method: "POST",
    body: { email_hint: emailHint ?? "" },
  });
}

export async function finishOAuthLogin(sessionId: string, callback: string) {
  return httpRequest<AccountMutationResponse>("/api/accounts/oauth/finish", {
    method: "POST",
    body: { session_id: sessionId, callback },
  });
}

export async function deleteAccounts(tokens: string[]) {
  return httpRequest<AccountMutationResponse>("/api/accounts?include_items=false", {
    method: "DELETE",
    body: { tokens },
  });
}

export type OutlookAccountRecoveryProgress = {
  progress_id: string;
  done: boolean;
  ok: boolean;
  stage: string;
  message: string;
  email: string;
  error?: string;
  result?: {
    email: string;
    quota: number;
    status: string;
    schedulable: boolean;
    old_removed: boolean;
    old_fp_inherited: boolean;
    login_via_chatgpt_email_otp: boolean;
    report_dir: string;
    backup_dir: string;
  } | null;
};

export async function recoverOutlookAccount(accessToken: string) {
  return httpRequest<{ progress_id: string }>("/api/accounts/recover-outlook", {
    method: "POST",
    body: { access_token: accessToken },
  });
}

export async function fetchOutlookAccountRecoveryProgress(progressId: string) {
  return httpRequest<OutlookAccountRecoveryProgress>(
    `/api/accounts/recover-outlook/progress/${progressId}`,
  );
}

export async function refreshAccounts(accessTokens: string[]) {
  return httpRequest<{ progress_id: string }>("/api/accounts/refresh", {
    method: "POST",
    body: { access_tokens: accessTokens },
  });
}

export async function fetchRefreshProgress(progressId: string) {
  return httpRequest<RefreshProgressResponse>(`/api/accounts/refresh/progress/${progressId}`);
}

export async function startRefreshAllAccounts(options: AccountRefreshAllStartOptions = {}) {
  return httpRequest<AccountRefreshAllStatus>("/api/accounts/refresh-all/start", {
    method: "POST",
    body: options,
  });
}

export async function fetchRefreshAllStatus() {
  return httpRequest<AccountRefreshAllStatus>("/api/accounts/refresh-all/status");
}

export async function stopRefreshAllAccounts() {
  return httpRequest<AccountRefreshAllStatus>("/api/accounts/refresh-all/stop", {
    method: "POST",
    body: {},
  });
}

export async function fetchAccountMaintenanceLoopStatus() {
  return httpRequest<AccountMaintenanceLoopStatus>("/api/accounts/maintenance-loop/status");
}

export async function updateAccountMaintenanceLoop(settings: AccountMaintenanceLoopSettings) {
  return httpRequest<AccountMaintenanceLoopStatus>("/api/accounts/maintenance-loop", {
    method: "POST",
    body: settings,
  });
}

export async function fetchOutlookAutoRecoveryStatus() {
  return httpRequest<OutlookAutoRecoveryStatus>("/api/accounts/outlook-auto-recovery/status");
}

export async function updateOutlookAutoRecovery(settings: OutlookAutoRecoverySettings) {
  return httpRequest<OutlookAutoRecoveryStatus>("/api/accounts/outlook-auto-recovery", {
    method: "POST",
    body: settings,
  });
}

export async function reLoginAccounts(accessTokens: string[]) {
  return httpRequest<{ progress_id: string }>("/api/accounts/re-login", {
    method: "POST",
    body: { access_tokens: accessTokens },
  });
}

export async function fetchReLoginProgress(progressId: string) {
  return httpRequest<RefreshProgressResponse>(`/api/accounts/re-login/progress/${progressId}`);
}

export async function updateAccount(
  accessToken: string,
  updates: {
    type?: AccountType;
    status?: AccountStatus;
    quota?: number;
    proxy?: string;
  },
) {
  return httpRequest<AccountUpdateResponse>("/api/accounts/update?include_items=false", {
    method: "POST",
    body: {
      access_token: accessToken,
      ...updates,
    },
  });
}

export async function generateImage(prompt: string, model?: ImageModel, size?: string, quality = "auto") {
  return httpRequest<ImageResponse>(
    "/v1/images/generations",
    {
      method: "POST",
      body: {
        prompt,
        ...(model ? { model } : {}),
        ...(size ? { size } : {}),
        quality,
        n: 1,
        response_format: "b64_json",
      },
    },
  );
}

export async function editImage(files: File | File[], prompt: string, model?: ImageModel, size?: string, quality = "auto") {
  const formData = new FormData();
  const uploadFiles = Array.isArray(files) ? files : [files];

  uploadFiles.forEach((file) => {
    formData.append("image", file);
  });
  formData.append("prompt", prompt);
  if (model) {
    formData.append("model", model);
  }
  if (size) {
    formData.append("size", size);
  }
  formData.append("quality", quality);
  formData.append("n", "1");

  return httpRequest<ImageResponse>(
    "/v1/images/edits",
    {
      method: "POST",
      body: formData,
    },
  );
}

export async function createImageGenerationTask(clientTaskId: string, prompt: string, model?: ImageModel, size?: string, quality = "auto") {
  return httpRequest<ImageTask>("/api/image-tasks/generations", {
    method: "POST",
    body: {
      client_task_id: clientTaskId,
      prompt,
      ...(model ? { model } : {}),
      ...(size ? { size } : {}),
      quality,
    },
  });
}

export async function createImageEditTask(
  clientTaskId: string,
  files: File | File[],
  prompt: string,
  model?: ImageModel,
  size?: string,
  quality = "auto",
) {
  const formData = new FormData();
  const uploadFiles = Array.isArray(files) ? files : [files];

  uploadFiles.forEach((file) => {
    formData.append("image", file);
  });
  formData.append("client_task_id", clientTaskId);
  formData.append("prompt", prompt);
  if (model) {
    formData.append("model", model);
  }
  if (size) {
    formData.append("size", size);
  }
  formData.append("quality", quality);

  return httpRequest<ImageTask>("/api/image-tasks/edits", {
    method: "POST",
    body: formData,
  });
}

export async function fetchImageTasks(ids: string[]) {
  const params = new URLSearchParams();
  if (ids.length > 0) {
    params.set("ids", ids.join(","));
  }
  params.set("_t", String(Date.now()));
  return httpRequest<ImageTaskListResponse>(`/api/image-tasks?${params.toString()}`);
}

export async function resumeImagePoll(taskId: string, extraTimeoutSecs = 30) {
  return httpRequest<ImageTask>(`/api/image-tasks/${encodeURIComponent(taskId)}/resume-poll`, {
    method: "POST",
    body: { extra_timeout_secs: extraTimeoutSecs },
  });
}

export async function fetchSettingsConfig() {
  return httpRequest<{ config: SettingsConfig }>("/api/settings");
}

export async function updateSettingsConfig(settings: SettingsConfig) {
  return httpRequest<{ config: SettingsConfig }>("/api/settings", {
    method: "POST",
    body: settings,
  });
}

export async function fetchThirdPartyApps() {
  return httpRequest<{ third_party_apps: ThirdPartyAppsSettings }>("/api/third-party-apps");
}

export async function testBackupConnection() {
  return httpRequest<{ result: { ok: boolean; status: number } }>("/api/backup/test", {
    method: "POST",
    body: {},
  });
}

export async function testImageStorageConnection() {
  return httpRequest<{ result: { ok: boolean; status: number; error?: string } }>("/api/image-storage/test", {
    method: "POST",
    body: {},
  });
}

export async function syncImageStorage() {
  return httpRequest<{ result: { uploaded: number; skipped: number; failed: number } }>("/api/image-storage/sync", {
    method: "POST",
    body: {},
  });
}

export async function fetchBackups() {
  return httpRequest<{ items: BackupItem[]; state: BackupState; settings: BackupSettings }>("/api/backups");
}

export async function runBackupNow() {
  return httpRequest<{ result: { key: string; size: number; encrypted: boolean } }>("/api/backups/run", {
    method: "POST",
    body: {},
  });
}

export async function deleteBackup(key: string) {
  return httpRequest<{ ok: boolean }>("/api/backups/delete", {
    method: "POST",
    body: { key },
  });
}

export async function fetchBackupDetail(key: string) {
  const params = new URLSearchParams();
  params.set("key", key);
  return httpRequest<{ item: BackupDetail }>(`/api/backups/detail?${params.toString()}`);
}

export function getBackupDownloadUrl(key: string) {
  const params = new URLSearchParams();
  params.set("key", key);
  return `/api/backups/download?${params.toString()}`;
}

export async function fetchManagedImages(filters: { start_date?: string; end_date?: string }) {
  const params = new URLSearchParams();
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  return httpRequest<{ items: ManagedImage[]; groups: Array<{ date: string; items: ManagedImage[] }> }>(
    `/api/images${params.toString() ? `?${params.toString()}` : ""}`,
  );
}

export async function deleteManagedImages(body: { paths?: string[]; start_date?: string; end_date?: string; all_matching?: boolean }) {
  return httpRequest<{ removed: number }>("/api/images/delete", { method: "POST", body });
}

export async function downloadImages(paths: string[]) {
  const response = await request.post("/api/images/download", { paths }, { responseType: "blob" });
  const blob = response.data as Blob;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "images.zip";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function downloadSingleImage(path: string) {
  const response = await request.get(`/api/images/download/${path}`, { responseType: "blob" });
  const blob = response.data as Blob;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = path.split("/").pop() || "image.png";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function fetchImageTags() {
  return httpRequest<{ tags: string[] }>("/api/images/tags");
}

export async function setImageTags(path: string, tags: string[]) {
  return httpRequest<{ ok: boolean; tags: string[] }>("/api/images/tags", {
    method: "POST",
    body: { path, tags },
  });
}

export async function deleteImageTag(tag: string) {
  return httpRequest<{ ok: boolean; removed_from: number }>(`/api/images/tags/${encodeURIComponent(tag)}`, {
    method: "DELETE",
  });
}

export type ImageStorageStats = {
  disk_total_mb: number; disk_used_mb: number; disk_free_mb: number;
  image_count: number; image_size_mb: number; image_size_bytes: number;
};

export async function fetchImageStorage() {
  return httpRequest<ImageStorageStats>("/api/images/storage");
}

export async function compressAllImages() {
  return httpRequest<{ compressed: number; saved_bytes: number; saved_mb: number }>("/api/images/storage/compress", { method: "POST" });
}

export async function deleteToTarget(targetFreeMb: number) {
  return httpRequest<{ removed: number; freed_mb: number; done: boolean }>(
    `/api/images/storage/cleanup-to-target?target_free_mb=${targetFreeMb}&dry_run=false`,
    { method: "POST" },
  );
}

export async function fetchSystemLogs(filters: { type?: string; start_date?: string; end_date?: string }) {
  const params = new URLSearchParams();
  if (filters.type) params.set("type", filters.type);
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  return httpRequest<{ items: SystemLog[] }>(`/api/logs${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function deleteSystemLogs(ids: string[]) {
  return httpRequest<{ removed: number }>("/api/logs/delete", {
    method: "POST",
    body: { ids },
  });
}

export async function fetchUserKeys() {
  return httpRequest<{ items: UserKey[] }>("/api/auth/users");
}

export async function createUserKey(name: string) {
  return httpRequest<{ item: UserKey; key: string; items: UserKey[] }>("/api/auth/users", {
    method: "POST",
    body: { name },
  });
}

export async function updateUserKey(keyId: string, updates: { enabled?: boolean; name?: string; key?: string }) {
  return httpRequest<{ item: UserKey; items: UserKey[] }>(`/api/auth/users/${keyId}`, {
    method: "POST",
    body: updates,
  });
}

export async function deleteUserKey(keyId: string) {
  return httpRequest<{ items: UserKey[] }>(`/api/auth/users/${keyId}`, {
    method: "DELETE",
  });
}

export async function fetchRegisterConfig() {
  return httpRequest<{ register: RegisterConfig }>("/api/register");
}

export async function updateRegisterConfig(updates: Partial<RegisterConfig>) {
  return httpRequest<{ register: RegisterConfig }>("/api/register", {
    method: "POST",
    body: updates,
  });
}

export async function startRegister() {
  return httpRequest<{ register: RegisterConfig }>("/api/register/start", { method: "POST" });
}

export async function stopRegister() {
  return httpRequest<{ register: RegisterConfig }>("/api/register/stop", { method: "POST" });
}

export async function resetRegister() {
  return httpRequest<{ register: RegisterConfig }>("/api/register/reset", { method: "POST" });
}

export async function resetOutlookPool(scope: "all" | "failed" | "unused" = "all") {
  return httpRequest<{ register: RegisterConfig }>("/api/register/outlook-pool/reset", {
    method: "POST",
    body: { scope },
  });
}

// ── CPA (CLIProxyAPI) ──────────────────────────────────────────────

export type CPAPool = {
  id: string;
  name: string;
  base_url: string;
  import_job?: CPAImportJob | null;
};

export type CPARemoteFile = {
  name: string;
  email: string;
};

export type CPAImportJob = {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  created_at: string;
  updated_at: string;
  total: number;
  completed: number;
  added: number;
  skipped: number;
  refreshed: number;
  failed: number;
  errors: Array<{ name: string; error: string }>;
};

export async function fetchCPAPools() {
  return httpRequest<{ pools: CPAPool[] }>("/api/cpa/pools");
}

export async function createCPAPool(pool: { name: string; base_url: string; secret_key: string }) {
  return httpRequest<{ pool: CPAPool; pools: CPAPool[] }>("/api/cpa/pools", {
    method: "POST",
    body: pool,
  });
}

export async function updateCPAPool(
  poolId: string,
  updates: { name?: string; base_url?: string; secret_key?: string },
) {
  return httpRequest<{ pool: CPAPool; pools: CPAPool[] }>(`/api/cpa/pools/${poolId}`, {
    method: "POST",
    body: updates,
  });
}

export async function deleteCPAPool(poolId: string) {
  return httpRequest<{ pools: CPAPool[] }>(`/api/cpa/pools/${poolId}`, {
    method: "DELETE",
  });
}

export async function fetchCPAPoolFiles(poolId: string) {
  return httpRequest<{ pool_id: string; files: CPARemoteFile[] }>(`/api/cpa/pools/${poolId}/files`);
}

export async function startCPAImport(poolId: string, names: string[]) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/cpa/pools/${poolId}/import`, {
    method: "POST",
    body: { names },
  });
}

export async function fetchCPAPoolImportJob(poolId: string) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/cpa/pools/${poolId}/import`);
}

// ── Sub2API ────────────────────────────────────────────────────────

export type Sub2APIServer = {
  id: string;
  name: string;
  base_url: string;
  email: string;
  has_api_key: boolean;
  group_id: string;
  import_job?: CPAImportJob | null;
};

export type Sub2APIRemoteAccount = {
  id: string;
  name: string;
  email: string;
  plan_type: string;
  status: string;
  expires_at: string;
  has_refresh_token: boolean;
};

export type Sub2APIRemoteGroup = {
  id: string;
  name: string;
  description: string;
  platform: string;
  status: string;
  account_count: number;
  active_account_count: number;
};

export async function fetchSub2APIServers() {
  return httpRequest<{ servers: Sub2APIServer[] }>("/api/sub2api/servers");
}

export async function createSub2APIServer(server: {
  name: string;
  base_url: string;
  email: string;
  password: string;
  api_key: string;
  group_id: string;
}) {
  return httpRequest<{ server: Sub2APIServer; servers: Sub2APIServer[] }>("/api/sub2api/servers", {
    method: "POST",
    body: server,
  });
}

export async function updateSub2APIServer(
  serverId: string,
  updates: {
    name?: string;
    base_url?: string;
    email?: string;
    password?: string;
    api_key?: string;
    group_id?: string;
  },
) {
  return httpRequest<{ server: Sub2APIServer; servers: Sub2APIServer[] }>(`/api/sub2api/servers/${serverId}`, {
    method: "POST",
    body: updates,
  });
}

export async function fetchSub2APIServerGroups(serverId: string) {
  return httpRequest<{ server_id: string; groups: Sub2APIRemoteGroup[] }>(
    `/api/sub2api/servers/${serverId}/groups`,
  );
}

export async function deleteSub2APIServer(serverId: string) {
  return httpRequest<{ servers: Sub2APIServer[] }>(`/api/sub2api/servers/${serverId}`, {
    method: "DELETE",
  });
}

export async function fetchSub2APIServerAccounts(serverId: string) {
  return httpRequest<{ server_id: string; accounts: Sub2APIRemoteAccount[] }>(
    `/api/sub2api/servers/${serverId}/accounts`,
  );
}

export async function startSub2APIImport(serverId: string, accountIds: string[]) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/sub2api/servers/${serverId}/import`, {
    method: "POST",
    body: { account_ids: accountIds },
  });
}

export async function fetchSub2APIImportJob(serverId: string) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/sub2api/servers/${serverId}/import`);
}

// ── Upstream proxy ────────────────────────────────────────────────

export type ProxySettings = {
  enabled: boolean;
  url: string;
};

export type ProxyTestResult = {
  ok: boolean;
  status: number;
  latency_ms: number;
  error: string | null;
  proxy_source?: string;
  has_proxy?: boolean;
};

export type ClearanceTestResult = {
  ok: boolean;
  status: string;
  latency_ms: number;
  has_cookies: boolean;
  user_agent: string;
  error: string | null;
  runtime: ProxyRuntimeStatus;
};

export async function fetchProxy() {
  return httpRequest<{ proxy: ProxySettings }>("/api/proxy");
}

export async function updateProxy(updates: { enabled?: boolean; url?: string }) {
  return httpRequest<{ proxy: ProxySettings }>("/api/proxy", {
    method: "POST",
    body: updates,
  });
}

export async function testProxy(url?: string) {
  return httpRequest<{ result: ProxyTestResult }>("/api/proxy/test", {
    method: "POST",
    body: { url: url ?? "" },
  });
}

export async function fetchProxyRuntime() {
  return httpRequest<ProxyRuntimeResponse>("/api/proxy/runtime");
}

export async function updateProxyRuntime(runtime: ProxyRuntimeSettings) {
  return httpRequest<ProxyRuntimeResponse>("/api/proxy/runtime", {
    method: "POST",
    body: runtime,
  });
}

export async function testProxyClearance(targetUrl?: string) {
  return httpRequest<{ result: ClearanceTestResult }>("/api/proxy/clearance/test", {
    method: "POST",
    body: { target_url: targetUrl ?? "https://chatgpt.com" },
  });
}
