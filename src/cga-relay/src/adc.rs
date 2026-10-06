use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

use crate::json::{self, Value};
use crate::{json_escape, post_cga_relay_tool, sha256_hex, AgentConfig, AgentError, AgentResult};

pub const TOOLS: &[&str] = &[
    "adc_catalog",
    "adc_release",
    "adc_current",
    "adc_history",
    "adc_diff",
    "adc_document",
    "adc_bundle",
    "adc_sync",
];

fn error(message: impl Into<String>) -> AgentError {
    AgentError(message.into())
}

fn unique_stamp() -> AgentResult<u128> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .map_err(|e| error(format!("System clock is invalid: {e}")))
}

pub fn schemas() -> String {
    TOOLS.iter().map(|name| {
        let properties = match *name {
            "adc_catalog" | "adc_history" => r#""offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":100}"#,
            "adc_release" | "adc_diff" => r#""release_id":{"type":"integer","minimum":1}"#,
            "adc_current" | "adc_bundle" => r#""revision":{"type":"integer","minimum":1}"#,
            "adc_document" => r#""path":{"type":"string"},"revision":{"type":"integer","minimum":1}"#,
            "adc_sync" => r#""apply":{"type":"boolean","default":false}"#,
            _ => "",
        };
        let required = match *name {
            "adc_release" | "adc_diff" => r#","required":["release_id"]"#,
            "adc_document" => r#","required":["path"]"#,
            _ => "",
        };
        let description = if *name == "adc_sync" {
            "Preview approved ADC sync to the configured local project. Set apply=true only after review. Conflicts block all writes; never approves overrides."
        } else { "Read ADC from CGA for this configured project; no global publication or project approval authority." };
        format!(r#"{{"name":"{name}","description":"{description}","inputSchema":{{"type":"object","properties":{{{properties}}},"additionalProperties":false{required}}}}}"#)
    }).collect::<Vec<_>>().join(",")
}

pub fn call(config: &AgentConfig, tool: &str, arguments: &str) -> AgentResult<String> {
    let Value::Object(mut args) = json::parse(arguments)? else {
        return Err(error("ADC arguments must be an object"));
    };
    if let Some(project) = args.remove("project_id") {
        if project.text()? != config.project_id {
            return Err(error("project_id does not match this local relay config"));
        }
    }
    if !TOOLS.contains(&tool) {
        return Err(error("unknown ADC tool"));
    }
    if tool == "adc_sync" {
        let apply = match args.remove("apply") {
            None | Some(Value::Bool(false)) => false,
            Some(Value::Bool(true)) => true,
            _ => return Err(error("apply must be a boolean")),
        };
        if !args.is_empty() {
            return Err(error(
                "ADC sync accepts only apply; project root and revision cannot be overridden",
            ));
        }
        let body = post_cga_relay_tool(config, "adc_bundle", "{}", &config.project_id)?;
        let envelope = json::parse(&body)?;
        return sync(config, envelope.get("result")?, apply);
    }
    post_cga_relay_tool(
        config,
        tool,
        &Value::Object(args).encode(),
        &config.project_id,
    )
}

pub fn command(args: &[String]) -> AgentResult<()> {
    let action = args.first().ok_or_else(|| {
        error("adc requires catalog/release/current/history/diff/document/bundle/sync")
    })?;
    let config = crate::load_config(crate::required_arg(args, "--config")?)?;
    let mut index = 1;
    while index < args.len() {
        match args[index].as_str() {
            "--config" | "--release-id" | "--revision" | "--offset" | "--limit" | "--path" => {
                if args.get(index + 1).is_none() {
                    return Err(error("Missing ADC option value"));
                }
                index += 2;
            }
            "--apply" | "--json" => index += 1,
            _ => return Err(error(format!("Unknown ADC option: {}", args[index]))),
        }
    }
    let tool = format!("adc_{action}");
    let mut arguments = BTreeMap::new();
    for (flag, key) in [
        ("--release-id", "release_id"),
        ("--revision", "revision"),
        ("--offset", "offset"),
        ("--limit", "limit"),
    ] {
        if let Some(index) = args.iter().position(|a| a == flag) {
            let value = args
                .get(index + 1)
                .ok_or_else(|| error(format!("missing {flag} value")))?;
            value
                .parse::<u32>()
                .map_err(|_| error(format!("{flag} must be a nonnegative integer")))?;
            arguments.insert(key.to_string(), Value::Number(value.clone()));
        }
    }
    if let Some(index) = args.iter().position(|a| a == "--path") {
        arguments.insert(
            "path".into(),
            Value::String(
                args.get(index + 1)
                    .ok_or_else(|| error("missing --path"))?
                    .clone(),
            ),
        );
    }
    if args.iter().any(|a| a == "--apply") {
        arguments.insert("apply".into(), Value::Bool(true));
    }
    println!(
        "{}",
        call(&config, &tool, &Value::Object(arguments).encode())?
    );
    Ok(())
}

fn safe_path(path: &str) -> AgentResult<()> {
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() < 2 || !matches!(parts[0], ".adc" | ".github") || path.len() > 240 {
        return Err(error("ADC file must be beneath .adc/ or .github/"));
    }
    for part in parts {
        let stem = part.split('.').next().unwrap_or("").to_ascii_uppercase();
        if part.is_empty()
            || matches!(part, "." | "..")
            || part.ends_with(['.', ' '])
            || part
                .chars()
                .any(|c| c.is_control() || "\\:<>\"|?*".contains(c))
            || matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || ((stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.len() == 4
                && stem.as_bytes()[3].is_ascii_digit())
        {
            return Err(error("unsafe ADC document path"));
        }
    }
    Ok(())
}

fn reject_link(path: &Path) -> AgentResult<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            let mut link = metadata.file_type().is_symlink();
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                link |= metadata.file_attributes() & 0x400 != 0;
            }
            if link {
                return Err(error(format!(
                    "ADC sync refuses symlink/reparse path: {}",
                    path.display()
                )));
            }
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(error(format!("Cannot inspect ADC path: {e}"))),
    }
}

fn inspect_chain(path: &Path) -> AgentResult<()> {
    for ancestor in path.ancestors() {
        reject_link(ancestor)?;
    }
    Ok(())
}

fn contents(path: &Path) -> AgentResult<Option<Vec<u8>>> {
    inspect_chain(path)?;
    match fs::metadata(path) {
        Ok(metadata) => {
            if !metadata.is_file() || metadata.len() > 8 * 1024 * 1024 {
                return Err(error("ADC target is not a bounded regular file"));
            }
            fs::read(path)
                .map(Some)
                .map_err(|e| error(format!("Cannot read ADC file: {e}")))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(error(format!("Cannot inspect ADC target: {e}"))),
    }
}

fn bundle_files(bundle: &Value, project_id: &str) -> AgentResult<BTreeMap<String, Vec<u8>>> {
    if bundle.get("schema")? != &Value::Number("1".into())
        || bundle.get("project_id")?.text()? != project_id
    {
        return Err(error("ADC bundle schema/project identity mismatch"));
    }
    if bundle.get("historical")? != &Value::Bool(false) {
        return Err(error("Historical exports cannot be auto-installed"));
    }
    let entries = bundle.get("files")?.array()?;
    if entries.is_empty() || entries.len() > 1001 {
        return Err(error("Invalid ADC file count"));
    }
    let mut files = BTreeMap::new();
    let mut normalized = BTreeSet::new();
    for entry in entries {
        let path = entry.get("path")?.text()?;
        safe_path(path)?;
        if !normalized.insert(path.to_lowercase()) {
            return Err(error("Duplicate ADC path ignoring case"));
        }
        let content = entry.get("content")?.text()?.as_bytes().to_vec();
        if sha256_hex(&content) != entry.get("sha256")?.text()? {
            return Err(error(format!("ADC content hash mismatch: {path}")));
        }
        files.insert(path.to_string(), content);
    }
    if !files.contains_key(".adc/adc-lock.json") {
        return Err(error("ADC provenance lock is missing"));
    }
    for path in files.keys() {
        let mut parent = Path::new(path).parent();
        while let Some(value) = parent {
            if normalized.contains(&value.to_string_lossy().replace('\\', "/").to_lowercase()) {
                return Err(error("ADC document collides with a parent directory"));
            }
            parent = value.parent();
        }
    }
    Ok(files)
}

fn sync(config: &AgentConfig, bundle: &Value, apply: bool) -> AgentResult<String> {
    let files = bundle_files(bundle, &config.project_id)?;
    inspect_chain(&config.project_root)?;
    let root = config
        .project_root
        .canonicalize()
        .map_err(|e| error(format!("Cannot resolve project root: {e}")))?;
    if !root.is_dir() {
        return Err(error("Project root is not a directory"));
    }
    let key = sha256_hex(format!("{}\n{}", root.display(), config.project_id).as_bytes());
    let state = config.state_dir.join(format!("adc-{key}"));
    inspect_chain(&state)?;
    let journal = state.join("pending.json");
    if journal.exists() {
        return Err(error(format!("Interrupted ADC sync detected. Preserve the journal/backups and recover before retrying: {}", state.display())));
    }
    let previous_path = state.join("applied.json");
    let previous = match contents(&previous_path)? {
        None => BTreeMap::new(),
        Some(bytes) => {
            let parsed = json::parse(
                std::str::from_utf8(&bytes)
                    .map_err(|_| error("Invalid ADC checkpoint encoding"))?,
            )?;
            bundle_files(&parsed, &config.project_id)?
        }
    };
    let paths: BTreeSet<_> = files.keys().chain(previous.keys()).cloned().collect();
    let mut changes: Vec<(String, Option<Vec<u8>>, Option<Vec<u8>>)> = Vec::new();
    let mut conflicts = Vec::new();
    for path in paths {
        safe_path(&path)?;
        for ancestor in root.join(&path).ancestors().skip(1) {
            if ancestor.exists() && !ancestor.is_dir() {
                return Err(error("ADC parent path is not a directory"));
            }
        }
        let current = contents(&root.join(&path))?;
        let desired = files.get(&path).cloned();
        if current == desired {
            continue;
        }
        let old = previous.get(&path).cloned();
        if current != old {
            conflicts.push(path);
        } else {
            changes.push((path, current, desired));
        }
    }
    if !conflicts.is_empty() {
        return Err(error(format!("ADC local modifications conflict; no files changed. Submit overrides/amendments for administrator approval: {}", conflicts.join(", "))));
    }
    if apply {
        fs::create_dir_all(&state).map_err(|e| error(format!("Cannot create ADC state: {e}")))?;
        inspect_chain(&state)?;
        let backup = state.join(format!("backup-{}", unique_stamp()?));
        fs::create_dir(&backup).map_err(|e| error(format!("Cannot create ADC backup: {e}")))?;
        for (index, (_, original, _)) in changes.iter().enumerate() {
            if let Some(bytes) = original {
                fs::write(backup.join(index.to_string()), bytes)
                    .map_err(|e| error(format!("Cannot back up ADC file: {e}")))?;
            }
        }
        let plan = Value::Object(BTreeMap::from([
            ("root".into(), Value::String(root.display().to_string())),
            ("backup".into(), Value::String(backup.display().to_string())),
            (
                "paths".into(),
                Value::Array(
                    changes
                        .iter()
                        .map(|(path, old, _)| {
                            Value::Object(BTreeMap::from([
                                ("path".into(), Value::String(path.clone())),
                                ("existed".into(), Value::Bool(old.is_some())),
                            ]))
                        })
                        .collect(),
                ),
            ),
        ]));
        write_new(&journal, plan.encode().as_bytes())?;
        // The persistent journal remains on any error or process interruption.
        // A later call refuses to proceed rather than accepting a partial tree.
        for (path, original, desired) in &changes {
            let target = root.join(path);
            if contents(&target)? != *original {
                return Err(error(
                    "ADC file changed after preview; pending journal retained",
                ));
            }
            match desired {
                Some(bytes) => {
                    let parent = target
                        .parent()
                        .ok_or_else(|| error("ADC target has no parent"))?;
                    fs::create_dir_all(parent)
                        .map_err(|e| error(format!("Cannot create ADC directory: {e}")))?;
                    inspect_chain(parent)?;
                    let temporary = parent.join(format!(".cga-adc-{}.tmp", unique_stamp()?));
                    write_new(&temporary, bytes)?;
                    if let Err(e) = fs::rename(&temporary, &target) {
                        return Err(error(format!(
                            "ADC atomic replace failed; journal retained: {e}"
                        )));
                    }
                }
                None => {
                    fs::remove_file(target)
                        .map_err(|e| error(format!("ADC removal failed; journal retained: {e}")))?;
                }
            }
        }
        let temporary = state.join("applied.next.json");
        write_new(&temporary, bundle.encode().as_bytes())?;
        fs::rename(temporary, &previous_path).map_err(|e| {
            error(format!(
                "Cannot publish ADC checkpoint; journal retained: {e}"
            ))
        })?;
        fs::remove_file(&journal).map_err(|e| error(format!("Cannot clear ADC journal: {e}")))?;
    }
    Ok(format!(
        r#"{{"ok":true,"applied":{apply},"project_id":"{}","changed_paths":[{}],"revision":{}}}"#,
        json_escape(&config.project_id),
        changes
            .iter()
            .map(|(p, _, _)| format!("\"{}\"", json_escape(p)))
            .collect::<Vec<_>>()
            .join(","),
        bundle.get("revision")?.encode()
    ))
}

fn write_new(path: &Path, bytes: &[u8]) -> AgentResult<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| error(format!("Cannot create ADC staging file: {e}")))?;
    file.write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(|e| error(format!("Cannot persist ADC staging file: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn adc_rejects_unsafe_paths_and_digest_mismatch() {
        for path in [
            "../x",
            ".adc/../x",
            ".adc\\x",
            ".adc/NUL",
            ".adc/name:stream",
            ".adc//x",
        ] {
            assert!(safe_path(path).is_err());
        }
        assert!(safe_path(".adc/knowledge/project.md").is_ok());
        let bundle = json::parse(r#"{"schema":1,"project_id":"p","historical":false,"revision":1,"files":[{"path":".adc/adc-lock.json","content":"x","sha256":"wrong"}]}"#).unwrap();
        assert!(bundle_files(&bundle, "p").is_err());
        assert!(bundle_files(&bundle, "another").is_err());
    }

    fn test_bundle(project: &str, docs: &[(&str, &str)]) -> Value {
        let files = docs
            .iter()
            .chain(std::iter::once(&(".adc/adc-lock.json", "{}")))
            .map(|(path, content)| {
                Value::Object(BTreeMap::from([
                    ("path".into(), Value::String((*path).into())),
                    ("content".into(), Value::String((*content).into())),
                    (
                        "sha256".into(),
                        Value::String(sha256_hex(content.as_bytes())),
                    ),
                ]))
            })
            .collect();
        Value::Object(BTreeMap::from([
            ("schema".into(), Value::Number("1".into())),
            ("project_id".into(), Value::String(project.into())),
            ("historical".into(), Value::Bool(false)),
            ("revision".into(), Value::Number("1".into())),
            ("files".into(), Value::Array(files)),
        ]))
    }

    #[test]
    fn adc_sync_previews_preserves_local_edits_and_tracks_removals() {
        let base = std::env::temp_dir().join(format!("cga-adc-unit-{}", unique_stamp().unwrap()));
        fs::create_dir(&base).unwrap();
        let repo = base.join("repo");
        fs::create_dir(&repo).unwrap();
        let config_path = base.join("relay.env");
        fs::write(&config_path, format!("AGENT_ID=test\nPROJECT_ID=p\nPROJECT_ROOT={}\nSTATE_DIR={}\nLOG_DIR={}\nAPI_BASE_URL=http://127.0.0.1:18001\nCONTROL_API_BASE_URL=http://127.0.0.1:18001\nAPI_KEY_ENV=TEST_API_KEY\nACCOUNT_EMAIL=\nACCOUNT_TOKEN_ENV=TEST_ACCOUNT\nINCLUDE_GLOBS=\nEXCLUDE_GLOBS=.git/**\nMAX_FILE_BYTES=1000\n",
            repo.display(), base.join("state").display(), base.join("logs").display())).unwrap();
        let config = crate::load_config(config_path.to_str().unwrap()).unwrap();
        let first = test_bundle("p", &[(".adc/index.md", "Unicode: \u{4e2d}\u{6587}")]);
        assert!(sync(&config, &first, false)
            .unwrap()
            .contains("\"applied\":false"));
        assert!(!repo.join(".adc").exists());
        sync(&config, &first, true).unwrap();
        assert_eq!(
            fs::read_to_string(repo.join(".adc/index.md")).unwrap(),
            "Unicode: \u{4e2d}\u{6587}"
        );
        fs::write(repo.join(".adc/local-only.md"), "keep").unwrap();
        fs::write(repo.join(".adc/index.md"), "local override").unwrap();
        let next = test_bundle(
            "p",
            &[(".adc/index.md", "upstream"), (".adc/new.md", "new")],
        );
        assert!(sync(&config, &next, true).is_err());
        assert!(!repo.join(".adc/new.md").exists());
        assert_eq!(
            fs::read_to_string(repo.join(".adc/index.md")).unwrap(),
            "local override"
        );
        fs::write(repo.join(".adc/index.md"), "Unicode: \u{4e2d}\u{6587}").unwrap();
        sync(&config, &next, true).unwrap();
        assert_eq!(
            fs::read_to_string(repo.join(".adc/index.md")).unwrap(),
            "upstream"
        );
        let removed = test_bundle("p", &[(".adc/new.md", "new")]);
        sync(&config, &removed, true).unwrap();
        assert!(!repo.join(".adc/index.md").exists());
        assert_eq!(
            fs::read_to_string(repo.join(".adc/local-only.md")).unwrap(),
            "keep"
        );
        fs::remove_dir_all(base).unwrap();
    }
}
