use std::collections::HashSet;

const PREFIX: &str = "sediment://";

pub struct SedimentParser {
    ids: HashSet<String>,
    carry: String,
}

impl SedimentParser {
    pub fn new() -> Self {
        Self {
            ids: HashSet::new(),
            carry: String::new(),
        }
    }

    pub fn feed(&mut self, chunk: &str) -> bool {
        let mut text = String::with_capacity(self.carry.len() + chunk.len());
        text.push_str(&self.carry);
        text.push_str(chunk);
        self.carry.clear();
        let mut found = false;
        let mut start = 0usize;
        while let Some(idx) = text[start..].find(PREFIX) {
            let abs = start + idx;
            let rest = &text[abs + PREFIX.len()..];
            let end = rest
                .find(|c: char| c.is_whitespace() || c == '"' || c == '\'' || c == ')' || c == ',')
                .unwrap_or(rest.len());
            let id = rest[..end].trim();
            if !id.is_empty() && self.ids.insert(id.to_string()) {
                found = true;
            }
            start = abs + PREFIX.len() + end;
        }
        if start < text.len() {
            let tail = &text[start..];
            if tail.contains("sediment") {
                self.carry = tail.to_string();
            }
        }
        found
    }

    pub fn ids_json(&self) -> String {
        let ids: Vec<&str> = self.ids.iter().map(String::as_str).collect();
        if ids.is_empty() {
            return "[]".to_string();
        }
        let body = ids
            .iter()
            .map(|id| format!("\"{}\"", id.replace('\\', "\\\\").replace('"', "\\\"")))
            .collect::<Vec<_>>()
            .join(",");
        format!("[{body}]")
    }
}
