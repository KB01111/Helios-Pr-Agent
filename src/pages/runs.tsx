import * as React from "react";
import { Check, ExternalLink, FileDiff, Radio, RefreshCw } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Hint, Page, Section } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { apiMut } from "@/lib/api";
import { syncRuns, toggleStream } from "@/lib/actions";
import { useApp, type Run } from "@/lib/store";
import { errText, openUrl } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

type Tone = "success" | "danger" | "warning" | "outline";
function statusTone(s: string): Tone {
  const v = String(s || "").toLowerCase();
  if (/approv|merged|done|success|completed/.test(v)) return "success";
  if (/fail|error/.test(v)) return "danger";
  if (/wait|pend|queued|running/.test(v)) return "warning";
  return "outline";
}
const runId = (r: Run) => String(r.run_id || r.id || "");

export function RunsPage({ active }: { active: boolean }) {
  const runs = useApp((s) => s.runs);
  const msg = useApp((s) => s.runsMsg);
  const streaming = useApp((s) => s.streaming);
  const [diffFor, setDiffFor] = React.useState<Run | null>(null);

  const approve = async (id: string) => {
    try {
      await apiMut("POST", "/runs/" + encodeURIComponent(id) + "/approve", {});
      toastOk("Run approved", id);
      await syncRuns();
    } catch (e) {
      toastErr("Approve failed", errText(e, 200));
    }
  };

  const latestWithDiff = runs.find((r) => r.diff) || runs[0];

  return (
    <Page active={active}>
      <Section
        title="Background runs"
        description="Pulls runs recorded while this PC was offline. Approve buttons only appear for runs awaiting approval."
        action={<Badge variant="outline">{runs.length} run(s)</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={() => syncRuns()}>
              <RefreshCw /> Sync runs
            </AsyncButton>
            <Button type="button" variant={streaming ? "default" : "outline"} onClick={() => toggleStream()}>
              <Radio className={streaming ? "animate-pulse" : ""} /> Live: {streaming ? "on" : "off"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={!latestWithDiff}
              onClick={() => latestWithDiff && setDiffFor(latestWithDiff)}
            >
              <FileDiff /> Open latest diff
            </Button>
            <span className="text-muted-foreground text-xs">{msg}</span>
          </ButtonRow>
        }
      />

      {runs.length === 0 ? (
        <Hint className="py-6 text-center">No runs yet.</Hint>
      ) : (
        <div className="grid gap-3">
          {runs.map((r, i) => {
            const id = runId(r);
            const status = String(r.status || "unknown");
            const repo = String(r.repo || r.target_repo || "?");
            const pr = String(r.pr_number || r.pr || "?");
            const provider = String(r.provider || r.runtime_mode || r.mode || "?");
            const cpu = String(r.cpu_hours ?? r.cpu_seconds ?? "?");
            const logs = (Array.isArray(r.logs) ? r.logs.join("\n") : String(r.logs || r.log || r.summary || "")).slice(0, 2000);
            const diff = String(r.diff || "");
            const canOpenPr = repo !== "?" && pr !== "?" && /^[^/\s]+\/[^/\s]+$/.test(repo) && /^\d+$/.test(pr);
            return (
              <div key={id || i} className="bg-card grid gap-3 rounded-lg border p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <strong className="text-sm">
                    {repo} #{pr}
                  </strong>
                  <Badge variant={statusTone(status)}>{status}</Badge>
                  <span className="text-muted-foreground ml-auto text-xs">
                    {provider} · {cpu} cpu
                  </span>
                </div>
                {logs ? <Console>{logs}</Console> : null}
                {diff ? (
                  <details>
                    <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
                      diff ({diff.length} chars)
                    </summary>
                    <Console className="mt-2" tall>
                      {diff.slice(0, 6000)}
                    </Console>
                  </details>
                ) : null}
                {canOpenPr || diff || (status === "queued" && id) ? (
                  <ButtonRow>
                    {canOpenPr ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => openUrl(`https://github.com/${repo}/pull/${pr}`)}
                      >
                        <ExternalLink /> Open PR
                      </Button>
                    ) : null}
                    {diff ? (
                      <Button type="button" variant="ghost" size="sm" onClick={() => setDiffFor(r)}>
                        <FileDiff /> View diff
                      </Button>
                    ) : null}
                    {status === "queued" && id ? (
                      <AsyncButton variant="outline" size="sm" onClick={() => approve(id)}>
                        <Check /> Approve
                      </AsyncButton>
                    ) : null}
                  </ButtonRow>
                ) : null}
              </div>
            );
          })}
        </div>
      )}

      <Dialog open={!!diffFor} onOpenChange={(o) => !o && setDiffFor(null)}>
        <DialogContent className="max-h-[85vh] sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>
              {diffFor ? `${diffFor.repo || "?"} #${diffFor.pr_number || "?"} — diff` : "Diff"}
            </DialogTitle>
            <DialogDescription>First 20,000 characters of the run's diff.</DialogDescription>
          </DialogHeader>
          <Console className="max-h-[60vh]">{String(diffFor?.diff || "(no diff)").slice(0, 20000)}</Console>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
