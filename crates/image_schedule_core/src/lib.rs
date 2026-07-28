//! Schedule core: dispatch gate, lease pool hints, sediment parser, slot ledger.

mod binding_calendar;
mod dispatch_gate;
mod lease_pool;
mod quota_prime;
mod quota_schedule;
mod sediment;
mod slot_ledger;

pub use binding_calendar::{
    account_phase_slot, binding_phase_slot_unix, current_phase_index, list_due_phase_indices,
    next_account_slot_unix, stable_u, PhaseSlot, PRIME_SALT, REFRESH_SALT,
};
pub use dispatch_gate::DispatchGate;
pub use lease_pool::LeasePool;
pub use quota_prime::{
    evaluate_prime_eligibility, evaluate_prime_json, is_new_image_account, is_true_unlimited,
    list_auto_eligible, list_eligible_json, normalize_plan_type, PrimeEvaluateRequest,
    PrimeEvaluateResult, PrimeEvalMode,
};
pub use quota_schedule::{evaluate_pick_json, ScheduleEvaluateInput, ScheduleEvaluateOutput};
pub use sediment::SedimentParser;
pub use slot_ledger::{ReconcileReport, SlotLedger};

use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::os::raw::c_char;
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::Duration;

static NEXT_POOL: AtomicU64 = AtomicU64::new(1);
static POOLS: OnceLock<parking_lot::Mutex<HashMap<u64, LeasePool>>> = OnceLock::new();
static GATES: OnceLock<parking_lot::Mutex<HashMap<u64, DispatchGate>>> = OnceLock::new();
static PARSERS: OnceLock<parking_lot::Mutex<HashMap<u64, SedimentParser>>> = OnceLock::new();
static LEDGERS: OnceLock<parking_lot::Mutex<HashMap<u64, SlotLedger>>> = OnceLock::new();

fn pools() -> &'static parking_lot::Mutex<HashMap<u64, LeasePool>> {
    POOLS.get_or_init(|| parking_lot::Mutex::new(HashMap::new()))
}

fn gates() -> &'static parking_lot::Mutex<HashMap<u64, DispatchGate>> {
    GATES.get_or_init(|| parking_lot::Mutex::new(HashMap::new()))
}

fn parsers() -> &'static parking_lot::Mutex<HashMap<u64, SedimentParser>> {
    PARSERS.get_or_init(|| parking_lot::Mutex::new(HashMap::new()))
}

fn ledgers() -> &'static parking_lot::Mutex<HashMap<u64, SlotLedger>> {
    LEDGERS.get_or_init(|| parking_lot::Mutex::new(HashMap::new()))
}

fn cstr_to_str(ptr: *const c_char) -> Option<&'static str> {
    if ptr.is_null() {
        return None;
    }
    unsafe { CStr::from_ptr(ptr) }.to_str().ok()
}

fn deadline_from_ns(deadline_ns: u64) -> Option<Duration> {
    if deadline_ns == 0 {
        None
    } else {
        Some(Duration::from_nanos(deadline_ns))
    }
}

#[no_mangle]
pub extern "C" fn isc_dispatch_should_wait(
    _last_start_mono_ns: u64,
    interval_ms: u32,
    inflight: u32,
    cap: u32,
    queued: u32,
) -> u8 {
    if DispatchGate::should_wait(interval_ms, inflight, cap, queued) {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn isc_lease_pool_create(cap: u32) -> u64 {
    let id = NEXT_POOL.fetch_add(1, Ordering::Relaxed);
    pools().lock().insert(id, LeasePool::new(cap));
    id
}

#[no_mangle]
pub extern "C" fn isc_lease_pool_destroy(handle: u64) {
    pools().lock().remove(&handle);
}

#[no_mangle]
pub extern "C" fn isc_lease_pool_depth(handle: u64) -> u32 {
    pools()
        .lock()
        .get(&handle)
        .map(|p| p.depth())
        .unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn isc_lease_pool_target_fill(handle: u64, inflight: u32) -> u32 {
    pools()
        .lock()
        .get(&handle)
        .map(|p| p.target_fill(inflight))
        .unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn isc_sediment_parser_create() -> u64 {
    let id = NEXT_POOL.fetch_add(1, Ordering::Relaxed);
    parsers().lock().insert(id, SedimentParser::new());
    id
}

#[no_mangle]
pub extern "C" fn isc_sediment_parser_destroy(handle: u64) {
    parsers().lock().remove(&handle);
}

#[no_mangle]
pub extern "C" fn isc_sediment_parser_feed(handle: u64, chunk: *const c_char, len: u32) -> u8 {
    if chunk.is_null() || len == 0 {
        return 0;
    }
    let slice = unsafe { std::slice::from_raw_parts(chunk as *const u8, len as usize) };
    let text = match std::str::from_utf8(slice) {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let mut map = parsers().lock();
    let Some(parser) = map.get_mut(&handle) else {
        return 0;
    };
    if parser.feed(text) {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn isc_sediment_parser_ids_json(handle: u64) -> *mut c_char {
    let map = parsers().lock();
    let Some(parser) = map.get(&handle) else {
        return ptr::null_mut();
    };
    let json = parser.ids_json();
    match CString::new(json) {
        Ok(c) => c.into_raw(),
        Err(_) => ptr::null_mut(),
    }
}

#[no_mangle]
pub extern "C" fn isc_free_string(ptr: *mut c_char) {
    if ptr.is_null() {
        return;
    }
    unsafe {
        drop(std::ffi::CString::from_raw(ptr));
    }
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_create() -> u64 {
    let id = NEXT_POOL.fetch_add(1, Ordering::Relaxed);
    ledgers().lock().insert(id, SlotLedger::new());
    id
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_destroy(handle: u64) {
    ledgers().lock().remove(&handle);
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_try_acquire_account(
    handle: u64,
    holder_key: *const c_char,
    token_hash: u64,
    deadline_ns: u64,
) -> u8 {
    let Some(key) = cstr_to_str(holder_key) else {
        return 0;
    };
    let mut map = ledgers().lock();
    let Some(ledger) = map.get_mut(&handle) else {
        return 0;
    };
    if ledger.try_acquire_account(key, token_hash, deadline_from_ns(deadline_ns)) {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_release_account(handle: u64, holder_key: *const c_char) -> u8 {
    let Some(key) = cstr_to_str(holder_key) else {
        return 0;
    };
    let mut map = ledgers().lock();
    let Some(ledger) = map.get_mut(&handle) else {
        return 0;
    };
    if ledger.release_account(key) {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_try_acquire_ss(
    handle: u64,
    holder_key: *const c_char,
    deadline_ns: u64,
) -> u8 {
    let Some(key) = cstr_to_str(holder_key) else {
        return 0;
    };
    let mut map = ledgers().lock();
    let Some(ledger) = map.get_mut(&handle) else {
        return 0;
    };
    if ledger.try_acquire_ss(key, deadline_from_ns(deadline_ns)) {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_release_ss(handle: u64, holder_key: *const c_char) -> u8 {
    let Some(key) = cstr_to_str(holder_key) else {
        return 0;
    };
    let mut map = ledgers().lock();
    let Some(ledger) = map.get_mut(&handle) else {
        return 0;
    };
    if ledger.release_ss(key) {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_watchdog_tick(handle: u64, force_release: u8) -> *mut c_char {
    let mut map = ledgers().lock();
    let Some(ledger) = map.get_mut(&handle) else {
        return ptr::null_mut();
    };
    let report = ledger.watchdog_tick(force_release != 0);
    let json = format!(
        r#"{{"account_held":{},"ss_held":{},"account_expired_forced":{},"ss_expired_forced":{},"forced_releases":{}}}"#,
        report.account_held,
        report.ss_held,
        report.account_expired_forced,
        report.ss_expired_forced,
        ledger.forced_release_count()
    );
    match CString::new(json) {
        Ok(c) => c.into_raw(),
        Err(_) => ptr::null_mut(),
    }
}

#[no_mangle]
pub extern "C" fn isc_slot_ledger_stats_json(handle: u64) -> *mut c_char {
    let map = ledgers().lock();
    let Some(ledger) = map.get(&handle) else {
        return ptr::null_mut();
    };
    match std::ffi::CString::new(ledger.stats_json()) {
        Ok(c) => c.into_raw(),
        Err(_) => ptr::null_mut(),
    }
}

#[no_mangle]
pub extern "C" fn isc_binding_calendar_account_slot(
    account_key: *const c_char,
    binding_key: *const c_char,
    local_date: *const c_char,
    phase_index: u32,
    tz_name: *const c_char,
    jitter_min: u32,
    jitter_max: u32,
    salt: *const c_char,
    out_binding_unix: *mut i64,
    out_account_unix: *mut i64,
) -> u8 {
    let Some(account_key) = cstr_to_str(account_key) else {
        return 0;
    };
    let Some(binding_key) = cstr_to_str(binding_key) else {
        return 0;
    };
    let Some(local_date) = cstr_to_str(local_date) else {
        return 0;
    };
    let Some(tz_name) = cstr_to_str(tz_name) else {
        return 0;
    };
    let salt = cstr_to_str(salt).unwrap_or(binding_calendar::REFRESH_SALT);
    let Ok(day) = chrono::NaiveDate::parse_from_str(local_date, "%Y-%m-%d") else {
        return 0;
    };
    let slot = binding_calendar::account_phase_slot(
        account_key,
        binding_key,
        day,
        phase_index as usize,
        tz_name,
        jitter_min,
        jitter_max,
        salt,
    );
    unsafe {
        if !out_binding_unix.is_null() {
            *out_binding_unix = slot.binding_slot_unix;
        }
        if !out_account_unix.is_null() {
            *out_account_unix = slot.account_slot_unix;
        }
    }
    1
}

#[no_mangle]
pub extern "C" fn isc_binding_calendar_next_slot_unix(
    account_key: *const c_char,
    binding_key: *const c_char,
    now_unix: i64,
    tz_name: *const c_char,
    jitter_min: u32,
    jitter_max: u32,
    salt: *const c_char,
) -> i64 {
    let Some(account_key) = cstr_to_str(account_key) else {
        return -1;
    };
    let Some(binding_key) = cstr_to_str(binding_key) else {
        return -1;
    };
    let Some(tz_name) = cstr_to_str(tz_name) else {
        return -1;
    };
    let salt = cstr_to_str(salt).unwrap_or(binding_calendar::REFRESH_SALT);
    binding_calendar::next_account_slot_unix(
        account_key,
        binding_key,
        now_unix,
        tz_name,
        jitter_min,
        jitter_max,
        salt,
    )
}

#[no_mangle]
pub extern "C" fn isc_quota_prime_evaluate(input_json: *const c_char) -> *mut c_char {
    let Some(text) = cstr_to_str(input_json) else {
        return ptr::null_mut();
    };
    let output = match quota_prime::evaluate_prime_json(text) {
        Ok(json) => json,
        Err(err) => format!(r#"{{"eligible":false,"reason":"{}"}}"#, err.replace('"', "'")),
    };
    match CString::new(output) {
        Ok(c) => c.into_raw(),
        Err(_) => ptr::null_mut(),
    }
}

#[no_mangle]
pub extern "C" fn isc_quota_prime_list_eligible(input_json: *const c_char) -> *mut c_char {
    let Some(text) = cstr_to_str(input_json) else {
        return ptr::null_mut();
    };
    let output = match quota_prime::list_eligible_json(text) {
        Ok(json) => json,
        Err(err) => format!(r#"{{"indices":[],"error":"{}"}}"#, err.replace('"', "'")),
    };
    match CString::new(output) {
        Ok(c) => c.into_raw(),
        Err(_) => ptr::null_mut(),
    }
}

#[no_mangle]
pub extern "C" fn isc_quota_schedule_evaluate(input_json: *const c_char) -> *mut c_char {
    let Some(text) = cstr_to_str(input_json) else {
        return ptr::null_mut();
    };
    let output = match quota_schedule::evaluate_pick_json(text) {
        Ok(json) => json,
        Err(err) => format!(r#"{{"error":"{}"}}"#, err.replace('"', "'")),
    };
    match CString::new(output) {
        Ok(c) => c.into_raw(),
        Err(_) => ptr::null_mut(),
    }
}

#[no_mangle]
pub extern "C" fn isc_version() -> *const c_char {
    static VER: &[u8] = b"image_schedule_core/0.1.0\0";
    VER.as_ptr() as *const c_char
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dispatch_zero_when_under_cap() {
        assert!(!DispatchGate::should_wait(1500, 3, 10, 2));
    }

    #[test]
    fn dispatch_wait_when_over_cap() {
        assert!(DispatchGate::should_wait(1500, 10, 10, 5));
    }
}
