//! Encrypted local secret vault for optional "Remember on this PC" persistence.
//!
//! Secrets are stored in `<app_data_dir>/vault.bin` (on Windows:
//! `%APPDATA%\com.pragent.control\vault.bin`).
//! - On Windows, payloads are encrypted via Windows DPAPI (`CryptProtectData` /
//!   `CryptUnprotectData`) scoped to the current Windows user with an
//!   application-specific entropy constant.
//! - On Unix / non-Windows platforms, the file is created with strict `0600`
//!   permissions so cross-platform builds remain clean and functional.
//!
//! Secret values are never logged or included in error messages.

use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use tauri::Manager;

/// Allowlist of secret keys that the desktop vault may persist.
pub const ALLOWED_KEYS: &[&str] = &[
    "backend_api_token",
    "github_token",
    "openrouter_api_key",
    "commandcode_api_key",
    "opencode_go_api_key",
];

const MAX_SECRET_BYTES: usize = 16 * 1024;
const MAX_VAULT_FILE_BYTES: u64 = 256 * 1024;

#[cfg(windows)]
const VAULT_MAGIC: &[u8; 8] = b"PRAGV1W\0";
#[cfg(not(windows))]
const VAULT_MAGIC: &[u8; 8] = b"PRAGV1U\0";

static VAULT_LOCK: Mutex<()> = Mutex::new(());
static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn zeroize(buf: &mut [u8]) {
    for byte in buf.iter_mut() {
        // SAFETY: `byte` is a valid, aligned mutable reference to a u8.
        unsafe { std::ptr::write_volatile(byte, 0) };
    }
    std::sync::atomic::compiler_fence(Ordering::SeqCst);
}

/// Validate a vault key against the strict allowlist.
pub fn validate_key(key: &str) -> Result<&'static str, String> {
    let trimmed = key.trim();
    ALLOWED_KEYS
        .iter()
        .copied()
        .find(|&allowed| allowed == trimmed)
        .ok_or_else(|| {
            format!(
                "unsupported vault key {:?} (allowed: {})",
                trimmed,
                ALLOWED_KEYS.join(", ")
            )
        })
}

fn validate_value(value: &str) -> Result<&str, String> {
    let trimmed = value.trim();
    if trimmed.len() > MAX_SECRET_BYTES {
        return Err("secret value exceeds maximum allowed length".into());
    }
    if trimmed.contains(['\r', '\n', '\0']) {
        return Err("secret value must be a single line without control characters".into());
    }
    Ok(trimmed)
}

#[cfg(windows)]
mod crypto {
    use super::zeroize;
    use std::ffi::c_void;
    use std::ptr;

    const VAULT_ENTROPY: &[u8] = b"com.pragent.control.vault.v1";
    const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x01;

    #[repr(C)]
    struct DataBlob {
        cb_data: u32,
        pb_data: *mut u8,
    }

    #[link(name = "crypt32")]
    extern "system" {
        fn CryptProtectData(
            p_data_in: *const DataBlob,
            sz_data_descr: *const u16,
            p_optional_entropy: *const DataBlob,
            pv_reserved: *mut c_void,
            p_prompt_struct: *mut c_void,
            dw_flags: u32,
            p_data_out: *mut DataBlob,
        ) -> i32;

        fn CryptUnprotectData(
            p_data_in: *const DataBlob,
            ppsz_data_descr: *mut *mut u16,
            p_optional_entropy: *const DataBlob,
            pv_reserved: *mut c_void,
            p_prompt_struct: *mut c_void,
            dw_flags: u32,
            p_data_out: *mut DataBlob,
        ) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn LocalFree(h_mem: *mut c_void) -> *mut c_void;
        fn GetLastError() -> u32;
    }

    pub(super) fn protect(plaintext: &[u8]) -> Result<Vec<u8>, String> {
        let len = u32::try_from(plaintext.len())
            .map_err(|_| "vault payload exceeds DPAPI size limit".to_string())?;
        let in_blob = DataBlob {
            cb_data: len,
            pb_data: plaintext.as_ptr() as *mut u8,
        };
        let entropy_blob = DataBlob {
            cb_data: VAULT_ENTROPY.len() as u32,
            pb_data: VAULT_ENTROPY.as_ptr() as *mut u8,
        };
        let mut out_blob = DataBlob {
            cb_data: 0,
            pb_data: ptr::null_mut(),
        };

        // SAFETY: `in_blob` and `entropy_blob` point to valid immutable slices for
        // the duration of the FFI call. DPAPI allocates `out_blob.pb_data` with
        // LocalAlloc (current-user scope because CRYPTPROTECT_LOCAL_MACHINE is unset),
        // which we release below via `LocalFree`.
        let ok = unsafe {
            CryptProtectData(
                &in_blob,
                ptr::null(),
                &entropy_blob,
                ptr::null_mut(),
                ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out_blob,
            )
        };
        if ok == 0 {
            let err = unsafe { GetLastError() };
            return Err(format!(
                "Windows DPAPI encryption failed (error 0x{:08X})",
                err
            ));
        }
        if out_blob.pb_data.is_null() {
            return Err("Windows DPAPI returned an empty encrypted buffer".into());
        }
        // SAFETY: `out_blob.pb_data` is valid for `out_blob.cb_data` bytes.
        let out = unsafe {
            let slice = std::slice::from_raw_parts(out_blob.pb_data, out_blob.cb_data as usize);
            let vec = slice.to_vec();
            LocalFree(out_blob.pb_data as *mut c_void);
            vec
        };
        Ok(out)
    }

    pub(super) fn unprotect(ciphertext: &[u8]) -> Result<Vec<u8>, String> {
        let len = u32::try_from(ciphertext.len())
            .map_err(|_| "vault payload exceeds DPAPI size limit".to_string())?;
        if len == 0 {
            return Err("vault file is empty or corrupted".into());
        }
        let in_blob = DataBlob {
            cb_data: len,
            pb_data: ciphertext.as_ptr() as *mut u8,
        };
        let entropy_blob = DataBlob {
            cb_data: VAULT_ENTROPY.len() as u32,
            pb_data: VAULT_ENTROPY.as_ptr() as *mut u8,
        };
        let mut out_blob = DataBlob {
            cb_data: 0,
            pb_data: ptr::null_mut(),
        };

        // SAFETY: `in_blob` and `entropy_blob` point to valid immutable slices for
        // the duration of the FFI call; `ppsz_data_descr` is null so no description
        // buffer is allocated.
        let ok = unsafe {
            CryptUnprotectData(
                &in_blob,
                ptr::null_mut(),
                &entropy_blob,
                ptr::null_mut(),
                ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out_blob,
            )
        };
        if ok == 0 {
            let err = unsafe { GetLastError() };
            return Err(format!(
                "Windows DPAPI decryption failed (error 0x{:08X}); the vault may be corrupted or belong to another Windows user",
                err
            ));
        }
        if out_blob.pb_data.is_null() {
            return Err("Windows DPAPI returned an empty decrypted buffer".into());
        }
        // SAFETY: `out_blob.pb_data` is valid for `out_blob.cb_data` bytes.
        // Scrub the decrypted OS buffer before freeing it.
        let out = unsafe {
            let slice = std::slice::from_raw_parts_mut(out_blob.pb_data, out_blob.cb_data as usize);
            let vec = slice.to_vec();
            zeroize(slice);
            LocalFree(out_blob.pb_data as *mut c_void);
            vec
        };
        Ok(out)
    }
}

#[cfg(not(windows))]
mod crypto {
    pub(super) fn protect(plaintext: &[u8]) -> Result<Vec<u8>, String> {
        Ok(plaintext.to_vec())
    }

    pub(super) fn unprotect(ciphertext: &[u8]) -> Result<Vec<u8>, String> {
        if ciphertext.is_empty() {
            return Err("vault file is empty or corrupted".into());
        }
        Ok(ciphertext.to_vec())
    }
}

fn read_map_unlocked(path: &Path) -> Result<HashMap<String, String>, String> {
    let meta = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(HashMap::new()),
        Err(e) => return Err(format!("could not inspect vault file: {}", e)),
    };
    if meta.len() > MAX_VAULT_FILE_BYTES {
        return Err("vault file exceeds maximum expected size".into());
    }
    let raw = std::fs::read(path).map_err(|e| format!("could not read vault file: {}", e))?;
    if raw.len() < VAULT_MAGIC.len() || &raw[..VAULT_MAGIC.len()] != VAULT_MAGIC {
        return Err("vault file format is invalid or from another platform".into());
    }
    let mut plaintext = crypto::unprotect(&raw[VAULT_MAGIC.len()..])?;
    let parsed: Result<HashMap<String, String>, _> = serde_json::from_slice(&plaintext);
    zeroize(&mut plaintext);
    let raw_map = parsed.map_err(|_| "vault contents could not be parsed".to_string())?;

    let mut filtered = HashMap::new();
    for (k, v) in raw_map {
        if let Ok(valid_k) = validate_key(&k) {
            let trimmed = v.trim();
            if !trimmed.is_empty() {
                filtered.insert(valid_k.to_string(), trimmed.to_string());
            }
        }
    }
    Ok(filtered)
}

fn write_map_unlocked(path: &Path, map: &HashMap<String, String>) -> Result<(), String> {
    if map.is_empty() {
        return remove_file_if_exists(path);
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("could not create vault directory: {}", e))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700));
        }
    }

    let mut plaintext =
        serde_json::to_vec(map).map_err(|_| "failed to serialize vault contents".to_string())?;
    let protected = crypto::protect(&plaintext);
    zeroize(&mut plaintext);
    let protected = protected?;

    let mut payload = Vec::with_capacity(VAULT_MAGIC.len() + protected.len());
    payload.extend_from_slice(VAULT_MAGIC);
    payload.extend_from_slice(&protected);

    let file_name = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("vault.bin");
    let seq = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let tmp_path = path.with_file_name(format!(".{}.tmp-{}-{}", file_name, std::process::id(), seq));

    let write_res = (|| -> Result<(), String> {
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts
            .open(&tmp_path)
            .map_err(|e| format!("could not create temporary vault file: {}", e))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(std::fs::Permissions::from_mode(0o600))
                .map_err(|e| format!("could not set vault file permissions: {}", e))?;
        }
        file.write_all(&payload)
            .map_err(|e| format!("could not write vault file: {}", e))?;
        file.sync_all()
            .map_err(|e| format!("could not sync vault file: {}", e))?;
        drop(file);
        std::fs::rename(&tmp_path, path)
            .map_err(|e| format!("could not finalize vault file: {}", e))?;
        Ok(())
    })();

    if write_res.is_err() {
        let _ = std::fs::remove_file(&tmp_path);
    }
    write_res
}

fn remove_file_if_exists(path: &Path) -> Result<(), String> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("could not remove vault file: {}", e)),
    }
}

pub fn save_secret_at(path: &Path, key: &str, value: &str) -> Result<(), String> {
    let valid_key = validate_key(key)?;
    let valid_val = validate_value(value)?;
    let _guard = VAULT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut map = read_map_unlocked(path)?;
    if valid_val.is_empty() {
        map.remove(valid_key);
    } else {
        map.insert(valid_key.to_string(), valid_val.to_string());
    }
    write_map_unlocked(path, &map)
}

pub fn load_secrets_at(path: &Path) -> Result<HashMap<String, String>, String> {
    let _guard = VAULT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    read_map_unlocked(path)
}

pub fn delete_secret_at(path: &Path, key: &str) -> Result<(), String> {
    let valid_key = validate_key(key)?;
    let _guard = VAULT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    if !path.exists() {
        return Ok(());
    }
    let mut map = read_map_unlocked(path)?;
    map.remove(valid_key);
    write_map_unlocked(path, &map)
}

pub fn clear_all_at(path: &Path) -> Result<(), String> {
    let _guard = VAULT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    remove_file_if_exists(path)
}

fn vault_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("could not resolve app data directory: {}", e))?;
    Ok(dir.join("vault.bin"))
}

#[tauri::command]
pub async fn vault_save_secret(
    app: tauri::AppHandle,
    key: String,
    value: String,
) -> Result<(), String> {
    let path = vault_path(&app)?;
    tauri::async_runtime::spawn_blocking(move || save_secret_at(&path, &key, &value))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn vault_load_secrets(app: tauri::AppHandle) -> Result<HashMap<String, String>, String> {
    let path = vault_path(&app)?;
    tauri::async_runtime::spawn_blocking(move || load_secrets_at(&path))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn vault_delete_secret(app: tauri::AppHandle, key: String) -> Result<(), String> {
    let path = vault_path(&app)?;
    tauri::async_runtime::spawn_blocking(move || delete_secret_at(&path, &key))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn vault_clear_all(app: tauri::AppHandle) -> Result<(), String> {
    let path = vault_path(&app)?;
    tauri::async_runtime::spawn_blocking(move || clear_all_at(&path))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch_vault_path(label: &str) -> (PathBuf, PathBuf) {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!(
            "pr-agent-vault-{}-{}-{}",
            label,
            std::process::id(),
            nanos
        ));
        let file = dir.join("vault.bin");
        (dir, file)
    }

    #[test]
    fn key_allowlist_validation() {
        for key in [
            "backend_api_token",
            "github_token",
            "openrouter_api_key",
            "commandcode_api_key",
            "opencode_go_api_key",
        ] {
            assert_eq!(validate_key(key).unwrap(), key);
            assert_eq!(validate_key(&format!("  {}  ", key)).unwrap(), key);
        }
        for bad in [
            "",
            "BACKEND_API_TOKEN",
            "aws_secret_access_key",
            "github_app_private_key",
            "../vault.bin",
            "random_key",
        ] {
            assert!(validate_key(bad).is_err(), "expected {:?} to be rejected", bad);
        }
    }

    #[test]
    fn vault_round_trip_update_delete_and_clear() {
        let (dir, path) = scratch_vault_path("roundtrip");
        // Empty vault before creation returns empty map.
        assert!(load_secrets_at(&path).unwrap().is_empty());

        save_secret_at(&path, "backend_api_token", "tok_secret_12345").unwrap();
        save_secret_at(&path, "github_token", "gho_example_token").unwrap();
        save_secret_at(&path, "openrouter_api_key", "sk-or-v1-abc").unwrap();

        // On Windows, verify the file on disk is DPAPI-encrypted (never plaintext).
        let raw_on_disk = std::fs::read(&path).unwrap();
        assert!(raw_on_disk.starts_with(VAULT_MAGIC));
        #[cfg(windows)]
        {
            assert!(
                !raw_on_disk
                    .windows("tok_secret_12345".len())
                    .any(|w| w == b"tok_secret_12345"),
                "DPAPI vault must not contain plaintext secrets on disk"
            );
            assert!(
                !raw_on_disk
                    .windows("backend_api_token".len())
                    .any(|w| w == b"backend_api_token"),
                "DPAPI vault must not contain plaintext keys on disk"
            );
        }

        let loaded = load_secrets_at(&path).unwrap();
        assert_eq!(loaded.len(), 3);
        assert_eq!(loaded.get("backend_api_token").map(String::as_str), Some("tok_secret_12345"));
        assert_eq!(loaded.get("github_token").map(String::as_str), Some("gho_example_token"));
        assert_eq!(loaded.get("openrouter_api_key").map(String::as_str), Some("sk-or-v1-abc"));

        // Overwriting an existing secret replaces cleanly.
        save_secret_at(&path, "backend_api_token", "tok_rotated_999").unwrap();
        let loaded = load_secrets_at(&path).unwrap();
        assert_eq!(loaded.get("backend_api_token").map(String::as_str), Some("tok_rotated_999"));
        assert_eq!(loaded.len(), 3);

        // Deleting one key preserves others.
        delete_secret_at(&path, "github_token").unwrap();
        let loaded = load_secrets_at(&path).unwrap();
        assert_eq!(loaded.len(), 2);
        assert!(!loaded.contains_key("github_token"));

        // Saving empty string removes the key cleanly.
        save_secret_at(&path, "openrouter_api_key", "   ").unwrap();
        let loaded = load_secrets_at(&path).unwrap();
        assert_eq!(loaded.len(), 1);
        assert!(loaded.contains_key("backend_api_token"));

        // Clear all deletes vault.bin completely.
        clear_all_at(&path).unwrap();
        assert!(!path.exists());
        assert!(load_secrets_at(&path).unwrap().is_empty());

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn rejects_invalid_keys_and_multiline_values() {
        let (dir, path) = scratch_vault_path("reject");
        assert!(save_secret_at(&path, "not_allowed", "val").is_err());
        assert!(delete_secret_at(&path, "not_allowed").is_err());
        let err = save_secret_at(&path, "backend_api_token", "line1\nline2").unwrap_err();
        assert!(!err.contains("line1"), "error must never leak secret value");
        assert!(!path.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn dpapi_detects_tampered_ciphertext() {
        let (dir, path) = scratch_vault_path("tamper");
        save_secret_at(&path, "backend_api_token", "super_secret_token").unwrap();
        let mut bytes = std::fs::read(&path).unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 0xFF;
        std::fs::write(&path, &bytes).unwrap();

        let err = load_secrets_at(&path).unwrap_err();
        assert!(err.contains("DPAPI decryption failed"), "unexpected err: {}", err);
        assert!(!err.contains("super_secret_token"));

        let _ = std::fs::remove_dir_all(&dir);
    }
}
