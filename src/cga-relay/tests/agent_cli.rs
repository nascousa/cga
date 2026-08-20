use std::ffi::OsStr;
use std::fs;
use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::ops::{Deref, DerefMut};
use std::path::{Path, PathBuf};
use std::process::{Command as ProcessCommand, Output};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const TEST_SECRET: &str = "TEST_SECRET_VALUE_SHOULD_NEVER_LEAK";
const RESPONSE_HEADER_MARKER: &str = "PRIVATE_RESPONSE_HEADER_MARKER";

struct Command(ProcessCommand);

impl Command {
    fn new(program: impl AsRef<OsStr>) -> Self {
        let mut command = ProcessCommand::new(program);
        let current_thread = thread::current();
        let scope = current_thread
            .name()
            .map(str::to_owned)
            .unwrap_or_else(|| format!("{:?}", current_thread.id()));
        command.env(
            "CGA_RELAY_TEST_INSTANCE_SCOPE",
            format!("{}:{scope}", std::process::id()),
        );
        Self(command)
    }
}

impl Deref for Command {
    type Target = ProcessCommand;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl DerefMut for Command {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}

struct TestDir {
    path: PathBuf,
}

impl TestDir {
    fn new(name: &str) -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time should be valid")
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("cga-relay-{name}-{}-{unique}", std::process::id()));
        fs::create_dir_all(&path).expect("temp dir should be created");
        Self { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn agent_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_cga-relay"))
}

fn run_agent(args: &[&str]) -> Output {
    Command::new(agent_bin())
        .args(args)
        .output()
        .expect("agent command should run")
}

fn write_safe_config(base: &Path, project_root: &Path, extra: &[(&str, String)]) -> PathBuf {
    let state_dir = base.join("state");
    let log_dir = base.join("logs");
    let mut values = vec![
        ("AGENT_ID", "dev-agent-01".to_string()),
        ("API_BASE_URL", "http://127.0.0.1:18001".to_string()),
        ("CONTROL_API_BASE_URL", "http://127.0.0.1:18001".to_string()),
        ("API_KEY_ENV", "CGA_TEST_API_KEY".to_string()),
        ("ACCOUNT_EMAIL", "".to_string()),
        ("ACCOUNT_TOKEN_ENV", "CGA_TEST_DEVELOPER_TOKEN".to_string()),
        ("PROJECT_ID", "PROJECT123".to_string()),
        ("PROJECT_ROOT", project_root.display().to_string()),
        ("STATE_DIR", state_dir.display().to_string()),
        ("LOG_DIR", log_dir.display().to_string()),
        ("INCLUDE_GLOBS", "".to_string()),
        ("EXCLUDE_GLOBS", ".git/**,node_modules/**,.venv/**,__pycache__/**,dist/**,build/**,target/**,<agent-state>/**".to_string()),
        ("MAX_FILE_BYTES", "64".to_string()),
    ];
    for (key, value) in extra {
        if let Some((_, existing)) = values.iter_mut().find(|(item_key, _)| item_key == key) {
            *existing = value.clone();
        } else {
            values.push((key, value.clone()));
        }
    }
    let config = base.join("agent.env");
    let body = values
        .into_iter()
        .map(|(key, value)| format!("{key}={value}"))
        .collect::<Vec<_>>()
        .join("\n");
    fs::write(&config, format!("{body}\n")).expect("config should be written");
    config
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn read_log_files(base: &Path) -> (Vec<String>, String) {
    let mut names = Vec::new();
    let mut combined = String::new();
    let log_dir = base.join("logs");
    for entry in fs::read_dir(log_dir).expect("log directory should exist") {
        let entry = entry.expect("log entry should be readable");
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("log") {
            continue;
        }
        names.push(path.file_name().unwrap().to_string_lossy().into_owned());
        combined.push_str(&fs::read_to_string(path).expect("log file should be readable"));
    }
    names.sort();
    (names, combined)
}

fn read_http_request(stream: &mut TcpStream) -> String {
    let mut bytes = Vec::new();
    let mut expected_len = None;
    loop {
        let mut buffer = [0_u8; 8192];
        let read = std::io::Read::read(stream, &mut buffer).expect("request should be readable");
        if read == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..read]);
        if expected_len.is_none() {
            if let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                let header_len = header_end + 4;
                let headers = String::from_utf8_lossy(&bytes[..header_len]);
                let content_len = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                expected_len = Some(header_len + content_len);
            }
        }
        if expected_len.is_some_and(|length| bytes.len() >= length) {
            break;
        }
    }
    String::from_utf8(bytes).expect("request should be UTF-8")
}

fn request_body(request: &str) -> &str {
    request
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .unwrap_or("")
}

fn snapshot_count(request: &str) -> usize {
    request_body(request).matches("\"path\":").count()
}

fn write_http_response(stream: &mut TcpStream, status: u16) {
    let (reason, body) = match status {
        202 => ("Accepted", "{\"accepted\":true}"),
        401 => ("Unauthorized", "{\"detail\":\"invalid account session\"}"),
        403 => ("Forbidden", "{\"detail\":\"project mismatch\"}"),
        413 => ("Payload Too Large", "{\"detail\":\"batch limit exceeded\"}"),
        _ => ("Internal Server Error", "{\"detail\":\"test failure\"}"),
    };
    let response = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nX-Test-Marker: {RESPONSE_HEADER_MARKER}\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(response.as_bytes())
        .expect("response should be writable");
}

fn spawn_health_server() -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let request = read_http_request(&mut stream);
        assert!(
            request.starts_with("GET /health HTTP/1.1"),
            "request: {request}"
        );
        let body = "{\"ok\":true}";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(response.as_bytes()).unwrap();
    });
    (format!("http://127.0.0.1:{port}"), server)
}

fn spawn_checkpoint_server() -> (u16, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        let mut first = listener.accept().unwrap().0;
        let first_request = read_http_request(&mut first);
        let first_count = snapshot_count(&first_request);
        requests.push(first_request);
        if first_count > 500 {
            write_http_response(&mut first, 413);
            return requests;
        }
        write_http_response(&mut first, 202);
        drop(first);

        let mut second = listener.accept().unwrap().0;
        requests.push(read_http_request(&mut second));
        write_http_response(&mut second, 500);
        requests
    });
    (port, server)
}

fn spawn_accepting_sync_server(
    max_body_bytes: usize,
    expected_snapshots: usize,
) -> (u16, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        let mut received_snapshots = 0;
        loop {
            let mut stream = listener.accept().unwrap().0;
            let request = read_http_request(&mut stream);
            let body_len = request_body(&request).len();
            let count = snapshot_count(&request);
            requests.push(request);
            if body_len > max_body_bytes || count > 500 {
                write_http_response(&mut stream, 413);
                break;
            }
            received_snapshots += count;
            write_http_response(&mut stream, 202);
            if received_snapshots >= expected_snapshots {
                break;
            }
        }
        requests
    });
    (port, server)
}

fn spawn_account_rejection_then_project_acceptance_server(
    expected_project_requests: usize,
) -> (u16, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        let mut account_stream = listener.accept().unwrap().0;
        requests.push(read_http_request(&mut account_stream));
        write_http_response(&mut account_stream, 401);
        drop(account_stream);

        for _ in 0..expected_project_requests {
            let mut project_stream = listener.accept().unwrap().0;
            requests.push(read_http_request(&mut project_stream));
            write_http_response(&mut project_stream, 202);
        }
        requests
    });
    (port, server)
}

fn spawn_sync_status_server(statuses: Vec<u16>) -> (u16, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        for status in statuses {
            let mut stream = listener.accept().unwrap().0;
            requests.push(read_http_request(&mut stream));
            write_http_response(&mut stream, status);
        }
        requests
    });
    (port, server)
}

fn spawn_malformed_sync_server(response_marker: &'static str) -> (u16, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let _ = read_http_request(&mut stream);
        stream
            .write_all(response_marker.as_bytes())
            .expect("malformed response should be writable");
    });
    (port, server)
}

fn spawn_oversized_sync_response_server() -> (u16, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let _ = read_http_request(&mut stream);
        let body_bytes = 8 * 1024 * 1024;
        let header = format!(
            "HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\nContent-Length: {body_bytes}\r\n\r\n"
        );
        if stream.write_all(header.as_bytes()).is_err() {
            return;
        }
        let chunk = [b'x'; 8192];
        for _ in 0..(body_bytes / chunk.len()) {
            if stream.write_all(&chunk).is_err() {
                break;
            }
        }
    });
    (port, server)
}

#[test]
fn help_output_lists_required_commands() {
    let output = run_agent(&["--help"]);
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let text = stdout(&output);
    for command in [
        "doctor", "login", "projects", "scan", "sync", "index", "refs", "settings", "tray", "mcp",
    ] {
        assert!(text.contains(command), "help missing {command}: {text}");
    }
}

fn assert_cli_tool_request(
    expected_tool: &'static str,
    expected_fragments: &'static [&'static str],
) -> (u16, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let mut buffer = [0_u8; 8192];
        let read = stream.read(&mut buffer).unwrap();
        let request = String::from_utf8_lossy(&buffer[..read]).into_owned();
        assert!(request.contains("POST /api/project/cga-relay/mcp-tool HTTP/1.1"));
        assert!(request.contains(expected_tool));
        for fragment in expected_fragments {
            assert!(
                request.contains(fragment),
                "request missing {fragment}: {request}"
            );
        }
        let body = format!("{{\"ok\":true,\"tool\":\"{expected_tool}\"}}");
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream.write_all(response.as_bytes()).unwrap();
    });
    (port, server)
}

#[test]
fn index_git_cli_forwards_branch_and_parent_ref() {
    let (port, server) = assert_cli_tool_request(
        "index_git_incremental",
        &[
            "feature/client-menu-order",
            "parent_ref",
            "include_untracked",
        ],
    );
    let tmp = TestDir::new("index-git-cli");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );

    let output = Command::new(agent_bin())
        .args([
            "index",
            "git",
            "--config",
            config.to_str().unwrap(),
            "--repo-path",
            repo.to_str().unwrap(),
            "--branch",
            "feature/client-menu-order",
            "--parent-ref",
            "main",
            "--no-include-untracked",
        ])
        .env("CGA_TEST_API_KEY", TEST_SECRET)
        .output()
        .unwrap();

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert!(stdout(&output).contains("index_git_incremental"));
    assert!(!stdout(&output).contains(TEST_SECRET));
    server.join().unwrap();
}

#[test]
fn index_incremental_cli_forwards_multiple_changed_paths() {
    let (port, server) = assert_cli_tool_request(
        "index_incremental",
        &["src/a.py", "src/b.py", "bugfix/cache-key", "changed_paths"],
    );
    let tmp = TestDir::new("index-incremental-cli");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );

    let output = Command::new(agent_bin())
        .args([
            "index",
            "incremental",
            "--config",
            config.to_str().unwrap(),
            "--repo-path",
            repo.to_str().unwrap(),
            "--changed-path",
            "src/a.py",
            "--changed-path",
            "src/b.py",
            "--ref",
            "bugfix/cache-key",
        ])
        .env("CGA_TEST_API_KEY", TEST_SECRET)
        .output()
        .unwrap();

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert!(stdout(&output).contains("index_incremental"));
    server.join().unwrap();
}

#[test]
fn refs_promote_cli_forwards_delete_option() {
    let (port, server) = assert_cli_tool_request(
        "promote_ref",
        &[
            "feature/client-menu-order",
            "parent_ref",
            "delete_ref_graph",
        ],
    );
    let tmp = TestDir::new("refs-promote-cli");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );

    let output = Command::new(agent_bin())
        .args([
            "refs",
            "promote",
            "--config",
            config.to_str().unwrap(),
            "--repo-path",
            repo.to_str().unwrap(),
            "--ref",
            "feature/client-menu-order",
            "--parent-ref",
            "main",
            "--delete-ref-graph",
        ])
        .env("CGA_TEST_API_KEY", TEST_SECRET)
        .output()
        .unwrap();

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert!(stdout(&output).contains("promote_ref"));
    server.join().unwrap();
}

#[test]
fn index_incremental_cli_requires_changed_path() {
    let tmp = TestDir::new("index-incremental-missing-path");
    let config = write_safe_config(tmp.path(), tmp.path(), &[]);

    let output = Command::new(agent_bin())
        .args([
            "index",
            "incremental",
            "--config",
            config.to_str().unwrap(),
            "--ref",
            "feature/example",
        ])
        .output()
        .unwrap();

    assert!(!output.status.success());
    assert!(stderr(&output).contains("requires at least one --changed-path"));
}

#[test]
fn refs_promote_cli_requires_ref() {
    let tmp = TestDir::new("refs-promote-missing-ref");
    let config = write_safe_config(tmp.path(), tmp.path(), &[]);

    let output = Command::new(agent_bin())
        .args(["refs", "promote", "--config", config.to_str().unwrap()])
        .output()
        .unwrap();

    assert!(!output.status.success());
    assert!(stderr(&output).contains("missing required option: --ref"));
}

#[test]
fn copied_agent_executable_runs_without_source_tree_runtime_dependencies() {
    let tmp = TestDir::new("standalone-copy");
    let copied = tmp.path().join(if cfg!(windows) {
        "cga-relay.exe"
    } else {
        "cga-relay"
    });
    fs::copy(agent_bin(), &copied).expect("agent executable should be copyable");

    let output = Command::new(&copied)
        .current_dir(tmp.path())
        .arg("--version")
        .output()
        .expect("copied agent executable should run");

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert!(stdout(&output).contains("cga-relay"));
}

#[test]
fn crate_has_no_third_party_runtime_dependencies() {
    let manifest = fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml"))
        .expect("Cargo.toml should be readable");
    let dependencies = manifest
        .split("[dependencies]")
        .nth(1)
        .unwrap_or("")
        .split('\n')
        .take_while(|line| !line.trim_start().starts_with('['))
        .filter(|line| {
            let trimmed = line.trim();
            !trimmed.is_empty() && !trimmed.starts_with('#')
        })
        .collect::<Vec<_>>();

    assert!(
        dependencies.is_empty(),
        "CGA-Relay must remain a standalone std-only executable; dependencies found: {dependencies:?}"
    );
}

#[test]
fn config_parser_accepts_safe_config_and_rejects_invalid_lines() {
    let tmp = TestDir::new("config");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let ok = run_agent(&["doctor", "--config", config.to_str().unwrap(), "--json"]);
    assert!(ok.status.success(), "stderr: {}", stderr(&ok));

    let invalid = tmp.path().join("bad.env");
    fs::write(&invalid, "AGENT_ID=ok\nthis line is invalid\n").unwrap();
    let bad = run_agent(&["doctor", "--config", invalid.to_str().unwrap(), "--json"]);
    assert!(!bad.status.success());
    assert!(stderr(&bad).contains("invalid config line"));

    let mut unknown_text = fs::read_to_string(&config).unwrap();
    unknown_text.push_str(&format!("CGA_DEVELOPER_TOKEN={TEST_SECRET}\n"));
    let unknown = tmp.path().join("unknown.env");
    fs::write(&unknown, unknown_text).unwrap();
    let rejected = run_agent(&["doctor", "--config", unknown.to_str().unwrap(), "--json"]);
    assert!(!rejected.status.success());
    assert!(stderr(&rejected).contains("unsupported config key: CGA_DEVELOPER_TOKEN"));
    assert!(!stdout(&rejected).contains(TEST_SECRET));
    assert!(!stderr(&rejected).contains(TEST_SECRET));
}

#[test]
fn config_parser_rejects_duplicate_keys() {
    let tmp = TestDir::new("duplicate-config-key");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let mut text = fs::read_to_string(&config).unwrap();
    text.push_str("PROJECT_ID=OVERRIDE\n");
    fs::write(&config, text).unwrap();

    let output = run_agent(&["doctor", "--config", config.to_str().unwrap(), "--json"]);

    assert!(!output.status.success());
    assert!(stderr(&output).contains("duplicate config key: PROJECT_ID"));
}

#[test]
fn config_parser_rejects_invalid_environment_variable_names() {
    let tmp = TestDir::new("invalid-config-env-name");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("API_KEY_ENV", "not-an-env-name".to_string())],
    );

    let output = run_agent(&["doctor", "--config", config.to_str().unwrap(), "--json"]);

    assert!(!output.status.success());
    assert!(stderr(&output).contains("invalid environment variable name for API_KEY_ENV"));
}

#[test]
fn login_rejects_invalid_token_environment_variable_name() {
    let tmp = TestDir::new("invalid-login-env-name");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);

    let output = run_agent(&[
        "login",
        "--config",
        config.to_str().unwrap(),
        "--email",
        "dev@example.test",
        "--token-env",
        "not-an-env-name",
    ]);

    assert!(!output.status.success());
    assert!(stderr(&output).contains("invalid environment variable name for --token-env"));
    assert!(!tmp.path().join("state").join("profile.json").exists());
}

#[test]
fn doctor_reports_redacted_status() {
    let tmp = TestDir::new("doctor");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let output = Command::new(agent_bin())
        .args(["doctor", "--config", config.to_str().unwrap(), "--json"])
        .env("CGA_TEST_API_KEY", TEST_SECRET)
        .output()
        .unwrap();
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"env_var\":\"CGA_TEST_API_KEY\""));
    assert!(out.contains("\"configured\":true"));
    assert!(!out.contains(TEST_SECRET));
    assert!(!stderr(&output).contains(TEST_SECRET));
}

#[test]
fn tray_status_reports_notification_area_mode_without_starting_loop() {
    let (api_base_url, server) = spawn_health_server();
    let tmp = TestDir::new("tray-status");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[("API_BASE_URL", api_base_url)]);
    let output = Command::new(agent_bin())
        .args([
            "tray",
            "--config",
            config.to_str().unwrap(),
            "--status",
            "--json",
        ])
        .env("CGA_TEST_API_KEY", TEST_SECRET)
        .output()
        .unwrap();

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"agent_id\":\"dev-agent-01\""));
    assert!(out.contains("\"tooltip\":\"CGA-Relay - dev-agent-01 - not signed in\""));
    assert!(out.contains("\"icon\":\"embedded-resource:4\""));
    assert!(out.contains("\"icon_variant\":\"gray\""));
    assert!(out.contains("\"backend_available\":true"));
    assert!(out.contains("\"logged_in\":false"));
    assert!(out.contains("\"username\":\"\""));
    assert!(out.contains(
        "\"menu\":[\"Not signed in\",\"Open CGA Web\",\"Settings\",\"Logs\",\"About\",\"Relaunch\",\"Exit\"]"
    ));
    assert!(out.contains("\"name\":\"CGA-Relay\""));
    assert!(out.contains("\"user_groups\":[]"));
    assert!(out.contains("\"user_group_count\":0"));
    assert!(!out.contains("\"project\":\"CGA-Relay\""));
    assert!(out.contains("\"author\":\"Nate Scott\""));
    assert!(out.contains("\"repository\":\"https://github.com/nascousa/cga\""));
    assert!(out.contains("\"support\":\"https://github.com/nascousa/cga/issues\""));
    assert!(out.contains(
        "\"menu_events\":[\"WM_LBUTTONDBLCLK\",\"WM_CONTEXTMENU\",\"WM_RBUTTONUP\",\"WM_TIMER\"]"
    ));
    if cfg!(windows) {
        assert!(out.contains("\"supported\":true"));
        assert!(out.contains("\"icon_loaded\":true"));
        assert!(out.contains("windows-shell-notify-icon"));
    }
    assert!(!out.contains(TEST_SECRET));
    assert!(!stderr(&output).contains(TEST_SECRET));
    server.join().unwrap();
}

#[test]
fn tray_status_warns_when_cga_server_container_is_unavailable() {
    let unavailable_listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let unavailable_port = unavailable_listener.local_addr().unwrap().port();
    drop(unavailable_listener);

    let tmp = TestDir::new("tray-status-backend-unavailable");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[(
            "API_BASE_URL",
            format!("http://127.0.0.1:{unavailable_port}"),
        )],
    );

    let output = run_agent(&[
        "tray",
        "--config",
        config.to_str().unwrap(),
        "--status",
        "--json",
    ]);

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"backend_available\":false"), "stdout: {out}");
    assert!(
        out.contains("\"icon\":\"embedded-resource:7\""),
        "stdout: {out}"
    );
    assert!(
        out.contains("\"icon_variant\":\"yellow-blink\""),
        "stdout: {out}"
    );
    assert!(
        out.contains("\"notification_title\":\"CGA Server Container is unavailable\""),
        "stdout: {out}"
    );
    assert!(
        out.contains(
            "\"notification_message\":\"Start the CGA Server Container to reconnect CGA-Relay.\""
        ),
        "stdout: {out}"
    );
}

#[test]
fn tray_status_uses_color_icon_and_username_when_signed_in() {
    let (api_base_url, server) = spawn_health_server();
    let tmp = TestDir::new("tray-status-signed-in");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let state = tmp.path().join("state");
    fs::create_dir_all(&state).unwrap();
    fs::write(
        state.join("account-session.json"),
        format!(
            "{{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"{}\"}}",
            TEST_SECRET
        ),
    )
    .unwrap();
    fs::write(
        state.join("account-groups.tsv"),
        "version\t1\ngroup\t1\tTeam Alpha\tPrimary access\t1\nproject\t1\tAlpha Project\tALPHA12345\tC:/repo\t1\n",
    )
    .unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[("API_BASE_URL", api_base_url)]);

    let output = Command::new(agent_bin())
        .args([
            "tray",
            "--config",
            config.to_str().unwrap(),
            "--status",
            "--json",
        ])
        .output()
        .unwrap();

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"icon\":\"embedded-resource:1\""));
    assert!(out.contains("\"icon_variant\":\"color\""));
    assert!(out.contains("\"logged_in\":true"));
    assert!(out.contains("\"username\":\"dev@example.com\""));
    assert!(out.contains(
        "\"menu\":[\"Signed in: dev@example.com\",\"Open CGA Web\",\"Settings\",\"Logs\",\"About\",\"Relaunch\",\"Exit\"]"
    ));
    assert!(out.contains("\"user_groups\":[\"Team Alpha\"]"));
    assert!(out.contains("\"user_group_count\":1"));
    assert!(out.contains("signed in as dev@example.com"));
    assert!(!out.contains(TEST_SECRET));
    assert!(!stderr(&output).contains(TEST_SECRET));
    server.join().unwrap();
    if cfg!(windows) {
        let persisted_session = fs::read_to_string(state.join("account-session.json")).unwrap();
        assert!(persisted_session.contains("\"access_token_dpapi\":"));
        assert!(!persisted_session.contains("\"access_token\":"));
        assert!(!persisted_session.contains(TEST_SECRET));
    }
}

#[test]
fn tray_status_treats_expired_account_jwt_as_signed_out() {
    let (api_base_url, server) = spawn_health_server();
    let tmp = TestDir::new("tray-status-expired");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let state = tmp.path().join("state");
    fs::create_dir_all(&state).unwrap();
    fs::write(
        state.join("account-session.json"),
        "{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"e30.eyJzdWIiOiJkZXYiLCJleHAiOjF9.sig\"}\n",
    )
    .unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[("API_BASE_URL", api_base_url)]);

    let output = run_agent(&[
        "tray",
        "--config",
        config.to_str().unwrap(),
        "--status",
        "--json",
    ]);

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"logged_in\":false"), "stdout: {out}");
    assert!(out.contains("\"icon_variant\":\"gray\""), "stdout: {out}");
    assert!(out.contains("\"username\":\"\""), "stdout: {out}");
    server.join().unwrap();

    let settings = run_agent(&[
        "settings",
        "--config",
        config.to_str().unwrap(),
        "--status",
        "--json",
    ]);
    assert!(settings.status.success(), "stderr: {}", stderr(&settings));
    assert!(
        stdout(&settings).contains("\"session_configured\":false"),
        "stdout: {}",
        stdout(&settings)
    );
}

#[test]
fn settings_render_shows_local_account_login_page() {
    let tmp = TestDir::new("settings-render");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let state = tmp.path().join("state");
    fs::create_dir_all(&state).unwrap();
    fs::write(
        state.join("account-session.json"),
        "{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"test-token\"}\n",
    )
    .unwrap();
    fs::write(
        state.join("account-groups.tsv"),
        "version\t1\ngroup\t1\tTeam Alpha\tPrimary access\t1\nproject\t1\tAlpha Project\tALPHA12345\tC:/repo\t1\n",
    )
    .unwrap();
    fs::write(
        state.join("account-projects.tsv"),
        "version\t1\nproject\tAlpha Project\tALPHA12345\tC:/repo\t1\nproject\tBeta Project\tBETA123456\tC:/other\t1\n",
    )
    .unwrap();
    fs::write(
        state.join("settings-url.txt"),
        "http://127.0.0.1:17860/settings\n",
    )
    .unwrap();

    let output = run_agent(&["settings", "--config", config.to_str().unwrap(), "--render"]);

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("CGA-Relay Settings"));
    assert!(out.contains("data-theme=\"dark\""));
    assert!(out.contains("color-scheme:dark"));
    assert!(out.contains("status-grid"));
    assert!(out.contains("version-pill"));
    assert!(!out.contains("<span>Project</span>"));
    assert!(out.contains("User Groups"));
    assert!(out.contains("Team Alpha"));
    assert!(out.contains("Alpha Project"));
    assert!(!out.contains("Beta Project"));
    assert!(!out.contains("<p class=\"eyebrow\">Projects</p>"));
    assert!(!out.contains("Account Projects"));
    assert!(out.contains("Signed in"));
    assert!(out.contains("dev@example.com"));
    assert!(out.contains("action=\"/refresh\""));
    assert!(out.contains("Refresh access"));
    assert!(!out.contains(TEST_SECRET));

    let status = run_agent(&[
        "settings",
        "--config",
        config.to_str().unwrap(),
        "--status",
        "--json",
    ]);
    assert!(status.status.success(), "stderr: {}", stderr(&status));
    let status_out = stdout(&status);
    assert!(status_out.contains("\"page\":\"local-account-settings\""));
    assert!(status_out.contains("\"project_id\":\"PROJECT123\""));
    assert!(status_out.contains("\"project_root\":"));
    assert!(status_out.contains("\"projects_endpoint\":\"/api/auth/me/groups\""));
    assert!(status_out
        .contains("\"index_endpoint\":\"http://127.0.0.1:17860/api/index-git-incremental\""));
    assert!(status_out.contains("\"project_count\":1"));
    assert!(status_out.contains("\"session_configured\":true"));
    assert!(status_out.contains("\"username\":\"dev@example.com\""));
}

#[test]
fn scan_dry_run_reports_counts_and_does_not_write_state() {
    let tmp = TestDir::new("scan-dry");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(repo.join("node_modules")).unwrap();
    fs::write(repo.join("keep.py"), "print('ok')\n").unwrap();
    fs::write(repo.join("node_modules").join("ignored.js"), "ignored\n").unwrap();
    fs::write(repo.join("large.txt"), "x".repeat(128)).unwrap();
    fs::write(repo.join("binary.bin"), b"abc\0def").unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[("MAX_FILE_BYTES", "32".to_string())]);

    let output = run_agent(&[
        "scan",
        "--config",
        config.to_str().unwrap(),
        "--dry-run",
        "--json",
    ]);
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    for expected in [
        "\"candidate\":3",
        "\"excluded\":0",
        "\"scanned\":1",
        "\"changed\":1",
        "\"unchanged\":0",
        "\"oversized\":1",
        "\"skipped_binary\":1",
        "\"tombstone\":0",
        "\"bytes_scanned\":12",
    ] {
        assert!(out.contains(expected), "missing {expected}: {out}");
    }
    assert!(!tmp.path().join("state").join("scan-state").exists());
}

#[test]
fn scan_normal_mode_writes_state_and_later_reports_unchanged() {
    let tmp = TestDir::new("scan-normal");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("keep.py"), "print('ok')\n").unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);

    let first = run_agent(&["scan", "--config", config.to_str().unwrap(), "--json"]);
    assert!(first.status.success(), "stderr: {}", stderr(&first));
    assert!(stdout(&first).contains("\"changed\":1"));
    assert!(tmp.path().join("state").join("scan-state").exists());

    let second = run_agent(&[
        "scan",
        "--config",
        config.to_str().unwrap(),
        "--dry-run",
        "--json",
    ]);
    assert!(second.status.success(), "stderr: {}", stderr(&second));
    assert!(stdout(&second).contains("\"unchanged\":1"));
}

#[test]
fn scanner_skips_excluded_oversized_binary_and_reports_tombstones() {
    let tmp = TestDir::new("scanner-tombstone");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(repo.join("node_modules")).unwrap();
    fs::write(repo.join("tracked.py"), "print('first')\n").unwrap();
    fs::write(repo.join("node_modules").join("ignored.js"), "ignored\n").unwrap();
    fs::write(repo.join("large.txt"), "x".repeat(128)).unwrap();
    fs::write(repo.join("binary.bin"), b"abc\0def").unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[("MAX_FILE_BYTES", "32".to_string())]);

    let first = run_agent(&["scan", "--config", config.to_str().unwrap(), "--json"]);
    assert!(first.status.success(), "stderr: {}", stderr(&first));
    fs::remove_file(repo.join("tracked.py")).unwrap();
    let second = run_agent(&[
        "scan",
        "--config",
        config.to_str().unwrap(),
        "--dry-run",
        "--json",
    ]);
    assert!(second.status.success(), "stderr: {}", stderr(&second));
    let out = stdout(&second);
    assert!(out.contains("\"excluded\":0"));
    assert!(out.contains("\"oversized\":1"));
    assert!(out.contains("\"skipped_binary\":1"));
    assert!(out.contains("\"tombstone\":1"));
    assert!(out.contains("tracked.py"));
}

#[test]
fn scanner_prunes_nested_venv_directories_before_counting_candidates() {
    let tmp = TestDir::new("scanner-nested-venv");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(repo.join("tools").join("worker").join("venv")).unwrap();
    fs::write(repo.join("keep.py"), "print('ok')\n").unwrap();
    fs::write(
        repo.join("tools")
            .join("worker")
            .join("venv")
            .join("ignored.py"),
        "print('dependency')\n",
    )
    .unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);

    let output = run_agent(&[
        "scan",
        "--config",
        config.to_str().unwrap(),
        "--dry-run",
        "--json",
    ]);

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"candidate\":1"), "unexpected scan: {out}");
    assert!(
        !out.contains("ignored.py"),
        "venv file leaked into scan: {out}"
    );
}

#[test]
fn login_persists_profile_without_token_value() {
    let tmp = TestDir::new("login");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let output = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let profile = fs::read_to_string(tmp.path().join("state").join("profile.json")).unwrap();
    assert!(profile.contains("dev@example.test"));
    assert!(profile.contains("CGA_TEST_DEVELOPER_TOKEN"));
    assert!(!profile.contains(TEST_SECRET));
    assert!(!stdout(&output).contains(TEST_SECRET));
    assert!(!stderr(&output).contains(TEST_SECRET));
}

#[test]
fn projects_add_list_maintains_central_registry() {
    let tmp = TestDir::new("projects");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);

    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "browser-agent",
        "--namespace",
        "dev",
        "--name",
        "Browser Agent",
        "--root",
        repo.to_str().unwrap(),
        "--json",
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));
    assert!(stdout(&add).contains("dev/browser-agent"));

    let list = run_agent(&[
        "projects",
        "list",
        "--config",
        config.to_str().unwrap(),
        "--json",
    ]);
    assert!(list.status.success(), "stderr: {}", stderr(&list));
    let out = stdout(&list);
    assert!(out.contains("\"count\":1"));
    assert!(out.contains(repo.to_str().unwrap().replace('\\', "\\\\").as_str()));
}

#[test]
fn sync_dry_run_scans_registered_projects_without_submitting() {
    let tmp = TestDir::new("sync-dry");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("a.py"), "print('a')\n").unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);

    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--dry-run",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"dry_run\":true"));
    assert!(out.contains("\"submitted\":0"));
    assert!(out.contains("\"changed\":1"));
    assert!(!out.contains(TEST_SECRET));
}

#[test]
fn sync_fails_when_developer_token_env_is_missing() {
    let tmp = TestDir::new("sync-missing-token");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let login = run_agent(&[
        "login",
        "--config",
        config.to_str().unwrap(),
        "--email",
        "dev@example.test",
        "--token-env",
        "MISSING_TOKEN_ENV",
    ]);
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = run_agent(&[
        "sync",
        "--config",
        config.to_str().unwrap(),
        "--all",
        "--dry-run",
        "--json",
    ]);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("MISSING_TOKEN_ENV"));
    assert!(!stderr(&output).contains(TEST_SECRET));

    let empty = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--dry-run",
            "--json",
        ])
        .env("MISSING_TOKEN_ENV", "")
        .output()
        .unwrap();
    assert!(!empty.status.success());
    assert!(stderr(&empty).contains("MISSING_TOKEN_ENV"));
}

#[test]
fn sync_reports_expired_account_login_before_scanning() {
    let tmp = TestDir::new("sync-expired-account");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let login = run_agent(&[
        "login",
        "--config",
        config.to_str().unwrap(),
        "--email",
        "dev@example.test",
        "--token-env",
        "MISSING_TOKEN_ENV",
    ]);
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    fs::write(
        tmp.path().join("state").join("account-session.json"),
        "{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"e30.eyJzdWIiOiJkZXYiLCJleHAiOjF9.sig\"}\n",
    )
    .unwrap();
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--namespace",
        "account",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--namespace",
            "account",
            "--project-tag",
            "repo",
            "--json",
        ])
        .env("MISSING_TOKEN_ENV", TEST_SECRET)
        .output()
        .unwrap();

    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("CGA account login expired; sign in again"),
        "stderr: {}",
        stderr(&output)
    );
    assert!(
        !stderr(&output).contains("scanning"),
        "stderr: {}",
        stderr(&output)
    );
}

#[test]
fn sync_prefers_valid_account_session_for_account_namespace_project() {
    let tmp = TestDir::new("sync-account-namespace");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("account.txt"), "account route\n").unwrap();
    let account_token = "e30.eyJzdWIiOiJkZXYiLCJleHAiOjQxMDI0NDQ4MDB9.sig";
    let (port, server) = spawn_accepting_sync_server(8 * 1024 * 1024, 1);
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let state = tmp.path().join("state");
    fs::write(
        state.join("account-session.json"),
        format!(
            "{{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"{account_token}\"}}\n"
        ),
    )
    .unwrap();
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--namespace",
        "account",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--namespace",
            "account",
            "--project-tag",
            "repo",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let requests = server.join().unwrap();
    assert_eq!(requests.len(), 1);
    assert!(requests[0].starts_with("POST /api/auth/cga-relay/sync HTTP/1.1"));
    assert!(requests[0].contains(&format!("Authorization: Bearer {account_token}")));
    assert!(!requests[0].contains(TEST_SECRET));
}

#[test]
fn sync_falls_back_to_project_token_when_account_session_is_rejected() {
    let tmp = TestDir::new("sync-account-fallback");
    let first_repo = tmp.path().join("first-repo");
    let second_repo = tmp.path().join("second-repo");
    fs::create_dir_all(&first_repo).unwrap();
    fs::create_dir_all(&second_repo).unwrap();
    fs::write(first_repo.join("account.txt"), "first account route\n").unwrap();
    fs::write(second_repo.join("account.txt"), "second account route\n").unwrap();
    let account_token = "e30.eyJzdWIiOiJkZXYiLCJleHAiOjQxMDI0NDQ4MDB9.sig";
    let (port, server) = spawn_account_rejection_then_project_acceptance_server(2);
    let config = write_safe_config(
        tmp.path(),
        &first_repo,
        &[("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    fs::write(
        tmp.path().join("state").join("account-session.json"),
        format!(
            "{{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"{account_token}\"}}\n"
        ),
    )
    .unwrap();
    let add_first = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "first",
        "--namespace",
        "account",
        "--root",
        first_repo.to_str().unwrap(),
    ]);
    assert!(add_first.status.success(), "stderr: {}", stderr(&add_first));
    let add_second = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "second",
        "--namespace",
        "account",
        "--root",
        second_repo.to_str().unwrap(),
    ]);
    assert!(
        add_second.status.success(),
        "stderr: {}",
        stderr(&add_second)
    );

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();

    let requests = server.join().unwrap();
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert_eq!(requests.len(), 3);
    assert!(requests[0].starts_with("POST /api/auth/cga-relay/sync HTTP/1.1"));
    assert!(requests[0].contains(&format!("Authorization: Bearer {account_token}")));
    assert!(requests[1].starts_with("POST /api/project/cga-relay/sync HTTP/1.1"));
    assert!(requests[1].contains(&format!("Authorization: Bearer {TEST_SECRET}")));
    assert!(requests[1].contains("X-Project-ID: PROJECT123"));
    assert!(requests[2].starts_with("POST /api/project/cga-relay/sync HTTP/1.1"));
    assert!(requests[2].contains(&format!("Authorization: Bearer {TEST_SECRET}")));
    assert!(requests[2].contains("X-Project-ID: PROJECT123"));
    assert!(!stdout(&output).contains(account_token));
    assert!(!stderr(&output).contains(account_token));
    assert!(!stdout(&output).contains(TEST_SECRET));
    assert!(!stderr(&output).contains(TEST_SECRET));
    assert!(!tmp
        .path()
        .join("state")
        .join("account-session.json")
        .exists());
}

#[test]
fn sync_does_not_fallback_or_delete_account_session_on_forbidden() {
    let tmp = TestDir::new("sync-account-forbidden");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("account.txt"), "account route\n").unwrap();
    let account_token = "e30.eyJzdWIiOiJkZXYiLCJleHAiOjQxMDI0NDQ4MDB9.sig";
    let (port, server) = spawn_sync_status_server(vec![403]);
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let session_path = tmp.path().join("state").join("account-session.json");
    fs::write(
        &session_path,
        format!(
            "{{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"{account_token}\"}}\n"
        ),
    )
    .unwrap();
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--namespace",
        "account",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--namespace",
            "account",
            "--project-tag",
            "repo",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();

    let requests = server.join().unwrap();
    assert!(!output.status.success());
    assert_eq!(requests.len(), 1);
    assert!(requests[0].starts_with("POST /api/auth/cga-relay/sync HTTP/1.1"));
    assert!(stderr(&output).contains("HTTP request failed with status 403"));
    assert!(!stderr(&output).contains("retrying with project token"));
    assert!(session_path.exists());
}

#[test]
fn sync_rejected_account_session_without_project_token_requires_login() {
    let tmp = TestDir::new("sync-account-unauthorized-no-fallback");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("account.txt"), "account route\n").unwrap();
    let account_token = "e30.eyJzdWIiOiJkZXYiLCJleHAiOjQxMDI0NDQ4MDB9.sig";
    let (port, server) = spawn_sync_status_server(vec![401]);
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let session_path = tmp.path().join("state").join("account-session.json");
    fs::write(
        &session_path,
        format!(
            "{{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"{account_token}\"}}\n"
        ),
    )
    .unwrap();
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--namespace",
        "account",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--namespace",
            "account",
            "--project-tag",
            "repo",
            "--json",
        ])
        .env_remove("CGA_TEST_DEVELOPER_TOKEN")
        .output()
        .unwrap();

    let requests = server.join().unwrap();
    assert!(!output.status.success());
    assert_eq!(requests.len(), 1);
    assert!(requests[0].starts_with("POST /api/auth/cga-relay/sync HTTP/1.1"));
    assert!(stderr(&output).contains("account login is no longer valid"));
    assert!(!session_path.exists());
}

#[test]
fn sync_fails_when_project_token_fallback_is_forbidden() {
    let tmp = TestDir::new("sync-project-fallback-forbidden");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("account.txt"), "account route\n").unwrap();
    let account_token = "e30.eyJzdWIiOiJkZXYiLCJleHAiOjQxMDI0NDQ4MDB9.sig";
    let (port, server) = spawn_sync_status_server(vec![401, 403]);
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let session_path = tmp.path().join("state").join("account-session.json");
    fs::write(
        &session_path,
        format!(
            "{{\"username\":\"dev@example.com\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"{account_token}\"}}\n"
        ),
    )
    .unwrap();
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--namespace",
        "account",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--namespace",
            "account",
            "--project-tag",
            "repo",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();

    let requests = server.join().unwrap();
    assert!(!output.status.success());
    assert_eq!(requests.len(), 2);
    assert!(requests[0].starts_with("POST /api/auth/cga-relay/sync HTTP/1.1"));
    assert!(requests[1].starts_with("POST /api/project/cga-relay/sync HTTP/1.1"));
    assert!(stderr(&output).contains("HTTP request failed with status 403"));
    assert!(!session_path.exists());
}

#[test]
fn sync_batches_checkpoints_progress_and_metadata_only_logs() {
    let tmp = TestDir::new("sync-batch-checkpoint");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    for index in 0..501 {
        fs::write(
            repo.join(format!("file-{index:03}.txt")),
            format!("PRIVATE_SOURCE_MARKER_{index:03}\n"),
        )
        .unwrap();
    }

    let (first_port, first_server) = spawn_checkpoint_server();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[
            (
                "CONTROL_API_BASE_URL",
                format!("http://127.0.0.1:{first_port}"),
            ),
            ("MAX_BATCH_BYTES", (8 * 1024 * 1024).to_string()),
        ],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let first = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(!first.status.success(), "first sync should stop on batch 2");
    assert!(stderr(&first).contains("scanning"));
    assert!(stderr(&first).contains("processed 500/501 candidates"));
    assert!(stderr(&first).contains("scan complete"));
    assert!(stderr(&first).contains("submitting batch 1/2"));
    let first_requests = first_server.join().unwrap();
    assert_eq!(first_requests.len(), 2);
    assert_eq!(snapshot_count(&first_requests[0]), 500);
    assert_eq!(snapshot_count(&first_requests[1]), 1);

    let state_file = tmp
        .path()
        .join("state")
        .join("scan-state")
        .join("default_repo.state");
    let checkpoint = fs::read_to_string(&state_file).expect("batch 1 should checkpoint state");
    assert_eq!(
        checkpoint
            .lines()
            .filter(|line| line.starts_with("file\t"))
            .count(),
        500
    );

    let (second_port, second_server) = spawn_accepting_sync_server(8 * 1024 * 1024, 1);
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[
            (
                "CONTROL_API_BASE_URL",
                format!("http://127.0.0.1:{second_port}"),
            ),
            ("MAX_BATCH_BYTES", (8 * 1024 * 1024).to_string()),
        ],
    );
    let second = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(second.status.success(), "stderr: {}", stderr(&second));
    assert!(stdout(&second).contains("\"batch_count\":1"));
    let second_requests = second_server.join().unwrap();
    assert_eq!(second_requests.len(), 1);
    assert_eq!(snapshot_count(&second_requests[0]), 1);

    let final_state = fs::read_to_string(state_file).unwrap();
    assert_eq!(
        final_state
            .lines()
            .filter(|line| line.starts_with("file\t"))
            .count(),
        501
    );
    let (_, log_text) = read_log_files(tmp.path());
    assert!(log_text.contains("body_bytes="));
    assert!(!log_text.contains("body_sha256="));
    assert!(!log_text.contains("body:\n"));
    assert!(!log_text.contains("PRIVATE_SOURCE_MARKER_"));
    assert!(!log_text.contains(RESPONSE_HEADER_MARKER));
}

#[test]
fn sync_respects_configured_batch_byte_limit() {
    let tmp = TestDir::new("sync-batch-bytes");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    for index in 0..4 {
        fs::write(repo.join(format!("payload-{index}.txt")), "x".repeat(300)).unwrap();
    }
    let max_batch_bytes = 1500;
    let (port, server) = spawn_accepting_sync_server(max_batch_bytes, 4);
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[
            ("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}")),
            ("MAX_FILE_BYTES", "1024".to_string()),
            ("MAX_BATCH_BYTES", max_batch_bytes.to_string()),
        ],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let requests = server.join().unwrap();
    assert!(requests.len() > 1, "expected byte-bounded batches");
    assert_eq!(
        requests
            .iter()
            .map(|request| snapshot_count(request))
            .sum::<usize>(),
        4
    );
    assert!(requests
        .iter()
        .all(|request| request_body(request).len() <= max_batch_bytes));
}

fn run_mcp(config: &Path, input: &str, extra_env: &[(&str, &str)]) -> Output {
    let mut command = Command::new(agent_bin());
    command.args(["mcp", "--config", config.to_str().unwrap()]);
    for (key, value) in extra_env {
        command.env(key, value);
    }
    let mut child = command
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn concurrent_processes_allow_exactly_one_mutex_holder() {
    let tmp = TestDir::new("single-instance");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let mut first = Command::new(agent_bin())
        .args(["mcp", "--config", config.to_str().unwrap()])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("first relay process should start");
    let mut second = Command::new(agent_bin())
        .args(["mcp", "--config", config.to_str().unwrap()])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("second relay process should start");

    let mut contender_exited = false;
    for _ in 0..200 {
        if first.try_wait().unwrap().is_some() || second.try_wait().unwrap().is_some() {
            contender_exited = true;
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }

    drop(first.stdin.take());
    drop(second.stdin.take());
    let first_output = first.wait_with_output().unwrap();
    let second_output = second.wait_with_output().unwrap();
    let outputs = [&first_output, &second_output];

    assert!(
        contender_exited,
        "neither relay process was rejected by the mutex"
    );
    assert_eq!(
        outputs
            .iter()
            .filter(|output| output.status.success())
            .count(),
        1,
        "exactly one relay process should hold the mutex: first={}, second={}",
        stderr(&first_output),
        stderr(&second_output)
    );
    let rejected = outputs
        .iter()
        .find(|output| !output.status.success())
        .expect("one relay process should be rejected");
    assert!(stderr(rejected).contains("CGA-Relay is already running"));
}

#[test]
fn mcp_initialize_returns_server_info() {
    let tmp = TestDir::new("mcp-init");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);

    let output = run_mcp(
        &config,
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\n",
        &[],
    );
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"serverInfo\""));
    assert!(out.contains("\"name\":\"cga-relay\""));
}

#[test]
fn mcp_tools_list_exposes_expected_tools() {
    let tmp = TestDir::new("mcp-tools");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);

    let output = run_mcp(
        &config,
        "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}\n",
        &[],
    );
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    for tool in [
        "health_check",
        "getstarted",
        "index_incremental",
        "index_git_incremental",
        "index_progress",
        "query_impact_graph",
        "fetch_minimal_code",
        "get_optimized_context",
        "promote_ref",
    ] {
        assert!(out.contains(tool), "tools/list missing {tool}: {out}");
    }
}

#[test]
fn mcp_promote_ref_forwards_branch_arguments() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let mut buffer = [0_u8; 4096];
        let read = stream.read(&mut buffer).unwrap();
        let request = String::from_utf8_lossy(&buffer[..read]).into_owned();
        assert!(request.contains("promote_ref"));
        assert!(request.contains("feature/client-menu-order"));
        assert!(request.contains("parent_ref"));
        assert!(request.contains("delete_ref_graph"));
        let body = "{\"ok\":true,\"tool\":\"promote_ref\"}";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream.write_all(response.as_bytes()).unwrap();
    });

    let tmp = TestDir::new("mcp-promote-ref");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let api = format!("http://127.0.0.1:{port}");
    let config = write_safe_config(tmp.path(), &repo, &[("API_BASE_URL", api)]);
    let input = "{\"jsonrpc\":\"2.0\",\"id\":34,\"method\":\"tools/call\",\"params\":{\"name\":\"promote_ref\",\"arguments\":{\"ref_id\":\"feature/client-menu-order\",\"parent_ref\":\"main\",\"repo_path\":\"C:/repo\",\"delete_ref_graph\":true}}}\n";
    let output = run_mcp(&config, input, &[("CGA_TEST_API_KEY", TEST_SECRET)]);

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert!(stdout(&output).contains("promote_ref"));
    assert!(!stdout(&output).contains(TEST_SECRET));
    server.join().unwrap();
}

#[test]
fn mcp_tools_call_forwards_authenticated_project_request_with_project_id() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let mut buffer = [0_u8; 4096];
        let read = stream.read(&mut buffer).unwrap();
        let request = String::from_utf8_lossy(&buffer[..read]).into_owned();
        assert!(request.contains("POST /api/project/cga-relay/mcp-tool HTTP/1.1"));
        assert!(request.contains("Authorization: Bearer TEST_SECRET_VALUE_SHOULD_NEVER_LEAK"));
        assert!(request.contains("X-Project-ID: PROJECT123"));
        assert!(request.contains("X-CGA-Communication-Profile: CRYSTALS-CNSA-2.0"));
        assert!(request.contains("X-CGA-Key-Establishment: ML-KEM-1024"));
        assert!(request.contains("X-CGA-Signature: ML-DSA-87"));
        assert!(request.contains("X-CGA-Transport-Scope: local-ipc"));
        assert!(request.contains("query_impact_graph"));
        assert!(request.contains("PROJECT123"));
        let body = "{\"ok\":true,\"project_id\":\"PROJECT123\"}";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream.write_all(response.as_bytes()).unwrap();
    });

    let tmp = TestDir::new("mcp-call");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let api = format!("http://127.0.0.1:{port}");
    let config = write_safe_config(tmp.path(), &repo, &[("API_BASE_URL", api)]);
    let input = "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"query_impact_graph\",\"arguments\":{\"query\":\"scanner\"}}}\n";
    let output = run_mcp(&config, input, &[("CGA_TEST_API_KEY", TEST_SECRET)]);
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"project_id\":\"PROJECT123\""));
    assert!(!out.contains(TEST_SECRET));
    server.join().unwrap();

    let (log_names, log_text) = read_log_files(tmp.path());
    assert!(
        !log_names.is_empty(),
        "expected at least one communication log"
    );
    for name in log_names {
        assert_eq!(name.len(), "20260604-15.log".len(), "log name: {name}");
        assert!(name.ends_with(".log"), "log name: {name}");
        assert_eq!(name.as_bytes()[8], b'-', "log name: {name}");
        assert!(
            name[..8].chars().all(|ch| ch.is_ascii_digit()),
            "log name: {name}"
        );
        assert!(
            name[9..11].chars().all(|ch| ch.is_ascii_digit()),
            "log name: {name}"
        );
    }
    assert!(log_text.contains("mcp.stdin"));
    assert!(log_text.contains("mcp.stdout"));
    assert!(log_text.contains("http.request"));
    assert!(log_text.contains("http.response"));
    assert!(log_text.contains("Authorization: <redacted>"));
    assert!(!log_text.contains(TEST_SECRET));
}

#[test]
fn mcp_tools_call_uses_account_session_when_project_token_env_is_missing() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let mut buffer = [0_u8; 4096];
        let read = stream.read(&mut buffer).unwrap();
        let request = String::from_utf8_lossy(&buffer[..read]).into_owned();
        assert!(request.contains("POST /api/auth/cga-relay/mcp-tool HTTP/1.1"));
        assert!(request.contains("Authorization: Bearer TEST_SECRET_VALUE_SHOULD_NEVER_LEAK"));
        assert!(request.contains("X-CGA-Communication-Profile: CRYSTALS-CNSA-2.0"));
        assert!(request.contains("X-CGA-Key-Establishment: ML-KEM-1024"));
        assert!(request.contains("X-CGA-Signature: ML-DSA-87"));
        assert!(request.contains("X-CGA-Transport-Scope: local-ipc"));
        assert!(request.contains("query_impact_graph"));
        assert!(request.contains("PROJECT123"));
        let body = "{\"ok\":true,\"actor_type\":\"account\",\"project_id\":\"PROJECT123\"}";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream.write_all(response.as_bytes()).unwrap();
    });

    let tmp = TestDir::new("mcp-account-call");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::create_dir_all(tmp.path().join("state")).unwrap();
    fs::write(
        tmp.path().join("state").join("account-session.json"),
        format!(
            "{{\"username\":\"dev\",\"role\":\"developer\",\"token_type\":\"bearer\",\"access_token\":\"{}\"}}",
            TEST_SECRET
        ),
    )
    .unwrap();
    let api = format!("http://127.0.0.1:{port}");
    let config = write_safe_config(tmp.path(), &repo, &[("API_BASE_URL", api)]);
    let input = "{\"jsonrpc\":\"2.0\",\"id\":33,\"method\":\"tools/call\",\"params\":{\"name\":\"query_impact_graph\",\"arguments\":{\"query\":\"scanner\"}}}\n";
    let output = run_mcp(&config, input, &[]);

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"actor_type\":\"account\""));
    assert!(!out.contains(TEST_SECRET));
    server.join().unwrap();
}

#[test]
fn mcp_index_git_incremental_uses_local_git_and_forwards_incremental_paths() {
    if Command::new("git").arg("--version").output().is_err() {
        return;
    }

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let mut stream = listener.accept().unwrap().0;
        let mut buffer = [0_u8; 8192];
        let read = stream.read(&mut buffer).unwrap();
        let request = String::from_utf8_lossy(&buffer[..read]).into_owned();
        assert!(request.contains("POST /api/project/cga-relay/mcp-tool HTTP/1.1"));
        assert!(request.contains("\"tool\":\"index_incremental\""));
        assert!(request.contains("\"changed_paths\""));
        assert!(request.contains("a.py"));
        assert!(!request.contains("\"tool\":\"index_git_incremental\""));
        let body = "{\"ok\":true,\"project_id\":\"PROJECT123\"}";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream.write_all(response.as_bytes()).unwrap();
    });

    let tmp = TestDir::new("mcp-git-incremental");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(repo.join("src")).unwrap();
    let init = Command::new("git").arg("init").arg(&repo).output().unwrap();
    assert!(init.status.success(), "stderr: {}", stderr(&init));
    fs::write(repo.join("src").join("a.py"), "print('a')\n").unwrap();
    let api = format!("http://127.0.0.1:{port}");
    let config = write_safe_config(tmp.path(), &repo, &[("API_BASE_URL", api)]);
    let input = "{\"jsonrpc\":\"2.0\",\"id\":30,\"method\":\"tools/call\",\"params\":{\"name\":\"index_git_incremental\",\"arguments\":{}}}\n";
    let output = run_mcp(&config, input, &[("CGA_TEST_API_KEY", TEST_SECRET)]);
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("\"project_id\":\"PROJECT123\""));
    server.join().unwrap();
}

#[test]
fn mcp_tools_call_rejects_non_loopback_plaintext_cga_url() {
    let tmp = TestDir::new("mcp-remote-http");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("API_BASE_URL", "http://example.com:18001".to_string())],
    );
    let input = "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"query_impact_graph\",\"arguments\":{\"query\":\"scanner\"}}}\n";
    let output = run_mcp(&config, input, &[("CGA_TEST_API_KEY", TEST_SECRET)]);

    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("CRYSTALS/CNSA 2.0 policy"), "stdout: {out}");
    assert!(!out.contains(TEST_SECRET));
}

#[test]
fn mcp_accepts_content_length_framing() {
    let tmp = TestDir::new("mcp-content-length");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let body = "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"ping\"}";
    let framed = format!("Content-Length: {}\r\n\r\n{}", body.len(), body);
    let output = run_mcp(&config, &framed, &[]);
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert!(stdout(&output).contains("\"status\":\"ok\""));
}

#[test]
fn communication_logs_store_mcp_payload_metadata_only() {
    const SOURCE_MARKER: &str = "PRIVATE_SOURCE_MARKER_SHOULD_NEVER_LEAK";
    let tmp = TestDir::new("comm-log-redaction");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let input = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":44,\"method\":\"ping\",\"params\":{{\"query\":\"{}\",\"password\":\"{}\",\"access_token\":\"{}\",\"form\":\"username=dev&password={}&access_token={}\"}}}}\n",
        SOURCE_MARKER, TEST_SECRET, TEST_SECRET, TEST_SECRET, TEST_SECRET
    );

    let output = run_mcp(&config, &input, &[]);
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    assert!(stdout(&output).contains("\"status\":\"ok\""));

    let (log_names, log_text) = read_log_files(tmp.path());
    assert!(
        !log_names.is_empty(),
        "expected at least one communication log"
    );
    assert!(log_text.contains("mcp.stdin"));
    assert!(log_text.contains("mcp.stdout"));
    assert!(log_text.contains("bytes="));
    assert!(!log_text.contains("sha256="));
    assert!(!log_text.contains(SOURCE_MARKER));
    assert!(!log_text.contains(TEST_SECRET));
}

#[test]
fn malformed_http_responses_are_logged_as_metadata_only() {
    const RESPONSE_MARKER: &str = "PRIVATE_MALFORMED_RESPONSE_MARKER";
    let tmp = TestDir::new("malformed-response-log");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("source.txt"), "source\n").unwrap();
    let (port, server) = spawn_malformed_sync_server(RESPONSE_MARKER);
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();

    server.join().unwrap();
    assert!(!output.status.success());
    assert!(stderr(&output).contains("invalid HTTP response"));
    let (_, log_text) = read_log_files(tmp.path());
    assert!(log_text.contains("invalid_response_bytes="));
    assert!(!log_text.contains("invalid_response_sha256="));
    assert!(!log_text.contains("invalid_response:\n"));
    assert!(!log_text.contains(RESPONSE_MARKER));
}

#[test]
fn oversized_http_responses_are_rejected_with_metadata_only_logs() {
    let tmp = TestDir::new("oversized-response-log");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    fs::write(repo.join("source.txt"), "source\n").unwrap();
    let (port, server) = spawn_oversized_sync_response_server();
    let config = write_safe_config(
        tmp.path(),
        &repo,
        &[("CONTROL_API_BASE_URL", format!("http://127.0.0.1:{port}"))],
    );
    let login = Command::new(agent_bin())
        .args([
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();
    assert!(login.status.success(), "stderr: {}", stderr(&login));
    let add = run_agent(&[
        "projects",
        "add",
        "--config",
        config.to_str().unwrap(),
        "--project-tag",
        "repo",
        "--root",
        repo.to_str().unwrap(),
    ]);
    assert!(add.status.success(), "stderr: {}", stderr(&add));

    let output = Command::new(agent_bin())
        .args([
            "sync",
            "--config",
            config.to_str().unwrap(),
            "--all",
            "--json",
        ])
        .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
        .output()
        .unwrap();

    server.join().unwrap();
    assert!(!output.status.success());
    assert!(stderr(&output).contains("HTTP response exceeds 8388608-byte limit"));
    let (_, log_text) = read_log_files(tmp.path());
    assert!(log_text.contains("response_limit_bytes=8388608"));
    assert!(log_text.contains("response_prefix_bytes="));
    assert!(!log_text.contains("response_prefix_sha256="));
    assert!(!log_text.contains("xxxxxxxxxxxxxxxx"));
}

#[test]
fn stdout_stderr_never_contain_test_secret_values() {
    let tmp = TestDir::new("secret-redaction");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let commands = [
        vec!["doctor", "--config", config.to_str().unwrap(), "--json"],
        vec![
            "login",
            "--config",
            config.to_str().unwrap(),
            "--email",
            "dev@example.test",
            "--token-env",
            "CGA_TEST_DEVELOPER_TOKEN",
            "--json",
        ],
        vec![
            "scan",
            "--config",
            config.to_str().unwrap(),
            "--dry-run",
            "--json",
        ],
    ];
    let mut combined = String::new();
    for args in commands {
        let output = Command::new(agent_bin())
            .args(args)
            .env("CGA_TEST_API_KEY", TEST_SECRET)
            .env("CGA_TEST_DEVELOPER_TOKEN", TEST_SECRET)
            .output()
            .unwrap();
        assert!(output.status.success(), "stderr: {}", stderr(&output));
        combined.push_str(&stdout(&output));
        combined.push_str(&stderr(&output));
    }
    assert!(!combined.contains(TEST_SECRET));
}

#[test]
fn project_mcp_config_launches_new_agent_not_legacy_per_project_server() {
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let config_path = repo_root
        .join("docs")
        .join("examples")
        .join("cga-relay.mcp.json");
    let text = fs::read_to_string(config_path).expect("example MCP config should exist");
    assert!(text.contains("\"type\": \"stdio\""));
    assert!(!text.contains("\"transport\""));
    assert!(text.contains("cga-relay"));
    assert!(text.contains("\"mcp\""));
    assert!(text.contains("\"--config\""));
    for forbidden_launcher in ["python", "cargo", "powershell", "pwsh", ".ps1", ".py"] {
        assert!(
            !text.to_ascii_lowercase().contains(forbidden_launcher),
            "project MCP pointer must launch only the installed standalone agent: {text}"
        );
    }
    assert!(!text.contains("contextgraph-mcp"));
    assert!(!text.contains("/mcp/sse"));
    assert!(!text.contains(TEST_SECRET));
}

trait ReadExt {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize>;
}

impl ReadExt for TcpStream {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        std::io::Read::read(self, buf)
    }
}
