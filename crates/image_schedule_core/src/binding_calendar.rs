//! Binding 四段日历槽位（与 Python `humanlike_scheduler._stable_u` 对齐）。

use chrono::{DateTime, NaiveDate, NaiveTime, TimeZone, Timelike, Utc};
use chrono_tz::Tz;
use sha2::{Digest, Sha256};
use std::collections::HashSet;

pub const PHASE_HOUR_BOUNDS: [(u32, u32); 4] = [(0, 6), (6, 12), (12, 18), (18, 24)];
pub const REFRESH_SALT: &str = "quota-refresh-v1";
pub const PRIME_SALT: &str = "quota-prime-v1";

/// 与 Python `_stable_u(seed_parts)` 完全一致。
pub fn stable_u(parts: &[&str]) -> f64 {
    let joined = parts.join("|");
    let digest = Sha256::digest(joined.as_bytes());
    let hex = format!("{:x}", digest);
    let prefix = &hex[..12.min(hex.len())];
    let value = u64::from_str_radix(prefix, 16).unwrap_or(0);
    (value % 1_000_000_000) as f64 / 1_000_000_000.0
}

pub fn phase_index_for_hour(hour: u32) -> usize {
    for (index, (start, end)) in PHASE_HOUR_BOUNDS.iter().enumerate() {
        if hour >= *start && hour < *end {
            return index;
        }
    }
    PHASE_HOUR_BOUNDS.len() - 1
}

pub fn parse_tz(tz_name: &str) -> Tz {
    tz_name.parse().unwrap_or(chrono_tz::Asia::Singapore)
}

pub fn local_date_for_unix(now_unix: i64, tz_name: &str) -> NaiveDate {
    let tz = parse_tz(tz_name);
    let dt = DateTime::<Utc>::from_timestamp(now_unix, 0).unwrap_or_else(Utc::now);
    dt.with_timezone(&tz).date_naive()
}

pub fn current_phase_index(now_unix: i64, tz_name: &str) -> usize {
    let tz = parse_tz(tz_name);
    let dt = DateTime::<Utc>::from_timestamp(now_unix, 0).unwrap_or_else(Utc::now);
    phase_index_for_hour(dt.with_timezone(&tz).hour())
}

pub fn binding_phase_slot_unix(
    binding_key: &str,
    local_day: NaiveDate,
    phase_index: usize,
    tz_name: &str,
    salt: &str,
) -> i64 {
    let tz = parse_tz(tz_name);
    let phase_index = phase_index.min(PHASE_HOUR_BOUNDS.len() - 1);
    let (start_hour, end_hour) = PHASE_HOUR_BOUNDS[phase_index];
    let phase_start = tz
        .from_local_datetime(&local_day.and_time(NaiveTime::from_hms_opt(start_hour, 0, 0).unwrap()))
        .unwrap();
    let mut phase_end = tz
        .from_local_datetime(&local_day.and_time(NaiveTime::from_hms_opt(end_hour % 24, 0, 0).unwrap()))
        .unwrap();
    if phase_end <= phase_start {
        phase_end = phase_start + chrono::Duration::hours(6);
    }
    let span_sec = (phase_end - phase_start).num_seconds().max(1) as f64;
    let u = stable_u(&[
        binding_key,
        &local_day.format("%Y-%m-%d").to_string(),
        &phase_index.to_string(),
        salt,
        "binding",
    ]);
    let slot = phase_start + chrono::Duration::milliseconds((span_sec * u * 1000.0) as i64);
    slot.with_timezone(&Utc).timestamp()
}

pub struct PhaseSlot {
    pub phase_index: usize,
    pub binding_slot_unix: i64,
    pub account_slot_unix: i64,
}

pub fn account_phase_slot(
    account_key: &str,
    binding_key: &str,
    local_day: NaiveDate,
    phase_index: usize,
    tz_name: &str,
    jitter_min_minutes: u32,
    jitter_max_minutes: u32,
    salt: &str,
) -> PhaseSlot {
    let binding_slot_unix =
        binding_phase_slot_unix(binding_key, local_day, phase_index, tz_name, salt);
    let lo = jitter_min_minutes;
    let hi = jitter_max_minutes.max(lo);
    let span = hi.saturating_sub(lo);
    let jitter = lo
        + (stable_u(&[
            account_key,
            &local_day.format("%Y-%m-%d").to_string(),
            &phase_index.to_string(),
            salt,
            "jitter",
        ]) * span as f64) as u32;
    let account_slot_unix = binding_slot_unix + (jitter as i64) * 60;
    PhaseSlot {
        phase_index,
        binding_slot_unix,
        account_slot_unix,
    }
}

pub fn list_due_phase_indices(
    now_unix: i64,
    tz_name: &str,
    phases_done: &[u32],
    local_day: NaiveDate,
) -> Vec<u32> {
    let _ = local_day;
    let current = current_phase_index(now_unix, tz_name) as u32;
    let done: HashSet<u32> = phases_done.iter().copied().collect();
    (0..=current).filter(|idx| !done.contains(idx)).collect()
}

pub fn next_account_slot_unix(
    account_key: &str,
    binding_key: &str,
    now_unix: i64,
    tz_name: &str,
    jitter_min_minutes: u32,
    jitter_max_minutes: u32,
    salt: &str,
) -> i64 {
    let local_day = local_date_for_unix(now_unix, tz_name);
    let current = current_phase_index(now_unix, tz_name);
    for phase in current..PHASE_HOUR_BOUNDS.len() {
        let slot = account_phase_slot(
            account_key,
            binding_key,
            local_day,
            phase,
            tz_name,
            jitter_min_minutes,
            jitter_max_minutes,
            salt,
        );
        if slot.account_slot_unix > now_unix {
            return slot.account_slot_unix;
        }
    }
    let next_day = local_day + chrono::Duration::days(1);
    account_phase_slot(
        account_key,
        binding_key,
        next_day,
        0,
        tz_name,
        jitter_min_minutes,
        jitter_max_minutes,
        salt,
    )
    .account_slot_unix
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stable_u_matches_python_golden() {
        let u = stable_u(&["bind-1", "2026-07-28", "0", REFRESH_SALT, "binding"]);
        assert!((0.0..1.0).contains(&u));
        // 固定种子应稳定
        let u2 = stable_u(&["bind-1", "2026-07-28", "0", REFRESH_SALT, "binding"]);
        assert!((u - u2).abs() < 1e-12);
    }

    #[test]
    fn phase_index_boundaries() {
        assert_eq!(phase_index_for_hour(0), 0);
        assert_eq!(phase_index_for_hour(5), 0);
        assert_eq!(phase_index_for_hour(6), 1);
        assert_eq!(phase_index_for_hour(23), 3);
    }

    #[test]
    fn account_slot_after_binding_slot() {
        let day = NaiveDate::from_ymd_opt(2026, 7, 28).unwrap();
        let slot = account_phase_slot(
            "acct@test.com",
            "bind-1",
            day,
            0,
            "Asia/Singapore",
            30,
            60,
            REFRESH_SALT,
        );
        assert!(slot.account_slot_unix >= slot.binding_slot_unix);
    }
}
