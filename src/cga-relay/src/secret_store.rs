use super::{AgentError, AgentResult};

pub(crate) struct PersistedAccessToken {
    pub(crate) field: &'static str,
    pub(crate) value: String,
}

#[cfg(windows)]
pub(crate) fn protect_access_token(access_token: &str) -> AgentResult<PersistedAccessToken> {
    windows_dpapi::protect(access_token).map(|value| PersistedAccessToken {
        field: "access_token_dpapi",
        value,
    })
}

#[cfg(not(windows))]
pub(crate) fn protect_access_token(access_token: &str) -> AgentResult<PersistedAccessToken> {
    Ok(PersistedAccessToken {
        field: "access_token",
        value: access_token.to_string(),
    })
}

#[cfg(windows)]
pub(crate) fn unprotect_access_token(protected: &str) -> AgentResult<String> {
    windows_dpapi::unprotect(protected)
}

#[cfg(not(windows))]
pub(crate) fn unprotect_access_token(_protected: &str) -> AgentResult<String> {
    Err(AgentError(
        "this account session is protected by Windows DPAPI and cannot be opened on this platform"
            .to_string(),
    ))
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
