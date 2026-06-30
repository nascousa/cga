use std::fs;
use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

const TEST_SECRET: &str = "TEST_SECRET_VALUE_SHOULD_NEVER_LEAK";

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

#[test]
fn help_output_lists_required_commands() {
    let output = run_agent(&["--help"]);
    assert!(output.status.success(), "stderr: {}", stderr(&output));
    let text = stdout(&output);
    for command in [
        "doctor", "login", "projects", "scan", "sync", "settings", "tray", "mcp",
    ] {
        assert!(text.contains(command), "help missing {command}: {text}");
    }
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
    let tmp = TestDir::new("tray-status");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
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
    assert!(out.contains("\"logged_in\":false"));
    assert!(out.contains("\"username\":\"\""));
    assert!(out.contains(
        "\"menu\":[\"Not signed in\",\"Open CGA Web\",\"Settings\",\"Logs\",\"About\",\"Exit\"]"
    ));
    assert!(out.contains("\"name\":\"CGA-Relay\""));
    assert!(out.contains("\"user_groups\":[]"));
    assert!(out.contains("\"user_group_count\":0"));
    assert!(!out.contains("\"project\":\"CGA-Relay\""));
    assert!(out.contains("\"author\":\"Nate Scott\""));
    assert!(out.contains("\"repository\":\"https://github.com/nascousa/cga\""));
    assert!(out.contains("\"support\":\"https://github.com/nascousa/cga/issues\""));
    assert!(out.contains("\"menu_events\":[\"WM_CONTEXTMENU\",\"WM_RBUTTONUP\",\"WM_TIMER\"]"));
    if cfg!(windows) {
        assert!(out.contains("\"supported\":true"));
        assert!(out.contains("\"icon_loaded\":true"));
        assert!(out.contains("windows-shell-notify-icon"));
    }
    assert!(!out.contains(TEST_SECRET));
    assert!(!stderr(&output).contains(TEST_SECRET));
}

#[test]
fn tray_status_uses_color_icon_and_username_when_signed_in() {
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
    let config = write_safe_config(tmp.path(), &repo, &[]);

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
        "\"menu\":[\"Signed in: dev@example.com\",\"Open CGA Web\",\"Settings\",\"Logs\",\"About\",\"Exit\"]"
    ));
    assert!(out.contains("\"user_groups\":[\"Team Alpha\"]"));
    assert!(out.contains("\"user_group_count\":1"));
    assert!(out.contains("signed in as dev@example.com"));
    assert!(!out.contains(TEST_SECRET));
    assert!(!stderr(&output).contains(TEST_SECRET));
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
    assert!(status_out.contains("\"projects_endpoint\":\"/api/auth/me/groups\""));
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
    ] {
        assert!(out.contains(tool), "tools/list missing {tool}: {out}");
    }
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
fn communication_logs_redact_sensitive_mcp_payloads() {
    let tmp = TestDir::new("comm-log-redaction");
    let repo = tmp.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    let config = write_safe_config(tmp.path(), &repo, &[]);
    let input = format!(
        "{{\"jsonrpc\":\"2.0\",\"id\":44,\"method\":\"ping\",\"params\":{{\"password\":\"{}\",\"access_token\":\"{}\",\"form\":\"username=dev&password={}&access_token={}\"}}}}\n",
        TEST_SECRET, TEST_SECRET, TEST_SECRET, TEST_SECRET
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
    assert!(log_text.contains("\"password\":\"<redacted>\""));
    assert!(log_text.contains("\"access_token\":\"<redacted>\""));
    assert!(log_text.contains("password=<redacted>"));
    assert!(log_text.contains("access_token=<redacted>"));
    assert!(!log_text.contains(TEST_SECRET));
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
    assert!(text.contains("\"transport\": \"stdio\""));
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
