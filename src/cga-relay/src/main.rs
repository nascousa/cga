use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

const VERSION: &str = "1.30.96";
const SERVER_NAME: &str = "cga-relay";
const TRAY_ICON_LOGGED_IN_RESOURCE_ID: u16 = 1;
const TRAY_ICON_LOGGED_OUT_RESOURCE_ID: u16 = 4;
const PROJECT_DISPLAY_NAME: &str = "CGA-Relay";
const PROJECT_AUTHOR: &str = "Nate Scott";
const PROJECT_REPOSITORY: &str = "https://github.com/nascousa/cga";
const PROJECT_SUPPORT: &str = "https://github.com/nascousa/cga/issues";
const PROJECT_LICENSE: &str = "Apache License 2.0";
const CRYSTALS_PROFILE: &str = "CRYSTALS-CNSA-2.0";
const CRYSTALS_KEM: &str = "ML-KEM-1024";
const CRYSTALS_SIGNATURE: &str = "ML-DSA-87";
const CRYSTALS_TRANSPORT_SCOPE: &str = "local-ipc";

#[derive(Debug)]
struct AgentError(String);

type AgentResult<T> = Result<T, AgentError>;

#[derive(Clone, Debug)]
struct AgentConfig {
    agent_id: String,
    api_base_url: String,
    control_api_base_url: String,
    api_key_env: String,
    account_email: String,
    account_token_env: String,
    project_id: String,
    project_root: PathBuf,
    state_dir: PathBuf,
    log_dir: PathBuf,
    include_globs: Vec<String>,
    exclude_globs: Vec<String>,
    max_file_bytes: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct TrayLoginStatus {
    logged_in: bool,
    username: String,
}

#[derive(Clone, Debug)]
struct ProjectEntry {
    namespace: String,
    project_tag: String,
    locator: String,
    name: String,
    root: PathBuf,
    project_id: String,
}

#[derive(Clone, Debug)]
struct AccountProject {
    name: String,
    project_id: String,
    repo_path: String,
    is_active: bool,
}

#[derive(Clone, Debug)]
struct AccountGroup {
    id: String,
    name: String,
    description: String,
    is_active: bool,
    projects: Vec<AccountProject>,
}

#[derive(Clone, Debug, Default)]
struct ScanCounts {
    candidate: u64,
    excluded: u64,
    scanned: u64,
    changed: u64,
    unchanged: u64,
    oversized: u64,
    skipped_binary: u64,
    tombstone: u64,
    bytes_scanned: u64,
}

#[derive(Clone, Debug)]
struct Snapshot {
    path: String,
    sha256: String,
    bytes: u64,
    content: String,
}

#[derive(Clone, Debug)]
struct ScanResult {
    root: PathBuf,
    state_key: String,
    dry_run: bool,
    counts: ScanCounts,
    changed_paths: Vec<String>,
    unchanged_paths: Vec<String>,
    excluded_paths: Vec<String>,
    oversized_paths: Vec<String>,
    skipped_binary_paths: Vec<String>,
    tombstones: Vec<String>,
    snapshots: Vec<Snapshot>,
    state: BTreeMap<String, (String, u64)>,
}

fn main() {
    let code = match run(env::args().skip(1).collect()) {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("error: {}", error.0);
            2
        }
    };
    process::exit(code);
}

fn run(args: Vec<String>) -> AgentResult<()> {
    if args.is_empty() || args[0] == "--help" || args[0] == "-h" {
        print_help();
        return Ok(());
    }

    match args[0].as_str() {
        "doctor" => cmd_doctor(&args[1..]),
        "login" => cmd_login(&args[1..]),
        "projects" => cmd_projects(&args[1..]),
        "scan" => cmd_scan(&args[1..]),
        "sync" => cmd_sync(&args[1..]),
        "settings" => cmd_settings(&args[1..]),
        "tray" => cmd_tray(&args[1..]),
        "mcp" => cmd_mcp(&args[1..]),
        "--version" | "-V" => {
            println!("{SERVER_NAME} {VERSION}");
            Ok(())
        }
        other => Err(AgentError(format!("unknown command: {other}"))),
    }
}

fn print_help() {
    println!("CGA-Relay {VERSION}");
    println!();
    println!("Usage: cga-relay <command> [options]");
    println!();
    println!("Commands:");
    println!("  doctor    Validate local config without printing secrets");
    println!("  login     Store developer profile metadata and token env var name");
    println!("  projects  Add/list central local project registry entries");
    println!("  scan      Scan the configured project root");
    println!("  sync      Scan registered projects and submit changed snapshots");
    println!("  settings  Render or inspect the local account settings page");
    println!("  tray      Run the Windows notification-area tray icon");
    println!("  mcp       Run the stdio MCP-compatible gateway");
}

fn cmd_doctor(args: &[String]) -> AgentResult<()> {
    let config = load_config(required_arg(args, "--config")?)?;
    println!("{}", doctor_json(&config));
    Ok(())
}

fn cmd_login(args: &[String]) -> AgentResult<()> {
    let config = load_config(required_arg(args, "--config")?)?;
    let email = required_arg(args, "--email")?;
    let token_env = required_arg(args, "--token-env")?;
    ensure_state_dirs(&config)?;
    let profile = format!(
        "{{\"account_email\":\"{}\",\"account_token_env\":\"{}\"}}\n",
        json_escape(email),
        json_escape(token_env)
    );
    write_file(&profile_path(&config), &profile)?;
    println!(
        "{{\"account_email\":\"{}\",\"token\":{{\"env_var\":\"{}\",\"configured\":{}}}}}",
        json_escape(email),
        json_escape(token_env),
        env::var(token_env).is_ok()
    );
    Ok(())
}

fn cmd_projects(args: &[String]) -> AgentResult<()> {
    let subcommand = args
        .first()
        .ok_or_else(|| AgentError("missing projects subcommand".to_string()))?;
    match subcommand.as_str() {
        "add" => {
            let rest = &args[1..];
            let config = load_config(required_arg(rest, "--config")?)?;
            let project_tag = required_arg(rest, "--project-tag")?;
            let root = required_arg(rest, "--root")?;
            let namespace = optional_arg(rest, "--namespace").unwrap_or("default");
            let name = optional_arg(rest, "--name").unwrap_or(project_tag);
            let project = add_project(&config, namespace, project_tag, name, Path::new(root))?;
            println!("{}", project_json(&project));
            Ok(())
        }
        "list" => {
            let rest = &args[1..];
            let config = load_config(required_arg(rest, "--config")?)?;
            let projects = load_projects(&config)?;
            println!("{}", projects_json(&projects));
            Ok(())
        }
        other => Err(AgentError(format!("unknown projects subcommand: {other}"))),
    }
}

fn cmd_scan(args: &[String]) -> AgentResult<()> {
    let config = load_config(required_arg(args, "--config")?)?;
    let dry_run = has_flag(args, "--dry-run");
    let result = scan_project(&config, &config.project_root, &config.project_id, dry_run)?;
    if !dry_run {
        persist_scan_result(&config, &result)?;
    }
    println!("{}", scan_json(&result));
    Ok(())
}

fn cmd_sync(args: &[String]) -> AgentResult<()> {
    let config = load_config(required_arg(args, "--config")?)?;
    let dry_run = has_flag(args, "--dry-run");
    let all = has_flag(args, "--all");
    let project_tag = optional_arg(args, "--project-tag");
    let namespace = optional_arg(args, "--namespace").unwrap_or("default");
    let profile = read_profile(&config)?;
    let token_env = profile.get("account_token_env").ok_or_else(|| {
        AgentError("login profile does not contain account_token_env".to_string())
    })?;
    let developer_token = env::var(token_env).ok();
    let account_token = read_account_session(&config)
        .ok()
        .and_then(|session| session.get("access_token").cloned());
    if developer_token.is_none() && account_token.is_none() {
        return Err(AgentError(format!(
            "developer token env var {token_env} is not set and CGA account login is missing"
        )));
    }
    let projects = select_projects(&config, all, project_tag, namespace)?;
    let mut project_payloads = Vec::new();
    let mut submitted = 0_u64;
    for project in projects {
        let result = scan_project(&config, &project.root, &project.locator, true)?;
        let mut submission = String::from("null");
        if !dry_run && (!result.snapshots.is_empty() || !result.tombstones.is_empty()) {
            submission = submit_sync(
                &config,
                &project,
                &result,
                developer_token.as_deref(),
                account_token.as_deref(),
            )?;
            persist_scan_result(&config, &result)?;
            submitted += 1;
        }
        project_payloads.push(format!(
            "{{\"locator\":\"{}\",\"project_id\":\"{}\",\"scan\":{},\"submitted\":{},\"submission\":{}}}",
            json_escape(&project.locator),
            json_escape(&project.project_id),
            scan_json(&result),
            submission != "null",
            submission
        ));
    }
    println!(
        "{{\"dry_run\":{},\"submitted\":{},\"project_count\":{},\"projects\":[{}]}}",
        dry_run,
        submitted,
        project_payloads.len(),
        project_payloads.join(",")
    );
    Ok(())
}

fn cmd_tray(args: &[String]) -> AgentResult<()> {
    let config = load_config(required_arg(args, "--config")?)?;
    if has_flag(args, "--status") {
        println!("{}", tray_status_json(&config));
        return Ok(());
    }
    ensure_state_dirs(&config)?;
    let settings_url = start_settings_server(config.clone())?;
    run_platform_tray(&config, &settings_url)
}

fn cmd_settings(args: &[String]) -> AgentResult<()> {
    let config = load_config(required_arg(args, "--config")?)?;
    if has_flag(args, "--status") {
        println!("{}", settings_status_json(&config));
        return Ok(());
    }
    if has_flag(args, "--render") {
        println!("{}", settings_page_html(&config, None));
        return Ok(());
    }
    Err(AgentError(
        "settings requires --status or --render".to_string(),
    ))
}

fn cmd_mcp(args: &[String]) -> AgentResult<()> {
    let config = load_config(required_arg(args, "--config")?)?;
    let mut input = String::new();
    std::io::stdin()
        .read_to_string(&mut input)
        .map_err(|err| AgentError(format!("failed to read stdin: {err}")))?;
    log_communication(&config, "mcp.stdin", &input);
    let responses = handle_mcp_session(&config, &input)?;
    log_communication(&config, "mcp.stdout", &responses);
    print!("{responses}");
    Ok(())
}

fn tray_status_json(config: &AgentConfig) -> String {
    let login = tray_login_status(config);
    let user_groups = tray_user_group_labels(config, &login);
    format!(
        "{{\"supported\":{},\"agent_id\":\"{}\",\"project_id\":\"{}\",\"mode\":\"{}\",\"tooltip\":\"{}\",\"icon\":\"embedded-resource:{}\",\"icon_variant\":\"{}\",\"icon_loaded\":{},\"logged_in\":{},\"username\":\"{}\",\"menu\":{},\"about\":{{\"name\":\"{}\",\"user_groups\":{},\"user_group_count\":{},\"author\":\"{}\",\"repository\":\"{}\",\"support\":\"{}\",\"license\":\"{}\"}},\"menu_events\":[\"WM_CONTEXTMENU\",\"WM_RBUTTONUP\",\"WM_TIMER\"]}}",
        tray_supported(),
        json_escape(&config.agent_id),
        json_escape(&config.project_id),
        if tray_supported() {
            "windows-shell-notify-icon"
        } else {
            "unsupported"
        },
        json_escape(&tray_tooltip(config, &login)),
        tray_icon_resource_id(&login),
        tray_icon_variant(&login),
        tray_icon_loaded(),
        login.logged_in,
        json_escape(&login.username),
        tray_menu_json(&login),
        json_escape(PROJECT_DISPLAY_NAME),
        string_array_json(&user_groups),
        user_groups.len(),
        json_escape(PROJECT_AUTHOR),
        json_escape(PROJECT_REPOSITORY),
        json_escape(PROJECT_SUPPORT),
        json_escape(PROJECT_LICENSE)
    )
}

fn tray_login_status(config: &AgentConfig) -> TrayLoginStatus {
    let session = read_account_session(config).unwrap_or_default();
    let logged_in = session
        .get("access_token")
        .map(|token| !token.trim().is_empty())
        .unwrap_or(false);
    let username = if logged_in {
        session
            .get("username")
            .map(|value| value.trim())
            .filter(|value| !value.is_empty())
            .unwrap_or("CGA account")
            .to_string()
    } else {
        String::new()
    };
    TrayLoginStatus {
        logged_in,
        username,
    }
}

fn tray_login_menu_label(login: &TrayLoginStatus) -> String {
    if login.logged_in {
        format!("Signed in: {}", login.username)
    } else {
        "Not signed in".to_string()
    }
}

fn tray_user_group_labels(config: &AgentConfig, login: &TrayLoginStatus) -> Vec<String> {
    if !login.logged_in {
        return Vec::new();
    }
    load_account_groups(config)
        .unwrap_or_default()
        .into_iter()
        .filter_map(|group| {
            let name = group.name.trim();
            if name.is_empty() {
                None
            } else if group.is_active {
                Some(name.to_string())
            } else {
                Some(format!("{name} (inactive)"))
            }
        })
        .collect()
}

fn tray_user_group_summary(config: &AgentConfig, login: &TrayLoginStatus) -> String {
    if !login.logged_in {
        return "Not signed in".to_string();
    }
    let user_groups = tray_user_group_labels(config, login);
    if user_groups.is_empty() {
        "No user groups loaded".to_string()
    } else {
        user_groups.join(", ")
    }
}

fn tray_icon_resource_id(login: &TrayLoginStatus) -> u16 {
    if login.logged_in {
        TRAY_ICON_LOGGED_IN_RESOURCE_ID
    } else {
        TRAY_ICON_LOGGED_OUT_RESOURCE_ID
    }
}

fn tray_icon_variant(login: &TrayLoginStatus) -> &'static str {
    if login.logged_in {
        "color"
    } else {
        "gray"
    }
}

fn tray_menu_json(login: &TrayLoginStatus) -> String {
    format!(
        "[\"{}\",\"Settings\",\"Logs\",\"About\",\"Exit\"]",
        json_escape(&tray_login_menu_label(login))
    )
}

fn tray_tooltip(config: &AgentConfig, login: &TrayLoginStatus) -> String {
    if login.logged_in {
        format!("CGA-Relay - signed in as {}", login.username)
    } else {
        format!("CGA-Relay - {} - not signed in", config.agent_id)
    }
}

fn tray_supported() -> bool {
    cfg!(windows)
}

#[cfg(windows)]
fn tray_icon_loaded() -> bool {
    windows_tray::embedded_icon_available(TRAY_ICON_LOGGED_IN_RESOURCE_ID)
        && windows_tray::embedded_icon_available(TRAY_ICON_LOGGED_OUT_RESOURCE_ID)
}

#[cfg(not(windows))]
fn tray_icon_loaded() -> bool {
    false
}

#[cfg(windows)]
fn run_platform_tray(config: &AgentConfig, settings_url: &str) -> AgentResult<()> {
    windows_tray::run(config, settings_url)
}

#[cfg(not(windows))]
fn run_platform_tray(_config: &AgentConfig, _settings_url: &str) -> AgentResult<()> {
    Err(AgentError(
        "system tray mode is currently supported only on Windows".to_string(),
    ))
}

fn required_arg<'a>(args: &'a [String], name: &str) -> AgentResult<&'a str> {
    optional_arg(args, name).ok_or_else(|| AgentError(format!("missing required option: {name}")))
}

fn optional_arg<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.windows(2)
        .find_map(|window| (window[0] == name).then_some(window[1].as_str()))
}

fn has_flag(args: &[String], name: &str) -> bool {
    args.iter().any(|arg| arg == name)
}

fn load_config(path: &str) -> AgentResult<AgentConfig> {
    let text =
        fs::read_to_string(path).map_err(|err| AgentError(format!("cannot read config: {err}")))?;
    let mut values = BTreeMap::new();
    for (index, raw_line) in text.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            return Err(AgentError(format!(
                "invalid config line {}: expected KEY=VALUE",
                index + 1
            )));
        };
        let key = key.trim();
        if !key
            .chars()
            .all(|ch| ch.is_ascii_uppercase() || ch.is_ascii_digit() || ch == '_')
        {
            return Err(AgentError(format!(
                "invalid config key on line {}",
                index + 1
            )));
        }
        values.insert(key.to_string(), value.trim().to_string());
    }

    for key in [
        "AGENT_ID",
        "API_BASE_URL",
        "CONTROL_API_BASE_URL",
        "API_KEY_ENV",
        "ACCOUNT_EMAIL",
        "ACCOUNT_TOKEN_ENV",
        "PROJECT_ID",
        "PROJECT_ROOT",
        "STATE_DIR",
        "LOG_DIR",
        "INCLUDE_GLOBS",
        "EXCLUDE_GLOBS",
        "MAX_FILE_BYTES",
    ] {
        if !values.contains_key(key) {
            return Err(AgentError(format!("missing required config key: {key}")));
        }
    }

    let max_file_bytes = values["MAX_FILE_BYTES"]
        .parse::<u64>()
        .map_err(|_| AgentError("MAX_FILE_BYTES must be an integer".to_string()))?;
    if max_file_bytes == 0 {
        return Err(AgentError(
            "MAX_FILE_BYTES must be greater than zero".to_string(),
        ));
    }

    Ok(AgentConfig {
        agent_id: values["AGENT_ID"].clone(),
        api_base_url: trim_trailing_slash(&values["API_BASE_URL"]),
        control_api_base_url: trim_trailing_slash(&values["CONTROL_API_BASE_URL"]),
        api_key_env: values["API_KEY_ENV"].clone(),
        account_email: values["ACCOUNT_EMAIL"].clone(),
        account_token_env: values["ACCOUNT_TOKEN_ENV"].clone(),
        project_id: values["PROJECT_ID"].clone(),
        project_root: PathBuf::from(expand_user_vars(&values["PROJECT_ROOT"])),
        state_dir: PathBuf::from(expand_user_vars(&values["STATE_DIR"])),
        log_dir: PathBuf::from(expand_user_vars(&values["LOG_DIR"])),
        include_globs: split_globs(&values["INCLUDE_GLOBS"]),
        exclude_globs: split_globs(&values["EXCLUDE_GLOBS"]),
        max_file_bytes,
    })
}

fn trim_trailing_slash(value: &str) -> String {
    value.trim_end_matches('/').to_string()
}

fn expand_user_vars(value: &str) -> String {
    let mut text = value.to_string();
    if let Ok(user_profile) = env::var("USERPROFILE") {
        text = text.replace("%USERPROFILE%", &user_profile);
    }
    if let Ok(home) = env::var("HOME") {
        if let Some(stripped) = text.strip_prefix("~/") {
            text = format!("{home}/{stripped}");
        }
    }
    text
}

fn split_globs(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(|item| item.replace('\\', "/"))
        .collect()
}

fn ensure_state_dirs(config: &AgentConfig) -> AgentResult<()> {
    fs::create_dir_all(&config.state_dir)
        .map_err(|err| AgentError(format!("cannot create state dir: {err}")))?;
    fs::create_dir_all(&config.log_dir)
        .map_err(|err| AgentError(format!("cannot create log dir: {err}")))?;
    fs::create_dir_all(scan_state_dir(config))
        .map_err(|err| AgentError(format!("cannot create scan state dir: {err}")))?;
    Ok(())
}

fn log_communication(config: &AgentConfig, event: &str, detail: &str) {
    let now = unix_timestamp_seconds();
    let timestamp = utc_timestamp(now);
    let file_name = hourly_log_file_name(now);
    let sanitized = redact_sensitive_text(detail);
    let line = format!("[{timestamp}] {event}\n{sanitized}\n\n");
    if fs::create_dir_all(&config.log_dir).is_err() {
        return;
    }
    let path = config.log_dir.join(file_name);
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = file.write_all(line.as_bytes());
    }
}

fn unix_timestamp_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn hourly_log_file_name(seconds: u64) -> String {
    let (year, month, day, hour, _, _) = utc_time_parts(seconds);
    format!("{year:04}{month:02}{day:02}-{hour:02}.log")
}

fn utc_timestamp(seconds: u64) -> String {
    let (year, month, day, hour, minute, second) = utc_time_parts(seconds);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

fn utc_time_parts(seconds: u64) -> (i32, u32, u32, u32, u32, u32) {
    let days = (seconds / 86_400) as i64;
    let seconds_of_day = seconds % 86_400;
    let (year, month, day) = civil_from_days(days);
    let hour = (seconds_of_day / 3_600) as u32;
    let minute = ((seconds_of_day % 3_600) / 60) as u32;
    let second = (seconds_of_day % 60) as u32;
    (year, month, day, hour, minute, second)
}

fn civil_from_days(days_since_unix_epoch: i64) -> (i32, u32, u32) {
    let z = days_since_unix_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    let year = year + if month <= 2 { 1 } else { 0 };
    (year as i32, month as u32, day as u32)
}

fn redact_sensitive_text(detail: &str) -> String {
    let mut sanitized = redact_sensitive_header_lines(detail);
    sanitized = redact_bearer_tokens(&sanitized);
    for field in [
        "access_token",
        "refresh_token",
        "password",
        "token",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "authorization",
        "cookie",
        "set_cookie",
    ] {
        sanitized = redact_json_field(&sanitized, field);
        sanitized = redact_form_field(&sanitized, field);
    }
    sanitized
}

fn redact_sensitive_header_lines(detail: &str) -> String {
    detail
        .lines()
        .map(|line| {
            if let Some((name, _value)) = line.split_once(':') {
                if is_sensitive_header_name(name.trim()) {
                    return format!("{}: <redacted>", name.trim());
                }
            }
            line.to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn is_sensitive_header_name(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    matches!(
        lower.as_str(),
        "authorization" | "cookie" | "set-cookie" | "x-api-key"
    ) || lower.contains("token")
        || lower.contains("password")
        || lower.contains("secret")
        || lower.contains("api-key")
        || lower.contains("api_key")
}

fn redact_bearer_tokens(detail: &str) -> String {
    let mut output = String::new();
    let mut cursor = 0;
    while let Some(relative) = find_ascii_case_insensitive(&detail[cursor..], "Bearer ") {
        let marker_start = cursor + relative;
        let value_start = marker_start + "Bearer ".len();
        output.push_str(&detail[cursor..value_start]);
        output.push_str("<redacted>");
        let value_end = detail[value_start..]
            .find(|ch: char| ch.is_ascii_whitespace() || matches!(ch, '"' | '\'' | ',' | ';'))
            .map_or(detail.len(), |offset| value_start + offset);
        cursor = value_end;
    }
    output.push_str(&detail[cursor..]);
    output
}

fn redact_json_field(detail: &str, field: &str) -> String {
    let marker = format!("\"{field}\"");
    let mut output = String::new();
    let mut cursor = 0;
    while let Some(relative) = find_ascii_case_insensitive(&detail[cursor..], &marker) {
        let marker_start = cursor + relative;
        let mut value_start = marker_start + marker.len();
        let bytes = detail.as_bytes();
        while value_start < detail.len() && bytes[value_start].is_ascii_whitespace() {
            value_start += 1;
        }
        if value_start >= detail.len() || bytes[value_start] != b':' {
            output.push_str(&detail[cursor..value_start]);
            cursor = value_start;
            continue;
        }
        value_start += 1;
        while value_start < detail.len() && bytes[value_start].is_ascii_whitespace() {
            value_start += 1;
        }
        output.push_str(&detail[cursor..value_start]);
        if value_start < detail.len() && bytes[value_start] == b'"' {
            output.push_str("\"<redacted>\"");
            cursor = skip_json_string(detail, value_start).unwrap_or(detail.len());
        } else {
            output.push_str("<redacted>");
            cursor = detail[value_start..]
                .find(|ch| matches!(ch, ',' | '}' | ']' | '\n' | '\r'))
                .map_or(detail.len(), |offset| value_start + offset);
        }
    }
    output.push_str(&detail[cursor..]);
    output
}

fn skip_json_string(detail: &str, quote_start: usize) -> Option<usize> {
    let mut escaped = false;
    for (offset, ch) in detail[quote_start + 1..].char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '"' {
            return Some(quote_start + 1 + offset + ch.len_utf8());
        }
    }
    None
}

fn redact_form_field(detail: &str, field: &str) -> String {
    let marker = format!("{field}=");
    let mut output = String::new();
    let mut cursor = 0;
    while let Some(relative) = find_ascii_case_insensitive(&detail[cursor..], &marker) {
        let marker_start = cursor + relative;
        if marker_start > 0 {
            let previous = detail.as_bytes()[marker_start - 1] as char;
            if !matches!(previous, '&' | '?' | ' ' | '\n' | '\r' | '\t') {
                let next = marker_start + marker.len();
                output.push_str(&detail[cursor..next]);
                cursor = next;
                continue;
            }
        }
        let value_start = marker_start + marker.len();
        output.push_str(&detail[cursor..value_start]);
        output.push_str("<redacted>");
        cursor = detail[value_start..]
            .find(|ch: char| matches!(ch, '&' | ' ' | '\n' | '\r' | '\t'))
            .map_or(detail.len(), |offset| value_start + offset);
    }
    output.push_str(&detail[cursor..]);
    output
}

fn find_ascii_case_insensitive(haystack: &str, needle: &str) -> Option<usize> {
    haystack
        .to_ascii_lowercase()
        .find(&needle.to_ascii_lowercase())
}

fn profile_path(config: &AgentConfig) -> PathBuf {
    config.state_dir.join("profile.json")
}

fn account_session_path(config: &AgentConfig) -> PathBuf {
    config.state_dir.join("account-session.json")
}

fn account_projects_path(config: &AgentConfig) -> PathBuf {
    config.state_dir.join("account-projects.tsv")
}

fn account_groups_path(config: &AgentConfig) -> PathBuf {
    config.state_dir.join("account-groups.tsv")
}

fn settings_url_path(config: &AgentConfig) -> PathBuf {
    config.state_dir.join("settings-url.txt")
}

fn registry_path(config: &AgentConfig) -> PathBuf {
    config.state_dir.join("projects.json")
}

fn scan_state_dir(config: &AgentConfig) -> PathBuf {
    config.state_dir.join("scan-state")
}

fn scan_state_path(config: &AgentConfig, key: &str) -> PathBuf {
    scan_state_dir(config).join(format!("{}.state", safe_file_stem(key)))
}

fn safe_file_stem(value: &str) -> String {
    let stem: String = value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
                ch
            } else {
                '_'
            }
        })
        .collect();
    if stem.trim_matches(['.', '_', '-']).is_empty() {
        "default".to_string()
    } else {
        stem
    }
}

fn write_file(path: &Path, text: &str) -> AgentResult<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| AgentError(format!("cannot create parent dir: {err}")))?;
    }
    fs::write(path, text)
        .map_err(|err| AgentError(format!("cannot write {}: {err}", path.display())))
}

fn doctor_json(config: &AgentConfig) -> String {
    format!(
        "{{\"agent_id\":\"{}\",\"api_base_url\":{{\"configured\":{}}},\"control_api_base_url\":{{\"configured\":{}}},\"api_key\":{{\"env_var\":\"{}\",\"configured\":{}}},\"account_token\":{{\"env_var\":\"{}\",\"configured\":{}}},\"account_email\":{{\"configured\":{}}},\"project\":{{\"project_id\":\"{}\",\"project_root\":\"{}\",\"root_exists\":{}}},\"state\":{{\"state_dir\":\"{}\",\"log_dir\":\"{}\"}},\"limits\":{{\"max_file_bytes\":{}}}}}",
        json_escape(&config.agent_id),
        !config.api_base_url.is_empty(),
        !config.control_api_base_url.is_empty(),
        json_escape(&config.api_key_env),
        env::var(&config.api_key_env).is_ok(),
        json_escape(&config.account_token_env),
        env::var(&config.account_token_env).is_ok(),
        !config.account_email.is_empty(),
        json_escape(&config.project_id),
        json_escape(&display_path(&config.project_root)),
        config.project_root.exists(),
        json_escape(&display_path(&config.state_dir)),
        json_escape(&display_path(&config.log_dir)),
        config.max_file_bytes
    )
}

fn settings_status_json(config: &AgentConfig) -> String {
    let session = read_account_session(config).unwrap_or_default();
    let groups = load_account_groups(config).unwrap_or_default();
    let projects = projects_from_account_groups(&groups);
    let settings_url = fs::read_to_string(settings_url_path(config)).unwrap_or_default();
    format!(
        "{{\"enabled\":true,\"page\":\"local-account-settings\",\"login_endpoint\":\"/login\",\"projects_endpoint\":\"/api/auth/me/groups\",\"session_configured\":{},\"username\":\"{}\",\"project_count\":{},\"settings_url\":\"{}\"}}",
        session.contains_key("access_token"),
        json_escape(session.get("username").map(String::as_str).unwrap_or("")),
        projects.len(),
        json_escape(settings_url.trim())
    )
}

fn start_settings_server(config: AgentConfig) -> AgentResult<String> {
    ensure_state_dirs(&config)?;
    for port in 17860..17880 {
        if let Ok(listener) = TcpListener::bind(("127.0.0.1", port)) {
            let url = format!("http://127.0.0.1:{port}/settings");
            write_file(&settings_url_path(&config), &format!("{url}\n"))?;
            thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let config = config.clone();
                    thread::spawn(move || {
                        let _ = handle_settings_connection(&config, stream);
                    });
                }
            });
            return Ok(url);
        }
    }
    Err(AgentError(
        "cannot bind local settings page on 127.0.0.1:17860-17879".to_string(),
    ))
}

fn handle_settings_connection(config: &AgentConfig, mut stream: TcpStream) -> AgentResult<()> {
    let request = read_http_request(&mut stream)?;
    log_communication(
        config,
        "settings.request",
        &format!(
            "method={}\npath={}\nbody:\n{}",
            request.method, request.path, request.body
        ),
    );
    let response = match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/") | ("GET", "/settings") => html_response(&settings_page_html(config, None)),
        ("POST", "/login") => match handle_settings_login(config, &request.body) {
            Ok(message) => html_response(&settings_page_html(config, Some(&message))),
            Err(error) => html_response(&settings_page_html(config, Some(&error.0))),
        },
        ("POST", "/refresh") => match handle_settings_refresh(config) {
            Ok(message) => html_response(&settings_page_html(config, Some(&message))),
            Err(error) => html_response(&settings_page_html(config, Some(&error.0))),
        },
        ("POST", "/logout") => {
            let _ = fs::remove_file(account_session_path(config));
            let _ = fs::remove_file(account_projects_path(config));
            let _ = fs::remove_file(account_groups_path(config));
            html_response(&settings_page_html(config, Some("Signed out.")))
        }
        ("GET", "/status.json") => json_response(&settings_status_json(config)),
        _ => plain_response(404, "Not Found"),
    };
    stream
        .write_all(response.as_bytes())
        .map_err(|err| AgentError(format!("settings response failed: {err}")))
}

struct LocalHttpRequest {
    method: String,
    path: String,
    body: String,
}

fn read_http_request(stream: &mut TcpStream) -> AgentResult<LocalHttpRequest> {
    let mut buffer = Vec::new();
    let mut temp = [0_u8; 2048];
    let header_end;
    loop {
        let read = stream
            .read(&mut temp)
            .map_err(|err| AgentError(format!("settings request failed: {err}")))?;
        if read == 0 {
            return Err(AgentError("empty settings request".to_string()));
        }
        buffer.extend_from_slice(&temp[..read]);
        if let Some(position) = find_bytes(&buffer, b"\r\n\r\n") {
            header_end = position;
            break;
        }
        if buffer.len() > 64 * 1024 {
            return Err(AgentError("settings request is too large".to_string()));
        }
    }
    let header_text = String::from_utf8_lossy(&buffer[..header_end]).into_owned();
    let mut lines = header_text.lines();
    let request_line = lines
        .next()
        .ok_or_else(|| AgentError("missing settings request line".to_string()))?;
    let mut request_parts = request_line.split_whitespace();
    let method = request_parts.next().unwrap_or("").to_string();
    let raw_path = request_parts.next().unwrap_or("/");
    let path = raw_path.split('?').next().unwrap_or("/").to_string();
    let mut content_length = 0_usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse::<usize>().unwrap_or(0);
            }
        }
    }
    let body_start = header_end + 4;
    while buffer.len().saturating_sub(body_start) < content_length {
        let read = stream
            .read(&mut temp)
            .map_err(|err| AgentError(format!("settings body failed: {err}")))?;
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&temp[..read]);
    }
    let body_end = body_start + content_length.min(buffer.len().saturating_sub(body_start));
    let body = String::from_utf8_lossy(&buffer[body_start..body_end]).into_owned();
    Ok(LocalHttpRequest { method, path, body })
}

fn handle_settings_login(config: &AgentConfig, body: &str) -> AgentResult<String> {
    let form = parse_form_urlencoded(body);
    let username = form
        .get("username")
        .map(String::as_str)
        .unwrap_or("")
        .trim();
    let password = form.get("password").map(String::as_str).unwrap_or("");
    if username.is_empty() || password.is_empty() {
        return Err(AgentError(
            "Username and password are required.".to_string(),
        ));
    }

    let login_body = format!(
        "{{\"username\":\"{}\",\"password\":\"{}\"}}",
        json_escape(username),
        json_escape(password)
    );
    let token_json = http_post_json(
        config,
        &format!("{}/api/auth/login", config.api_base_url),
        &[],
        &login_body,
    )
    .map_err(|_| AgentError("CGA login failed.".to_string()))?;
    let access_token = json_string_field(&token_json, "access_token").ok_or_else(|| {
        AgentError("CGA login response did not include an access token.".to_string())
    })?;
    let auth = [("Authorization", format!("Bearer {access_token}"))];
    let me_json = http_get_json_with_headers(
        config,
        &format!("{}/api/auth/me", config.api_base_url),
        &auth,
    )
    .map_err(|_| AgentError("Could not load CGA account profile.".to_string()))?;
    let groups_json = http_get_json_with_headers(
        config,
        &format!("{}/api/auth/me/groups", config.api_base_url),
        &auth,
    )
    .map_err(|_| AgentError("Could not load CGA account user groups.".to_string()))?;
    let account_username =
        json_string_field(&me_json, "username").unwrap_or_else(|| username.to_string());
    let role = json_string_field(&me_json, "role").unwrap_or_default();
    let groups = parse_account_groups_json(&groups_json);
    let projects = projects_from_account_groups(&groups);
    write_account_session(config, &account_username, &role, &access_token)?;
    write_account_projects(config, &projects)?;
    write_account_groups(config, &groups)?;
    let local_count = sync_account_projects_to_registry(config, &projects)?;
    Ok(format!(
        "Signed in as {}. Loaded {} group-authorized CGA projects; {} have local repo paths and are available to CGA-Relay.",
        account_username,
        projects.len(),
        local_count
    ))
}

fn handle_settings_refresh(config: &AgentConfig) -> AgentResult<String> {
    let session = read_account_session(config)?;
    if !session.contains_key("access_token") {
        return Err(AgentError("Sign in before refreshing access.".to_string()));
    }
    let groups = refresh_account_groups(config, &session)?;
    let projects = projects_from_account_groups(&groups);
    Ok(format!(
        "Access refreshed. Loaded {} user groups and {} group-authorized CGA projects.",
        groups.len(),
        projects.len()
    ))
}

fn settings_page_html(config: &AgentConfig, message: Option<&str>) -> String {
    let session = read_account_session(config).unwrap_or_default();
    let groups = refresh_account_groups(config, &session)
        .or_else(|_| load_account_groups(config))
        .unwrap_or_default();
    let signed_in = session.contains_key("access_token");
    let username = session.get("username").map(String::as_str).unwrap_or("");
    let message_html = message
        .map(|text| format!("<div class=\"notice\">{}</div>", html_escape(text)))
        .unwrap_or_default();
    let group_html = render_account_groups(&groups, signed_in);
    let account_html = if signed_in {
        format!(
            "<section class=\"panel account-panel\"><div><p class=\"eyebrow\">Account</p><h2>Signed in</h2><p class=\"muted\">Connected as <strong>{}</strong>.</p></div><div class=\"account-actions\"><form method=\"post\" action=\"/refresh\"><button type=\"submit\">Refresh access</button></form><form method=\"post\" action=\"/logout\"><button class=\"secondary\" type=\"submit\">Sign out</button></form></div></section>",
            html_escape(username)
        )
    } else {
        "<section class=\"panel account-panel\"><div><p class=\"eyebrow\">Account</p><h2>Sign in to CGA</h2><p class=\"muted\">Use your CGA account to load accessible projects onto this machine.</p></div><form class=\"login-form\" method=\"post\" action=\"/login\"><label><span>Username</span><input name=\"username\" autocomplete=\"username\" required></label><label><span>Password</span><input name=\"password\" type=\"password\" autocomplete=\"current-password\" required></label><button type=\"submit\">Sign in</button></form></section>".to_string()
    };
    let stylesheet = r#"
:root{color-scheme:dark;--bg:#070b0e;--panel:#11181d;--panel-2:#0d1317;--line:#27343b;--text:#e8f4f1;--muted:#8ea19d;--accent:#2ee6a6;--accent-2:#ffcc66;--danger:#ff7a90;--shadow:0 24px 70px rgba(0,0,0,.42)}
*{box-sizing:border-box}html{min-height:100%}body{min-height:100%;margin:0;font-family:Segoe UI,Arial,sans-serif;background:#070b0e;color:var(--text);letter-spacing:0}body::before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);background-size:44px 44px;mask-image:linear-gradient(to bottom,#000,transparent 82%)}main{width:min(1120px,calc(100vw - 40px));margin:0 auto;padding:36px 0 44px}.topbar{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:18px}.eyebrow{margin:0 0 8px;color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}h1,h2{margin:0;letter-spacing:0}h1{font-size:34px;line-height:1.08}h2{font-size:20px}h3{margin:0;font-size:16px}.muted{color:var(--muted);line-height:1.55}.version-pill{border:1px solid var(--line);background:#0b1115;border-radius:999px;color:var(--accent-2);padding:8px 12px;white-space:nowrap}.notice{border:1px solid rgba(46,230,166,.38);background:rgba(46,230,166,.1);color:#d8fff2;border-radius:8px;padding:12px 14px;margin:18px 0}.status-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:20px 0}.metric{background:rgba(17,24,29,.82);border:1px solid var(--line);border-radius:8px;padding:12px}.metric span{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}.metric strong{font-size:14px;word-break:break-word}.panel{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);padding:22px;margin:16px 0}.account-panel{display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,420px);gap:22px;align-items:start}.account-actions{display:flex;justify-content:flex-end;align-items:flex-start;gap:10px;flex-wrap:wrap}.account-actions form{margin:0}.group-list{display:grid;gap:16px;margin-top:14px}.group-card{border:1px solid rgba(39,52,59,.78);border-radius:8px;background:rgba(8,13,16,.42);padding:14px}.group-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.login-form{display:grid;gap:14px}label span{display:block;color:var(--muted);font-size:13px;margin-bottom:6px}input{width:100%;height:40px;border:1px solid #31434a;border-radius:8px;background:#080d10;color:var(--text);padding:0 12px;outline:none}input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(46,230,166,.14)}button{height:40px;border:0;border-radius:8px;background:var(--accent);color:#03110c;font-weight:800;padding:0 16px;cursor:pointer}button.secondary{border:1px solid #34444a;background:#0a1014;color:var(--text)}table{width:100%;border-collapse:collapse;margin-top:16px;overflow:hidden}th,td{border-bottom:1px solid var(--line);text-align:left;padding:12px 10px;vertical-align:top}th{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase}code{color:#b5fff0;background:#07100e;border:1px solid #1a3b34;border-radius:6px;padding:3px 6px;font-size:12px}.project-name{font-weight:700}.status{display:inline-flex;align-items:center;border-radius:999px;padding:4px 9px;font-size:12px;font-weight:800;white-space:nowrap}.ready{background:rgba(46,230,166,.14);color:#7dffd3;border:1px solid rgba(46,230,166,.35)}.pending{background:rgba(255,204,102,.13);color:#ffe0a3;border:1px solid rgba(255,204,102,.36)}.empty-state{color:var(--muted);padding:28px 10px}@media(max-width:760px){main{width:min(100vw - 24px,1120px);padding-top:24px}.topbar,.account-panel{display:block}.account-actions{justify-content:flex-start;margin-top:16px}.version-pill{display:inline-block;margin-top:14px}.status-grid{grid-template-columns:1fr}h1{font-size:28px}td,th{padding:10px 8px}}
"#;
    format!(
        "<!doctype html><html data-theme=\"dark\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>CGA-Relay Settings</title><style>{stylesheet}</style></head><body><main><header class=\"topbar\"><div><p class=\"eyebrow\">CGA-Relay</p><h1>CGA-Relay Settings</h1><p class=\"muted\">Secure relay access for your CGA account.</p></div><div class=\"version-pill\">v{VERSION}</div></header><div class=\"status-grid\"><div class=\"metric\"><span>Relay</span><strong>{}</strong></div><div class=\"metric\"><span>API</span><strong>{}</strong></div></div>{message_html}{account_html}{group_html}</main></body></html>",
        html_escape(&config.agent_id),
        html_escape(&config.api_base_url)
    )
}

fn render_account_groups(groups: &[AccountGroup], signed_in: bool) -> String {
    if !signed_in {
        return String::new();
    }
    let body = if groups.is_empty() {
        "<div class=\"empty-state\">No user groups loaded for this account.</div>".to_string()
    } else {
        groups
            .iter()
            .map(|group| {
                let project_rows = if group.projects.is_empty() {
                    "<tr><td colspan=\"4\" class=\"empty-state\">No projects mapped to this group.</td></tr>".to_string()
                } else {
                    group
                        .projects
                        .iter()
                        .map(|project| {
                            let state = if project.is_active { "Active" } else { "Inactive" };
                            format!(
                                "<tr><td><span class=\"project-name\">{}</span></td><td><code>{}</code></td><td>{}</td><td>{}</td></tr>",
                                html_escape(&project.name),
                                html_escape(&project.project_id),
                                html_escape(&project.repo_path),
                                state
                            )
                        })
                        .collect::<Vec<_>>()
                        .join("")
                };
                let state = if group.is_active { "Active" } else { "Inactive" };
                let description = if group.description.trim().is_empty() {
                    String::new()
                } else {
                    format!("<p class=\"muted\">{}</p>", html_escape(&group.description))
                };
                format!(
                    "<div class=\"group-card\"><div class=\"group-head\"><h3>{}</h3><span class=\"status ready\">{}</span></div>{}<table><thead><tr><th>Project</th><th>Project ID</th><th>Local Path</th><th>Status</th></tr></thead><tbody>{}</tbody></table></div>",
                    html_escape(&group.name),
                    state,
                    description,
                    project_rows
                )
            })
            .collect::<Vec<_>>()
            .join("")
    };
    format!(
        "<section class=\"panel\"><p class=\"eyebrow\">Access</p><h2>User Groups</h2><div class=\"group-list\">{body}</div></section>"
    )
}

fn refresh_account_groups(
    config: &AgentConfig,
    session: &BTreeMap<String, String>,
) -> AgentResult<Vec<AccountGroup>> {
    let access_token = session
        .get("access_token")
        .ok_or_else(|| AgentError("not signed in".to_string()))?;
    let auth = [("Authorization", format!("Bearer {access_token}"))];
    let groups_json = http_get_json_with_headers(
        config,
        &format!("{}/api/auth/me/groups", config.api_base_url),
        &auth,
    )?;
    let groups = parse_account_groups_json(&groups_json);
    write_account_groups(config, &groups)?;
    let projects = projects_from_account_groups(&groups);
    write_account_projects(config, &projects)?;
    let _ = sync_account_projects_to_registry(config, &projects)?;
    Ok(groups)
}

fn parse_account_projects_json(text: &str) -> Vec<AccountProject> {
    json_object_slices(text)
        .into_iter()
        .filter_map(|object| {
            let name = json_string_field(&object, "project_name")?;
            let project_id = json_string_field(&object, "project_id")?;
            let repo_path = json_string_field(&object, "repo_path").unwrap_or_default();
            let is_active = json_field(&object, "is_active")
                .map(|value| value == "true")
                .unwrap_or(true);
            Some(AccountProject {
                name,
                project_id,
                repo_path,
                is_active,
            })
        })
        .collect()
}

fn parse_account_groups_json(text: &str) -> Vec<AccountGroup> {
    json_object_slices(text)
        .into_iter()
        .filter_map(|object| {
            let id = json_field(&object, "id").unwrap_or_default();
            let name = json_string_field(&object, "group_name")?;
            let description = json_string_field(&object, "description").unwrap_or_default();
            let is_active = json_field(&object, "is_active")
                .map(|value| value == "true")
                .unwrap_or(true);
            let projects = json_array_field(&object, "projects")
                .map(|value| parse_account_projects_json(&value))
                .unwrap_or_default();
            Some(AccountGroup {
                id,
                name,
                description,
                is_active,
                projects,
            })
        })
        .collect()
}

fn projects_from_account_groups(groups: &[AccountGroup]) -> Vec<AccountProject> {
    let mut seen_project_ids = BTreeSet::new();
    let mut projects = Vec::new();
    for group in groups.iter().filter(|group| group.is_active) {
        for project in &group.projects {
            let project_key = if project.project_id.trim().is_empty() {
                format!("{}|{}", project.name, project.repo_path)
            } else {
                project.project_id.clone()
            };
            if seen_project_ids.insert(project_key) {
                projects.push(project.clone());
            }
        }
    }
    projects
}

fn json_object_slices(text: &str) -> Vec<String> {
    let mut objects = Vec::new();
    let mut start = None;
    let mut depth = 0_i32;
    let mut in_string = false;
    let mut escaped = false;
    for (index, ch) in text.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if in_string {
            if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
            }
            continue;
        }
        match ch {
            '"' => in_string = true,
            '{' => {
                if depth == 0 {
                    start = Some(index);
                }
                depth += 1;
            }
            '}' => {
                depth -= 1;
                if depth == 0 {
                    if let Some(start_index) = start.take() {
                        objects.push(text[start_index..=index].to_string());
                    }
                }
            }
            _ => {}
        }
    }
    objects
}

fn parse_form_urlencoded(body: &str) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    for pair in body.split('&') {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        values.insert(percent_decode(key), percent_decode(value));
    }
    values
}

fn percent_decode(value: &str) -> String {
    let mut bytes = Vec::new();
    let mut input = value.as_bytes().iter().copied().peekable();
    while let Some(byte) = input.next() {
        if byte == b'+' {
            bytes.push(b' ');
        } else if byte == b'%' {
            let high = input.next();
            let low = input.next();
            if let (Some(high), Some(low)) = (high, low) {
                if let (Some(high), Some(low)) = (hex_value(high), hex_value(low)) {
                    bytes.push((high << 4) | low);
                }
            }
        } else {
            bytes.push(byte);
        }
    }
    String::from_utf8_lossy(&bytes).into_owned()
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn html_response(body: &str) -> String {
    http_response(200, "text/html; charset=utf-8", body)
}

fn json_response(body: &str) -> String {
    http_response(200, "application/json; charset=utf-8", body)
}

fn plain_response(status: u16, body: &str) -> String {
    http_response(status, "text/plain; charset=utf-8", body)
}

fn http_response(status: u16, content_type: &str, body: &str) -> String {
    let reason = match status {
        200 => "OK",
        404 => "Not Found",
        _ => "OK",
    };
    format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn html_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn add_project(
    config: &AgentConfig,
    namespace: &str,
    project_tag: &str,
    name: &str,
    root: &Path,
) -> AgentResult<ProjectEntry> {
    validate_identifier("namespace", namespace)?;
    validate_identifier("project_tag", project_tag)?;
    let root = root.to_path_buf();
    if !root.is_dir() {
        return Err(AgentError(format!(
            "project root does not exist: {}",
            root.display()
        )));
    }
    ensure_state_dirs(config)?;
    let locator = format!("{namespace}/{project_tag}");
    let entry = ProjectEntry {
        namespace: namespace.to_string(),
        project_tag: project_tag.to_string(),
        locator: locator.clone(),
        name: name.to_string(),
        root,
        project_id: config.project_id.clone(),
    };
    let mut projects = load_projects(config)?;
    projects.retain(|project| project.locator != locator);
    projects.push(entry.clone());
    projects.sort_by(|left, right| left.locator.cmp(&right.locator));
    write_projects(config, &projects)?;
    Ok(entry)
}

fn validate_identifier(label: &str, value: &str) -> AgentResult<()> {
    if value.is_empty()
        || !value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'))
    {
        return Err(AgentError(format!("{label} contains invalid characters")));
    }
    Ok(())
}

fn load_projects(config: &AgentConfig) -> AgentResult<Vec<ProjectEntry>> {
    let path = registry_path(config);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text = fs::read_to_string(&path)
        .map_err(|err| AgentError(format!("cannot read project registry: {err}")))?;
    let mut projects = Vec::new();
    for line in text.lines().filter(|line| line.starts_with("project\t")) {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() != 7 {
            continue;
        }
        projects.push(ProjectEntry {
            namespace: unescape_field(parts[1]),
            project_tag: unescape_field(parts[2]),
            locator: unescape_field(parts[3]),
            name: unescape_field(parts[4]),
            root: PathBuf::from(unescape_field(parts[5])),
            project_id: unescape_field(parts[6]),
        });
    }
    Ok(projects)
}

fn write_projects(config: &AgentConfig, projects: &[ProjectEntry]) -> AgentResult<()> {
    let mut text = String::from("version\t1\n");
    for project in projects {
        text.push_str(&format!(
            "project\t{}\t{}\t{}\t{}\t{}\t{}\n",
            escape_field(&project.namespace),
            escape_field(&project.project_tag),
            escape_field(&project.locator),
            escape_field(&project.name),
            escape_field(&display_path(&project.root)),
            escape_field(&project.project_id)
        ));
    }
    write_file(&registry_path(config), &text)
}

fn select_projects(
    config: &AgentConfig,
    all: bool,
    project_tag: Option<&str>,
    namespace: &str,
) -> AgentResult<Vec<ProjectEntry>> {
    let projects = load_projects(config)?;
    if all {
        return Ok(projects);
    }
    let Some(project_tag) = project_tag else {
        return Err(AgentError(
            "project_tag is required unless --all is used".to_string(),
        ));
    };
    let locator = format!("{namespace}/{project_tag}");
    let selected: Vec<ProjectEntry> = projects
        .into_iter()
        .filter(|project| project.locator == locator)
        .collect();
    if selected.is_empty() {
        Err(AgentError(format!("project is not registered: {locator}")))
    } else {
        Ok(selected)
    }
}

fn project_json(project: &ProjectEntry) -> String {
    format!(
        "{{\"namespace\":\"{}\",\"project_tag\":\"{}\",\"locator\":\"{}\",\"name\":\"{}\",\"root\":\"{}\",\"project_id\":\"{}\"}}",
        json_escape(&project.namespace),
        json_escape(&project.project_tag),
        json_escape(&project.locator),
        json_escape(&project.name),
        json_escape(&display_path(&project.root)),
        json_escape(&project.project_id)
    )
}

fn projects_json(projects: &[ProjectEntry]) -> String {
    format!(
        "{{\"count\":{},\"projects\":[{}]}}",
        projects.len(),
        projects
            .iter()
            .map(project_json)
            .collect::<Vec<_>>()
            .join(",")
    )
}

fn scan_project(
    config: &AgentConfig,
    root: &Path,
    state_key: &str,
    dry_run: bool,
) -> AgentResult<ScanResult> {
    let previous = load_scan_state(config, state_key)?;
    let mut current = BTreeMap::new();
    let mut result = ScanResult {
        root: root.to_path_buf(),
        state_key: state_key.to_string(),
        dry_run,
        counts: ScanCounts::default(),
        changed_paths: Vec::new(),
        unchanged_paths: Vec::new(),
        excluded_paths: Vec::new(),
        oversized_paths: Vec::new(),
        skipped_binary_paths: Vec::new(),
        tombstones: Vec::new(),
        snapshots: Vec::new(),
        state: BTreeMap::new(),
    };
    let mut files = Vec::new();
    collect_files(config, root, root, &mut files)?;
    files.sort_by(|left, right| {
        normalized_relative(root, left).cmp(&normalized_relative(root, right))
    });

    for path in files {
        let rel_path = normalized_relative(root, &path);
        result.counts.candidate += 1;
        if !included(config, &rel_path) || excluded(config, root, &rel_path) {
            result.counts.excluded += 1;
            result.excluded_paths.push(rel_path);
            continue;
        }
        let metadata = fs::metadata(&path)
            .map_err(|err| AgentError(format!("cannot stat {}: {err}", path.display())))?;
        if metadata.len() > config.max_file_bytes {
            result.counts.oversized += 1;
            result.oversized_paths.push(rel_path);
            continue;
        }
        let bytes = fs::read(&path)
            .map_err(|err| AgentError(format!("cannot read {}: {err}", path.display())))?;
        if bytes.contains(&0) {
            result.counts.skipped_binary += 1;
            result.skipped_binary_paths.push(rel_path);
            continue;
        }
        let Ok(content) = String::from_utf8(bytes.clone()) else {
            result.counts.skipped_binary += 1;
            result.skipped_binary_paths.push(rel_path);
            continue;
        };
        let digest = sha256_hex(&bytes);
        let byte_count = bytes.len() as u64;
        result.counts.scanned += 1;
        result.counts.bytes_scanned += byte_count;
        current.insert(rel_path.clone(), (digest.clone(), byte_count));
        if previous.get(&rel_path).map(|(hash, _)| hash.as_str()) == Some(digest.as_str()) {
            result.counts.unchanged += 1;
            result.unchanged_paths.push(rel_path);
        } else {
            result.counts.changed += 1;
            result.changed_paths.push(rel_path.clone());
            result.snapshots.push(Snapshot {
                path: rel_path,
                sha256: digest,
                bytes: byte_count,
                content,
            });
        }
    }

    for rel_path in previous.keys() {
        if !current.contains_key(rel_path)
            && (!root.join(rel_path).exists()
                || !included(config, rel_path)
                || excluded(config, root, rel_path))
        {
            result.tombstones.push(rel_path.clone());
        }
    }
    result.counts.tombstone = result.tombstones.len() as u64;
    result.state = current;
    Ok(result)
}

fn collect_files(
    config: &AgentConfig,
    scan_root: &Path,
    current_dir: &Path,
    files: &mut Vec<PathBuf>,
) -> AgentResult<()> {
    if !current_dir.exists() {
        return Err(AgentError(format!(
            "project root does not exist: {}",
            current_dir.display()
        )));
    }
    for entry in fs::read_dir(current_dir)
        .map_err(|err| AgentError(format!("cannot read dir {}: {err}", current_dir.display())))?
    {
        let entry =
            entry.map_err(|err| AgentError(format!("cannot read directory entry: {err}")))?;
        let path = entry.path();
        if path.is_dir() {
            let rel_path = normalized_relative(scan_root, &path);
            if excluded(config, scan_root, &rel_path) {
                continue;
            }
            collect_files(config, scan_root, &path, files)?;
        } else if path.is_file() {
            files.push(path);
        }
    }
    Ok(())
}

fn normalized_relative(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn included(config: &AgentConfig, rel_path: &str) -> bool {
    config.include_globs.is_empty()
        || config
            .include_globs
            .iter()
            .any(|pattern| glob_match(pattern, rel_path))
}

fn excluded(config: &AgentConfig, root: &Path, rel_path: &str) -> bool {
    if always_excluded(rel_path) {
        return true;
    }
    config.exclude_globs.iter().any(|pattern| {
        let pattern = agent_state_pattern(config, root, pattern);
        glob_match(&pattern, rel_path)
    })
}

fn always_excluded(rel_path: &str) -> bool {
    let normalized = rel_path.replace('\\', "/");
    let lower = normalized.to_ascii_lowercase();
    let segments: Vec<&str> = lower.split('/').filter(|part| !part.is_empty()).collect();
    if segments.iter().any(|part| {
        matches!(
            *part,
            ".git"
                | "node_modules"
                | ".venv"
                | "__pycache__"
                | "target"
                | "dist"
                | "build"
                | ".pytest_cache"
                | ".ruff_cache"
                | ".deploy-keys"
                | ".nasco"
        )
    }) {
        return true;
    }
    let file_name = segments.last().copied().unwrap_or("");
    if file_name == ".env" {
        return true;
    }
    if file_name.starts_with(".env.") && file_name != ".env.example" {
        return true;
    }
    if matches!(file_name, "id_rsa" | "id_dsa" | "id_ecdsa" | "id_ed25519") {
        return true;
    }
    lower.ends_with(".pem")
        || lower.ends_with(".pfx")
        || lower.ends_with(".p12")
        || lower.ends_with(".key")
}

fn agent_state_pattern(config: &AgentConfig, root: &Path, pattern: &str) -> String {
    if !pattern.starts_with("<agent-state>") {
        return pattern.to_string();
    }
    let Ok(rel) = config.state_dir.strip_prefix(root) else {
        return "__cga_state_outside_project__/**".to_string();
    };
    let suffix = pattern
        .trim_start_matches("<agent-state>")
        .trim_start_matches('/');
    if suffix.is_empty() {
        rel.to_string_lossy().replace('\\', "/")
    } else {
        format!("{}/{}", rel.to_string_lossy().replace('\\', "/"), suffix)
    }
}

fn glob_match(pattern: &str, value: &str) -> bool {
    if let Some(prefix) = pattern.strip_suffix("/**") {
        let prefix = prefix.trim_end_matches('/');
        return value == prefix || value.starts_with(&format!("{prefix}/"));
    }
    glob_match_chars(pattern.as_bytes(), value.as_bytes())
}

fn glob_match_chars(pattern: &[u8], value: &[u8]) -> bool {
    if pattern.is_empty() {
        return value.is_empty();
    }
    match pattern[0] {
        b'*' => {
            glob_match_chars(&pattern[1..], value)
                || (!value.is_empty() && glob_match_chars(pattern, &value[1..]))
        }
        b'?' => !value.is_empty() && glob_match_chars(&pattern[1..], &value[1..]),
        ch => !value.is_empty() && ch == value[0] && glob_match_chars(&pattern[1..], &value[1..]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_excludes_secret_like_files() {
        assert!(always_excluded(".env"));
        assert!(always_excluded(".env.local"));
        assert!(always_excluded(".deploy-keys/deploy_key"));
        assert!(always_excluded("certs/service.pem"));
        assert!(always_excluded(".nasco/local-notes.md"));
        assert!(always_excluded(
            "src/cga-relay/target/release/cga-relay.exe"
        ));
        assert!(!always_excluded(".env.example"));
        assert!(!always_excluded("docs/deploy_key.md"));
    }

    #[test]
    fn directory_globs_match_nested_paths() {
        assert!(glob_match(
            "src/cga-relay/target/**",
            "src/cga-relay/target"
        ));
        assert!(glob_match(
            "src/cga-relay/target/**",
            "src/cga-relay/target/debug/object.o"
        ));
        assert!(glob_match("target/**", "target/release/app"));
        assert!(!glob_match(
            "target/**",
            "src/cga-relay/target/debug/object.o"
        ));
    }
}

fn load_scan_state(
    config: &AgentConfig,
    key: &str,
) -> AgentResult<BTreeMap<String, (String, u64)>> {
    let path = scan_state_path(config, key);
    if !path.exists() {
        return Ok(BTreeMap::new());
    }
    let text = fs::read_to_string(&path)
        .map_err(|err| AgentError(format!("cannot read scan state {}: {err}", path.display())))?;
    let mut state = BTreeMap::new();
    for line in text.lines().filter(|line| line.starts_with("file\t")) {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() != 4 {
            continue;
        }
        let bytes = parts[3].parse::<u64>().unwrap_or(0);
        state.insert(unescape_field(parts[1]), (unescape_field(parts[2]), bytes));
    }
    Ok(state)
}

fn persist_scan_result(config: &AgentConfig, result: &ScanResult) -> AgentResult<()> {
    ensure_state_dirs(config)?;
    let mut text = format!(
        "version\t1\nroot\t{}\n",
        escape_field(&display_path(&result.root))
    );
    for (path, (hash, bytes)) in &result.state {
        text.push_str(&format!(
            "file\t{}\t{}\t{}\n",
            escape_field(path),
            escape_field(hash),
            bytes
        ));
    }
    write_file(&scan_state_path(config, &result.state_key), &text)
}

fn scan_json(result: &ScanResult) -> String {
    format!(
        "{{\"project_root\":\"{}\",\"state_key\":\"{}\",\"dry_run\":{},\"counts\":{},\"changed_paths\":{},\"unchanged_paths\":{},\"excluded_paths\":{},\"oversized_paths\":{},\"skipped_binary_paths\":{},\"tombstones\":{}}}",
        json_escape(&display_path(&result.root)),
        json_escape(&result.state_key),
        result.dry_run,
        counts_json(&result.counts),
        string_array_json(&result.changed_paths),
        string_array_json(&result.unchanged_paths),
        string_array_json(&result.excluded_paths),
        string_array_json(&result.oversized_paths),
        string_array_json(&result.skipped_binary_paths),
        string_array_json(&result.tombstones),
    )
}

fn counts_json(counts: &ScanCounts) -> String {
    format!(
        "{{\"candidate\":{},\"excluded\":{},\"scanned\":{},\"changed\":{},\"unchanged\":{},\"oversized\":{},\"skipped_binary\":{},\"tombstone\":{},\"bytes_scanned\":{}}}",
        counts.candidate,
        counts.excluded,
        counts.scanned,
        counts.changed,
        counts.unchanged,
        counts.oversized,
        counts.skipped_binary,
        counts.tombstone,
        counts.bytes_scanned
    )
}

fn read_profile(config: &AgentConfig) -> AgentResult<BTreeMap<String, String>> {
    let path = profile_path(config);
    if !path.exists() {
        return Err(AgentError(
            "login profile is missing; run login first".to_string(),
        ));
    }
    let text = fs::read_to_string(&path)
        .map_err(|err| AgentError(format!("cannot read login profile: {err}")))?;
    Ok(parse_flat_json_object(&text))
}

fn read_account_session(config: &AgentConfig) -> AgentResult<BTreeMap<String, String>> {
    let path = account_session_path(config);
    if !path.exists() {
        return Ok(BTreeMap::new());
    }
    let text = fs::read_to_string(&path)
        .map_err(|err| AgentError(format!("cannot read account session: {err}")))?;
    let mut values = BTreeMap::new();
    for key in ["username", "role", "access_token", "token_type"] {
        if let Some(value) = json_string_field(&text, key) {
            values.insert(key.to_string(), value);
        }
    }
    Ok(values)
}

fn write_account_session(
    config: &AgentConfig,
    username: &str,
    role: &str,
    access_token: &str,
) -> AgentResult<()> {
    let body = format!(
        "{{\"username\":\"{}\",\"role\":\"{}\",\"token_type\":\"bearer\",\"access_token\":\"{}\"}}\n",
        json_escape(username),
        json_escape(role),
        json_escape(access_token)
    );
    write_file(&account_session_path(config), &body)
}

fn write_account_projects(config: &AgentConfig, projects: &[AccountProject]) -> AgentResult<()> {
    let mut text = String::from("version\t1\n");
    for project in projects {
        text.push_str(&format!(
            "project\t{}\t{}\t{}\t{}\n",
            escape_field(&project.name),
            escape_field(&project.project_id),
            escape_field(&project.repo_path),
            if project.is_active { "1" } else { "0" }
        ));
    }
    write_file(&account_projects_path(config), &text)
}

fn load_account_groups(config: &AgentConfig) -> AgentResult<Vec<AccountGroup>> {
    let path = account_groups_path(config);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text = fs::read_to_string(&path)
        .map_err(|err| AgentError(format!("cannot read account groups: {err}")))?;
    let mut groups: Vec<AccountGroup> = Vec::new();
    for line in text.lines() {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.first() == Some(&"group") && parts.len() == 5 {
            groups.push(AccountGroup {
                id: unescape_field(parts[1]),
                name: unescape_field(parts[2]),
                description: unescape_field(parts[3]),
                is_active: parts[4] == "1",
                projects: Vec::new(),
            });
        } else if parts.first() == Some(&"project") && parts.len() == 6 {
            let group_id = unescape_field(parts[1]);
            if let Some(group) = groups.iter_mut().find(|group| group.id == group_id) {
                group.projects.push(AccountProject {
                    name: unescape_field(parts[2]),
                    project_id: unescape_field(parts[3]),
                    repo_path: unescape_field(parts[4]),
                    is_active: parts[5] == "1",
                });
            }
        }
    }
    Ok(groups)
}

fn write_account_groups(config: &AgentConfig, groups: &[AccountGroup]) -> AgentResult<()> {
    let mut text = String::from("version\t1\n");
    for group in groups {
        text.push_str(&format!(
            "group\t{}\t{}\t{}\t{}\n",
            escape_field(&group.id),
            escape_field(&group.name),
            escape_field(&group.description),
            if group.is_active { "1" } else { "0" }
        ));
        for project in &group.projects {
            text.push_str(&format!(
                "project\t{}\t{}\t{}\t{}\t{}\n",
                escape_field(&group.id),
                escape_field(&project.name),
                escape_field(&project.project_id),
                escape_field(&project.repo_path),
                if project.is_active { "1" } else { "0" }
            ));
        }
    }
    write_file(&account_groups_path(config), &text)
}

fn sync_account_projects_to_registry(
    config: &AgentConfig,
    projects: &[AccountProject],
) -> AgentResult<usize> {
    let mut registry = load_projects(config)?;
    registry.retain(|project| project.namespace != "account");
    let mut added = 0_usize;
    for project in projects {
        let root = PathBuf::from(project.repo_path.trim());
        if !project.is_active || project.project_id.trim().is_empty() || !root.is_dir() {
            continue;
        }
        let project_tag = safe_file_stem(&project.name);
        let locator = format!("account/{project_tag}");
        registry.push(ProjectEntry {
            namespace: "account".to_string(),
            project_tag,
            locator,
            name: project.name.clone(),
            root,
            project_id: project.project_id.clone(),
        });
        added += 1;
    }
    registry.sort_by(|left, right| left.locator.cmp(&right.locator));
    write_projects(config, &registry)?;
    Ok(added)
}

fn submit_sync(
    config: &AgentConfig,
    project: &ProjectEntry,
    result: &ScanResult,
    developer_token: Option<&str>,
    account_token: Option<&str>,
) -> AgentResult<String> {
    let snapshots_json = result
        .snapshots
        .iter()
        .map(snapshot_json)
        .collect::<Vec<_>>()
        .join(",");
    let body = format!(
        "{{\"agent_id\":\"{}\",\"project_id\":\"{}\",\"namespace\":\"{}\",\"project_tag\":\"{}\",\"root\":\"{}\",\"counts\":{},\"snapshots\":[{}],\"tombstones\":{}}}",
        json_escape(&config.agent_id),
        json_escape(&project.project_id),
        json_escape(&project.namespace),
        json_escape(&project.project_tag),
        json_escape(&display_path(&project.root)),
        counts_json(&result.counts),
        snapshots_json,
        string_array_json(&result.tombstones),
    );
    if let Some(developer_token) = developer_token {
        http_post_json(
            config,
            &format!("{}/api/project/cga-relay/sync", config.control_api_base_url),
            &[
                ("Authorization", format!("Bearer {developer_token}")),
                ("X-Project-ID", project.project_id.clone()),
            ],
            &body,
        )
    } else if let Some(account_token) = account_token {
        http_post_json(
            config,
            &format!("{}/api/auth/cga-relay/sync", config.control_api_base_url),
            &[("Authorization", format!("Bearer {account_token}"))],
            &body,
        )
    } else {
        Err(AgentError(
            "sync requires a developer token or CGA account login".to_string(),
        ))
    }
}

fn snapshot_json(snapshot: &Snapshot) -> String {
    format!(
        "{{\"path\":\"{}\",\"sha256\":\"{}\",\"bytes\":{},\"content\":\"{}\"}}",
        json_escape(&snapshot.path),
        json_escape(&snapshot.sha256),
        snapshot.bytes,
        json_escape(&snapshot.content)
    )
}

fn handle_mcp_session(config: &AgentConfig, input: &str) -> AgentResult<String> {
    let messages = parse_mcp_messages(input)?;
    let mut responses = Vec::new();
    for message in messages {
        if let Some(response) = handle_mcp_message(config, &message) {
            responses.push(response);
        }
    }
    Ok(if responses.is_empty() {
        String::new()
    } else {
        format!("{}\n", responses.join("\n"))
    })
}

fn parse_mcp_messages(input: &str) -> AgentResult<Vec<String>> {
    let mut messages = Vec::new();
    let mut index = 0;
    while index < input.len() {
        while index < input.len() && input.as_bytes()[index].is_ascii_whitespace() {
            index += 1;
        }
        if index >= input.len() {
            break;
        }
        if input[index..]
            .to_ascii_lowercase()
            .starts_with("content-length:")
        {
            let Some(header_end) = input[index..].find("\r\n\r\n").map(|pos| index + pos) else {
                return Err(AgentError("invalid Content-Length frame".to_string()));
            };
            let header = &input[index..header_end];
            let mut length = None;
            for line in header.lines() {
                if let Some((name, value)) = line.split_once(':') {
                    if name.eq_ignore_ascii_case("content-length") {
                        length =
                            Some(value.trim().parse::<usize>().map_err(|_| {
                                AgentError("invalid Content-Length value".to_string())
                            })?);
                    }
                }
            }
            let length = length.ok_or_else(|| AgentError("missing Content-Length".to_string()))?;
            let body_start = header_end + 4;
            let body_end = body_start + length;
            if body_end > input.len() {
                return Err(AgentError("short Content-Length frame".to_string()));
            }
            messages.push(input[body_start..body_end].to_string());
            index = body_end;
        } else {
            let line_end = input[index..]
                .find('\n')
                .map_or(input.len(), |pos| index + pos);
            let line = input[index..line_end].trim();
            if !line.is_empty() {
                messages.push(line.to_string());
            }
            index = line_end.saturating_add(1);
        }
    }
    Ok(messages)
}

fn handle_mcp_message(config: &AgentConfig, message: &str) -> Option<String> {
    let id = json_field(message, "id").unwrap_or_else(|| "null".to_string());
    let method = json_string_field(message, "method").unwrap_or_default();
    match method.as_str() {
        "initialize" => Some(format!(
            "{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{{\"tools\":{{}}}},\"serverInfo\":{{\"name\":\"{}\",\"version\":\"{}\"}}}}}}",
            id, SERVER_NAME, VERSION
        )),
        "tools/list" => Some(format!(
            "{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{{\"tools\":[{}]}}}}",
            id,
            mcp_tools_json()
        )),
        "tools/call" => Some(handle_mcp_tool_call(config, message, &id)),
        "ping" => Some(format!("{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{{\"status\":\"ok\"}}}}", id)),
        "notifications/initialized" => None,
        _ => Some(format!(
            "{{\"jsonrpc\":\"2.0\",\"id\":{},\"error\":{{\"code\":-32601,\"message\":\"method not found\"}}}}",
            id
        )),
    }
}

fn mcp_tools_json() -> String {
    [
        "health_check",
        "getstarted",
        "index_incremental",
        "index_git_incremental",
        "index_progress",
        "query_impact_graph",
        "fetch_minimal_code",
        "get_optimized_context",
    ]
    .into_iter()
    .map(|name| {
        format!(
            "{{\"name\":\"{}\",\"description\":\"CGA-Relay tool: {}\",\"inputSchema\":{{\"type\":\"object\",\"additionalProperties\":true}}}}",
            name, name
        )
    })
    .collect::<Vec<_>>()
    .join(",")
}

fn handle_mcp_tool_call(config: &AgentConfig, message: &str, id: &str) -> String {
    let Some(tool_name) = json_string_field(message, "name") else {
        return rpc_error(id, -32602, "missing tool name");
    };
    let mut arguments = json_object_field(message, "arguments").unwrap_or_else(|| "{}".to_string());
    if tool_name == "health_check" {
        return match http_get_json(config, &format!("{}/health", config.api_base_url)) {
            Ok(body) => rpc_text_result(id, &body),
            Err(_) => rpc_error(id, -32000, "health check failed"),
        };
    }
    if tool_name == "getstarted" {
        let body = format!(
            "{{\"agent_id\":\"{}\",\"server\":\"{}\",\"project_id\":\"{}\",\"project_root\":\"{}\"}}",
            json_escape(&config.agent_id),
            SERVER_NAME,
            json_escape(&config.project_id),
            json_escape(&display_path(&config.project_root))
        );
        return rpc_text_result(id, &body);
    }
    let known_tools: BTreeSet<&str> = [
        "index_incremental",
        "index_git_incremental",
        "index_progress",
        "query_impact_graph",
        "fetch_minimal_code",
        "get_optimized_context",
    ]
    .into_iter()
    .collect();
    if !known_tools.contains(tool_name.as_str()) {
        return rpc_error(id, -32602, "unknown tool");
    }
    let project_id =
        json_string_field(&arguments, "project_id").unwrap_or_else(|| config.project_id.clone());
    if !arguments.contains("\"project_id\"") {
        arguments = insert_json_field(&arguments, "project_id", &project_id);
    }
    let request_body = format!(
        "{{\"tool\":\"{}\",\"arguments\":{},\"project_id\":\"{}\"}}",
        json_escape(&tool_name),
        arguments,
        json_escape(&project_id)
    );
    let call = if let Ok(api_key) = env::var(&config.api_key_env) {
        http_post_json(
            config,
            &format!("{}/api/project/cga-relay/mcp-tool", config.api_base_url),
            &[
                ("Authorization", format!("Bearer {api_key}")),
                ("X-Project-ID", project_id),
            ],
            &request_body,
        )
    } else if let Ok(session) = read_account_session(config) {
        if let Some(access_token) = session.get("access_token") {
            http_post_json(
                config,
                &format!("{}/api/auth/cga-relay/mcp-tool", config.api_base_url),
                &[("Authorization", format!("Bearer {access_token}"))],
                &request_body,
            )
        } else {
            Err(AgentError(
                "API key env var is not set and CGA account login is missing".to_string(),
            ))
        }
    } else {
        Err(AgentError(
            "API key env var is not set and CGA account login is missing".to_string(),
        ))
    };
    match call {
        Ok(body) => rpc_text_result(id, &body),
        Err(error) => rpc_error(id, -32000, &error.0),
    }
}

fn rpc_text_result(id: &str, body: &str) -> String {
    format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{{\"content\":[{{\"type\":\"text\",\"text\":\"{}\"}}],\"data\":{}}}}}",
        id,
        json_escape(body),
        body
    )
}

fn rpc_error(id: &str, code: i32, message: &str) -> String {
    format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":{},\"error\":{{\"code\":{},\"message\":\"{}\"}}}}",
        id,
        code,
        json_escape(message)
    )
}

fn http_get_json(config: &AgentConfig, url: &str) -> AgentResult<String> {
    http_get_json_with_headers(config, url, &[])
}

fn http_get_json_with_headers(
    config: &AgentConfig,
    url: &str,
    headers: &[(&str, String)],
) -> AgentResult<String> {
    let parsed = parse_http_url(url)?;
    let mut stream = TcpStream::connect((parsed.host.as_str(), parsed.port))
        .map_err(|err| AgentError(format!("connect failed: {err}")))?;
    let crystals = crystals_headers();
    log_http_request(config, "GET", url, headers, &crystals, "");
    let mut request = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n",
        parsed.path, parsed.host
    );
    append_request_headers(&mut request, headers);
    append_request_headers(&mut request, &crystals);
    request.push_str("\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|err| AgentError(format!("request failed: {err}")))?;
    read_http_response(config, "GET", url, stream)
}

fn http_post_json(
    config: &AgentConfig,
    url: &str,
    headers: &[(&str, String)],
    body: &str,
) -> AgentResult<String> {
    let parsed = parse_http_url(url)?;
    let mut stream = TcpStream::connect((parsed.host.as_str(), parsed.port))
        .map_err(|err| AgentError(format!("connect failed: {err}")))?;
    let crystals = crystals_headers();
    log_http_request(config, "POST", url, headers, &crystals, body);
    let mut request = format!(
        "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
        parsed.path,
        parsed.host,
        body.len()
    );
    for (name, value) in headers {
        request.push_str(&format!("{}: {}\r\n", name, value));
    }
    append_request_headers(&mut request, &crystals);
    request.push_str("\r\n");
    request.push_str(body);
    stream
        .write_all(request.as_bytes())
        .map_err(|err| AgentError(format!("request failed: {err}")))?;
    read_http_response(config, "POST", url, stream)
}

fn log_http_request(
    config: &AgentConfig,
    method: &str,
    url: &str,
    headers: &[(&str, String)],
    crystals: &[(String, String)],
    body: &str,
) {
    let mut detail = format!(
        "method={method}\nurl={}\nheaders:\n",
        redact_sensitive_text(url)
    );
    for (name, value) in headers {
        detail.push_str(&format!("{name}: {value}\n"));
    }
    for (name, value) in crystals {
        detail.push_str(&format!("{name}: {value}\n"));
    }
    if !body.is_empty() {
        detail.push_str("body:\n");
        detail.push_str(body);
    }
    log_communication(config, "http.request", &detail);
}

fn crystals_headers() -> [(String, String); 4] {
    [
        (
            "X-CGA-Communication-Profile".to_string(),
            CRYSTALS_PROFILE.to_string(),
        ),
        (
            "X-CGA-Key-Establishment".to_string(),
            CRYSTALS_KEM.to_string(),
        ),
        (
            "X-CGA-Signature".to_string(),
            CRYSTALS_SIGNATURE.to_string(),
        ),
        (
            "X-CGA-Transport-Scope".to_string(),
            CRYSTALS_TRANSPORT_SCOPE.to_string(),
        ),
    ]
}

fn append_request_headers(request: &mut String, headers: &[(impl AsRef<str>, impl AsRef<str>)]) {
    for (name, value) in headers {
        request.push_str(&format!("{}: {}\r\n", name.as_ref(), value.as_ref()));
    }
}

struct ParsedUrl {
    host: String,
    port: u16,
    path: String,
}

fn parse_http_url(url: &str) -> AgentResult<ParsedUrl> {
    let Some(rest) = url.strip_prefix("http://") else {
        return Err(AgentError(
            "only http:// URLs are supported by CGA-Relay".to_string(),
        ));
    };
    let (authority, path) = match rest.split_once('/') {
        Some((authority, path)) => (authority, format!("/{path}")),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (
            host.to_string(),
            port.parse::<u16>()
                .map_err(|_| AgentError("invalid URL port".to_string()))?,
        ),
        None => (authority.to_string(), 80),
    };
    if !is_loopback_host(&host) {
        return Err(AgentError(
            "CRYSTALS/CNSA 2.0 policy allows CGA-Relay plaintext HTTP only on loopback; use a PQC-capable TLS endpoint or local proxy for remote CGA URLs".to_string(),
        ));
    }
    Ok(ParsedUrl { host, port, path })
}

fn is_loopback_host(host: &str) -> bool {
    matches!(host, "localhost" | "127.0.0.1" | "::1" | "[::1]")
}

fn read_http_response(
    config: &AgentConfig,
    method: &str,
    url: &str,
    mut stream: TcpStream,
) -> AgentResult<String> {
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|err| AgentError(format!("response failed: {err}")))?;
    let text = String::from_utf8_lossy(&response);
    let Some((head, body)) = text.split_once("\r\n\r\n") else {
        log_communication(
            config,
            "http.response",
            &format!("method={method}\nurl={url}\ninvalid_response:\n{text}"),
        );
        return Err(AgentError("invalid HTTP response".to_string()));
    };
    let status = head.lines().next().unwrap_or("HTTP response");
    log_communication(
        config,
        "http.response",
        &format!("method={method}\nurl={url}\nstatus={status}\nheaders:\n{head}\nbody:\n{body}"),
    );
    if !head.starts_with("HTTP/1.1 2") && !head.starts_with("HTTP/1.0 2") {
        return Err(AgentError("HTTP request failed".to_string()));
    }
    Ok(body.to_string())
}

fn json_string_field(text: &str, field: &str) -> Option<String> {
    let marker = format!("\"{}\":", field);
    let start = text.find(&marker)? + marker.len();
    let tail = text[start..].trim_start();
    if !tail.starts_with('"') {
        return None;
    }
    let mut escaped = false;
    let mut out = String::new();
    for ch in tail[1..].chars() {
        if escaped {
            out.push(match ch {
                'n' => '\n',
                'r' => '\r',
                't' => '\t',
                '"' => '"',
                '\\' => '\\',
                other => other,
            });
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '"' {
            return Some(out);
        }
        out.push(ch);
    }
    None
}

fn json_field(text: &str, field: &str) -> Option<String> {
    let marker = format!("\"{}\":", field);
    let start = text.find(&marker)? + marker.len();
    let tail = text[start..].trim_start();
    let mut end = 0;
    for (index, ch) in tail.char_indices() {
        if ch == ',' || ch == '}' {
            break;
        }
        end = index + ch.len_utf8();
    }
    Some(tail[..end].trim().to_string())
}

fn json_object_field(text: &str, field: &str) -> Option<String> {
    let marker = format!("\"{}\":", field);
    let start = text.find(&marker)? + marker.len();
    let tail = text[start..].trim_start();
    if !tail.starts_with('{') {
        return None;
    }
    let mut depth = 0_i32;
    let mut in_string = false;
    let mut escaped = false;
    for (index, ch) in tail.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if in_string {
            if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
            }
            continue;
        }
        match ch {
            '"' => in_string = true,
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(tail[..=index].to_string());
                }
            }
            _ => {}
        }
    }
    None
}

fn json_array_field(text: &str, field: &str) -> Option<String> {
    let marker = format!("\"{}\":", field);
    let start = text.find(&marker)? + marker.len();
    let tail = text[start..].trim_start();
    if !tail.starts_with('[') {
        return None;
    }
    let mut depth = 0_i32;
    let mut in_string = false;
    let mut escaped = false;
    for (index, ch) in tail.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if in_string {
            if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
            }
            continue;
        }
        match ch {
            '"' => in_string = true,
            '[' => depth += 1,
            ']' => {
                depth -= 1;
                if depth == 0 {
                    return Some(tail[..=index].to_string());
                }
            }
            _ => {}
        }
    }
    None
}

fn insert_json_field(object: &str, key: &str, value: &str) -> String {
    let trimmed = object.trim();
    if trimmed == "{}" {
        return format!("{{\"{}\":\"{}\"}}", json_escape(key), json_escape(value));
    }
    let without_end = trimmed.trim_end_matches('}');
    format!(
        "{},\"{}\":\"{}\"}}",
        without_end,
        json_escape(key),
        json_escape(value)
    )
}

fn parse_flat_json_object(text: &str) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    for key in ["account_email", "account_token_env"] {
        if let Some(value) = json_string_field(text, key) {
            values.insert(key.to_string(), value);
        }
    }
    values
}

fn json_escape(value: &str) -> String {
    let mut out = String::new();
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ch if ch.is_control() => out.push(' '),
            ch => out.push(ch),
        }
    }
    out
}

fn string_array_json(values: &[String]) -> String {
    format!(
        "[{}]",
        values
            .iter()
            .map(|value| format!("\"{}\"", json_escape(value)))
            .collect::<Vec<_>>()
            .join(",")
    )
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().to_string()
}

fn escape_field(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('\t', "\\t")
        .replace('\n', "\\n")
}

fn unescape_field(value: &str) -> String {
    let mut out = String::new();
    let mut chars = value.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            match chars.next() {
                Some('t') => out.push('\t'),
                Some('n') => out.push('\n'),
                Some('\\') => out.push('\\'),
                Some(other) => out.push(other),
                None => out.push(ch),
            }
        } else {
            out.push(ch);
        }
    }
    out
}

#[cfg(windows)]
mod windows_tray {
    use super::{
        tray_icon_resource_id, tray_login_menu_label, tray_login_status, tray_tooltip,
        tray_user_group_summary, AgentConfig, AgentError, AgentResult, TrayLoginStatus,
        PROJECT_AUTHOR, PROJECT_DISPLAY_NAME, PROJECT_LICENSE, PROJECT_REPOSITORY, PROJECT_SUPPORT,
        SERVER_NAME, TRAY_ICON_LOGGED_IN_RESOURCE_ID, VERSION,
    };
    use std::ffi::c_void;
    use std::mem::{size_of, zeroed};
    use std::ptr::{null, null_mut};

    type Bool = i32;
    type Dword = u32;
    type Hbrush = isize;
    type Hcursor = isize;
    type Hicon = isize;
    type Hinstance = isize;
    type Hmenu = isize;
    type Hwnd = isize;
    type Lparam = isize;
    type Lresult = isize;
    type Uint = u32;
    type Wparam = usize;
    type Wndproc = Option<unsafe extern "system" fn(Hwnd, Uint, Wparam, Lparam) -> Lresult>;

    const ID_TRAY_ICON: Uint = 1;
    const ID_LOGIN_REFRESH_TIMER: usize = 1;
    const IDI_APPLICATION: usize = 32512;
    const GWLP_USERDATA: i32 = -21;
    const ID_MENU_SETTINGS: usize = 1001;
    const ID_MENU_ABOUT: usize = 1002;
    const ID_MENU_LOGS: usize = 1003;
    const ID_MENU_EXIT: usize = 1004;
    const MF_DISABLED: Uint = 0x00000002;
    const MF_GRAYED: Uint = 0x00000001;
    const MF_SEPARATOR: Uint = 0x00000800;
    const MF_STRING: Uint = 0x00000000;
    const MB_ICONINFORMATION: Uint = 0x00000040;
    const MB_OK: Uint = 0x00000000;
    const NIF_ICON: Uint = 0x00000002;
    const NIF_MESSAGE: Uint = 0x00000001;
    const NIF_TIP: Uint = 0x00000004;
    const NIM_ADD: Dword = 0x00000000;
    const NIM_DELETE: Dword = 0x00000002;
    const NIM_MODIFY: Dword = 0x00000001;
    const NIM_SETVERSION: Dword = 0x00000004;
    const NOTIFYICON_VERSION_4: Uint = 4;
    const SW_SHOWNORMAL: i32 = 1;
    const TPM_NONOTIFY: Uint = 0x00000080;
    const TPM_RETURNCMD: Uint = 0x00000100;
    const TPM_RIGHTBUTTON: Uint = 0x00000002;
    const TRAY_CALLBACK_MESSAGE: Uint = WM_USER + 1;
    const WM_CONTEXTMENU: Uint = 0x007B;
    const WM_DESTROY: Uint = 0x0002;
    const WM_LBUTTONUP: Uint = 0x0202;
    const WM_NULL: Uint = 0x0000;
    const WM_RBUTTONUP: Uint = 0x0205;
    const WM_TIMER: Uint = 0x0113;
    const WM_USER: Uint = 0x0400;

    #[repr(C)]
    struct Point {
        x: i32,
        y: i32,
    }

    #[repr(C)]
    struct Msg {
        hwnd: Hwnd,
        message: Uint,
        w_param: Wparam,
        l_param: Lparam,
        time: Dword,
        pt: Point,
    }

    #[repr(C)]
    struct WndClassW {
        style: Uint,
        lpfn_wnd_proc: Wndproc,
        cb_cls_extra: i32,
        cb_wnd_extra: i32,
        h_instance: Hinstance,
        h_icon: Hicon,
        h_cursor: Hcursor,
        hbr_background: Hbrush,
        lpsz_menu_name: *const u16,
        lpsz_class_name: *const u16,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Guid {
        data1: u32,
        data2: u16,
        data3: u16,
        data4: [u8; 8],
    }

    #[repr(C)]
    struct NotifyIconDataW {
        cb_size: Dword,
        hwnd: Hwnd,
        uid: Uint,
        u_flags: Uint,
        u_callback_message: Uint,
        h_icon: Hicon,
        sz_tip: [u16; 128],
        dw_state: Dword,
        dw_state_mask: Dword,
        sz_info: [u16; 256],
        u_version_or_timeout: Uint,
        sz_info_title: [u16; 64],
        dw_info_flags: Dword,
        guid_item: Guid,
        h_balloon_icon: Hicon,
    }

    struct TrayState {
        config: AgentConfig,
        login: TrayLoginStatus,
        account_label: Vec<u16>,
        settings_url: Vec<u16>,
        log_dir: Vec<u16>,
        status_text: Vec<u16>,
        about_text: Vec<u16>,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn FreeConsole() -> Bool;
        fn GetModuleHandleW(lp_module_name: *const u16) -> Hinstance;
    }

    #[link(name = "shell32")]
    extern "system" {
        fn Shell_NotifyIconW(dw_message: Dword, lp_data: *mut NotifyIconDataW) -> Bool;
        fn ShellExecuteW(
            hwnd: Hwnd,
            lp_operation: *const u16,
            lp_file: *const u16,
            lp_parameters: *const u16,
            lp_directory: *const u16,
            n_show_cmd: i32,
        ) -> isize;
    }

    #[link(name = "user32")]
    extern "system" {
        fn AppendMenuW(
            hmenu: Hmenu,
            u_flags: Uint,
            u_id_new_item: usize,
            lp_new_item: *const u16,
        ) -> Bool;
        fn CreateWindowExW(
            dw_ex_style: Dword,
            lp_class_name: *const u16,
            lp_window_name: *const u16,
            dw_style: Dword,
            x: i32,
            y: i32,
            n_width: i32,
            n_height: i32,
            hwnd_parent: Hwnd,
            hmenu: Hmenu,
            hinstance: Hinstance,
            lp_param: *mut c_void,
        ) -> Hwnd;
        fn CreatePopupMenu() -> Hmenu;
        fn DefWindowProcW(hwnd: Hwnd, msg: Uint, w_param: Wparam, l_param: Lparam) -> Lresult;
        fn DestroyMenu(hmenu: Hmenu) -> Bool;
        fn DestroyWindow(hwnd: Hwnd) -> Bool;
        fn DispatchMessageW(lp_msg: *const Msg) -> Lresult;
        fn GetCursorPos(lp_point: *mut Point) -> Bool;
        fn GetMessageW(
            lp_msg: *mut Msg,
            hwnd: Hwnd,
            msg_filter_min: Uint,
            msg_filter_max: Uint,
        ) -> Bool;
        fn GetWindowLongPtrW(hwnd: Hwnd, index: i32) -> isize;
        fn KillTimer(hwnd: Hwnd, id_event: usize) -> Bool;
        fn LoadIconW(h_instance: Hinstance, lp_icon_name: *const u16) -> Hicon;
        fn MessageBoxW(
            hwnd: Hwnd,
            lp_text: *const u16,
            lp_caption: *const u16,
            u_type: Uint,
        ) -> i32;
        fn PostMessageW(hwnd: Hwnd, msg: Uint, w_param: Wparam, l_param: Lparam) -> Bool;
        fn PostQuitMessage(exit_code: i32);
        fn RegisterClassW(lp_wnd_class: *const WndClassW) -> u16;
        fn SetForegroundWindow(hwnd: Hwnd) -> Bool;
        fn SetTimer(
            hwnd: Hwnd,
            n_id_event: usize,
            u_elapse: Uint,
            lp_timer_func: *const c_void,
        ) -> usize;
        fn SetWindowLongPtrW(hwnd: Hwnd, index: i32, value: isize) -> isize;
        fn TrackPopupMenu(
            hmenu: Hmenu,
            u_flags: Uint,
            x: i32,
            y: i32,
            n_reserved: i32,
            hwnd: Hwnd,
            prc_rect: *const c_void,
        ) -> i32;
        fn TranslateMessage(lp_msg: *const Msg) -> Bool;
    }

    pub fn run(config: &AgentConfig, settings_url: &str) -> AgentResult<()> {
        let class_name = wide_null("CgaRelayTrayWindow");
        let title = wide_null(SERVER_NAME);
        let login = tray_login_status(config);
        let logged_in = login.logged_in;
        let tooltip = tray_tooltip(config, &login);

        unsafe {
            let h_instance = GetModuleHandleW(null());
            let app_icon = load_app_icon(h_instance);
            let wnd_class = WndClassW {
                style: 0,
                lpfn_wnd_proc: Some(window_proc),
                cb_cls_extra: 0,
                cb_wnd_extra: 0,
                h_instance,
                h_icon: app_icon,
                h_cursor: 0,
                hbr_background: 0,
                lpsz_menu_name: null(),
                lpsz_class_name: class_name.as_ptr(),
            };
            if RegisterClassW(&wnd_class) == 0 {
                return Err(AgentError(
                    "failed to register tray window class".to_string(),
                ));
            }

            let hwnd = CreateWindowExW(
                0,
                class_name.as_ptr(),
                title.as_ptr(),
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                h_instance,
                null_mut(),
            );
            if hwnd == 0 {
                return Err(AgentError("failed to create tray window".to_string()));
            }

            let state_ptr = Box::into_raw(Box::new(tray_state(config, settings_url, login)));
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, state_ptr as isize);
            if let Err(error) = add_icon(hwnd, h_instance, &tooltip, logged_in) {
                SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
                let _ = Box::from_raw(state_ptr);
                DestroyWindow(hwnd);
                return Err(error);
            }
            SetTimer(hwnd, ID_LOGIN_REFRESH_TIMER, 3000, null());
            FreeConsole();
            let mut msg: Msg = zeroed();
            while GetMessageW(&mut msg, 0, 0, 0) > 0 {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            KillTimer(hwnd, ID_LOGIN_REFRESH_TIMER);
            delete_icon(hwnd);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            let _ = Box::from_raw(state_ptr);
        }

        Ok(())
    }

    fn tray_state(config: &AgentConfig, settings_url: &str, login: TrayLoginStatus) -> TrayState {
        let status_text = tray_status_text(config, &login);
        let about_text = tray_about_text(config, &login);
        let account_label = escape_menu_label(&tray_login_menu_label(&login));
        TrayState {
            config: config.clone(),
            login,
            account_label: wide_null(&account_label),
            settings_url: wide_null(settings_url),
            log_dir: wide_null(&config.log_dir.to_string_lossy()),
            status_text: wide_null(&status_text),
            about_text: wide_null(&about_text),
        }
    }

    fn tray_status_text(config: &AgentConfig, login: &TrayLoginStatus) -> String {
        let account = if login.logged_in {
            format!("Signed in as {}", login.username)
        } else {
            "Not signed in".to_string()
        };
        let user_groups = tray_user_group_summary(config, login);
        format!(
            "CGA-Relay is running.\nRelay: {}\nUser Groups: {}\nAccount: {}\nRight-click for Settings, Logs, About, or Exit.",
            config.agent_id, user_groups, account
        )
    }

    fn tray_about_text(config: &AgentConfig, login: &TrayLoginStatus) -> String {
        let account = if login.logged_in {
            format!("Signed in as {}", login.username)
        } else {
            "Not signed in".to_string()
        };
        let user_groups = tray_user_group_summary(config, login);
        format!(
            "{PROJECT_DISPLAY_NAME}\nCommand: {SERVER_NAME}\nVersion: {VERSION}\n\nAuthor: {PROJECT_AUTHOR}\nRepository: {PROJECT_REPOSITORY}\nSupport: {PROJECT_SUPPORT}\nLicense: {PROJECT_LICENSE}\n\nRelay: {}\nUser Groups: {}\nAccount: {}",
            config.agent_id, user_groups, account
        )
    }

    unsafe extern "system" fn window_proc(
        hwnd: Hwnd,
        msg: Uint,
        w_param: Wparam,
        l_param: Lparam,
    ) -> Lresult {
        match msg {
            TRAY_CALLBACK_MESSAGE => match tray_event(l_param) {
                WM_LBUTTONUP => {
                    let caption = wide_null("CGA-Relay");
                    let fallback = wide_null("CGA-Relay is running. Right-click for menu.");
                    let text = tray_state_from_hwnd(hwnd)
                        .map(|state| state.status_text.as_ptr())
                        .unwrap_or(fallback.as_ptr());
                    MessageBoxW(hwnd, text, caption.as_ptr(), MB_OK | MB_ICONINFORMATION);
                    0
                }
                WM_RBUTTONUP => {
                    show_context_menu(hwnd);
                    0
                }
                WM_CONTEXTMENU => {
                    show_context_menu(hwnd);
                    0
                }
                _ => DefWindowProcW(hwnd, msg, w_param, l_param),
            },
            WM_DESTROY => {
                KillTimer(hwnd, ID_LOGIN_REFRESH_TIMER);
                PostQuitMessage(0);
                0
            }
            WM_TIMER => {
                if w_param == ID_LOGIN_REFRESH_TIMER {
                    refresh_login_state(hwnd);
                    0
                } else {
                    DefWindowProcW(hwnd, msg, w_param, l_param)
                }
            }
            _ => DefWindowProcW(hwnd, msg, w_param, l_param),
        }
    }

    unsafe fn show_context_menu(hwnd: Hwnd) {
        refresh_login_state(hwnd);
        let menu = CreatePopupMenu();
        if menu == 0 {
            return;
        }
        if let Some(state) = tray_state_from_hwnd(hwnd) {
            AppendMenuW(
                menu,
                MF_STRING | MF_DISABLED | MF_GRAYED,
                0,
                state.account_label.as_ptr(),
            );
            AppendMenuW(menu, MF_SEPARATOR, 0, null());
        }
        append_menu_item(menu, ID_MENU_SETTINGS, "Settings");
        append_menu_item(menu, ID_MENU_LOGS, "Logs");
        append_menu_item(menu, ID_MENU_ABOUT, "About");
        append_menu_item(menu, ID_MENU_EXIT, "Exit");

        let mut point = Point { x: 0, y: 0 };
        GetCursorPos(&mut point);
        SetForegroundWindow(hwnd);
        let command = TrackPopupMenu(
            menu,
            TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            point.x,
            point.y,
            0,
            hwnd,
            null(),
        );
        DestroyMenu(menu);
        PostMessageW(hwnd, WM_NULL, 0, 0);
        if command > 0 {
            handle_menu_command(hwnd, command as usize);
        }
    }

    fn tray_event(l_param: Lparam) -> Uint {
        let value = l_param as usize;
        let low_word = (value & 0xffff) as Uint;
        if low_word != 0 {
            low_word
        } else {
            value as Uint
        }
    }

    unsafe fn refresh_login_state(hwnd: Hwnd) {
        let h_instance = GetModuleHandleW(null());
        let Some(state) = tray_state_from_hwnd_mut(hwnd) else {
            return;
        };
        let login = tray_login_status(&state.config);
        if login == state.login {
            return;
        }
        state.login = login;
        state.account_label = wide_null(&escape_menu_label(&tray_login_menu_label(&state.login)));
        state.status_text = wide_null(&tray_status_text(&state.config, &state.login));
        state.about_text = wide_null(&tray_about_text(&state.config, &state.login));
        let tooltip = tray_tooltip(&state.config, &state.login);
        modify_icon(hwnd, h_instance, &tooltip, state.login.logged_in);
    }

    unsafe fn append_menu_item(menu: Hmenu, id: usize, label: &str) {
        let label = wide_null(&escape_menu_label(label));
        AppendMenuW(menu, MF_STRING, id, label.as_ptr());
    }

    fn escape_menu_label(label: &str) -> String {
        label.replace('&', "&&")
    }

    unsafe fn handle_menu_command(hwnd: Hwnd, command: usize) {
        match command {
            ID_MENU_SETTINGS => {
                if let Some(state) = tray_state_from_hwnd(hwnd) {
                    open_shell_target(hwnd, state.settings_url.as_ptr());
                }
            }
            ID_MENU_ABOUT => {
                if let Some(state) = tray_state_from_hwnd(hwnd) {
                    let caption = wide_null("About CGA-Relay");
                    MessageBoxW(
                        hwnd,
                        state.about_text.as_ptr(),
                        caption.as_ptr(),
                        MB_OK | MB_ICONINFORMATION,
                    );
                }
            }
            ID_MENU_LOGS => {
                if let Some(state) = tray_state_from_hwnd(hwnd) {
                    open_shell_target(hwnd, state.log_dir.as_ptr());
                }
            }
            ID_MENU_EXIT => {
                delete_icon(hwnd);
                DestroyWindow(hwnd);
            }
            _ => {}
        }
    }

    unsafe fn open_shell_target(hwnd: Hwnd, target: *const u16) {
        let operation = wide_null("open");
        let result = ShellExecuteW(
            hwnd,
            operation.as_ptr(),
            target,
            null(),
            null(),
            SW_SHOWNORMAL,
        );
        if result <= 32 {
            let text = wide_null("Windows could not open the selected tray menu target.");
            let caption = wide_null("CGA-Relay");
            MessageBoxW(
                hwnd,
                text.as_ptr(),
                caption.as_ptr(),
                MB_OK | MB_ICONINFORMATION,
            );
        }
    }

    unsafe fn tray_state_from_hwnd<'a>(hwnd: Hwnd) -> Option<&'a TrayState> {
        let ptr = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *const TrayState;
        ptr.as_ref()
    }

    unsafe fn tray_state_from_hwnd_mut<'a>(hwnd: Hwnd) -> Option<&'a mut TrayState> {
        let ptr = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut TrayState;
        ptr.as_mut()
    }

    pub fn embedded_icon_available(resource_id: u16) -> bool {
        unsafe {
            let h_instance = GetModuleHandleW(null());
            LoadIconW(h_instance, make_int_resource(resource_id)) != 0
        }
    }

    unsafe fn add_icon(
        hwnd: Hwnd,
        h_instance: Hinstance,
        tooltip: &str,
        logged_in: bool,
    ) -> AgentResult<()> {
        let mut data = notify_icon_data(hwnd);
        data.u_flags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
        data.u_callback_message = TRAY_CALLBACK_MESSAGE;
        data.h_icon = load_tray_icon(h_instance, logged_in);
        fill_wide_buffer(&mut data.sz_tip, tooltip);
        if Shell_NotifyIconW(NIM_ADD, &mut data) == 0 {
            return Err(AgentError("failed to add tray icon".to_string()));
        }
        data.u_version_or_timeout = NOTIFYICON_VERSION_4;
        Shell_NotifyIconW(NIM_SETVERSION, &mut data);
        Ok(())
    }

    unsafe fn modify_icon(hwnd: Hwnd, h_instance: Hinstance, tooltip: &str, logged_in: bool) {
        let mut data = notify_icon_data(hwnd);
        data.u_flags = NIF_ICON | NIF_TIP;
        data.h_icon = load_tray_icon(h_instance, logged_in);
        fill_wide_buffer(&mut data.sz_tip, tooltip);
        Shell_NotifyIconW(NIM_MODIFY, &mut data);
    }

    unsafe fn delete_icon(hwnd: Hwnd) {
        let mut data = notify_icon_data(hwnd);
        Shell_NotifyIconW(NIM_DELETE, &mut data);
    }

    unsafe fn load_app_icon(h_instance: Hinstance) -> Hicon {
        let icon = LoadIconW(
            h_instance,
            make_int_resource(TRAY_ICON_LOGGED_IN_RESOURCE_ID),
        );
        if icon != 0 {
            icon
        } else {
            LoadIconW(0, IDI_APPLICATION as *const u16)
        }
    }

    unsafe fn load_tray_icon(h_instance: Hinstance, logged_in: bool) -> Hicon {
        let resource_id = tray_icon_resource_id(&TrayLoginStatus {
            logged_in,
            username: String::new(),
        });
        let icon = LoadIconW(h_instance, make_int_resource(resource_id));
        if icon != 0 {
            icon
        } else {
            load_app_icon(h_instance)
        }
    }

    fn make_int_resource(id: u16) -> *const u16 {
        id as usize as *const u16
    }

    fn notify_icon_data(hwnd: Hwnd) -> NotifyIconDataW {
        NotifyIconDataW {
            cb_size: size_of::<NotifyIconDataW>() as Dword,
            hwnd,
            uid: ID_TRAY_ICON,
            u_flags: 0,
            u_callback_message: 0,
            h_icon: 0,
            sz_tip: [0; 128],
            dw_state: 0,
            dw_state_mask: 0,
            sz_info: [0; 256],
            u_version_or_timeout: 0,
            sz_info_title: [0; 64],
            dw_info_flags: 0,
            guid_item: Guid {
                data1: 0,
                data2: 0,
                data3: 0,
                data4: [0; 8],
            },
            h_balloon_icon: 0,
        }
    }

    fn fill_wide_buffer(buffer: &mut [u16], value: &str) {
        let mut encoded = value.encode_utf16().take(buffer.len().saturating_sub(1));
        for slot in buffer.iter_mut() {
            *slot = encoded.next().unwrap_or(0);
        }
    }

    fn wide_null(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(std::iter::once(0)).collect()
    }
}

fn sha256_hex(data: &[u8]) -> String {
    let digest = sha256(data);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn sha256(data: &[u8]) -> [u8; 32] {
    const H0: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h = H0;
    let bit_len = (data.len() as u64) * 8;
    let mut padded = data.to_vec();
    padded.push(0x80);
    while (padded.len() % 64) != 56 {
        padded.push(0);
    }
    padded.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in padded.chunks(64) {
        let mut w = [0_u32; 64];
        for (index, word) in w.iter_mut().take(16).enumerate() {
            let start = index * 4;
            *word = u32::from_be_bytes([
                chunk[start],
                chunk[start + 1],
                chunk[start + 2],
                chunk[start + 3],
            ]);
        }
        for index in 16..64 {
            let s0 = w[index - 15].rotate_right(7)
                ^ w[index - 15].rotate_right(18)
                ^ (w[index - 15] >> 3);
            let s1 = w[index - 2].rotate_right(17)
                ^ w[index - 2].rotate_right(19)
                ^ (w[index - 2] >> 10);
            w[index] = w[index - 16]
                .wrapping_add(s0)
                .wrapping_add(w[index - 7])
                .wrapping_add(s1);
        }
        let mut a = h[0];
        let mut b = h[1];
        let mut c = h[2];
        let mut d = h[3];
        let mut e = h[4];
        let mut f = h[5];
        let mut g = h[6];
        let mut hh = h[7];
        for index in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[index])
                .wrapping_add(w[index]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    let mut out = [0_u8; 32];
    for (index, word) in h.iter().enumerate() {
        out[index * 4..index * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}
