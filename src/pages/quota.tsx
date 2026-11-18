import * as React from "react";
import { Activity, RefreshCw, Rocket } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, SecretInput, Section } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { fetchQuota } from "@/lib/actions";
import { DEFAULT_MODEL, selectedModel, useApp, type QuotaSlot } from "@/lib/store";
import { errText, tauriInvoke } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

function QuotaCard({ title, slot }: { title: string; slot?: QuotaSlot }) {
  const over = !!slot && slot.used >= slot.limit;
  const pct = slot && slot.limit > 0 ? Math.min(100, (slot.used / slot.limit) * 100) : 0;
  return (
    <div className="bg-muted/20 grid gap-2.5 rounded-lg border p-4">
      <Badge variant="outline" className="w-fit">
        {title}
      </Badge>
      <div className="text-2xl font-semibold tabular-nums">
        {slot ? `${slot.used.toLocaleString()} / ${slot.limit.toLocaleString()}` : "—"}
      </div>
      <Progress value={pct} className={over ? "[&_[data-slot=progress-indicator]]:bg-destructive" : ""} />
      <p className="text-muted-foreground text-xs">{slot ? `remaining ${slot.remaining.toLocaleString()} vCPU-s` : "—"}</p>
    </div>
  );
}

export function QuotaPage({ active }: { active: boolean }) {
  const quota = useApp((s) => s.quota);
  const secrets = useApp((s) => s.secrets);
  const setSecret = useApp((s) => s.setSecret);
  const picked = useApp((s) => s.pickedRepos);
  useApp((s) => s.review); // re-render when the model selection changes

  const [boxName, setBoxName] = React.useState("pr-agent-runner");
  const [cron, setCron] = React.useState("*/15 * * * *");
  const [repos, setRepos] = React.useState("");
  const [out, setOut] = React.useState("Box idle.");

  // Mirror the agent repo picker into an empty "repos to watch" field.
  React.useEffect(() => {
    setRepos((cur) => (!cur.trim() && picked.length ? picked.join(", ") : cur));
  }, [picked]);

  const args = (statusOnly: boolean) => {
    const fail = (m: string, t = m) => {
      setOut(m);
      toastErr("Box", t);
      return null;
    };
    if (!secrets.box) return fail("Enter UPSTASH_BOX_API_KEY first.");
    const repoList = repos.trim() || picked.join(", ");
    if (!statusOnly) {
      if (!secrets.github) return fail("Enter GITHUB_TOKEN first (Settings → Connect).", "Connect GitHub first.");
      if (!secrets.openrouter) return fail("Enter OPENROUTER_API_KEY first.");
      if (!repoList) return fail("Enter at least one repo.");
    }
    let model = DEFAULT_MODEL;
    try {
      model = selectedModel();
    } catch {
      /* keep default */
    }
    return {
      boxKey: secrets.box,
      githubToken: secrets.github,
      openrouterKey: secrets.openrouter,
      repos: repoList,
      boxName: boxName.trim() || "pr-agent-runner",
      cron: cron.trim() || "*/15 * * * *",
      model,
      statusOnly,
    };
  };

  const provision = async () => {
    const a = args(false);
    if (!a) return;
    setOut("Provisioning box + schedule…");
    try {
      setOut(await tauriInvoke<string>("box_provision", a));
      toastOk("Box provisioned", "Schedule started.");
    } catch (e) {
      setOut("Provision failed: " + errText(e, 2000));
      toastErr("Provision failed", errText(e, 220));
    }
  };
  const status = async () => {
    const a = args(true);
    if (!a) return;
    setOut("Checking box…");
    try {
      setOut(await tauriInvoke<string>("box_provision", a));
    } catch (e) {
      setOut("Status failed: " + errText(e, 2000));
      toastErr("Box status failed", errText(e, 200));
    }
  };

  const summary = quota
    ? `Updated ${new Date(quota.updatedAt).toLocaleTimeString()} · Box ${quota.box.used}/${quota.box.limit} · Vercel ${quota.vercel.used}/${quota.vercel.limit}`
    : "Quota idle — 18 000 vCPU-seconds per provider per month.";

  return (
    <Page active={active}>
      <Section
        title="Quota dashboard"
        description={summary}
        action={
          <AsyncButton variant="outline" size="sm" onClick={fetchQuota}>
            <RefreshCw /> Refresh quota
          </AsyncButton>
        }
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <QuotaCard title="Upstash Box · interactive" slot={quota?.box} />
          <QuotaCard title="Vercel Sandbox · ephemeral" slot={quota?.vercel} />
        </div>
      </Section>

      <Section
        title="Auto-run on Upstash Box"
        description={
          <>
            Provisions a <code>python</code> box, uploads the review loop, sets a cron schedule. Keys stay in memory — never
            written to disk.
          </>
        }
        action={<Badge variant="outline">cron</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={provision}>
              <Rocket /> Provision + start schedule
            </AsyncButton>
            <AsyncButton variant="outline" onClick={status}>
              <Activity /> Status
            </AsyncButton>
          </ButtonRow>
        }
      >
        <FieldGrid>
          <Field label="UPSTASH_BOX_API_KEY" htmlFor="box-key" hint="box_… — session only">
            <SecretInput
              id="box-key"
              value={secrets.box}
              placeholder="box_…"
              onChange={(e) => setSecret("box", e.target.value)}
            />
          </Field>
          <Field label="GITHUB_TOKEN" htmlFor="gh-key" hint="Mirrored from Settings → GitHub.">
            <SecretInput
              id="gh-key"
              value={secrets.github}
              placeholder="ghp_… — connect in Settings"
              onChange={(e) => setSecret("github", e.target.value)}
            />
          </Field>
        </FieldGrid>
        <FieldGrid>
          <Field label="OPENROUTER_API_KEY" htmlFor="box-or-key" hint="Shared with the Review tab — session only.">
            <SecretInput
              id="box-or-key"
              value={secrets.openrouter}
              placeholder="sk-or-…"
              onChange={(e) => setSecret("openrouter", e.target.value)}
            />
          </Field>
          <Field label="Box name" htmlFor="box-name">
            <Input id="box-name" value={boxName} onChange={(e) => setBoxName(e.target.value)} autoComplete="off" />
          </Field>
        </FieldGrid>
        <FieldGrid>
          <Field label="Schedule (UTC cron)" htmlFor="box-cron">
            <Input
              id="box-cron"
              className="font-mono"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Repos to watch (comma separated owner/repo)" htmlFor="box-repos">
            <Input
              id="box-repos"
              value={repos}
              onChange={(e) => setRepos(e.target.value)}
              placeholder="owner/repo, owner/other"
              autoComplete="off"
            />
          </Field>
        </FieldGrid>
        <Console tall>{out}</Console>
        <Hint>Model used: <code>{selectedModel()}</code></Hint>
      </Section>
    </Page>
  );
}
