use serde::Serialize;
use std::time::Instant;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum EventKind {
    TaskQueued = 1,
    TaskWorkerStart = 2,
    PipelineAdmit = 3,
    AccountWaitStart = 4,
    AccountAcquired = 5,
    ReadyBufferWaitStart = 6,
    ReadyBufferWaitEnd = 7,
    SsQueueEnter = 8,
    SsSlotAcquired = 9,
    SsSlotReleased = 10,
    SseStreamEnd = 11,
    PollResolveEnd = 12,
    DownloadStart = 13,
    DownloadEnd = 14,
    PipelineFinish = 15,
    PsQueueEnter = 16,
    PsSlotAcquired = 17,
    PsSlotReleased = 18,
    TaskTerminal = 19,
    GlobalConcurrencyWaitStart = 20,
    GlobalConcurrencyWaitEnd = 21,
    QuotaRefreshStart = 22,
    QuotaRefreshEnd = 23,
    QuotaPrimeStart = 24,
    QuotaPrimeEnd = 25,
}

impl EventKind {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            1 => Some(Self::TaskQueued),
            2 => Some(Self::TaskWorkerStart),
            3 => Some(Self::PipelineAdmit),
            4 => Some(Self::AccountWaitStart),
            5 => Some(Self::AccountAcquired),
            6 => Some(Self::ReadyBufferWaitStart),
            7 => Some(Self::ReadyBufferWaitEnd),
            8 => Some(Self::SsQueueEnter),
            9 => Some(Self::SsSlotAcquired),
            10 => Some(Self::SsSlotReleased),
            11 => Some(Self::SseStreamEnd),
            12 => Some(Self::PollResolveEnd),
            13 => Some(Self::DownloadStart),
            14 => Some(Self::DownloadEnd),
            15 => Some(Self::PipelineFinish),
            16 => Some(Self::PsQueueEnter),
            17 => Some(Self::PsSlotAcquired),
            18 => Some(Self::PsSlotReleased),
            19 => Some(Self::TaskTerminal),
            20 => Some(Self::GlobalConcurrencyWaitStart),
            21 => Some(Self::GlobalConcurrencyWaitEnd),
            22 => Some(Self::QuotaRefreshStart),
            23 => Some(Self::QuotaRefreshEnd),
            24 => Some(Self::QuotaPrimeStart),
            25 => Some(Self::QuotaPrimeEnd),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::TaskQueued => "task_queued",
            Self::TaskWorkerStart => "task_worker_start",
            Self::PipelineAdmit => "pipeline_admit",
            Self::AccountWaitStart => "account_wait_start",
            Self::AccountAcquired => "account_acquired",
            Self::ReadyBufferWaitStart => "ready_buffer_wait_start",
            Self::ReadyBufferWaitEnd => "ready_buffer_wait_end",
            Self::SsQueueEnter => "ss_queue_enter",
            Self::SsSlotAcquired => "ss_slot_acquired",
            Self::SsSlotReleased => "ss_slot_released",
            Self::SseStreamEnd => "sse_stream_end",
            Self::PollResolveEnd => "poll_resolve_end",
            Self::DownloadStart => "download_start",
            Self::DownloadEnd => "download_end",
            Self::PipelineFinish => "pipeline_finish",
            Self::PsQueueEnter => "ps_queue_enter",
            Self::PsSlotAcquired => "ps_slot_acquired",
            Self::PsSlotReleased => "ps_slot_released",
            Self::TaskTerminal => "task_terminal",
            Self::GlobalConcurrencyWaitStart => "global_concurrency_wait_start",
            Self::GlobalConcurrencyWaitEnd => "global_concurrency_wait_end",
            Self::QuotaRefreshStart => "quota_refresh_start",
            Self::QuotaRefreshEnd => "quota_refresh_end",
            Self::QuotaPrimeStart => "quota_prime_start",
            Self::QuotaPrimeEnd => "quota_prime_end",
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TraceEvent {
    pub kind: String,
    pub mono_ns: u64,
    pub aux: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pool_active: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pool_queued: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub slot: Option<u16>,
}

#[derive(Clone, Debug)]
pub struct TraceRun {
    pub id: u64,
    pub task_key: String,
    pub account_email: String,
    origin: Instant,
    events: Vec<(EventKind, u64, u32)>,
}

impl TraceRun {
    pub fn new(id: u64, task_key: String, account_email: String) -> Self {
        Self {
            id,
            task_key,
            account_email,
            origin: Instant::now(),
            events: Vec::with_capacity(32),
        }
    }

    pub fn set_account_email(&mut self, email: String) {
        if !email.is_empty() {
            self.account_email = email;
        }
    }

    pub fn emit(&mut self, kind: EventKind, aux: u32) {
        let mono_ns = self.origin.elapsed().as_nanos() as u64;
        self.events.push((kind, mono_ns, aux));
    }

    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        let events: Vec<TraceEvent> = self
            .events
            .iter()
            .map(|(k, mono_ns, aux)| decode_event(*k, *mono_ns, *aux))
            .collect();
        let model = super::model::build_model(&self.events);
        let payload = serde_json::json!({
            "engine": "rust",
            "version": "0.1.0",
            "task_key": self.task_key,
            "account_email": self.account_email,
            "event_count": events.len(),
            "events": events,
            "phases_ms": model.phases_ms,
            "explanations": model.explanations,
            "checkpoints": model.checkpoints,
        });
        serde_json::to_string(&payload)
    }
}

fn decode_event(kind: EventKind, mono_ns: u64, aux: u32) -> TraceEvent {
    let (pool_active, pool_queued, slot) = match kind {
        EventKind::SsQueueEnter
        | EventKind::PsQueueEnter
        | EventKind::SsSlotAcquired
        | EventKind::PsSlotAcquired => {
            let active = ((aux >> 16) & 0xFFFF) as u16;
            let queued = (aux & 0xFFFF) as u16;
            let slot = if matches!(kind, EventKind::SsSlotAcquired | EventKind::PsSlotAcquired) {
                Some((aux & 0xFFFF) as u16)
            } else {
                None
            };
            (
                Some(active),
                Some(queued),
                slot,
            )
        }
        EventKind::SsSlotReleased | EventKind::PsSlotReleased => (None, None, Some(aux as u16)),
        _ => (None, None, None),
    };
    TraceEvent {
        kind: kind.name().to_string(),
        mono_ns,
        aux,
        pool_active,
        pool_queued,
        slot,
    }
}
