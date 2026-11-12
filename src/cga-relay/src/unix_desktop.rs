//! Native UI helpers are children of the lock-owning Relay process, not daemons.
//! stdin carries public status only; stdout carries a small allowlist of actions.
#![cfg_attr(not(target_os = "linux"), allow(dead_code))]
use crate::*;
use std::io::BufReader;
use std::process::{Child, ChildStdin, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::Instant;

#[cfg(target_os = "linux")]
const LINUX_HELPER: &str = include_str!("../desktop/linux/tray.py");

#[derive(Debug, PartialEq, Eq)]
enum Action {
    Ready,
    Login,
    Logout,
    Web,
    Settings,
    Logs,
    Relaunch,
    Exit,
}

fn action(line: &str) -> AgentResult<Action> {
    match line.trim_end_matches(['\r', '\n']) {
        "ready" => Ok(Action::Ready),
        "login" => Ok(Action::Login),
        "logout" => Ok(Action::Logout),
        "web" => Ok(Action::Web),
        "settings" => Ok(Action::Settings),
        "logs" => Ok(Action::Logs),
        "relaunch" => Ok(Action::Relaunch),
        "exit" => Ok(Action::Exit),
        _ => Err(AgentError("invalid native desktop helper action".into())),
    }
}

pub fn mode() -> &'static str {
    "linux-appindicator"
}

fn account_status(config: &AgentConfig) -> (TrayLoginStatus, String) {
    let session = match read_account_session(config) {
        Ok(session) => session,
        Err(error) => {
            return (
                TrayLoginStatus {
                    logged_in: false,
                    username: String::new(),
                },
                error.0,
            )
        }
    };
    let logged_in = current_account_access_token(&session).is_some();
    let username = if logged_in {
        session
            .get("username")
            .map(|name| name.trim())
            .filter(|name| !name.is_empty())
            .unwrap_or("CGA account")
            .to_string()
    } else {
        String::new()
    };
    (
        TrayLoginStatus {
            logged_in,
            username,
        },
        String::new(),
    )
}

pub fn status_json(config: &AgentConfig) -> String {
    let (login, credential_error) = account_status(config);
    let backend = tray_backend_available(config);
    let groups = tray_user_group_labels(config, &login);
    format!(
        "{{\"supported\":true,\"mode\":\"{}\",\"runtime_check\":\"performed-on-launch\",\"agent_id\":\"{}\",\"project_id\":\"{}\",\"logged_in\":{},\"username\":\"{}\",\"backend_available\":{},\"tooltip\":\"{}\",\"icon\":\"native-status-item\",\"icon_variant\":\"{}\",\"credential_error\":\"{}\",\"menu\":[\"{}\",\"{}\",\"Open CGA Web\",\"Settings\",\"Logs\",\"About\",\"Relaunch\",\"Exit\"],\"menu_events\":[\"helper-stdio\"],\"notification_title\":\"{}\",\"notification_message\":\"{}\",\"about\":{{\"name\":\"{}\",\"user_groups\":{},\"user_group_count\":{},\"author\":\"{}\",\"repository\":\"{}\",\"support\":\"{}\",\"license\":\"{}\"}}}}",
        mode(), json_escape(&config.agent_id), json_escape(&config.project_id),
        login.logged_in, json_escape(&login.username), backend,
        json_escape(&tray_tooltip(config, &login, backend)),
        tray_icon_variant(&login, backend), json_escape(&credential_error), json_escape(&tray_login_menu_label(&login)),
        if login.logged_in { "Sign out" } else { "Sign in..." },
        json_escape(BACKEND_NOTIFICATION_TITLE), json_escape(BACKEND_NOTIFICATION_MESSAGE),
        json_escape(PROJECT_DISPLAY_NAME), string_array_json(&groups), groups.len(),
        json_escape(PROJECT_AUTHOR), json_escape(PROJECT_REPOSITORY), json_escape(PROJECT_SUPPORT),
        json_escape(PROJECT_LICENSE),
    )
}

// Keeping the pipes and child together ensures all error paths terminate the UI.
struct Helper {
    child: Child,
    input: Option<ChildStdin>,
}

impl Drop for Helper {
    fn drop(&mut self) {
        self.input.take();
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if self.child.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(25));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn helper_command() -> AgentResult<Command> {
    #[cfg(target_os = "linux")]
    {
        if env::var_os("DISPLAY").is_none() && env::var_os("WAYLAND_DISPLAY").is_none() {
            return Err(AgentError("Linux tray requires a graphical desktop session (DISPLAY or WAYLAND_DISPLAY); use 'serve' on a headless host.".into()));
        }
        // Distribution GI packages belong to system Python, not an activated venv.
        let python = env::var_os("CGA_RELAY_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("/usr/bin/python3"));
        if !python.is_absolute() || !python.is_file() {
            return Err(AgentError("Linux tray requires system /usr/bin/python3 or an absolute CGA_RELAY_PYTHON executable path with distribution PyGObject/GTK3 packages; install python3 and python3-gi.".into()));
        }
        let mut command = Command::new(&python);
        command.args(["-u", "-c", LINUX_HELPER]);
        return Ok(command);
    }
    #[cfg(not(target_os = "linux"))]
    Err(AgentError(
        "native desktop helper is unsupported on this platform".into(),
    ))
}

fn status_message(config: &AgentConfig, backend: bool, error: &str) -> String {
    let (login, credential_error) = account_status(config);
    let about = format!(
        "{PROJECT_DISPLAY_NAME}\nVersion: {VERSION}\nAuthor: {PROJECT_AUTHOR}\nRepository: {PROJECT_REPOSITORY}\nSupport: {PROJECT_SUPPORT}\nLicense: {PROJECT_LICENSE}\n\nRelay: {}\nAccount: {}\nUser groups: {}",
        config.agent_id, tray_login_menu_label(&login), tray_user_group_summary(config, &login),
    );
    let values = [
        (
            "account",
            json::Value::String(tray_login_menu_label(&login)),
        ),
        ("logged_in", json::Value::Bool(login.logged_in)),
        ("backend", json::Value::Bool(backend)),
        (
            "tooltip",
            json::Value::String(tray_tooltip(config, &login, backend)),
        ),
        ("about", json::Value::String(about)),
        (
            "error",
            json::Value::String(if error.is_empty() {
                credential_error
            } else {
                error.to_string()
            }),
        ),
    ];
    json::Value::Object(
        values
            .into_iter()
            .map(|(key, value)| (key.into(), value))
            .collect(),
    )
    .encode()
}

fn send_status(
    helper: &mut Helper,
    config: &AgentConfig,
    backend: bool,
    error: &str,
) -> AgentResult<()> {
    let input = helper
        .input
        .as_mut()
        .ok_or_else(|| AgentError("desktop helper input closed".into()))?;
    writeln!(input, "{}", status_message(config, backend, error))
        .and_then(|_| input.flush())
        .map_err(|e| AgentError(format!("cannot update native desktop helper: {e}")))
}

fn open_target(target: &std::ffi::OsStr) -> AgentResult<()> {
    let program = "xdg-open";
    // Wait/reap instead of leaving zombie opener children behind.
    let result = Command::new(program)
        .arg(target)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .status()
        .map_err(|e| {
            AgentError(format!(
                "cannot launch {program}: {e}; install a desktop URL/file opener"
            ))
        })?;
    if result.success() {
        Ok(())
    } else {
        Err(AgentError(format!(
            "{program} could not open the requested desktop target ({result})"
        )))
    }
}

pub fn run(config: &AgentConfig, settings_url: &str) -> AgentResult<()> {
    let mut command = helper_command()?;
    // The UI does not need even credentials supplied through environment variables.
    command
        .env_remove(&config.api_key_env)
        .env_remove(&config.account_token_env);
    let mut child = command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit())
        .spawn().map_err(|e| AgentError(format!(
            "cannot start {} helper: {e}. Linux requires system python3, PyGObject, GTK3 and Ayatana/AppIndicator3.",
            mode()
        )))?;
    let input = child.stdin.take();
    let mut helper = Helper { child, input };
    let output = helper
        .child
        .stdout
        .take()
        .ok_or_else(|| AgentError("native helper stdout unavailable".into()))?;
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        for line in BufReader::new(output).lines() {
            match line {
                Ok(line) if line.len() <= 80 => {
                    if sender.send(action(&line)).is_err() {
                        break;
                    }
                }
                _ => {
                    let _ = sender.send(Err(AgentError("native helper IPC read failed".into())));
                    break;
                }
            }
        }
    });
    match receiver.recv_timeout(Duration::from_secs(60)) {
        Ok(Ok(Action::Ready)) => {}
        _ => return Err(AgentError("native desktop helper failed to initialize; see its dependency/display error above. No tray or daemon was started.".into())),
    }
    let mut backend = false;
    let mut error = String::new();
    send_status(&mut helper, config, backend, &error)?;
    let (health_sender, health_receiver) = mpsc::channel();
    let (open_sender, open_receiver) = mpsc::channel();
    let mut open_pending = false;
    let mut next_health = Instant::now();
    let mut health_pending = false;
    let mut next_status = Instant::now();
    loop {
        if let Some(status) = helper
            .child
            .try_wait()
            .map_err(|e| AgentError(format!("cannot inspect desktop helper: {e}")))?
        {
            return Err(AgentError(format!(
                "native desktop helper exited unexpectedly ({status}); Relay is stopping"
            )));
        }
        if !health_pending && Instant::now() >= next_health {
            let sender = health_sender.clone();
            let config = config.clone();
            health_pending = true;
            thread::spawn(move || {
                let _ = sender.send(tray_backend_available(&config));
            });
        }
        if let Ok(available) = health_receiver.try_recv() {
            backend = available;
            health_pending = false;
            next_health = Instant::now() + Duration::from_secs(5);
            next_status = Instant::now();
        }
        if let Ok(result) = open_receiver.try_recv() {
            open_pending = false;
            error = result;
            next_status = Instant::now();
        }
        if Instant::now() >= next_status {
            send_status(&mut helper, config, backend, &error)?;
            next_status = Instant::now() + Duration::from_secs(2);
        }
        let event = match receiver.recv_timeout(Duration::from_millis(200)) {
            Ok(event) => event?,
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => {
                return Err(AgentError(
                    "native desktop helper disconnected; Relay is stopping".into(),
                ))
            }
        };
        let target = match event {
            Action::Login | Action::Settings => Some(std::ffi::OsString::from(settings_url)),
            Action::Web => Some(std::ffi::OsString::from(cga_admin_web_url(config))),
            Action::Logs => Some(config.log_dir.clone().into_os_string()),
            _ => None,
        };
        if let Some(target) = target {
            if !open_pending {
                let sender = open_sender.clone();
                open_pending = true;
                // Some desktop openers remain alive with the browser. Do not block Exit.
                thread::spawn(move || {
                    let result = open_target(&target).err().map(|e| e.0).unwrap_or_default();
                    let _ = sender.send(result);
                });
            }
            continue;
        }
        let result = match event {
            Action::Ready => Err(AgentError("duplicate desktop helper initialization".into())),
            Action::Logout => clear_account_session(config),
            Action::Login | Action::Settings | Action::Web | Action::Logs => unreachable!(),
            Action::Exit => return Ok(()),
            Action::Relaunch => {
                let executable = env::current_exe()
                    .map_err(|e| AgentError(format!("cannot locate Relay for relaunch: {e}")))?;
                let arguments: Vec<_> = env::args_os()
                    .skip(1)
                    .filter(|arg| arg != "--relaunch")
                    .collect();
                match Command::new(executable)
                    .args(arguments)
                    .arg("--relaunch")
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::inherit())
                    .spawn()
                {
                    Ok(_) => return Ok(()),
                    Err(e) => Err(AgentError(format!("cannot relaunch Relay: {e}"))),
                }
            }
        };
        error = result.err().map(|e| e.0).unwrap_or_default();
        next_status = Instant::now();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn helper_actions_are_an_exact_allowlist() {
        for (command, expected) in [
            ("ready", Action::Ready),
            ("login", Action::Login),
            ("logout\r\n", Action::Logout),
            ("web", Action::Web),
            ("settings", Action::Settings),
            ("logs", Action::Logs),
            ("relaunch", Action::Relaunch),
            ("exit", Action::Exit),
        ] {
            assert_eq!(action(command).unwrap(), expected);
        }
        assert!(action("logout --token=secret").is_err());
        assert!(action(" exit").is_err());
        assert!(action("web\nexit").is_err());
    }
}
