import * as React from "react";
import { ClipboardCopy, Download, FilePlus2, Stethoscope } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Page, Section } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { errText, tauriInvoke } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

type Provider = "pr-agent" | "openrabbit";

export function ProvidersPage({ active }: { active: boolean }) {
  const [paOut, setPaOut] = React.useState("Needs repo secret OPENROUTER_API_KEY.");
  const [orOut, setOrOut] = React.useState("Model pinned: thinkingmachines/inkling-2-small:free (review_mode: both).");

  const check = async () => {
    setPaOut("Checking…");
    const parts: string[] = [];
    for (const t of ["pr-agent", "python", "gh"]) {
      try {
        parts.push(`${t}: ${await tauriInvoke("check_tool", { name: t })}`);
      } catch (e) {
        parts.push(`${t}: MISSING (${errText(e, 200)})`);
      }
    }
    setPaOut(parts.join("\n"));
  };

  const install = async () => {
    setPaOut("Creating a dedicated venv and installing pr-agent[github] — takes a few minutes…");
    try {
      setPaOut(await tauriInvoke<string>("install_pr_agent"));
      toastOk("Install finished", "pr-agent CLI installed.");
    } catch (e) {
      setPaOut("Install failed: " + errText(e, 2000));
      toastErr("Install failed", errText(e, 200));
    }
  };

  const write = (provider: Provider, setOut: (s: string) => void, file: string, secret: string) => async () => {
    try {
      const p = await tauriInvoke<string>("write_workflow", { provider });
      setOut(`Wrote workflow to ${p}\nCommit + push, add secret ${secret}.`);
      toastOk("Workflow written", file);
    } catch (e) {
      setOut("Write failed: " + errText(e, 500));
      toastErr("Write failed", errText(e, 160));
    }
  };

  const copy = (provider: Provider, setOut: (s: string) => void, label: string) => async () => {
    try {
      const y = await tauriInvoke<string>("get_workflow_template", { provider });
      await navigator.clipboard.writeText(y);
      setOut("Workflow YAML copied.");
      toastOk("Copied", `${label} workflow YAML.`);
    } catch (e) {
      setOut("Copy failed: " + errText(e, 500));
      toastErr("Copy failed", errText(e, 160));
    }
  };

  return (
    <Page active={active}>
      <Section
        title="Provider: PR-Agent"
        description="Full review quality. The Action workflow below runs on every PR event — no PC needed. The local CLI installs into a dedicated venv so backend Python packages are not touched."
        action={<Badge variant="outline">always online via Actions</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton variant="outline" onClick={check}>
              <Stethoscope /> Check pr-agent CLI
            </AsyncButton>
            <AsyncButton variant="secondary" onClick={install}>
              <Download /> Install CLI (pip)
            </AsyncButton>
            <AsyncButton variant="secondary" onClick={write("pr-agent", setPaOut, "pr-agent.yml", "OPENROUTER_API_KEY")}>
              <FilePlus2 /> Write workflow to this repo
            </AsyncButton>
            <AsyncButton variant="ghost" onClick={copy("pr-agent", setPaOut, "pr-agent")}>
              <ClipboardCopy /> Copy workflow YAML
            </AsyncButton>
          </ButtonRow>
        }
      >
        <Console tall>{paOut}</Console>
      </Section>

      <Section
        title="Provider: OpenRabbit"
        description={
          <>
            Simplest zero-host bot: fully automatic on PR open/sync. Needs repo secret <code>LLM_API_KEY</code> (= OpenRouter
            key).
          </>
        }
        action={<Badge variant="outline">zero-host bot</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton variant="secondary" onClick={write("openrabbit", setOrOut, "openrabbit.yml", "LLM_API_KEY")}>
              <FilePlus2 /> Write workflow to this repo
            </AsyncButton>
            <AsyncButton variant="ghost" onClick={copy("openrabbit", setOrOut, "openrabbit")}>
              <ClipboardCopy /> Copy workflow YAML
            </AsyncButton>
          </ButtonRow>
        }
      >
        <Console>{orOut}</Console>
      </Section>
    </Page>
  );
}
