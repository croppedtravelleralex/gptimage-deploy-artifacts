use serde::Serialize;
use std::collections::HashMap;

use crate::trace::EventKind;

#[derive(Debug, Serialize)]
pub struct PhaseModel {
    pub phases_ms: HashMap<String, i64>,
    pub explanations: Vec<String>,
    pub checkpoints: HashMap<String, u64>,
}

pub fn build_model_json(events: &[(EventKind, u64, u32)]) -> String {
    let model = build_model(events);
    serde_json::to_string(&model).unwrap_or_else(|_| "{}".to_string())
}

pub fn build_model(events: &[(EventKind, u64, u32)]) -> PhaseModel {
    let mut first: HashMap<EventKind, u64> = HashMap::new();
    let mut last: HashMap<EventKind, u64> = HashMap::new();
    let mut ss_queue_enter: Option<(u64, u32)> = None;
    let mut explanations: Vec<String> = Vec::new();

    for (kind, mono_ns, aux) in events {
        first.entry(*kind).or_insert(*mono_ns);
        last.insert(*kind, *mono_ns);
        if *kind == EventKind::SsQueueEnter {
            ss_queue_enter = Some((*mono_ns, *aux));
        }
    }

    let ns_to_ms = |a: u64, b: u64| -> i64 {
        if b <= a {
            0
        } else {
            ((b - a) as f64 / 1_000_000.0).round() as i64
        }
    };

    let delta = |start: EventKind, end: EventKind| -> i64 {
        match (first.get(&start), last.get(&end)) {
            (Some(a), Some(b)) => ns_to_ms(*a, *b),
            _ => 0,
        }
    };

    let delta_pair = |start: EventKind, end: EventKind| -> i64 {
        match (first.get(&start), first.get(&end)) {
            (Some(a), Some(b)) => ns_to_ms(*a, *b),
            _ => 0,
        }
    };

    let mut phases_ms = HashMap::new();
    phases_ms.insert(
        "task_queue_ms".into(),
        delta_pair(EventKind::TaskQueued, EventKind::TaskWorkerStart),
    );
    phases_ms.insert(
        "admit_queue_ms".into(),
        delta_pair(EventKind::TaskWorkerStart, EventKind::PipelineAdmit),
    );
    phases_ms.insert(
        "account_queue_ms".into(),
        delta_pair(EventKind::AccountWaitStart, EventKind::AccountAcquired),
    );
    phases_ms.insert(
        "ready_buffer_wait_ms".into(),
        delta_pair(EventKind::ReadyBufferWaitStart, EventKind::ReadyBufferWaitEnd),
    );

    let ss_queue_ms = match (
        ss_queue_enter,
        first.get(&EventKind::SsSlotAcquired),
    ) {
        (Some((enter_ns, aux)), Some(acq_ns)) => {
            let ms = ns_to_ms(enter_ns, *acq_ns);
            if ms > 0 {
                let active = ((aux >> 16) & 0xFFFF) as u16;
                let queued = (aux & 0xFFFF) as u16;
                explanations.push(format!(
                    "ss_queue {ms}ms: pool active={active} queued={queued} at queue enter"
                ));
            }
            ms
        }
        _ => 0,
    };
    phases_ms.insert("ss_queue_ms".into(), ss_queue_ms);

    phases_ms.insert(
        "sse_stream_ms".into(),
        delta_pair(EventKind::AccountAcquired, EventKind::SseStreamEnd),
    );
    phases_ms.insert(
        "poll_resolve_ms".into(),
        delta_pair(EventKind::SseStreamEnd, EventKind::PollResolveEnd),
    );
    phases_ms.insert(
        "download_ms".into(),
        delta_pair(EventKind::DownloadStart, EventKind::DownloadEnd),
    );
    phases_ms.insert(
        "wall_clock_ms".into(),
        delta(EventKind::TaskQueued, EventKind::PipelineFinish)
            .max(delta_pair(EventKind::PipelineAdmit, EventKind::PipelineFinish)),
    );
    phases_ms.insert(
        "global_concurrency_wait_ms".into(),
        delta_pair(
            EventKind::GlobalConcurrencyWaitStart,
            EventKind::GlobalConcurrencyWaitEnd,
        ),
    );
    phases_ms.insert(
        "ps_queue_ms".into(),
        delta_pair(EventKind::PsQueueEnter, EventKind::PsSlotAcquired),
    );

    if phases_ms.get("account_queue_ms").copied().unwrap_or(0) > 5000 {
        explanations.push(format!(
            "account_queue {}ms: likely global/binding/account concurrency or preflight",
            phases_ms["account_queue_ms"]
        ));
    }
    if phases_ms.get("task_queue_ms").copied().unwrap_or(0) > 3000 {
        explanations.push(format!(
            "task_queue {}ms: submit_workers or per_user_running limit",
            phases_ms["task_queue_ms"]
        ));
    }
    if phases_ms.get("sse_stream_ms").copied().unwrap_or(0) > 45000 {
        explanations.push(format!(
            "sse_stream {}ms: upstream requirements→image_gen dominates",
            phases_ms["sse_stream_ms"]
        ));
    }

    let mut checkpoints = HashMap::new();
    for (k, v) in &first {
        checkpoints.insert(k.name().to_string(), *v);
    }

    PhaseModel {
        phases_ms,
        explanations,
        checkpoints,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ss_queue_from_enter_to_acquire() {
        let events = vec![
            (EventKind::SsQueueEnter, 1_000_000_000, (10 << 16) | 3),
            (EventKind::SsSlotAcquired, 1_025_000_000, 2),
        ];
        let m = build_model(&events);
        assert_eq!(m.phases_ms.get("ss_queue_ms"), Some(&25));
    }
}
