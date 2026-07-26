//! Low-overhead schedule trace: checkpoint timestamps → phase model + explanations.

mod model;
mod trace;

pub use model::{build_model_json, PhaseModel};
pub use trace::{EventKind, TraceEvent, TraceRun};

use std::ffi::{CStr, CString};
use std::os::raw::c_char;
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

struct Registry {
    runs: std::collections::HashMap<u64, TraceRun>,
}

fn registry() -> &'static Mutex<Registry> {
    static REG: std::sync::OnceLock<Mutex<Registry>> = std::sync::OnceLock::new();
    REG.get_or_init(|| {
        Mutex::new(Registry {
            runs: std::collections::HashMap::new(),
        })
    })
}

/// Begin a trace run. Returns opaque handle (>0) or 0 on failure.
#[no_mangle]
pub extern "C" fn ist_trace_begin(task_key: *const c_char, account_email: *const c_char) -> u64 {
    let task_key = unsafe {
        if task_key.is_null() {
            return 0;
        }
        match CStr::from_ptr(task_key).to_str() {
            Ok(s) => s.to_string(),
            Err(_) => return 0,
        }
    };
    let account_email = unsafe {
        if account_email.is_null() {
            String::new()
        } else {
            CStr::from_ptr(account_email)
                .to_str()
                .unwrap_or("")
                .to_string()
        }
    };
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let run = TraceRun::new(id, task_key, account_email);
    if let Ok(mut reg) = registry().lock() {
        reg.runs.insert(id, run);
        id
    } else {
        0
    }
}

/// Emit event. `aux` packs pool state: high 16 = active, low 16 = queued (or slot index for acquire).
#[no_mangle]
pub extern "C" fn ist_trace_emit(handle: u64, kind: u8, aux: u32) {
    if handle == 0 {
        return;
    }
    let event_kind = EventKind::from_u8(kind);
    if event_kind.is_none() {
        return;
    }
    if let Ok(mut reg) = registry().lock() {
        if let Some(run) = reg.runs.get_mut(&handle) {
            run.emit(event_kind.unwrap(), aux);
        }
    }
}

/// Set account email after acquisition (hot path: one CString copy at most once).
#[no_mangle]
pub extern "C" fn ist_trace_set_account(handle: u64, account_email: *const c_char) {
    if handle == 0 || account_email.is_null() {
        return;
    }
    let email = unsafe {
        CStr::from_ptr(account_email)
            .to_str()
            .unwrap_or("")
            .to_string()
    };
    if let Ok(mut reg) = registry().lock() {
        if let Some(run) = reg.runs.get_mut(&handle) {
            run.set_account_email(email);
        }
    }
}

/// Finish trace, return JSON string (caller must `ist_trace_free_string`). Removes run from registry.
#[no_mangle]
pub extern "C" fn ist_trace_finish(handle: u64) -> *mut c_char {
    if handle == 0 {
        return ptr::null_mut();
    }
    let run = if let Ok(mut reg) = registry().lock() {
        reg.runs.remove(&handle)
    } else {
        None
    };
    let Some(run) = run else {
        return ptr::null_mut();
    };
    let json = match run.to_json() {
        Ok(s) => s,
        Err(_) => return ptr::null_mut(),
    };
    match CString::new(json) {
        Ok(c) => c.into_raw(),
        Err(_) => ptr::null_mut(),
    }
}

#[no_mangle]
pub extern "C" fn ist_trace_free_string(ptr: *mut c_char) {
    if ptr.is_null() {
        return;
    }
    unsafe {
        drop(CString::from_raw(ptr));
    }
}

#[no_mangle]
pub extern "C" fn ist_version() -> *const c_char {
    static VER: &[u8] = b"image_schedule_trace/0.1.0\0";
    VER.as_ptr() as *const c_char
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    #[test]
    fn begin_emit_finish_roundtrip() {
        let tk = CString::new("task-1").unwrap();
        let em = CString::new("a@b.me").unwrap();
        let h = ist_trace_begin(tk.as_ptr(), em.as_ptr());
        assert!(h > 0);
        ist_trace_emit(h, EventKind::TaskQueued as u8, 0);
        ist_trace_emit(h, EventKind::TaskWorkerStart as u8, 0);
        ist_trace_emit(h, EventKind::AccountWaitStart as u8, 0);
        ist_trace_emit(h, EventKind::AccountAcquired as u8, 0);
        ist_trace_emit(h, EventKind::SsQueueEnter as u8, (10 << 16) | 2);
        ist_trace_emit(h, EventKind::SsSlotAcquired as u8, 3);
        ist_trace_emit(h, EventKind::SseStreamEnd as u8, 0);
        ist_trace_emit(h, EventKind::SsSlotReleased as u8, 3);
        ist_trace_emit(h, EventKind::PipelineFinish as u8, 0);
        let json_ptr = ist_trace_finish(h);
        assert!(!json_ptr.is_null());
        let json = unsafe { CStr::from_ptr(json_ptr).to_str().unwrap().to_string() };
        ist_trace_free_string(json_ptr);
        assert!(json.contains("phases_ms"));
        assert!(json.contains("ss_queue_ms"));
    }
}
