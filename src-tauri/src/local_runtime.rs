use std::path::Path;
use std::process::Command;

// Embedded in the binary, so installed builds need no source checkout.
const DEFAULT_CONFIG: &str = include_str!("../../.pr_agent.toml");

pub fn ensure_pip(python: &Path) -> Result<(), String> {
    let working = || {
        Command::new(python)
            .args(["-m", "pip", "--version"])
            .output()
            .map(|out| out.status.success())
            .unwrap_or(false)
    };
    if working() {
        return Ok(());
    }
    let out = Command::new(python)
        .args(["-m", "ensurepip", "--upgrade"])
        .output()
        .map_err(|e| format!("failed to repair pip in {}: {e}", python.display()))?;
    if !out.status.success() || !working() {
        return Err(format!(
            "pip repair failed in {}: {}",
            python.display(),
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    Ok(())
}

pub fn prepare_config(path: &Path) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    // Never overwrite user settings when initializing or upgrading the app.
    use std::io::Write;
    match std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
    {
        Ok(mut file) => file
            .write_all(DEFAULT_CONFIG.as_bytes())
            .map_err(|e| e.to_string())?,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(e) => return Err(e.to_string()),
    }
    let raw = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    if let Some(updated) = migrate_token_limit(&raw)? {
        std::fs::write(path, updated).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn migrate_token_limit(raw: &str) -> Result<Option<String>, String> {
    let mut doc = raw
        .parse::<toml_edit::Document>()
        .map_err(|e| e.to_string())?;
    if doc
        .get("config")
        .and_then(|c| c.get("max_model_tokens"))
        .is_some()
    {
        return Ok(None);
    }
    let limit = doc
        .get("config")
        .and_then(|c| c.get("custom_model_max_tokens"))
        .and_then(|v| v.as_integer())
        .unwrap_or(200000);
    doc["config"]["max_model_tokens"] = toml_edit::value(limit);
    Ok(Some(doc.to_string()))
}

/// Windows copy commands target PowerShell; other platforms target POSIX sh.
pub fn shell_command(args: &[String], powershell: bool) -> String {
    let quoted = args
        .iter()
        .map(|arg| {
            let escaped = if powershell {
                arg.replace('\'', "''")
            } else {
                arg.replace('\'', "'\"'\"'")
            };
            format!("'{escaped}'")
        })
        .collect::<Vec<_>>()
        .join(" ");
    if powershell {
        format!("& {quoted}")
    } else {
        quoted
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn migration_preserves_settings_and_existing_limit() {
        let legacy = "# keep\n[config]\ncustom_model_max_tokens = 12345 # budget\nmodel = 'custom'\n[other]\nmax_model_tokens = 9\n";
        let updated = migrate_token_limit(legacy).unwrap().unwrap();
        let parsed = updated.parse::<toml_edit::Document>().unwrap();
        assert_eq!(
            parsed["config"]["max_model_tokens"].as_integer(),
            Some(12345)
        );
        assert_eq!(parsed["other"]["max_model_tokens"].as_integer(), Some(9));
        assert!(updated.contains("# keep"));
        assert!(updated.contains("# budget"));
        assert!(migrate_token_limit(&updated).unwrap().is_none());
        assert!(migrate_token_limit(
            "[config]\nmax_model_tokens = 42\ncustom_model_max_tokens = 12\n"
        )
        .unwrap()
        .is_none());
    }

    #[test]
    fn missing_limit_uses_original_default() {
        let updated = migrate_token_limit("[config]\nmodel = 'custom'\n")
            .unwrap()
            .unwrap();
        assert_eq!(
            updated.parse::<toml_edit::Document>().unwrap()["config"]["max_model_tokens"]
                .as_integer(),
            Some(200000)
        );
    }

    #[test]
    fn quotes_shell_metacharacters_and_spaces() {
        let args = vec![
            "C:/User's Files/python.exe".into(),
            "-m".into(),
            "pr_agent.cli".into(),
            "ask".into(),
            "what's $new?".into(),
        ];
        assert_eq!(
            shell_command(&args, true),
            "& 'C:/User''s Files/python.exe' '-m' 'pr_agent.cli' 'ask' 'what''s $new?'"
        );
        assert_eq!(
            shell_command(&args, false),
            "'C:/User'\"'\"'s Files/python.exe' '-m' 'pr_agent.cli' 'ask' 'what'\"'\"'s $new?'"
        );
    }
    fn scratch_dir(label: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "pr-agent-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn initializes_config_and_keeps_user_edits() {
        let dir = scratch_dir("config");
        let path = dir.join("user config/.pr_agent.toml");
        prepare_config(&path).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), DEFAULT_CONFIG);
        let edited = "# user settings\n[config]\nmax_model_tokens = 42\nmodel = 'my model'\n";
        std::fs::write(&path, edited).unwrap();
        prepare_config(&path).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), edited);
        std::fs::write(&path, "[config]\ncustom_model_max_tokens = 8192\n").unwrap();
        prepare_config(&path).unwrap();
        let migrated = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            migrated.parse::<toml_edit::Document>().unwrap()["config"]["max_model_tokens"]
                .as_integer(),
            Some(8192)
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn repairs_missing_pip_and_preserves_healthy_environment() {
        let dir = scratch_dir("venv with spaces");
        let out = Command::new("python3")
            .args(["-m", "venv", "--without-pip"])
            .arg(&dir)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let python = dir.join("bin/python");
        assert!(!Command::new(&python)
            .args(["-m", "pip", "--version"])
            .output()
            .unwrap()
            .status
            .success());
        ensure_pip(&python).unwrap();
        let marker = dir.join("keep.txt");
        std::fs::write(&marker, "installed state").unwrap();
        let before = std::fs::metadata(dir.join("pyvenv.cfg"))
            .unwrap()
            .modified()
            .unwrap();
        ensure_pip(&python).unwrap();
        assert_eq!(std::fs::read_to_string(marker).unwrap(), "installed state");
        assert_eq!(
            std::fs::metadata(dir.join("pyvenv.cfg"))
                .unwrap()
                .modified()
                .unwrap(),
            before
        );
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[cfg(unix)]
    #[test]
    fn posix_shell_round_trips_arguments() {
        let values = vec![
            "path with spaces",
            "what's changed?",
            "$HOME; echo injected",
            "",
            "https://example.test/pull/1?a=1&b=2",
        ];
        let mut args = vec![
            "python3".to_string(),
            "-c".into(),
            "import sys; print(chr(0).join(sys.argv[1:]), end='')".into(),
        ];
        args.extend(values.iter().map(|v| v.to_string()));
        let out = Command::new("sh")
            .args(["-c", &shell_command(&args, false)])
            .output()
            .unwrap();
        assert!(out.status.success());
        assert_eq!(String::from_utf8(out.stdout).unwrap(), values.join("\0"));
    }
}
