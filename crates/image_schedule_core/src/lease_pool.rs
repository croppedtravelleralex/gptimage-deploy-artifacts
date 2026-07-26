pub struct LeasePool {
    cap: u32,
    depth: u32,
}

impl LeasePool {
    pub fn new(cap: u32) -> Self {
        Self { cap: cap.max(1), depth: 0 }
    }

    pub fn depth(&self) -> u32 {
        self.depth
    }

    pub fn target_fill(&self, inflight: u32) -> u32 {
        self.cap.saturating_sub(inflight).saturating_sub(self.depth)
    }
}
