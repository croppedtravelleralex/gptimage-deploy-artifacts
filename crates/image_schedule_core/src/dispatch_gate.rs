pub struct DispatchGate;

impl DispatchGate {
    /// True when interval pacing should apply (not in adaptive zero-window).
    pub fn should_wait(interval_ms: u32, inflight: u32, cap: u32, queued: u32) -> bool {
        if interval_ms == 0 {
            return false;
        }
        let cap = cap.max(1);
        !(inflight < cap && queued <= cap.saturating_sub(inflight))
    }
}
