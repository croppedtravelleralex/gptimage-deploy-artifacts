//! Slot ledger: account inflight + sS slot FSM with watchdog reconcile.

use parking_lot::Mutex;
use std::collections::HashMap;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlotKind {
    Account,
    Ss,
}

#[derive(Debug, Clone)]
struct HeldLease {
    holder_key: String,
    token_hash: u64,
    acquired_at: Instant,
    deadline: Option<Instant>,
}

#[derive(Debug, Default, Clone)]
pub struct ReconcileReport {
    pub account_held: u32,
    pub ss_held: u32,
    pub account_expired_forced: u32,
    pub ss_expired_forced: u32,
    pub orphan_account: u32,
    pub orphan_ss: u32,
}

#[derive(Debug, Default)]
pub struct SlotLedger {
    account_by_holder: HashMap<String, HeldLease>,
    ss_by_holder: HashMap<String, HeldLease>,
    account_count_by_token: HashMap<u64, u32>,
    forced_releases: u32,
}

impl SlotLedger {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn try_acquire_account(
        &mut self,
        holder_key: &str,
        token_hash: u64,
        deadline: Option<Duration>,
    ) -> bool {
        if holder_key.is_empty() {
            return false;
        }
        if self.account_by_holder.contains_key(holder_key) {
            return false;
        }
        let lease = HeldLease {
            holder_key: holder_key.to_string(),
            token_hash,
            acquired_at: Instant::now(),
            deadline: deadline.map(|d| Instant::now() + d),
        };
        self.account_by_holder
            .insert(holder_key.to_string(), lease);
        *self
            .account_count_by_token
            .entry(token_hash)
            .or_insert(0) += 1;
        true
    }

    pub fn release_account(&mut self, holder_key: &str) -> bool {
        let Some(lease) = self.account_by_holder.remove(holder_key) else {
            return false;
        };
        if let Some(count) = self.account_count_by_token.get_mut(&lease.token_hash) {
            *count = count.saturating_sub(1);
            if *count == 0 {
                self.account_count_by_token.remove(&lease.token_hash);
            }
        }
        true
    }

    pub fn try_acquire_ss(&mut self, holder_key: &str, deadline: Option<Duration>) -> bool {
        if holder_key.is_empty() {
            return false;
        }
        if self.ss_by_holder.contains_key(holder_key) {
            return false;
        }
        let lease = HeldLease {
            holder_key: holder_key.to_string(),
            token_hash: 0,
            acquired_at: Instant::now(),
            deadline: deadline.map(|d| Instant::now() + d),
        };
        self.ss_by_holder.insert(holder_key.to_string(), lease);
        true
    }

    pub fn release_ss(&mut self, holder_key: &str) -> bool {
        self.ss_by_holder.remove(holder_key).is_some()
    }

    pub fn account_inflight_for_token(&self, token_hash: u64) -> u32 {
        self.account_count_by_token.get(&token_hash).copied().unwrap_or(0)
    }

    pub fn total_account_inflight(&self) -> u32 {
        self.account_count_by_token.values().copied().sum()
    }

    pub fn ss_held_count(&self) -> u32 {
        self.ss_by_holder.len() as u32
    }

    pub fn forced_release_count(&self) -> u32 {
        self.forced_releases
    }

    /// Drop leases past deadline; returns holders force-released.
    pub fn watchdog_tick(&mut self, force_release_expired: bool) -> ReconcileReport {
        let mut report = ReconcileReport {
            account_held: self.account_by_holder.len() as u32,
            ss_held: self.ss_by_holder.len() as u32,
            ..Default::default()
        };
        let now = Instant::now();

        let expired_accounts: Vec<String> = self
            .account_by_holder
            .iter()
            .filter_map(|(k, lease)| {
                lease
                    .deadline
                    .filter(|d| now >= *d)
                    .map(|_| k.clone())
            })
            .collect();
        let expired_ss: Vec<String> = self
            .ss_by_holder
            .iter()
            .filter_map(|(k, lease)| {
                lease
                    .deadline
                    .filter(|d| now >= *d)
                    .map(|_| k.clone())
            })
            .collect();

        if force_release_expired {
            for key in &expired_accounts {
                if self.release_account(key) {
                    report.account_expired_forced += 1;
                    self.forced_releases += 1;
                }
            }
            for key in &expired_ss {
                if self.release_ss(key) {
                    report.ss_expired_forced += 1;
                    self.forced_releases += 1;
                }
            }
        }

        report.account_held = self.account_by_holder.len() as u32;
        report.ss_held = self.ss_by_holder.len() as u32;
        report
    }

    pub fn stats_json(&self) -> String {
        format!(
            r#"{{"account_held":{},"ss_held":{},"total_account_inflight":{},"forced_releases":{}}}"#,
            self.account_by_holder.len(),
            self.ss_by_holder.len(),
            self.total_account_inflight(),
            self.forced_releases
        )
    }
}

pub type SharedSlotLedger = Mutex<SlotLedger>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn account_acquire_release() {
        let mut ledger = SlotLedger::new();
        assert!(ledger.try_acquire_account("t1", 42, None));
        assert_eq!(ledger.total_account_inflight(), 1);
        assert!(ledger.release_account("t1"));
        assert_eq!(ledger.total_account_inflight(), 0);
    }

    #[test]
    fn ss_watchdog_force_release() {
        let mut ledger = SlotLedger::new();
        assert!(ledger.try_acquire_ss("ss-1", Some(Duration::from_millis(1))));
        std::thread::sleep(Duration::from_millis(5));
        let report = ledger.watchdog_tick(true);
        assert_eq!(report.ss_expired_forced, 1);
        assert_eq!(ledger.ss_held_count(), 0);
    }

    #[test]
    fn duplicate_holder_rejected() {
        let mut ledger = SlotLedger::new();
        assert!(ledger.try_acquire_account("h", 1, None));
        assert!(!ledger.try_acquire_account("h", 1, None));
    }
}
