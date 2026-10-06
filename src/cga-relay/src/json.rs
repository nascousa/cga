use std::collections::BTreeMap;

use crate::{json_escape, AgentError, AgentResult};

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Value>),
    Object(BTreeMap<String, Value>),
}

impl Value {
    pub fn get(&self, key: &str) -> AgentResult<&Value> {
        match self {
            Self::Object(map) => map.get(key).ok_or_else(|| error("missing JSON field")),
            _ => Err(error("expected JSON object")),
        }
    }
    pub fn text(&self) -> AgentResult<&str> {
        match self {
            Self::String(value) => Ok(value),
            _ => Err(error("expected JSON string")),
        }
    }
    pub fn array(&self) -> AgentResult<&[Value]> {
        match self {
            Self::Array(value) => Ok(value),
            _ => Err(error("expected JSON array")),
        }
    }
    pub fn encode(&self) -> String {
        match self {
            Self::Null => "null".into(),
            Self::Bool(value) => value.to_string(),
            Self::Number(value) => value.clone(),
            Self::String(value) => format!("\"{}\"", escape(value)),
            Self::Array(values) => format!(
                "[{}]",
                values
                    .iter()
                    .map(Self::encode)
                    .collect::<Vec<_>>()
                    .join(",")
            ),
            Self::Object(values) => format!(
                "{{{}}}",
                values
                    .iter()
                    .map(|(k, v)| format!("\"{}\":{}", escape(k), v.encode()))
                    .collect::<Vec<_>>()
                    .join(",")
            ),
        }
    }
}

fn escape(value: &str) -> String {
    let mut out = String::new();
    for ch in value.chars() {
        if ch.is_control() && !matches!(ch, '\n' | '\r' | '\t') {
            out.push_str(&format!("\\u{:04x}", ch as u32));
        } else {
            out.push_str(&json_escape(&ch.to_string()));
        }
    }
    out
}

fn error(message: &str) -> AgentError {
    AgentError(message.to_string())
}

pub fn parse(input: &str) -> AgentResult<Value> {
    if input.len() > crate::MAX_HTTP_RESPONSE_BYTES {
        return Err(error("JSON exceeds size limit"));
    }
    let mut parser = Parser {
        input: input.as_bytes(),
        pos: 0,
    };
    let value = parser.value(0)?;
    parser.space();
    if parser.pos != parser.input.len() {
        return Err(error("trailing JSON data"));
    }
    Ok(value)
}

struct Parser<'a> {
    input: &'a [u8],
    pos: usize,
}
impl Parser<'_> {
    fn space(&mut self) {
        while self
            .input
            .get(self.pos)
            .is_some_and(|c| matches!(c, b' ' | b'\n' | b'\r' | b'\t'))
        {
            self.pos += 1;
        }
    }
    fn take(&mut self, value: u8) -> bool {
        self.space();
        if self.input.get(self.pos) == Some(&value) {
            self.pos += 1;
            true
        } else {
            false
        }
    }
    fn value(&mut self, depth: usize) -> AgentResult<Value> {
        if depth > 32 {
            return Err(error("JSON nesting limit exceeded"));
        }
        self.space();
        match self.input.get(self.pos).copied() {
            Some(b'"') => Ok(Value::String(self.string()?)),
            Some(b'{') => {
                self.pos += 1;
                let mut map = BTreeMap::new();
                if self.take(b'}') {
                    return Ok(Value::Object(map));
                }
                loop {
                    self.space();
                    let key = self.string()?;
                    if !self.take(b':') {
                        return Err(error("expected JSON colon"));
                    }
                    let value = self.value(depth + 1)?;
                    if map.insert(key, value).is_some() {
                        return Err(error("duplicate JSON key"));
                    }
                    if self.take(b'}') {
                        break;
                    }
                    if !self.take(b',') {
                        return Err(error("expected JSON comma"));
                    }
                }
                Ok(Value::Object(map))
            }
            Some(b'[') => {
                self.pos += 1;
                let mut values = Vec::new();
                if self.take(b']') {
                    return Ok(Value::Array(values));
                }
                loop {
                    values.push(self.value(depth + 1)?);
                    if self.take(b']') {
                        break;
                    }
                    if !self.take(b',') {
                        return Err(error("expected JSON comma"));
                    }
                }
                Ok(Value::Array(values))
            }
            Some(b't') => {
                self.literal(b"true")?;
                Ok(Value::Bool(true))
            }
            Some(b'f') => {
                self.literal(b"false")?;
                Ok(Value::Bool(false))
            }
            Some(b'n') => {
                self.literal(b"null")?;
                Ok(Value::Null)
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(error("invalid JSON value")),
        }
    }
    fn literal(&mut self, value: &[u8]) -> AgentResult<()> {
        if self.input.get(self.pos..self.pos + value.len()) != Some(value) {
            return Err(error("invalid JSON literal"));
        }
        self.pos += value.len();
        Ok(())
    }
    fn digits(&mut self) -> AgentResult<()> {
        let start = self.pos;
        while self.input.get(self.pos).is_some_and(u8::is_ascii_digit) {
            self.pos += 1;
        }
        if self.pos == start {
            return Err(error("invalid JSON number"));
        }
        Ok(())
    }
    fn number(&mut self) -> AgentResult<Value> {
        let start = self.pos;
        if self.input.get(self.pos) == Some(&b'-') {
            self.pos += 1;
        }
        if self.input.get(self.pos) == Some(&b'0') {
            self.pos += 1;
        } else {
            self.digits()?;
        }
        if self.input.get(self.pos) == Some(&b'.') {
            self.pos += 1;
            self.digits()?;
        }
        if self
            .input
            .get(self.pos)
            .is_some_and(|c| matches!(c, b'e' | b'E'))
        {
            self.pos += 1;
            if self
                .input
                .get(self.pos)
                .is_some_and(|c| matches!(c, b'+' | b'-'))
            {
                self.pos += 1;
            }
            self.digits()?;
        }
        Ok(Value::Number(
            String::from_utf8(self.input[start..self.pos].to_vec())
                .map_err(|_| error("invalid number encoding"))?,
        ))
    }
    fn hex4(&mut self) -> AgentResult<u32> {
        let bytes = self
            .input
            .get(self.pos..self.pos + 4)
            .ok_or_else(|| error("short unicode escape"))?;
        let text = std::str::from_utf8(bytes).map_err(|_| error("invalid unicode escape"))?;
        let value = u32::from_str_radix(text, 16).map_err(|_| error("invalid unicode escape"))?;
        self.pos += 4;
        Ok(value)
    }
    fn string(&mut self) -> AgentResult<String> {
        if self.input.get(self.pos) != Some(&b'"') {
            return Err(error("expected JSON string"));
        }
        self.pos += 1;
        let mut bytes = Vec::new();
        loop {
            let ch = *self
                .input
                .get(self.pos)
                .ok_or_else(|| error("unterminated JSON string"))?;
            self.pos += 1;
            match ch {
                b'"' => return String::from_utf8(bytes).map_err(|_| error("invalid UTF-8")),
                0..=31 => return Err(error("unescaped JSON control character")),
                b'\\' => {
                    let escaped = *self
                        .input
                        .get(self.pos)
                        .ok_or_else(|| error("short JSON escape"))?;
                    self.pos += 1;
                    match escaped {
                        b'"' | b'\\' | b'/' => bytes.push(escaped),
                        b'b' => bytes.push(8),
                        b'f' => bytes.push(12),
                        b'n' => bytes.push(10),
                        b'r' => bytes.push(13),
                        b't' => bytes.push(9),
                        b'u' => {
                            let first = self.hex4()?;
                            let code = if (0xd800..=0xdbff).contains(&first) {
                                self.literal(b"\\u")?;
                                let low = self.hex4()?;
                                if !(0xdc00..=0xdfff).contains(&low) {
                                    return Err(error("invalid surrogate pair"));
                                }
                                0x10000 + ((first - 0xd800) << 10) + low - 0xdc00
                            } else {
                                first
                            };
                            let ch = char::from_u32(code)
                                .ok_or_else(|| error("invalid unicode scalar"))?;
                            let mut buffer = [0; 4];
                            bytes.extend_from_slice(ch.encode_utf8(&mut buffer).as_bytes());
                        }
                        _ => return Err(error("invalid JSON escape")),
                    }
                }
                _ => bytes.push(ch),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_json_round_trip() {
        let value = parse(r#"{"content":"\u4e2d\u6587 \ud83d\ude80\n\u0000","nested":{"project_id":"other"},"n":-1.25e+2}"#).unwrap();
        assert_eq!(value.get("content").unwrap().text().unwrap(), "中文 🚀\n\0");
        assert_eq!(parse(&value.encode()).unwrap(), value);
        assert!(value.get("project_id").is_err());
        for input in [
            r#"{"x":1,"x":2}"#,
            "[1,]",
            r#""\ud800""#,
            "01",
            "1x",
            "{\"x\":NaN}",
        ] {
            assert!(parse(input).is_err(), "{input}");
        }
    }
}
