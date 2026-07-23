use super::{AgentError, AgentResult};

#[cfg(windows)]
const INSTANCE_NAME: &str = "Global\\Nascousa.CGA-Relay.SingleInstance";
#[cfg(not(windows))]
const INSTANCE_NAME: &str = "Nascousa.CGA-Relay.SingleInstance";

pub(crate) struct SingleInstanceGuard {
    _platform: platform::Guard,
}

pub(crate) fn acquire() -> AgentResult<SingleInstanceGuard> {
    match platform::try_acquire(&instance_name()) {
        Ok(Some(guard)) => Ok(SingleInstanceGuard { _platform: guard }),
        Ok(None) => Err(AgentError(
            "CGA-Relay is already running; use the existing instance instead of starting another process"
                .to_string(),
        )),
        Err(error) => Err(AgentError(format!(
            "failed to acquire the CGA-Relay single-instance mutex: {error}"
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
    use std::fs::{File, OpenOptions};
    use std::io;
    use std::os::fd::AsRawFd;

    const LOCK_EX: i32 = 2;
    const LOCK_NB: i32 = 4;

    pub(super) struct Guard {
        _file: File,
    }

    pub(super) fn try_acquire(name: &str) -> io::Result<Option<Guard>> {
        let path = std::env::temp_dir().join(format!("{name}.lock"));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(path)?;
        if unsafe { flock(file.as_raw_fd(), LOCK_EX | LOCK_NB) } == 0 {
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
    }
}

#[cfg(not(any(windows, unix)))]
mod platform {
    use std::io;

    pub(super) struct Guard;

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

        drop(first);
        assert!(platform::try_acquire(&name)
            .expect("mutex reacquisition should not fail")
            .is_some());
    }
}
