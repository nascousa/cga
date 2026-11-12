use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const TOKEN: &str = "CREDENTIAL_TEST_PRIVATE_VALUE";

struct Fixture {
    root: PathBuf,
    stop: Arc<AtomicBool>,
    server: Option<thread::JoinHandle<()>>,
}

impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "credential-sessions-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(root.join("state")).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let server = thread::spawn(move || {
            while !stopped.load(Ordering::Relaxed) {
                let Ok((mut stream, _)) = listener.accept() else {
                    thread::sleep(Duration::from_millis(5));
                    continue;
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut bytes = Vec::new();
                let mut buffer = [0; 4096];
                loop {
                    let read = stream.read(&mut buffer).unwrap();
                    if read == 0 {
                        break;
                    }
                    bytes.extend_from_slice(&buffer[..read]);
                    let request = String::from_utf8_lossy(&bytes);
                    if let Some(end) = request.find("\r\n\r\n") {
                        let length = request[..end]
                            .lines()
                            .find_map(|line| {
                                let (name, value) = line.split_once(':')?;
                                name.eq_ignore_ascii_case("content-length")
                                    .then(|| value.trim().parse::<usize>().unwrap())
                            })
                            .unwrap_or(0);
                        if bytes.len() >= end + 4 + length {
                            break;
                        }
                    }
                }
                let request = String::from_utf8_lossy(&bytes);
                let body = if request.starts_with("POST /api/auth/login ") {
                    format!("{{\"access_token\":\"{TOKEN}\"}}")
                } else if request.starts_with("GET /api/auth/me/groups ") {
                    "[]".into()
                } else {
                    "{\"username\":\"credential-user\",\"role\":\"developer\"}".into()
                };
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                )
                .unwrap();
            }
        });
        fs::write(root.join("config.env"), format!(
            "AGENT_ID=credential-test\nAPI_BASE_URL=http://{address}\nCONTROL_API_BASE_URL=http://{address}\nAPI_KEY_ENV=CGA_CREDENTIAL_UNUSED_KEY\nACCOUNT_EMAIL=credential@example.test\nACCOUNT_TOKEN_ENV=CGA_CREDENTIAL_UNUSED_TOKEN\nPROJECT_ID=PROJECT123\nPROJECT_ROOT={}\nSTATE_DIR={}\nLOG_DIR={}\nINCLUDE_GLOBS=**/*\nEXCLUDE_GLOBS=.git/**\nMAX_FILE_BYTES=1048576\n",
            root.display(), root.join("state").display(), root.join("logs").display()
        )).unwrap();
        Self {
            root,
            stop,
            server: Some(server),
        }
    }

    fn start(&self, mode: Option<&str>, missing_keyring: bool) -> Relay {
        let address_file = self.root.join("state").join("settings-url.txt");
        let _ = fs::remove_file(&address_file);
        let mut command = Command::new(env!("CARGO_BIN_EXE_cga-relay"));
        command
            .args(["serve", "--config"])
            .arg(self.root.join("config.env"))
            .env_remove("CGA_RELAY_CREDENTIAL_MODE")
            .env("CGA_RELAY_TEST_INSTANCE_SCOPE", self.root.as_os_str())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        if let Some(mode) = mode {
            command.env("CGA_RELAY_CREDENTIAL_MODE", mode);
        }
        if missing_keyring {
            command.env("PATH", self.root.join("no-secret-tool-here"));
        }
        let mut relay = Relay {
            child: command.spawn().unwrap(),
            authority: String::new(),
        };
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Ok(url) = fs::read_to_string(&address_file) {
                if !url.trim().is_empty() {
                    relay.authority = url
                        .trim()
                        .strip_prefix("http://")
                        .unwrap()
                        .split('/')
                        .next()
                        .unwrap()
                        .to_string();
                    break;
                }
            }
            if relay.child.try_wait().unwrap().is_some() {
                let mut error = String::new();
                relay
                    .child
                    .stderr
                    .take()
                    .unwrap()
                    .read_to_string(&mut error)
                    .unwrap();
                panic!("relay exited before readiness: {error}");
            }
            assert!(Instant::now() < deadline, "relay did not become ready");
            thread::sleep(Duration::from_millis(10));
        }
        relay
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.server.take().unwrap().join().unwrap();
        let _ = fs::remove_dir_all(&self.root);
    }
}

struct Relay {
    child: Child,
    authority: String,
}

impl Relay {
    fn request(&self, method: &str, path: &str, body: &str) -> String {
        let mut stream = TcpStream::connect(&self.authority).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        write!(stream, "{method} {path} HTTP/1.1\r\nHost: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", self.authority, body.len()).unwrap();
        let mut response = String::new();
        stream.read_to_string(&mut response).unwrap();
        assert!(response.starts_with("HTTP/1.1 200"), "{response}");
        assert!(
            !response.contains(TOKEN),
            "credential must not appear in local HTTP responses"
        );
        response
    }
}

impl Drop for Relay {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn session_only_login_read_logout_and_restart_never_persist_a_token() {
    let fixture = Fixture::new();
    let relay = fixture.start(Some("session"), true);
    assert!(relay
        .request("POST", "/login", "username=dev&password=test")
        .contains("Signed in as credential-user"));
    let path = fixture.root.join("state").join("account-session.json");
    let metadata = fs::read_to_string(&path).unwrap();
    assert!(metadata.contains("\"access_token_memory\":"));
    assert!(!metadata.contains(TOKEN));
    assert!(relay
        .request("GET", "/status.json", "")
        .contains("\"session_configured\":true"));
    assert!(relay.request("POST", "/logout", "").contains("Signed out."));
    assert!(!path.exists());
    assert!(relay
        .request("GET", "/status.json", "")
        .contains("\"session_configured\":false"));
    relay.request("POST", "/login", "username=dev&password=test");
    drop(relay);
    let restarted = fixture.start(Some("session"), true);
    assert!(restarted
        .request("GET", "/status.json", "")
        .contains("\"session_configured\":false"));
}

#[cfg(target_os = "linux")]
#[test]
#[ignore = "requires an isolated unlocked Secret Service session"]
fn native_secret_service_login_survives_restart_and_logout_deletes_credentials() {
    let fixture = Fixture::new();
    let relay = fixture.start(None, false);
    assert!(relay
        .request("POST", "/login", "username=dev&password=test")
        .contains("Signed in as credential-user"));
    let path = fixture.root.join("state").join("account-session.json");
    let metadata = fs::read_to_string(&path).unwrap();
    assert!(!metadata.contains(TOKEN));
    drop(relay);
    let restarted = fixture.start(None, false);
    assert!(restarted
        .request("GET", "/status.json", "")
        .contains("\"session_configured\":true"));
    assert!(restarted
        .request("POST", "/logout", "")
        .contains("Signed out."));
    assert!(!path.exists());
}

#[cfg(target_os = "linux")]
#[test]
fn missing_secret_service_does_not_create_a_plaintext_session() {
    let fixture = Fixture::new();
    let relay = fixture.start(None, true);
    let response = relay.request("POST", "/login", "username=dev&password=test");
    assert!(response.contains("cannot start secret-tool"));
    assert!(response.contains("CGA_RELAY_CREDENTIAL_MODE=session"));
    assert!(!fixture
        .root
        .join("state")
        .join("account-session.json")
        .exists());
    assert!(relay
        .request("GET", "/status.json", "")
        .contains("\"session_configured\":false"));
}
