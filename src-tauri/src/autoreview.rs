//! Configurable PR-Agent GitHub Actions workflow for one-click auto review.
//!
//! The desktop app renders the workflow from [`WorkflowOptions`] and commits it to
//! each repository. The options are written into the file's first line as JSON, so
//! the app can read back what a repo runs and tell when the file is outdated.

use serde::{Deserialize, Serialize};

/// Bumped whenever the rendered template changes in a way existing repos should pick up.
pub const TEMPLATE_VERSION: u32 = 6;
/// First-line marker of a workflow this app manages.
pub const MANAGED_MARKER: &str = "# pr-agent-control: ";
/// Pinned so a compromised upstream branch cannot receive repository secrets.
const PR_AGENT_ACTION: &str = "the-pr-agent/pr-agent@dec3e88c029c0a32957bd95aa694cb34d2b256e0";
pub const DEFAULT_MODEL: &str = "openrouter/thinkingmachines/inkling-2-small:free";
const MAX_INSTRUCTIONS: usize = 2000;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct WorkflowOptions {
    /// Template version the options were written with (set on render).
    pub v: u32,
    pub model: String,
    pub fallback_models: Vec<String>,
    pub max_tokens: u32,
    pub reasoning_effort: String,
    /// Post a code review when a PR is opened / becomes ready.
    pub auto_review: bool,
    /// Write the PR title/description when a PR is opened.
    pub auto_describe: bool,
    /// Post code suggestions when a PR is opened.
    pub auto_improve: bool,
    /// Re-review on every push; the review comment is updated in place.
    pub review_on_push: bool,
    /// Wait until a draft PR is marked ready for review.
    pub skip_drafts: bool,
    /// Extra guidance for the reviewer (house rules, focus areas).
    pub extra_instructions: String,
}

impl Default for WorkflowOptions {
    fn default() -> Self {
        Self {
            v: TEMPLATE_VERSION,
            model: DEFAULT_MODEL.to_string(),
            fallback_models: vec!["openrouter/free".to_string()],
            max_tokens: 200_000,
            reasoning_effort: "low".to_string(),
            auto_review: true,
            auto_describe: true,
            auto_improve: false,
            review_on_push: true,
            skip_drafts: true,
            extra_instructions: String::new(),
        }
    }
}

fn valid_model(m: &str) -> bool {
    !m.is_empty()
        && m.len() <= 200
        && m.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '.' | '_' | ':' | '-'))
}

impl WorkflowOptions {
    /// Trim and check every value that ends up in the YAML file.
    pub fn normalized(mut self) -> Result<Self, String> {
        self.v = TEMPLATE_VERSION;
        self.model = self.model.trim().to_string();
        if self.model.is_empty() {
            self.model = DEFAULT_MODEL.to_string();
        }
        if !valid_model(&self.model) {
            return Err(format!("model id {:?} has unsupported characters", self.model));
        }
        let mut fallbacks = Vec::new();
        for m in &self.fallback_models {
            let m = m.trim();
            if m.is_empty() || fallbacks.iter().any(|f: &String| f == m) {
                continue;
            }
            if !valid_model(m) {
                return Err(format!("fallback model id {:?} has unsupported characters", m));
            }
            fallbacks.push(m.to_string());
        }
        if fallbacks.len() > 5 {
            return Err("use at most 5 fallback models".into());
        }
        self.fallback_models = fallbacks;
        if !(1_000..=2_000_000).contains(&self.max_tokens) {
            return Err("max_tokens must be between 1000 and 2000000".into());
        }
        self.reasoning_effort = self.reasoning_effort.trim().to_lowercase();
        if self.reasoning_effort.is_empty() {
            self.reasoning_effort = "low".into();
        }
        if !["none", "low", "medium", "high"].contains(&self.reasoning_effort.as_str()) {
            return Err("reasoning_effort must be none, low, medium or high".into());
        }
        let text = self.extra_instructions.replace("\r\n", "\n");
        let text = text.trim();
        if text.chars().count() > MAX_INSTRUCTIONS {
            return Err(format!("extra instructions are limited to {} characters", MAX_INSTRUCTIONS));
        }
        // GitHub evaluates ${{ … }} anywhere in a workflow value; never let user text do that.
        if text.contains("${{") {
            return Err("extra instructions must not contain '${{'".into());
        }
        if text.chars().any(|c| c.is_control() && c != '\n' && c != '\t') {
            return Err("extra instructions contain control characters".into());
        }
        self.extra_instructions = text.to_string();
        if !(self.auto_review || self.auto_describe || self.auto_improve) {
            return Err("enable at least one of review, describe or improve".into());
        }
        Ok(self)
    }
}

fn yaml_bool(b: bool) -> &'static str {
    if b {
        "\"true\""
    } else {
        "\"false\""
    }
}

/// Render the workflow. `opts` must come from [`WorkflowOptions::normalized`].
pub fn render_pr_agent_workflow(opts: &WorkflowOptions) -> String {
    // serde_json output is a single line and a valid YAML double-quoted scalar.
    let header = serde_json::to_string(opts).unwrap_or_else(|_| "{}".into());
    let mut types = vec!["opened", "reopened", "ready_for_review"];
    if opts.review_on_push {
        types.push("synchronize");
    }
    let pr_actions = serde_json::to_string(&types).unwrap_or_default();
    let fallbacks = serde_json::to_string(&opts.fallback_models).unwrap_or_else(|_| "[]".into());

    let draft_guard = if opts.skip_drafts {
        "\n          (github.event_name != 'pull_request' || !github.event.pull_request.draft) &&"
    } else {
        ""
    };
    // Describe/improve run once, on the first event where the PR is eligible. Pushes and
    // reopens only re-run the review, otherwise they would rewrite the description and
    // re-post suggestions. With drafts skipped the first eligible event can be
    // ready_for_review (the draft's opened run was skipped); with drafts allowed it is
    // always opened, so ready_for_review must not run them a second time.
    let on_open_only = |enabled: bool| -> String {
        if !enabled {
            yaml_bool(false).to_string()
        } else if opts.skip_drafts {
            "${{ github.event.action == 'opened' || github.event.action == 'ready_for_review' }}".to_string()
        } else {
            "${{ github.event.action == 'opened' }}".to_string()
        }
    };
    let instructions = if opts.extra_instructions.is_empty() {
        String::new()
    } else {
        let quoted = serde_json::to_string(&opts.extra_instructions).unwrap_or_default();
        format!(
            "          PR_REVIEWER__EXTRA_INSTRUCTIONS: {q}\n          PR_CODE_SUGGESTIONS__EXTRA_INSTRUCTIONS: {q}\n",
            q = quoted
        )
    };

    format!(
        r#"{marker}{header}
# Managed by PR Agent Control (Auto review). Change the settings in the app and
# click Update; edits made here are replaced on the next update.
name: PR Agent

on:
  pull_request:
    types: [{types}]
  issue_comment:
    types: [created, edited]

permissions:
  contents: read
  issues: write
  pull-requests: write

concurrency:
  # Runs for one PR are queued, one at a time. Nothing is cancelled mid-run, so a PR
  # description being written is never dropped, and a review can never overwrite the
  # review of a newer commit. Pending runs are preserved.
  group: ${{{{ github.workflow }}}}-${{{{ (github.event_name == 'pull_request' && format('pr-{{0}}', github.event.pull_request.number)) || (github.event_name == 'issue_comment' && github.event.issue.pull_request && github.event.sender.type != 'Bot' && contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association) && (github.event.comment.body == '/review' || startsWith(github.event.comment.body, '/review ') || startsWith(github.event.comment.body, fromJSON('"/review\n"')) || startsWith(github.event.comment.body, fromJSON('"/review\r"')) || startsWith(github.event.comment.body, fromJSON('"/review\t"'))) && format('pr-{{0}}', github.event.issue.number)) || format('run-{{0}}', github.run_id) }}}}
  queue: max
  cancel-in-progress: false

jobs:
  pr_agent_job:
    if: >-
      ${{{{ github.event.sender.type != 'Bot' &&{draft_guard}
          (github.event_name != 'issue_comment' ||
           (github.event.issue.pull_request && github.event.sender.type != 'Bot' && contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association) && (github.event.comment.body == '/review' || startsWith(github.event.comment.body, '/review ') || startsWith(github.event.comment.body, fromJSON('"/review\n"')) || startsWith(github.event.comment.body, fromJSON('"/review\r"')) || startsWith(github.event.comment.body, fromJSON('"/review\t"'))))) }}}}
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - name: Check for OpenRouter key
        id: key-check
        env:
          OPENROUTER_API_KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}
        run: |
          if [ -z "$OPENROUTER_API_KEY" ]; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "::warning::OPENROUTER_API_KEY is not configured (or this PR comes from a fork); skipping PR Agent."
          else
            echo "skip=false" >> "$GITHUB_OUTPUT"
          fi
      - name: PR Agent
        if: steps.key-check.outputs.skip != 'true'
        uses: {action}
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          OPENROUTER__KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}
          CONFIG__MODEL: "{model}"
          CONFIG__FALLBACK_MODELS: '{fallbacks}'
          CONFIG__MAX_MODEL_TOKENS: "{max_tokens}"
          CONFIG__CUSTOM_MODEL_MAX_TOKENS: "{max_tokens}"
          CONFIG__REASONING_EFFORT: "{effort}"
          OPENROUTER__REASONING_EFFORT: "{effort}"
          CONFIG__AI_TIMEOUT: "180"
          CONFIG__PROPAGATE_TOOL_ERRORS: "true"
          GITHUB_ACTION_CONFIG__FAIL_ON_COMMAND_ERRORS: "true"
          # One review comment per PR, edited in place on every new push.
          PR_REVIEWER__PERSISTENT_COMMENT: "true"
          PR_REVIEWER__INLINE_CODE_COMMENTS: "true"
{instructions}          github_action_config.pr_actions: '{pr_actions}'
          github_action_config.auto_review: {auto_review}
          github_action_config.auto_describe: {auto_describe}
          github_action_config.auto_improve: {auto_improve}
"#,
        marker = MANAGED_MARKER,
        header = header,
        types = types.join(", "),
        draft_guard = draft_guard,
        action = PR_AGENT_ACTION,
        model = opts.model,
        fallbacks = fallbacks,
        max_tokens = opts.max_tokens,
        effort = opts.reasoning_effort,
        instructions = instructions,
        pr_actions = pr_actions,
        auto_review = yaml_bool(opts.auto_review),
        auto_describe = on_open_only(opts.auto_describe),
        auto_improve = on_open_only(opts.auto_improve),
    )
}

/// The default workflow (used by the Providers tab and as the fallback template).
pub fn default_pr_agent_workflow() -> String {
    let opts = WorkflowOptions::default()
        .normalized()
        .expect("default workflow options are valid");
    render_pr_agent_workflow(&opts)
}

/// Read the settings back from a workflow file this app wrote, if it has the marker.
pub fn parse_managed(content: &str) -> Option<WorkflowOptions> {
    let first = content.lines().next()?;
    let json = first.strip_prefix(MANAGED_MARKER)?;
    serde_json::from_str(json.trim()).ok()
}

/// What the app knows about a committed PR-Agent workflow file.
pub fn describe_workflow(content: &str) -> serde_json::Value {
    match parse_managed(content) {
        Some(opts) => {
            // Re-rendering the stored settings with today's template tells us whether
            // the file is stale (older template or hand-edited).
            let current = opts.clone().normalized().ok().map(|o| render_pr_agent_workflow(&o));
            let outdated = current.as_deref() != Some(content);
            serde_json::json!({ "managed": true, "outdated": outdated, "settings": opts })
        }
        None => serde_json::json!({ "managed": false, "outdated": true, "settings": null }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const OPEN_ONLY: &str =
        "${{ github.event.action == 'opened' || github.event.action == 'ready_for_review' }}";
    const OPENED_ONLY: &str = "${{ github.event.action == 'opened' }}";

    fn opts() -> WorkflowOptions {
        WorkflowOptions::default().normalized().unwrap()
    }

    #[test]
    fn default_workflow_round_trips_and_is_current() {
        let yaml = default_pr_agent_workflow();
        assert!(yaml.starts_with(MANAGED_MARKER));
        assert_eq!(parse_managed(&yaml).unwrap(), opts());
        let info = describe_workflow(&yaml);
        assert_eq!(info["managed"], true);
        assert_eq!(info["outdated"], false);
    }

    #[test]
    fn default_workflow_has_expected_settings() {
        let yaml = default_pr_agent_workflow();
        assert!(yaml.contains("CONFIG__MAX_MODEL_TOKENS: \"200000\""));
        assert!(yaml.contains("CONFIG__CUSTOM_MODEL_MAX_TOKENS: \"200000\""));
        assert!(yaml.contains("OPENROUTER__REASONING_EFFORT: \"low\""));
        assert!(yaml.contains("CONFIG__AI_TIMEOUT: \"180\""));
        assert!(yaml.contains("CONFIG__PROPAGATE_TOOL_ERRORS: \"true\""));
        assert!(yaml.contains("GITHUB_ACTION_CONFIG__FAIL_ON_COMMAND_ERRORS: \"true\""));
        assert!(yaml.contains("types: [opened, reopened, ready_for_review, synchronize]"));
        assert!(yaml.contains("!github.event.pull_request.draft"));
        assert!(yaml.contains("PR_REVIEWER__PERSISTENT_COMMENT: \"true\""));
        assert!(yaml.contains(
            "github_action_config.pr_actions: '[\"opened\",\"reopened\",\"ready_for_review\",\"synchronize\"]'"
        ));
        // Describe runs once when the PR opens or becomes ready, not on pushes or reopens.
        assert!(yaml.contains(&format!("github_action_config.auto_describe: {}", OPEN_ONLY)));
        assert!(yaml.contains("github_action_config.auto_improve: \"false\""));
        // One queue per PR: no run is cancelled mid-flight, so nothing is dropped or reordered.
        assert!(yaml.contains(r#"group: ${{ github.workflow }}-${{ (github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number)) || (github.event_name == 'issue_comment' && github.event.issue.pull_request && github.event.sender.type != 'Bot' && contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association) && (github.event.comment.body == '/review' || startsWith(github.event.comment.body, '/review ') || startsWith(github.event.comment.body, fromJSON('"/review\n"')) || startsWith(github.event.comment.body, fromJSON('"/review\r"')) || startsWith(github.event.comment.body, fromJSON('"/review\t"'))) && format('pr-{0}', github.event.issue.number))"#));
        assert!(yaml.contains("queue: max"));
        assert!(yaml.contains("cancel-in-progress: false"));
        assert!(!yaml.contains("EXTRA_INSTRUCTIONS"));
    }

    #[test]
    fn options_change_the_triggers() {
        let o = WorkflowOptions {
            review_on_push: false,
            skip_drafts: false,
            auto_improve: true,
            ..WorkflowOptions::default()
        }
        .normalized()
        .unwrap();
        let yaml = render_pr_agent_workflow(&o);
        assert!(yaml.contains("types: [opened, reopened, ready_for_review]\n"));
        assert!(!yaml.contains("pull_request.draft"));
        // Drafts are allowed here, so the first eligible event is always `opened`:
        // ready_for_review must not run describe/improve a second time.
        assert!(yaml.contains(&format!("github_action_config.auto_improve: {}", OPENED_ONLY)));
        assert!(yaml.contains(&format!("github_action_config.auto_describe: {}", OPENED_ONLY)));
    }

    #[test]
    fn instructions_are_quoted_and_injection_safe() {
        let o = WorkflowOptions {
            extra_instructions: "Focus on \"security\".\r\nIgnore docs: #1".into(),
            ..WorkflowOptions::default()
        }
        .normalized()
        .unwrap();
        let yaml = render_pr_agent_workflow(&o);
        assert!(yaml.contains(
            "PR_REVIEWER__EXTRA_INSTRUCTIONS: \"Focus on \\\"security\\\".\\nIgnore docs: #1\""
        ));
        // The settings header stays on the first line (newlines are escaped).
        assert!(yaml.lines().next().unwrap().contains("Ignore docs: #1"));
        assert_eq!(parse_managed(&yaml).unwrap(), o);

        let bad = WorkflowOptions {
            extra_instructions: "x ${{ secrets.GITHUB_TOKEN }}".into(),
            ..WorkflowOptions::default()
        };
        assert!(bad.normalized().is_err());
    }

    #[test]
    fn rejects_bad_values() {
        let bad_model = WorkflowOptions { model: "a\"b".into(), ..WorkflowOptions::default() };
        assert!(bad_model.normalized().is_err());
        let bad_fallback = WorkflowOptions {
            fallback_models: vec!["ok/model".into(), "x'y".into()],
            ..WorkflowOptions::default()
        };
        assert!(bad_fallback.normalized().is_err());
        let nothing = WorkflowOptions {
            auto_review: false,
            auto_describe: false,
            auto_improve: false,
            ..WorkflowOptions::default()
        };
        assert!(nothing.normalized().is_err());
        let effort = WorkflowOptions { reasoning_effort: "max".into(), ..WorkflowOptions::default() };
        assert!(effort.normalized().is_err());
    }

    #[test]
    fn unmanaged_or_edited_files_are_flagged() {
        let info = describe_workflow("name: PR Agent\non: pull_request\n");
        assert_eq!(info["managed"], false);
        let mut edited = default_pr_agent_workflow();
        edited.push_str("# local tweak\n");
        let info = describe_workflow(&edited);
        assert_eq!(info["managed"], true);
        assert_eq!(info["outdated"], true);
    }

    #[test]
    fn missing_fields_fall_back_to_defaults() {
        let yaml = format!("{}{{\"model\":\"openrouter/free\"}}\nname: x\n", MANAGED_MARKER);
        let o = parse_managed(&yaml).unwrap();
        assert_eq!(o.model, "openrouter/free");
        assert!(o.auto_review);
    }
}
