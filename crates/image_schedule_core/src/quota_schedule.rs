//! 批量评估四段日历 due 账号（Rust 侧选最优候选）。

use crate::binding_calendar::{
    account_phase_slot, list_due_phase_indices, local_date_for_unix, REFRESH_SALT,
};
use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Deserialize)]
pub struct ScheduleAccountInput {
    pub index: usize,
    pub account_key: String,
    pub binding_key: String,
    pub tz_name: String,
    pub local_date: String,
    #[serde(default)]
    pub phases_done: Vec<u32>,
    #[serde(default)]
    pub schedulable: bool,
}

#[derive(Debug, Deserialize)]
pub struct ScheduleEvaluateInput {
    pub now_unix: i64,
    #[serde(default)]
    pub binding_last_refresh_unix: HashMap<String, f64>,
    #[serde(default = "default_binding_gap_sec")]
    pub binding_gap_sec: f64,
    #[serde(default = "default_jitter_min")]
    pub jitter_min_minutes: u32,
    #[serde(default = "default_jitter_max")]
    pub jitter_max_minutes: u32,
    pub accounts: Vec<ScheduleAccountInput>,
}

fn default_binding_gap_sec() -> f64 {
    7200.0
}
fn default_jitter_min() -> u32 {
    30
}
fn default_jitter_max() -> u32 {
    60
}

#[derive(Debug, Serialize)]
pub struct SchedulePick {
    pub index: usize,
    pub phase_index: u32,
    pub account_slot_unix: i64,
    pub binding_key: String,
}

#[derive(Debug, Serialize)]
pub struct ScheduleEvaluateOutput {
    pub picked: Option<SchedulePick>,
}

fn parse_local_date(text: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d").ok()
}

pub fn evaluate_pick(input: &ScheduleEvaluateInput) -> ScheduleEvaluateOutput {
    let mut best: Option<(i64, SchedulePick)> = None;
    for account in &input.accounts {
        if !account.schedulable {
            continue;
        }
        let local_day = match parse_local_date(&account.local_date) {
            Some(day) => day,
            None => local_date_for_unix(input.now_unix, &account.tz_name),
        };
        let due_phases = list_due_phase_indices(
            input.now_unix,
            &account.tz_name,
            &account.phases_done,
            local_day,
        );
        if due_phases.is_empty() {
            continue;
        }
        let last = input
            .binding_last_refresh_unix
            .get(&account.binding_key)
            .copied()
            .unwrap_or(0.0);
        if input.now_unix as f64 - last < input.binding_gap_sec {
            continue;
        }
        for phase in due_phases {
            let slot = account_phase_slot(
                &account.account_key,
                &account.binding_key,
                local_day,
                phase as usize,
                &account.tz_name,
                input.jitter_min_minutes,
                input.jitter_max_minutes,
                REFRESH_SALT,
            );
            if slot.account_slot_unix <= input.now_unix {
                let pick = SchedulePick {
                    index: account.index,
                    phase_index: phase,
                    account_slot_unix: slot.account_slot_unix,
                    binding_key: account.binding_key.clone(),
                };
                match &best {
                    None => best = Some((slot.account_slot_unix, pick)),
                    Some((ts, _)) if slot.account_slot_unix < *ts => {
                        best = Some((slot.account_slot_unix, pick));
                    }
                    _ => {}
                }
                break;
            }
        }
    }
    ScheduleEvaluateOutput {
        picked: best.map(|(_, pick)| pick),
    }
}

pub fn evaluate_pick_json(input_json: &str) -> Result<String, String> {
    let input: ScheduleEvaluateInput =
        serde_json::from_str(input_json).map_err(|e| format!("parse input: {e}"))?;
    let output = evaluate_pick(&input);
    serde_json::to_string(&output).map_err(|e| format!("serialize output: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn picks_earliest_due() {
        let day = chrono::NaiveDate::from_ymd_opt(2026, 7, 28).unwrap();
        let now = crate::binding_calendar::account_phase_slot(
            "a1",
            "b1",
            day,
            0,
            "Asia/Singapore",
            30,
            60,
            REFRESH_SALT,
        )
        .account_slot_unix
            + 1;
        let input = ScheduleEvaluateInput {
            now_unix: now,
            binding_last_refresh_unix: HashMap::new(),
            binding_gap_sec: 0.0,
            jitter_min_minutes: 30,
            jitter_max_minutes: 60,
            accounts: vec![ScheduleAccountInput {
                index: 3,
                account_key: "a1".into(),
                binding_key: "b1".into(),
                tz_name: "Asia/Singapore".into(),
                local_date: day.format("%Y-%m-%d").to_string(),
                phases_done: vec![],
                schedulable: true,
            }],
        };
        let out = evaluate_pick(&input);
        assert!(out.picked.is_some());
        assert_eq!(out.picked.unwrap().index, 3);
    }
}
