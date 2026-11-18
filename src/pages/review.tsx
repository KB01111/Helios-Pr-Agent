import * as React from "react";
import { Copy, ExternalLink, Play, RotateCcw, Save } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, SecretInput, Section } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { loadConfig, saveConfig } from "@/lib/actions";
import { DEFAULT_MODEL, KNOWN_MODELS, useApp } from "@/lib/store";
import { errText, openUrl, tauriInvoke } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

const COMMANDS = ["review", "describe", "improve", "ask"] as const;
const EFFORTS = [
  { value: "none", label: "none" },
  { value: "low", label: "low (fast, free-tier friendly)" },
  { value: "medium", label: "medium" },
  { value: "high", label: "high" },
];

export function ReviewPage({ active }: { active: boolean }) {
  const review = useApp((s) => s.review);
  const setReview = useApp((s) => s.setReview);
  const openrouter = useApp((s) => s.secrets.openrouter);
  const github = useApp((s) => s.secrets.github);
  const setSecret = useApp((s) => s.setSecret);

  const [cfgMsg, setCfgMsg] = React.useState("");
  const [prUrl, setPrUrl] = React.useState("");
  const [cmd, setCmd] = React.useState<(typeof COMMANDS)[number]>("review");
  const [askQ, setAskQ] = React.useState("");
  const [out, setOut] = React.useState("Ready.");

  const url = prUrl.trim();
  // The question travels as its own argument so quotes and spacing are preserved.
  const question = () => (cmd === "ask" ? askQ.trim() || "What does this PR change?" : null);

  const copyCli = async () => {
    if (!url) return toastErr("Review", "Enter a PR URL first.");
    try {
      const s = await tauriInvoke<string>("build_cli_command", { prUrl: url, command: cmd, question: question() });
      await navigator.clipboard.writeText(s);
      setOut("Copied: " + s);
      toastOk("Copied", s.slice(0, 120));
    } catch (e) {
      setOut("Clipboard failed: " + errText(e, 300));
      toastErr("Copy failed", errText(e, 160));
    }
  };

  const runLocal = async () => {
    if (!url) return toastErr("Review", "Enter a PR URL first.");
    if (!openrouter) return toastErr("Review", "Enter OPENROUTER_API_KEY first (session only).");
    if (!github) {
      setOut("GitHub token required. Connect GitHub in Settings → GitHub, then retry.");
      return toastErr("Review", "Connect GitHub first (Settings → GitHub).");
    }
    setOut("Running local review…");
    try {
      const r = await tauriInvoke<string>("run_local_review", {
        prUrl: url,
        command: cmd,
        question: question(),
        openrouterKey: openrouter,
        githubToken: github,
      });
      setOut(r || "(empty output)");
      toastOk("Review finished", String(r || "").slice(0, 140));
    } catch (e) {
      setOut("Run failed: " + errText(e, 2000));
      toastErr("Run failed", errText(e, 220));
    }
  };

  const openActions = () => {
    const m = url.match(/github\.com\/([^/]+\/[^/]+)/);
    void openUrl(m ? "https://github.com/" + m[1].replace(/\/pull\/.*$/, "") + "/actions" : "https://github.com");
  };

  return (
    <Page active={active}>
      <Section
        title="1 · Model (OpenRouter free)"
        description="Written to the repo's .pr_agent.toml and applied to every review the app runs."
        action={<Badge variant="outline">.pr_agent.toml</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={async () => setCfgMsg(await saveConfig())}>
              <Save /> Save config
            </AsyncButton>
            <AsyncButton variant="outline" onClick={async () => setCfgMsg(await loadConfig())}>
              <RotateCcw /> Reload
            </AsyncButton>
            {cfgMsg ? <span className="text-muted-foreground text-xs">{cfgMsg}</span> : null}
          </ButtonRow>
        }
      >
        <Field label="Model" htmlFor="model">
          <Select value={review.modelChoice} onValueChange={(v) => setReview({ modelChoice: v })}>
            <SelectTrigger id="model" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {KNOWN_MODELS.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        {review.modelChoice === "custom" ? (
          <Field label="Custom model id" htmlFor="custom-model">
            <Input
              id="custom-model"
              value={review.customModel}
              placeholder={`openrouter/org/model:free  (blank → ${DEFAULT_MODEL})`}
              onChange={(e) => setReview({ customModel: e.target.value })}
              autoComplete="off"
            />
          </Field>
        ) : null}
        <FieldGrid>
          <Field label="Fallback models (comma separated)" htmlFor="fallbacks">
            <Input
              id="fallbacks"
              value={review.fallbacks}
              onChange={(e) => setReview({ fallbacks: e.target.value })}
              autoComplete="off"
            />
          </Field>
          <Field label="Max tokens" htmlFor="max-tokens">
            <Input
              id="max-tokens"
              inputMode="numeric"
              value={review.maxTokens}
              onChange={(e) => setReview({ maxTokens: e.target.value })}
              autoComplete="off"
            />
          </Field>
        </FieldGrid>
        <Field label="Reasoning effort" htmlFor="effort">
          <Select value={review.effort} onValueChange={(v) => setReview({ effort: v })}>
            <SelectTrigger id="effort" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {EFFORTS.map((e) => (
                <SelectItem key={e.value} value={e.value}>
                  {e.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <div className="grid gap-3 sm:grid-cols-3">
          {(
            [
              ["autoDescribe", "auto_describe"],
              ["autoReview", "auto_review"],
              ["autoImprove", "auto_improve"],
            ] as const
          ).map(([key, label]) => (
            <Label key={key} className="bg-muted/30 flex items-center justify-between gap-3 rounded-md border px-3 py-2 font-mono text-xs">
              {label}
              <Switch checked={review[key]} onCheckedChange={(v) => setReview({ [key]: v })} />
            </Label>
          ))}
        </div>
        {review.path ? <Hint className="font-mono text-xs break-all">config: {review.path}</Hint> : null}
        {review.raw ? (
          <details className="group">
            <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
              Show raw .pr_agent.toml
            </summary>
            <Console className="mt-2" tall>
              {review.raw}
            </Console>
          </details>
        ) : null}
      </Section>

      <Section
        title="2 · Run a review"
        description={
          <>
            Needs GitHub sign-in (Settings) and the PR-Agent CLI (<code>Providers → Install CLI</code>). The app's{" "}
            <code>.pr_agent.toml</code> is applied to the target PR automatically.
          </>
        }
        action={<Badge variant="outline">local CLI</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={runLocal}>
              <Play /> Run locally
            </AsyncButton>
            <Button type="button" variant="outline" onClick={copyCli}>
              <Copy /> Copy CLI command
            </Button>
            <Button type="button" variant="ghost" onClick={() => url && openUrl(url)} disabled={!url}>
              <ExternalLink /> Open PR
            </Button>
            <Button type="button" variant="ghost" onClick={openActions}>
              <ExternalLink /> Open Actions
            </Button>
          </ButtonRow>
        }
      >
        <Field label="PR URL" htmlFor="pr-url">
          <Input
            id="pr-url"
            value={prUrl}
            onChange={(e) => setPrUrl(e.target.value)}
            placeholder="https://github.com/owner/repo/pull/123"
            autoComplete="off"
          />
        </Field>
        <FieldGrid>
          <Field label="Command" htmlFor="cmd">
            <Select value={cmd} onValueChange={(v) => setCmd(v as typeof cmd)}>
              <SelectTrigger id="cmd" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {COMMANDS.map((c) => (
                  <SelectItem key={c} value={c}>
                    {c}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="OPENROUTER_API_KEY" htmlFor="or-key" hint="Session only — never saved to disk.">
            <SecretInput
              id="or-key"
              value={openrouter}
              placeholder="sk-or-…"
              onChange={(e) => setSecret("openrouter", e.target.value)}
            />
          </Field>
        </FieldGrid>
        {cmd === "ask" ? (
          <Field label="Question (for /ask)" htmlFor="ask-q">
            <Input
              id="ask-q"
              value={askQ}
              onChange={(e) => setAskQ(e.target.value)}
              placeholder="What does this PR change?"
              autoComplete="off"
            />
          </Field>
        ) : null}
        <Console tall>{out}</Console>
      </Section>
    </Page>
  );
}
