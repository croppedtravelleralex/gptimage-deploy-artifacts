//! Schedule core: dispatch gate, lease pool hints, sediment parser, slot ledger.

mod dispatch_gate;
mod lease_pool;
mod sediment;
mod slot_ledger;

pub use dispatch_gate::DispatchGate;
pub use lease_pool::LeasePool;
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
