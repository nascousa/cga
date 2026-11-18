use super::{AgentError, AgentResult};
use crate::json::Value;
use std::collections::BTreeMap;

fn diagnostics(resource: &str, kind: &str, owner: Option<Value>, status: &str) -> String {
    let held_ms = match owner.as_ref() {
        Some(Value::Object(fields)) => fields.get("acquired_at_unix_ms").and_then(|value| {
            let Value::Number(value) = value else {
                return None;
            };
            let acquired = value.parse::<u128>().ok()?;
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .ok()?
                .as_millis();
            now.checked_sub(acquired)
                .map(|elapsed| Value::Number(elapsed.to_string()))
        }),
        _ => None,
    };
    Value::Object(BTreeMap::from([
        (
            "code".into(),
            Value::String("CGA_RELAY_ALREADY_RUNNING".into()),
        ),
        ("lock_resource".into(), Value::String(resource.into())),
        ("lock_type".into(), Value::String(kind.into())),
        ("owner".into(), owner.unwrap_or(Value::Null)),
        ("held_ms".into(), held_ms.unwrap_or(Value::Null)),
        ("owner_metadata_status".into(), Value::String(status.into())),
        ("process_started_at".into(), Value::Null),
        (
            "process_start_status".into(),
            Value::String("Not tracked; a recorded PID is not proof of liveness.".into()),
        ),
        ("heartbeat_at".into(), Value::Null),
        (
            "heartbeat_status".into(),
            Value::String("Not used; the OS lock is authoritative.".into()),
        ),
        ("expires_at".into(), Value::Null),
        (
            "expiry_policy".into(),
            Value::String(
                "No TTL; released when the OS guard closes. No lock is removed by a contender."
                    .into(),
            ),
        ),
    ]))
    .encode()
}

#[cfg(windows)]
const INSTANCE_NAME: &str = "Global\\Nascousa.CGA-Relay.SingleInstance";
#[cfg(not(windows))]
const INSTANCE_NAME: &str = "Nascousa.CGA-Relay.SingleInstance";

pub(crate) struct SingleInstanceGuard {
    _platform: platform::Guard,
}

pub(crate) fn acquire() -> AgentResult<SingleInstanceGuard> {
    let name = instance_name();
    match platform::try_acquire(&name) {
        Ok(Some(guard)) => Ok(SingleInstanceGuard { _platform: guard }),
        Ok(None) => Err(AgentError(format!(
            "CGA-Relay is already running; use the existing instance instead of starting another process. \
             No lock was removed. Lock diagnostics: {}",
            platform::contention_details(&name),
        ))),
        Err(error) => Err(AgentError(format!(
            "failed to acquire the CGA-Relay single-instance mutex {:?}: {error}",
            platform::resource(&name),
        ))),
    }
}

fn instance_name() -> String {
    #[cfg(debug_assertions)]
    if let Some(scope) = std::env::var_os("CGA_RELAY_TEST_INSTANCE_SCOPE") {
        use std::hash::{DefaultHasher, Hash, Hasher};

        let mut hasher = DefaultHasher::new();
        scope.hash(&mut hasher);
        return format!("{INSTANCE_NAME}.test.{:016x}", hasher.finish());
    }

    INSTANCE_NAME.to_string()
}

#[cfg(windows)]
mod platform {
    use std::ffi::c_void;
    use std::io;
    use std::ptr;

    const ERROR_ALREADY_EXISTS: u32 = 183;

    type Handle = *mut c_void;

    pub(super) struct Guard {
        handle: Handle,
    }

    pub(super) fn resource(name: &str) -> String {
        name.to_string()
    }

    pub(super) fn contention_details(name: &str) -> String {
        super::diagnostics(name, "windows_named_mutex", None,
            "Unavailable: Windows named mutexes do not expose the creator PID, task/session IDs or acquisition time.")
    }

    impl Drop for Guard {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }

    pub(super) fn try_acquire(name: &str) -> io::Result<Option<Guard>> {
        let name = name
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect::<Vec<_>>();
        let handle = unsafe { CreateMutexW(ptr::null(), 0, name.as_ptr()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }

        let already_exists = unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;
        if already_exists {
            unsafe {
                CloseHandle(handle);
            }
            Ok(None)
        } else {
            Ok(Some(Guard { handle }))
        }
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateMutexW(
            mutex_attributes: *const c_void,
            initial_owner: i32,
            name: *const u16,
        ) -> Handle;
        fn GetLastError() -> u32;
        fn CloseHandle(object: Handle) -> i32;
    }
}

#[cfg(unix)]
mod platform {
    use std::fs::{self, DirBuilder, File, OpenOptions};
    use std::io::{self, Read, Seek, Write};
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{BTreeMap, Value};

    const LOCK_EX: i32 = 2;
    const LOCK_NB: i32 = 4;

    pub(super) struct Guard {
        _file: File,
    }

    fn lock_path(name: &str) -> PathBuf {
        let uid = unsafe { geteuid() };
        std::path::Path::new("/tmp")
            .join(format!("cga-relay-{uid}"))
            .join(format!("{name}.lock"))
    }

    pub(super) fn resource(name: &str) -> String {
        lock_path(name).display().to_string()
    }

    fn context_id(name: &str) -> io::Result<Value> {
        match std::env::var(name) {
            Ok(value)
                if !value.is_empty()
                    && value.len() <= 128
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"_.:-".contains(&byte)) =>
            {
                Ok(Value::String(value))
            }
            Err(std::env::VarError::NotPresent) => Ok(Value::Null),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("{name} must be a bounded identifier"),
            )),
        }
    }

    fn owner_metadata() -> io::Result<Value> {
        let mut hostname = [0u8; 256];
        if unsafe { gethostname(hostname.as_mut_ptr().cast(), hostname.len()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let length = hostname
            .iter()
            .position(|byte| *byte == 0)
            .unwrap_or(hostname.len());
        let acquired = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?;
        Ok(Value::Object(BTreeMap::from([
            ("pid".into(), Value::Number(std::process::id().to_string())),
            (
                "host".into(),
                Value::String(String::from_utf8_lossy(&hostname[..length]).into_owned()),
            ),
            (
                "acquired_at_unix_ms".into(),
                Value::Number(acquired.as_millis().to_string()),
            ),
            ("task_id".into(), context_id("CGA_TASK_ID")?),
            ("session_id".into(), context_id("CGA_SESSION_ID")?),
        ])))
    }

    fn read_owner(name: &str) -> io::Result<Value> {
        let path = lock_path(name);
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.is_file() || metadata.uid() != unsafe { geteuid() } {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "owner metadata is not a regular owned file",
            ));
        }
        let mut raw = String::new();
        File::open(path)?.take(4097).read_to_string(&mut raw)?;
        if raw.len() > 4096 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "owner metadata exceeds size limit",
            ));
        }
        let invalid = || {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "owner metadata is invalid or from a legacy lock",
            )
        };
        let parsed = crate::json::parse(&raw).map_err(|_| invalid())?;
        let Value::Object(mut fields) = parsed else {
            return Err(invalid());
        };
        let mut owner = BTreeMap::new();
        for key in [
            "pid",
            "host",
            "acquired_at_unix_ms",
            "task_id",
            "session_id",
        ] {
            let value = fields.remove(key).ok_or_else(invalid)?;
            let valid = match (key, &value) {
                ("pid", Value::Number(value)) => value.parse::<u32>().is_ok_and(|pid| pid > 0),
                ("acquired_at_unix_ms", Value::Number(value)) => value.parse::<u64>().is_ok(),
                ("host", Value::String(value)) => !value.is_empty() && value.len() <= 256,
                ("task_id" | "session_id", Value::Null) => true,
                ("task_id" | "session_id", Value::String(value)) => {
                    !value.is_empty()
                        && value.len() <= 128
                        && value
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || b"_.:-".contains(&byte))
                }
                _ => false,
            };
            if !valid {
                return Err(invalid());
            }
            owner.insert(key.into(), value);
        }
        Ok(Value::Object(owner))
    }

    pub(super) fn contention_details(name: &str) -> String {
        let resource = resource(name);
        match read_owner(name) {
            Ok(owner) => super::diagnostics(&resource, "unix_flock", Some(owner),
                "Recorded owner snapshot; missing task/session IDs were not supplied. PID is not proof of liveness."),
            Err(error) => super::diagnostics(&resource, "unix_flock", None,
                &format!("Unavailable owner metadata: {}", error.kind())),
        }
    }

    pub(super) fn try_acquire(name: &str) -> io::Result<Option<Guard>> {
        let uid = unsafe { geteuid() };
        let directory = std::path::Path::new("/tmp").join(format!("cga-relay-{uid}"));
        match DirBuilder::new().mode(0o700).create(&directory) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
        let metadata = fs::symlink_metadata(&directory)?;
        if !metadata.is_dir() || metadata.uid() != uid || metadata.permissions().mode() & 0o077 != 0
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "single-instance directory must be a private directory owned by the current user",
            ));
        }
        let path = lock_path(name);
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(&path)?;
        let path_metadata = fs::symlink_metadata(&path)?;
        let file_metadata = file.metadata()?;
        if !path_metadata.is_file()
            || file_metadata.uid() != uid
            || path_metadata.dev() != file_metadata.dev()
            || path_metadata.ino() != file_metadata.ino()
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "single-instance file must be a regular owned file",
            ));
        }
        if unsafe { flock(file.as_raw_fd(), LOCK_EX | LOCK_NB) } == 0 {
            let owner = owner_metadata()?.encode();
            file.rewind()?;
            file.set_len(0)?;
            file.write_all(owner.as_bytes())?;
            file.flush()?;
            return Ok(Some(Guard { _file: file }));
        }

        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::WouldBlock {
            Ok(None)
        } else {
            Err(error)
        }
    }

    unsafe extern "C" {
        fn flock(file_descriptor: i32, operation: i32) -> i32;
        fn geteuid() -> u32;
        fn gethostname(name: *mut std::ffi::c_char, length: usize) -> i32;
    }
}

#[cfg(not(any(windows, unix)))]
mod platform {
    use std::io;

    pub(super) struct Guard;

    pub(super) fn resource(name: &str) -> String {
        name.to_string()
    }

    pub(super) fn contention_details(name: &str) -> String {
        super::diagnostics(
            name,
            "unsupported",
            None,
            "Single-instance locking is unsupported.",
        )
    }

    pub(super) fn try_acquire(_name: &str) -> io::Result<Option<Guard>> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "single-instance locking is not supported on this platform",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::{platform, INSTANCE_NAME};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn rejects_second_guard_until_first_guard_is_dropped() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time should be valid")
            .as_nanos();
        let name = format!("{INSTANCE_NAME}.unit.{}.{unique}", std::process::id());

        let first = platform::try_acquire(&name)
            .expect("first mutex acquisition should not fail")
            .expect("first mutex acquisition should succeed");
        assert!(platform::try_acquire(&name)
            .expect("second mutex acquisition should not fail")
            .is_none());
        let details = crate::json::parse(&platform::contention_details(&name)).unwrap();
        assert_eq!(
            details.get("expires_at").unwrap(),
            &crate::json::Value::Null
        );
        assert_eq!(
            details.get("lock_resource").unwrap().text().unwrap(),
            platform::resource(&name)
        );
        #[cfg(windows)]
        {
            assert_eq!(details.get("owner").unwrap(), &crate::json::Value::Null);
            assert!(details
                .get("owner_metadata_status")
                .unwrap()
                .text()
                .unwrap()
                .contains("Windows named mutexes"));
        }
        #[cfg(unix)]
        {
            let owner = details.get("owner").unwrap();
            assert_eq!(
                owner.get("pid").unwrap(),
                &crate::json::Value::Number(std::process::id().to_string())
            );
            assert_ne!(details.get("held_ms").unwrap(), &crate::json::Value::Null);
        }

        drop(first);
        assert!(platform::try_acquire(&name)
            .expect("mutex reacquisition should not fail")
            .is_some());
    }

    #[cfg(unix)]
    #[test]
    fn invalid_or_legacy_metadata_never_releases_the_kernel_lock() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let name = format!("{INSTANCE_NAME}.invalid.{}.{unique}", std::process::id());
        let first = platform::try_acquire(&name).unwrap().unwrap();
        let path = platform::resource(&name);
        for data in ["", "invalid-secret-sentinel", r#"{"pid":true}"#] {
            std::fs::write(&path, data).unwrap();
            assert!(platform::try_acquire(&name).unwrap().is_none());
            let report = platform::contention_details(&name);
            assert!(!report.contains("secret-sentinel"));
            let details = crate::json::parse(&report).unwrap();
            assert_eq!(details.get("owner").unwrap(), &crate::json::Value::Null);
            assert!(details
                .get("owner_metadata_status")
                .unwrap()
                .text()
                .unwrap()
                .contains("Unavailable"));
            assert_eq!(std::fs::read_to_string(&path).unwrap(), data);
        }
        drop(first);
        let next = platform::try_acquire(&name).unwrap().unwrap();
        let details = crate::json::parse(&platform::contention_details(&name)).unwrap();
        assert_ne!(details.get("owner").unwrap(), &crate::json::Value::Null);
        drop(next);
        std::fs::remove_file(path).unwrap();
    }
}
