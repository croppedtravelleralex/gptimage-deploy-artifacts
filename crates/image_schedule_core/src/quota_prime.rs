//! 额度窗口预热准入规则（与 Python `quota_window_prime_service` 对齐）。

use serde::{Deserialize, Serialize};

const NEW_MATURITY_STAGES: &[&str] = &[
    "observe", "t0", "t1h", "t6h", "t24h", "t72h", "new", "incoming",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PrimeEvalMode {
    Auto,
    /// Admin 号池按钮：放宽观察期/新号/已生图，仍要求满额可调度。
    Manual,
    Force,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PrimeSettingsInput {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_full_quota")]
    pub full_quota: i32,
    #[serde(default = "default_min_age_days")]
    pub min_account_age_days: f64,
    #[serde(default)]
    pub skip_panda_sync_states: Vec<String>,
}

fn default_full_quota() -> i32 {
    25
}
fn default_min_age_days() -> f64 {
    7.0
}

#[derive(Debug, Clone, Deserialize)]
pub struct PrimeAccountInput {
    #[serde(default)]
    pub index: usize,
    pub quota: i32,
    #[serde(default)]
    pub success: i32,
    #[serde(default)]
    pub prime_state: String,
    #[serde(default)]
    pub primed_at: String,
    #[serde(default)]
    pub attempts: u32,
    #[serde(default)]
    pub image_quota_unknown: bool,
    #[serde(default)]
    pub account_type: String,
    #[serde(default)]
    pub image_schedulable: bool,
    #[serde(default)]
    pub panda_sync_state: String,
    #[serde(default)]
    pub panda_receive_state: String,
    #[serde(default)]
    pub maturity_stage: String,
    #[serde(default)]
    pub created_at_unix: Option<i64>,
    #[serde(default)]
    pub imported_at_unix: Option<i64>,
    #[serde(default)]
    pub first_seen_at_unix: Option<i64>,
    #[serde(default)]
    pub registered_at_unix: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct PrimeEvaluateRequest {
    pub mode: PrimeEvalMode,
    #[serde(default)]
    pub now_unix: i64,
    pub settings: PrimeSettingsInput,
    pub account: PrimeAccountInput,
}

#[derive(Debug, Serialize)]
pub struct PrimeEvaluateResult {
    pub eligible: bool,
    pub reason: String,
}

#[derive(Debug, Deserialize)]
pub struct PrimeListEligibleRequest {
    #[serde(default)]
    pub now_unix: i64,
    pub settings: PrimeSettingsInput,
    #[serde(default)]
    pub max_attempts: u32,
    pub accounts: Vec<PrimeAccountInput>,
}

#[derive(Debug, Serialize)]
pub struct PrimeListEligibleResult {
    pub indices: Vec<usize>,
}

pub fn normalize_plan_type(raw: &str) -> String {
    let text = raw.trim();
    if text.is_empty() {
        return String::new();
    }
    let lower = text.to_ascii_lowercase().replace('-', "_");
    match lower.as_str() {
        "pro" => "Pro".to_string(),
        "prolite" | "pro_lite" => "ProLite".to_string(),
        "plus" => "Plus".to_string(),
        "free" => "free".to_string(),
        _ => text.to_string(),
    }
}

pub fn is_true_unlimited(account_type: &str) -> bool {
    let normalized = normalize_plan_type(account_type);
    normalized == "Pro" || normalized == "ProLite"
}

pub fn is_new_image_account(account: &PrimeAccountInput, now_unix: i64, max_age_days: f64) -> bool {
    let stage = account.maturity_stage.trim().to_ascii_lowercase();
    if !stage.is_empty() {
        if NEW_MATURITY_STAGES.contains(&stage.as_str()) {
            return true;
        }
    } else if NEW_MATURITY_STAGES.contains(&"") {
        // empty stage is in Python list: stage in {"", "observe", ...} with `if stage: return True`
        // empty stage alone does NOT return True from stage check
    }
    let max_age = max_age_days.max(0.0);
    if max_age <= 0.0 {
        return false;
    }
    for ts in [
        account.created_at_unix,
        account.imported_at_unix,
        account.first_seen_at_unix,
        account.registered_at_unix,
    ] {
        let Some(unix) = ts else {
            continue;
        };
        let age_days = (now_unix - unix).max(0) as f64 / 86400.0;
        if age_days < max_age {
            return true;
        }
    }
    false
}

const VERIFIED_RECEIVE_STATES: &[&str] = &[
    "verified_ready",
    "verified",
    "local_verified",
    "ready",
];

fn is_verified_on_panda(receive_state: &str) -> bool {
    let receive = receive_state.trim().to_ascii_lowercase();
    VERIFIED_RECEIVE_STATES.contains(&receive.as_str())
}

fn panda_sync_blocks_auto(account: &PrimeAccountInput, skip_states: &[String]) -> bool {
    let sync = account.panda_sync_state.trim().to_ascii_lowercase();
    if sync.is_empty() || sync == "synced" {
        return false;
    }
    if !skip_states
        .iter()
        .any(|item| item.trim().eq_ignore_ascii_case(&sync))
    {
        return false;
    }
    // Panda 已验收号：sync=ready 仅表示本地上传语义，不是注册观察期。
    if is_verified_on_panda(&account.panda_receive_state) {
        return false;
    }
    true
}

fn prime_state_of(account: &PrimeAccountInput) -> String {
    let state = account.prime_state.trim().to_ascii_lowercase();
    if state.is_empty() {
        "none".to_string()
    } else {
        state
    }
}

pub fn evaluate_prime_eligibility(req: &PrimeEvaluateRequest) -> PrimeEvaluateResult {
    let settings = &req.settings;
    let account = &req.account;
    if !settings.enabled {
        return PrimeEvaluateResult {
            eligible: false,
            reason: "disabled".into(),
        };
    }
    let state = prime_state_of(account);
    if req.mode == PrimeEvalMode::Force {
        if state == "pending" || state == "running" {
            return PrimeEvaluateResult {
                eligible: false,
                reason: format!("state:{state}"),
            };
        }
        return PrimeEvaluateResult {
            eligible: true,
            reason: "force".into(),
        };
    }
    let manual = req.mode == PrimeEvalMode::Manual;
    if state == "pending" || state == "running" || (!manual && state == "done") {
        return PrimeEvaluateResult {
            eligible: false,
            reason: format!("state:{state}"),
        };
    }
    if is_true_unlimited(&account.account_type) {
        return PrimeEvaluateResult {
            eligible: false,
            reason: "unlimited".into(),
        };
    }
    if account.image_quota_unknown {
        return PrimeEvaluateResult {
            eligible: false,
            reason: "unknown_quota".into(),
        };
    }
    if !account.image_schedulable {
        return PrimeEvaluateResult {
            eligible: false,
            reason: "not_schedulable".into(),
        };
    }
    if account.quota != settings.full_quota {
        return PrimeEvaluateResult {
            eligible: false,
            reason: "quota_not_full".into(),
        };
    }
    if !manual {
        if account.success > 0 {
            return PrimeEvaluateResult {
                eligible: false,
                reason: "already_imaged".into(),
            };
        }
        if !account.primed_at.trim().is_empty() {
            return PrimeEvaluateResult {
                eligible: false,
                reason: "already_primed".into(),
            };
        }
        if panda_sync_blocks_auto(account, &settings.skip_panda_sync_states) {
            return PrimeEvaluateResult {
                eligible: false,
                reason: "panda_sync".into(),
            };
        }
        if account.panda_receive_state.trim().eq_ignore_ascii_case("incoming") {
            return PrimeEvaluateResult {
                eligible: false,
                reason: "incoming".into(),
            };
        }
        if is_new_image_account(account, req.now_unix, settings.min_account_age_days) {
            return PrimeEvaluateResult {
                eligible: false,
                reason: "new_account".into(),
            };
        }
    } else if state == "done" {
        return PrimeEvaluateResult {
            eligible: false,
            reason: "state:done".into(),
        };
    }
    PrimeEvaluateResult {
        eligible: true,
        reason: if manual { "manual".into() } else { "eligible".into() },
    }
}

pub fn list_auto_eligible(req: &PrimeListEligibleRequest) -> PrimeListEligibleResult {
    let max_attempts = req.max_attempts.max(1);
    let mut indices = Vec::new();
    for account in &req.accounts {
        if account.attempts >= max_attempts {
            continue;
        }
        let eval = evaluate_prime_eligibility(&PrimeEvaluateRequest {
            mode: PrimeEvalMode::Auto,
            now_unix: req.now_unix,
            settings: req.settings.clone(),
            account: account.clone(),
        });
        if eval.eligible {
            indices.push(account.index);
        }
    }
    PrimeListEligibleResult { indices }
}

pub fn evaluate_prime_json(input_json: &str) -> Result<String, String> {
    let req: PrimeEvaluateRequest =
        serde_json::from_str(input_json).map_err(|e| format!("parse input: {e}"))?;
    let out = evaluate_prime_eligibility(&req);
    serde_json::to_string(&out).map_err(|e| format!("serialize output: {e}"))
}

pub fn list_eligible_json(input_json: &str) -> Result<String, String> {
    let req: PrimeListEligibleRequest =
        serde_json::from_str(input_json).map_err(|e| format!("parse input: {e}"))?;
    let out = list_auto_eligible(&req);
    serde_json::to_string(&out).map_err(|e| format!("serialize output: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base_account() -> PrimeAccountInput {
        PrimeAccountInput {
            index: 1,
            quota: 25,
            success: 0,
            prime_state: "none".into(),
            primed_at: String::new(),
            attempts: 0,
            image_quota_unknown: false,
            account_type: "Plus".into(),
            image_schedulable: true,
            panda_sync_state: "synced".into(),
            panda_receive_state: String::new(),
            maturity_stage: String::new(),
            created_at_unix: Some(1_600_000_000),
            imported_at_unix: None,
            first_seen_at_unix: None,
            registered_at_unix: None,
        }
    }

    fn base_settings() -> PrimeSettingsInput {
        PrimeSettingsInput {
            enabled: true,
            full_quota: 25,
            min_account_age_days: 7.0,
            skip_panda_sync_states: vec!["staging".into(), "ready".into()],
        }
    }

    #[test]
    fn eligible_plus_full_quota() {
        let out = evaluate_prime_eligibility(&PrimeEvaluateRequest {
            mode: PrimeEvalMode::Auto,
            now_unix: 1_900_000_000,
            settings: base_settings(),
            account: base_account(),
        });
        assert!(out.eligible);
    }

    #[test]
    fn rejects_new_account() {
        let mut account = base_account();
        account.created_at_unix = Some(1_899_500_000);
        let out = evaluate_prime_eligibility(&PrimeEvaluateRequest {
            mode: PrimeEvalMode::Auto,
            now_unix: 1_900_000_000,
            settings: base_settings(),
            account,
        });
        assert!(!out.eligible);
        assert_eq!(out.reason, "new_account");
    }

    #[test]
    fn force_bypasses_new_account() {
        let mut account = base_account();
        account.created_at_unix = Some(1_899_000_000);
        let out = evaluate_prime_eligibility(&PrimeEvaluateRequest {
            mode: PrimeEvalMode::Force,
            now_unix: 1_900_000_000,
            settings: base_settings(),
            account,
        });
        assert!(out.eligible);
    }

    #[test]
    fn verified_ready_bypasses_panda_sync_ready() {
        let mut account = base_account();
        account.panda_sync_state = "ready".into();
        account.panda_receive_state = "verified_ready".into();
        let out = evaluate_prime_eligibility(&PrimeEvaluateRequest {
            mode: PrimeEvalMode::Auto,
            now_unix: 1_900_000_000,
            settings: base_settings(),
            account,
        });
        assert!(out.eligible);
    }

    #[test]
    fn manual_allows_already_imaged() {
        let mut account = base_account();
        account.success = 3;
        let out = evaluate_prime_eligibility(&PrimeEvaluateRequest {
            mode: PrimeEvalMode::Manual,
            now_unix: 1_900_000_000,
            settings: base_settings(),
            account,
        });
        assert!(out.eligible);
        assert_eq!(out.reason, "manual");
    }
}
