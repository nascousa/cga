use super::{AgentError, AgentResult};
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Mutex, OnceLock};

const SESSION_MODE_ENV: &str = "CGA_RELAY_CREDENTIAL_MODE";
#[cfg(not(windows))]
const SERVICE: &str = "com.cga.relay.account";
static SESSION_TOKENS: OnceLock<Mutex<BTreeMap<String, String>>> = OnceLock::new();

enum CredentialMode {
    Native,
    Session,
}

fn parse_mode(value: Option<&str>) -> AgentResult<CredentialMode> {
    match value {
        None => Ok(CredentialMode::Native),
        Some("session") => Ok(CredentialMode::Session),
        _ => Err(AgentError(format!(
            "{SESSION_MODE_ENV} must be unset for OS-protected storage or exactly 'session' for process-memory-only login"
        ))),
    }
}

fn mode() -> AgentResult<CredentialMode> {
    match std::env::var(SESSION_MODE_ENV) {
        Ok(value) => parse_mode(Some(&value)),
        Err(std::env::VarError::NotPresent) => parse_mode(None),
        Err(_) => Err(AgentError(format!(
            "{SESSION_MODE_ENV} is not valid Unicode"
        ))),
    }
}

fn scope(path: &Path) -> AgentResult<String> {
    let parent = path
        .parent()
        .ok_or_else(|| AgentError("account session has no parent directory".into()))?;
    let parent = parent.canonicalize().map_err(|_| {
        AgentError("cannot resolve account session directory for credential storage".into())
    })?;
    let path = parent.join(
        path.file_name()
            .ok_or_else(|| AgentError("account session has no filename".into()))?,
    );
    // Encode the complete canonical path, rather than a lossy or collision-prone hash.
    let mut encoded = String::new();
    #[cfg(unix)]
    let bytes = {
        use std::os::unix::ffi::OsStrExt;
        path.as_os_str().as_bytes().to_vec()
    };
    #[cfg(windows)]
    let bytes = {
        use std::os::windows::ffi::OsStrExt;
        path.as_os_str()
            .encode_wide()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>()
    };
    #[cfg(not(any(unix, windows)))]
    let bytes = path.as_os_str().as_encoded_bytes().to_vec();
    for byte in bytes {
        use std::fmt::Write;
        let _ = write!(encoded, "{byte:02x}");
    }
    Ok(encoded)
}

fn memory_tokens() -> AgentResult<std::sync::MutexGuard<'static, BTreeMap<String, String>>> {
    SESSION_TOKENS
        .get_or_init(|| Mutex::new(BTreeMap::new()))
        .lock()
        .map_err(|_| {
            AgentError("session-only credential memory is unavailable; restart CGA Relay".into())
        })
}

#[cfg(not(windows))]
fn backend_error(message: impl std::fmt::Display) -> AgentError {
    AgentError(format!(
        "{message}; unlock/configure the OS credential store and retry, or explicitly set {SESSION_MODE_ENV}=session before starting CGA Relay for process-memory-only login"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session_path(name: &str) -> std::path::PathBuf {
        std::env::current_dir()
            .unwrap()
            .join(format!("{name}-account-session.json"))
    }

    #[test]
    fn memory_credentials_are_scoped_readable_and_deleted_without_disk_secrets() {
        let first = session_path("credential-memory-first");
        let second = session_path("credential-memory-second");
        let persisted =
            protect_with_mode(&first, "private-token", CredentialMode::Session).unwrap();
        assert_eq!(persisted.field, "access_token_memory");
        assert!(!persisted.value.contains("private-token"));
        assert_eq!(
            load_access_token(&first, persisted.field, &persisted.value).unwrap(),
            Some("private-token".to_string())
        );
        assert!(load_access_token(&second, persisted.field, &persisted.value).is_err());
        delete_access_token(&first, persisted.field, &persisted.value).unwrap();
        assert_eq!(
            load_access_token(&first, persisted.field, &persisted.value).unwrap(),
            None
        );
        delete_access_token(&first, persisted.field, &persisted.value).unwrap();
        assert!(!first.exists());
    }

    #[test]
    fn distinct_session_paths_do_not_replace_each_other() {
        let first = session_path("credential-scope-first");
        let second = session_path("credential-scope-second");
        let a = protect_with_mode(&first, "first", CredentialMode::Session).unwrap();
        let b = protect_with_mode(&second, "second", CredentialMode::Session).unwrap();
        assert_ne!(a.value, b.value);
        delete_access_token(&first, a.field, &a.value).unwrap();
        assert_eq!(
            load_access_token(&second, b.field, &b.value).unwrap(),
            Some("second".into())
        );
        delete_access_token(&second, b.field, &b.value).unwrap();
    }

    #[test]
    fn empty_tokens_and_unknown_storage_markers_fail_closed() {
        let path = session_path("credential-invalid");
        assert!(protect_with_mode(&path, "", CredentialMode::Session).is_err());
        assert!(load_access_token(&path, "access_token_unknown", "not-a-token").is_err());
        assert!(delete_access_token(&path, "access_token_memory", "another-scope").is_err());
    }

    #[test]
    fn session_mode_requires_explicit_recognized_opt_in() {
        assert!(matches!(parse_mode(None).unwrap(), CredentialMode::Native));
        assert!(matches!(
            parse_mode(Some("session")).unwrap(),
            CredentialMode::Session
        ));
        assert!(parse_mode(Some("plaintext")).is_err());
        assert!(parse_mode(Some("")).is_err());
    }
}

pub(crate) struct PersistedAccessToken {
    pub(crate) field: &'static str,
    pub(crate) value: String,
}

pub(crate) fn protect_access_token(
    path: &Path,
    access_token: &str,
) -> AgentResult<PersistedAccessToken> {
    protect_with_mode(path, access_token, mode()?)
}

fn protect_with_mode(
    path: &Path,
    access_token: &str,
    mode: CredentialMode,
) -> AgentResult<PersistedAccessToken> {
    if access_token.trim().is_empty() {
        return Err(AgentError("CGA access token is empty".into()));
    }
    if matches!(mode, CredentialMode::Session) {
        let value = scope(path)?;
        memory_tokens()?.insert(value.clone(), access_token.to_string());
        return Ok(PersistedAccessToken {
            field: "access_token_memory",
            value,
        });
    }
    #[cfg(windows)]
    {
        windows_dpapi::protect(access_token).map(|value| PersistedAccessToken {
            field: "access_token_dpapi",
            value,
        })
    }
    #[cfg(not(windows))]
    {
        let value = scope(path)?;
        native::store(&value, access_token)?;
        Ok(PersistedAccessToken {
            field: native::FIELD,
            value,
        })
    }
}

#[cfg(test)]
pub(crate) fn protect_session_for_test(
    path: &Path,
    token: &str,
) -> AgentResult<PersistedAccessToken> {
    protect_with_mode(path, token, CredentialMode::Session)
}

pub(crate) fn load_access_token(
    path: &Path,
    field: &str,
    value: &str,
) -> AgentResult<Option<String>> {
    if field == "access_token_memory" {
        validate_scope(path, value)?;
        // A new process must not recover a previous process's session-only token.
        return Ok(memory_tokens()?.get(value).cloned());
    }
    if matches!(mode()?, CredentialMode::Session) {
        // Explicit session mode never opens an existing durable credential store.
        return Ok(None);
    }
    #[cfg(windows)]
    if field == "access_token_dpapi" {
        return windows_dpapi::unprotect(value).map(Some);
    }
    #[cfg(not(windows))]
    if field == native::FIELD {
        validate_scope(path, value)?;
        return native::load(value).map(Some);
    }
    Err(AgentError("account session uses an unsupported credential backend; sign out and sign in again on this machine".into()))
}

pub(crate) fn delete_access_token(path: &Path, field: &str, value: &str) -> AgentResult<()> {
    if field == "access_token_memory" {
        validate_scope(path, value)?;
        memory_tokens()?.remove(value);
        return Ok(());
    }
    #[cfg(not(windows))]
    if field == native::FIELD {
        validate_scope(path, value)?;
        return native::delete(value);
    }
    // DPAPI and foreign-platform references have no local external credential to delete.
    if matches!(
        field,
        "access_token_dpapi" | "access_token_keychain" | "access_token_secret_service"
    ) {
        return Ok(());
    }
    Err(AgentError(
        "account session uses an unknown credential backend".into(),
    ))
}

fn validate_scope(path: &Path, value: &str) -> AgentResult<()> {
    if scope(path)? != value {
        return Err(AgentError(
            "account credential belongs to a different session path; sign out and sign in again"
                .into(),
        ));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
mod native {
    use super::*;
    use std::io::Write;
    use std::process::{Command, Stdio};

    pub(super) const FIELD: &str = "access_token_secret_service";

    fn command(operation: &str, account: &str) -> Command {
        let mut command = Command::new("secret-tool");
        command.arg(operation);
        if operation == "store" {
            command.arg("--label=CGA Relay account");
        }
        command.args(["service", SERVICE, "account", account]);
        command
    }

    pub(super) fn store(account: &str, token: &str) -> AgentResult<()> {
        store_with_command(command("store", account), token)
    }

    fn store_with_command(mut command: Command, token: &str) -> AgentResult<()> {
        let mut child = command
            .stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn().map_err(|_| backend_error("cannot start secret-tool; install libsecret's secret-tool and a Secret Service provider"))?;
        let written = child
            .stdin
            .take()
            .ok_or_else(|| backend_error("secret-tool stdin is unavailable"))
            .and_then(|mut stdin| {
                stdin
                    .write_all(token.as_bytes())
                    .map_err(|_| backend_error("cannot send credential to Secret Service"))
            });
        if written.is_err() {
            let _ = child.kill();
        }
        let status = child
            .wait()
            .map_err(|_| backend_error("cannot wait for Secret Service credential storage"))?;
        written?;
        if !status.success() {
            return Err(backend_error("Secret Service rejected credential storage"));
        }
        Ok(())
    }

    pub(super) fn load(account: &str) -> AgentResult<String> {
        load_with_command(command("lookup", account))
    }

    fn load_with_command(mut command: Command) -> AgentResult<String> {
        let output = command
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .map_err(|_| {
                backend_error("cannot start secret-tool; install libsecret's secret-tool")
            })?;
        if !output.status.success() {
            return Err(backend_error("Secret Service credential lookup failed or the credential is missing; sign out and sign in again"));
        }
        let token = String::from_utf8(output.stdout)
            .map_err(|_| backend_error("Secret Service returned an invalid credential"))?;
        let token = token.strip_suffix('\n').unwrap_or(&token).to_string();
        if token.trim().is_empty() {
            return Err(backend_error(
                "Secret Service returned an empty credential; sign out and sign in again",
            ));
        }
        Ok(token)
    }

    pub(super) fn delete(account: &str) -> AgentResult<()> {
        delete_with_command(command("clear", account))
    }

    fn delete_with_command(mut command: Command) -> AgentResult<()> {
        let status = command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|_| backend_error("cannot start secret-tool to delete account credential"))?;
        if !status.success() {
            return Err(backend_error("Secret Service credential deletion failed"));
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn secret_tool_arguments_never_contain_the_token() {
            let command = command("store", "session-scope");
            let args = command
                .get_args()
                .map(|arg| arg.to_str().unwrap())
                .collect::<Vec<_>>();
            assert_eq!(
                args,
                [
                    "store",
                    "--label=CGA Relay account",
                    "service",
                    SERVICE,
                    "account",
                    "session-scope"
                ]
            );
        }

        fn shell(script: &str) -> Command {
            let mut command = Command::new("sh");
            command.args(["-c", script]);
            command
        }

        #[test]
        fn secret_service_accepts_token_on_stdin_and_reads_its_output() {
            store_with_command(
                shell("value=$(cat); test \"$value\" = credential-test-value"),
                "credential-test-value",
            )
            .unwrap();
            assert_eq!(
                load_with_command(shell("printf 'credential-test-value\\n'")).unwrap(),
                "credential-test-value"
            );
            delete_with_command(shell("exit 0")).unwrap();
        }

        #[test]
        fn missing_secret_tool_and_backend_failures_are_explicit_and_redacted() {
            let error = store_with_command(
                Command::new("cga-relay-missing-secret-tool-test"),
                "credential-test-value",
            )
            .unwrap_err();
            assert!(error.0.contains("install"));
            assert!(error.0.contains("CGA_RELAY_CREDENTIAL_MODE=session"));
            let error =
                store_with_command(shell("cat >&2; exit 23"), "credential-test-value").unwrap_err();
            assert!(!error.0.contains("credential-test-value"));
            assert!(load_with_command(shell("printf 'credential-test-value'; exit 23")).is_err());
            assert!(load_with_command(shell("exit 0")).is_err());
            assert!(delete_with_command(shell("exit 23")).is_err());
        }
    }
}

#[cfg(target_os = "macos")]
mod native {
    use super::*;
    use std::ffi::c_void;
    use std::ptr;

    pub(super) const FIELD: &str = "access_token_keychain";
    const ITEM_NOT_FOUND: i32 = -25300;
    type Item = *mut c_void;

    #[link(name = "Security", kind = "framework")]
    unsafe extern "C" {
        fn SecKeychainFindGenericPassword(
            keychain: *const c_void,
            service_len: u32,
            service: *const u8,
            account_len: u32,
            account: *const u8,
            length: *mut u32,
            data: *mut *mut c_void,
            item: *mut Item,
        ) -> i32;
        fn SecKeychainAddGenericPassword(
            keychain: *const c_void,
            service_len: u32,
            service: *const u8,
            account_len: u32,
            account: *const u8,
            length: u32,
            data: *const c_void,
            item: *mut Item,
        ) -> i32;
        fn SecKeychainItemModifyAttributesAndData(
            item: Item,
            attributes: *const c_void,
            length: u32,
            data: *const c_void,
        ) -> i32;
        fn SecKeychainItemFreeContent(attributes: *mut c_void, data: *mut c_void) -> i32;
        fn SecKeychainItemDelete(item: Item) -> i32;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        fn CFRelease(value: *const c_void);
    }

    fn check(status: i32, operation: &str) -> AgentResult<()> {
        if status != 0 {
            return Err(backend_error(format!(
                "macOS Keychain {operation} failed (OSStatus {status})"
            )));
        }
        Ok(())
    }

    fn find(account: &str, length: *mut u32, data: *mut *mut c_void, item: *mut Item) -> i32 {
        unsafe {
            SecKeychainFindGenericPassword(
                ptr::null(),
                SERVICE.len() as u32,
                SERVICE.as_ptr(),
                account.len() as u32,
                account.as_ptr(),
                length,
                data,
                item,
            )
        }
    }

    pub(super) fn store(account: &str, token: &str) -> AgentResult<()> {
        let length = u32::try_from(token.len())
            .map_err(|_| backend_error("CGA access token is too large"))?;
        let mut item = ptr::null_mut();
        let status = find(account, ptr::null_mut(), ptr::null_mut(), &mut item);
        let result = if status == ITEM_NOT_FOUND {
            unsafe {
                SecKeychainAddGenericPassword(
                    ptr::null(),
                    SERVICE.len() as u32,
                    SERVICE.as_ptr(),
                    account.len() as u32,
                    account.as_ptr(),
                    length,
                    token.as_ptr().cast(),
                    ptr::null_mut(),
                )
            }
        } else {
            check(status, "lookup")?;
            unsafe {
                SecKeychainItemModifyAttributesAndData(
                    item,
                    ptr::null(),
                    length,
                    token.as_ptr().cast(),
                )
            }
        };
        if !item.is_null() {
            unsafe {
                CFRelease(item);
            }
        }
        check(result, "storage")
    }

    pub(super) fn load(account: &str) -> AgentResult<String> {
        let mut length = 0;
        let mut data = ptr::null_mut();
        check(
            find(account, &mut length, &mut data, ptr::null_mut()),
            "lookup",
        )?;
        let bytes = if length == 0 {
            &[][..]
        } else {
            unsafe { std::slice::from_raw_parts(data.cast::<u8>(), length as usize) }
        };
        let result = std::str::from_utf8(bytes)
            .map(str::to_owned)
            .map_err(|_| backend_error("macOS Keychain returned an invalid credential"));
        unsafe {
            SecKeychainItemFreeContent(ptr::null_mut(), data);
        }
        let token = result?;
        if token.trim().is_empty() {
            return Err(backend_error("macOS Keychain returned an empty credential"));
        }
        Ok(token)
    }

    pub(super) fn delete(account: &str) -> AgentResult<()> {
        let mut item = ptr::null_mut();
        let status = find(account, ptr::null_mut(), ptr::null_mut(), &mut item);
        if status == ITEM_NOT_FOUND {
            return Ok(());
        }
        check(status, "lookup for deletion")?;
        let status = unsafe { SecKeychainItemDelete(item) };
        unsafe {
            CFRelease(item);
        }
        check(status, "deletion")
    }
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
mod native {
    use super::*;
    pub(super) const FIELD: &str = "access_token_unsupported";
    pub(super) fn store(_: &str, _: &str) -> AgentResult<()> {
        Err(backend_error(
            "this platform has no supported credential store",
        ))
    }
    pub(super) fn load(_: &str) -> AgentResult<String> {
        Err(backend_error(
            "this platform has no supported credential store",
        ))
    }
    pub(super) fn delete(_: &str) -> AgentResult<()> {
        Err(backend_error(
            "this platform has no supported credential store",
        ))
    }
}

#[cfg(windows)]
mod windows_dpapi {
    use super::{AgentError, AgentResult};
    use std::ffi::c_void;
    use std::ptr;

    const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x1;

    #[repr(C)]
    struct DataBlob {
        length: u32,
        data: *mut u8,
    }

    #[link(name = "crypt32")]
    unsafe extern "system" {
        fn CryptProtectData(
            input: *mut DataBlob,
            description: *const u16,
            optional_entropy: *mut DataBlob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            output: *mut DataBlob,
        ) -> i32;

        fn CryptUnprotectData(
            input: *mut DataBlob,
            description: *mut *mut u16,
            optional_entropy: *mut DataBlob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            output: *mut DataBlob,
        ) -> i32;
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn LocalFree(memory: *mut c_void) -> *mut c_void;
    }

    pub(super) fn protect(access_token: &str) -> AgentResult<String> {
        if access_token.is_empty() {
            return Err(AgentError("CGA access token is empty".to_string()));
        }
        let length = u32::try_from(access_token.len())
            .map_err(|_| AgentError("CGA access token is too large".to_string()))?;
        let mut input = DataBlob {
            length,
            data: access_token.as_ptr().cast_mut(),
        };
        let mut output = DataBlob {
            length: 0,
            data: ptr::null_mut(),
        };
        let succeeded = unsafe {
            CryptProtectData(
                &mut input,
                ptr::null(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut output,
            )
        };
        if succeeded == 0 {
            return Err(AgentError(format!(
                "cannot protect CGA account session with Windows DPAPI: {}",
                std::io::Error::last_os_error()
            )));
        }

        let protected = unsafe { std::slice::from_raw_parts(output.data, output.length as usize) };
        let encoded = encode_hex(protected);
        unsafe {
            let _ = LocalFree(output.data.cast());
        }
        Ok(encoded)
    }

    pub(super) fn unprotect(encoded: &str) -> AgentResult<String> {
        let mut protected = decode_hex(encoded)?;
        let length = u32::try_from(protected.len())
            .map_err(|_| AgentError("protected CGA access token is too large".to_string()))?;
        let mut input = DataBlob {
            length,
            data: protected.as_mut_ptr(),
        };
        let mut output = DataBlob {
            length: 0,
            data: ptr::null_mut(),
        };
        let succeeded = unsafe {
            CryptUnprotectData(
                &mut input,
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut output,
            )
        };
        if succeeded == 0 {
            return Err(AgentError(format!(
                "cannot open CGA account session with Windows DPAPI: {}",
                std::io::Error::last_os_error()
            )));
        }

        let plaintext = unsafe { std::slice::from_raw_parts(output.data, output.length as usize) };
        let access_token = std::str::from_utf8(plaintext)
            .map(str::to_owned)
            .map_err(|_| AgentError("protected CGA access token is not UTF-8".to_string()));
        unsafe {
            ptr::write_bytes(output.data, 0, output.length as usize);
            let _ = LocalFree(output.data.cast());
        }
        access_token
    }

    fn encode_hex(bytes: &[u8]) -> String {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut encoded = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            encoded.push(HEX[(byte >> 4) as usize] as char);
            encoded.push(HEX[(byte & 0x0f) as usize] as char);
        }
        encoded
    }

    fn decode_hex(encoded: &str) -> AgentResult<Vec<u8>> {
        if !encoded.len().is_multiple_of(2) {
            return Err(AgentError(
                "protected CGA access token has invalid encoding".to_string(),
            ));
        }
        encoded
            .as_bytes()
            .chunks_exact(2)
            .map(|pair| {
                let high = decode_nibble(pair[0])?;
                let low = decode_nibble(pair[1])?;
                Ok((high << 4) | low)
            })
            .collect()
    }

    fn decode_nibble(value: u8) -> AgentResult<u8> {
        match value {
            b'0'..=b'9' => Ok(value - b'0'),
            b'a'..=b'f' => Ok(value - b'a' + 10),
            b'A'..=b'F' => Ok(value - b'A' + 10),
            _ => Err(AgentError(
                "protected CGA access token has invalid encoding".to_string(),
            )),
        }
    }

    #[cfg(test)]
    mod tests {
        use super::{protect, unprotect};

        #[test]
        fn dpapi_round_trip_does_not_expose_plaintext() {
            let plaintext = "DPAPI_TEST_TOKEN_VALUE";
            let protected = protect(plaintext).expect("token should be protected");

            assert!(!protected.contains(plaintext));
            assert_eq!(unprotect(&protected).unwrap(), plaintext);
        }
    }
}
