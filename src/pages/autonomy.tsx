import * as React from "react";
import { ExternalLink, Play, RefreshCw, Save, Square, Zap, SkipForward, Rocket } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, KV, Page, Section } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { apiGet, apiMut, backendUrl } from "@/lib/api";
import { lsGet, lsSet, useApp } from "@/lib/store";
import { errText, openUrl, tauriInvoke, tauriUnavailable } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

type Level = "observe" | "assist" | "auto";

const LEVELS: Record<Level, string> = {
  observe: "Shadow mode. Decisions are recorded, but every PR still gets its normal review — nothing changes.",
  assist:
    "Acts alone only on cheap, confident calls (skip or fast review at/above the auto threshold). Sandbox runs and anything unsure wait in the inbox.",
  auto: "Runs everything at/above the floor, sandboxes included. Only safety flags and gray-zone PRs reach you.",
};
const REASONS: Record<string, string> = {
  SAFETY_FLAGGED: "Touches workflows / secrets / auth, or contains secret-like content.",
  LOW_CONFIDENCE_GRAY_ZONE: "The decider is unsure (or the diff is too big to judge) — needs your call.",
  RUNTIME_TEST_NEEDED: "Wants a runtime test in a warm Upstash Box.",
  UI_PREVIEW_NEEDED: "Wants a UI preview in a Vercel sandbox.",
  STATIC_DIFF_OK: "A diff review should be enough.",
  DOCS_ONLY: "Docs only — nothing to review.",
  LOCKFILE_BUMP: "Dependency lockfile change.",
};
const PREFS_KEY = "pragent.daemon";

interface Prefs {
  autostart: boolean;
  interval: number;
  sidecar: string;
}
function loadPrefs(): Prefs {
  const def: Prefs = { autostart: false, interval: 30, sidecar: "" };
  try {
    return { ...def, ...JSON.parse(lsGet(PREFS_KEY) || "{}") };
  } catch {
    return def;
  }
}
const savePrefs = (p: Prefs) => lsSet(PREFS_KEY, JSON.stringify(p));

function ago(iso?: string): string {
  const t = Date.parse(iso || "");
  if (!t) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 90) return s + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}
const secsAgo = (epoch?: number) => (epoch ? ago(new Date(epoch * 1000).toISOString()) : "never");
const prUrl = (rec: any) => `https://github.com/${rec.repo}/pull/${rec.pr_number}`;

function stateTone(state: string): "success" | "warning" | "danger" | "outline" {
  return ({ dispatched: "success", skipped: "success", escalated: "warning", failed: "danger", dismissed: "outline" } as const)[
    state as "dispatched"
  ] || "outline";
}

export function AutonomyPage({ active }: { active: boolean }) {
  const setEscCount = useApp((s) => s.setEscCount);
  const apiToken = useApp((s) => s.secrets.api);

  /* ---------- autonomy level ---------- */
  const [level, setLevel] = React.useState<Level>("assist");
  const [confAuto, setConfAuto] = React.useState(0.75);
  const [confFloor, setConfFloor] = React.useState(0.55);
  const [policy, setPolicy] = React.useState("policy …");

  const applyPolicy = (a: any) => {
    setLevel(a.level);
    setConfAuto(a.conf_auto);
    setConfFloor(a.conf_floor);
  };
  const loadAutonomy = async () => {
    try {
      const a = await apiGet("/autonomy");
      applyPolicy(a);
      setPolicy("policy " + (a.policy_version || "?"));
    } catch {
      setPolicy("backend unreachable");
    }
  };
  const saveAutonomy = async () => {
    try {
      const a = await apiMut("PUT", "/autonomy", { level, conf_auto: confAuto, conf_floor: confFloor });
      applyPolicy(a);
      toastOk("Autonomy saved", `${a.level} · auto ≥ ${Number(a.conf_auto).toFixed(2)} · floor ${Number(a.conf_floor).toFixed(2)}`);
    } catch (e) {
      toastErr("Could not save autonomy", errText(e, 200));
    }
  };

  /* ---------- daemon ---------- */
  const [prefs, setPrefs] = React.useState<Prefs>(loadPrefs);
  const [intervalText, setIntervalText] = React.useState(String(prefs.interval));
  const [local, setLocal] = React.useState<any>(null);
  const [cloud, setCloud] = React.useState<any>(null);
  const [daemonErr, setDaemonErr] = React.useState("");

  const refreshDaemon = React.useCallback(async () => {
    let l: any = null;
    let c: any = null;
    try {
      l = await tauriInvoke("daemon_status");
      setDaemonErr("");
    } catch (e) {
      if (!tauriUnavailable(e)) console.warn("daemon_status", e);
      setDaemonErr("The daemon runs in the desktop app (Tauri) only.");
    }
    try {
      c = await apiGet("/daemon/status");
    } catch {
      /* cloud status is optional */
    }
    if (l) {
      setLocal(l);
      setCloud(c);
    }
  }, []);

  const startDaemon = React.useCallback(
    async (silent: boolean, override?: Prefs) => {
      const p = override ?? loadPrefs();
      try {
        const s = await tauriInvoke("daemon_start", {
          backendUrl: backendUrl(),
          apiToken: useApp.getState().secrets.api || null,
          intervalSecs: p.interval,
          sidecarUrl: p.sidecar || null,
        });
        setLocal(s);
        setDaemonErr("");
        if (!silent) toastOk("Daemon started", `Polling every ${s.interval_secs} s (${s.model}).`);
        void refreshDaemon();
      } catch (e) {
        if (!silent || !tauriUnavailable(e)) toastErr("Daemon did not start", errText(e, 220));
      }
    },
    [refreshDaemon],
  );

  const onStartClick = () => {
    const interval = Math.min(60, Math.max(5, parseInt(intervalText, 10) || 30));
    const next = { ...prefs, interval, sidecar: prefs.sidecar.trim() };
    setIntervalText(String(interval));
    setPrefs(next);
    savePrefs(next);
    return startDaemon(false, next);
  };
  const stopDaemon = async () => {
    try {
      setLocal(await tauriInvoke("daemon_stop"));
      toastOk("Daemon stopped", "The cloud decider handles PRs now.");
      void refreshDaemon();
    } catch (e) {
      toastErr("Could not stop daemon", errText(e, 200));
    }
  };

  /* ---------- escalation inbox ---------- */
  const [escalations, setEscalations] = React.useState<any[]>([]);
  const [recent, setRecent] = React.useState<any[]>([]);
  const [stats, setStats] = React.useState<any>({});
  const [inboxErr, setInboxErr] = React.useState(false);
  const inboxSeq = React.useRef(0);

  const refreshInbox = React.useCallback(async () => {
    // Only the newest response may render: periodic and action-triggered refreshes can overlap.
    const seq = ++inboxSeq.current;
    try {
      // Query the waiting states directly so old unresolved items never fall off a "newest N" window.
      const [escalated, failed, rec, st] = await Promise.all([
        apiGet("/decisions?state=escalated&limit=200"),
        apiGet("/decisions?state=failed&limit=200"),
        apiGet("/decisions?limit=25"),
        apiGet("/decisions/stats"),
      ]);
      if (seq !== inboxSeq.current) return;
      const items = escalated.concat(failed);
      setEscalations(items);
      setEscCount(items.length);
      setRecent(rec);
      setStats(st);
      setInboxErr(false);
    } catch {
      if (seq !== inboxSeq.current) return;
      setInboxErr(true);
    }
  }, [setEscCount]);

  const resolveItem = async (rec: any, action: "fast_review" | "sandbox" | "skip") => {
    try {
      await apiMut("POST", "/decisions/" + encodeURIComponent(rec.id) + "/resolve", {
        action,
        resolved_by: "desktop-user",
      });
      toastOk(action === "skip" ? "Skipped" : "Dispatched", `${rec.repo}#${rec.pr_number}`);
    } catch (e) {
      toastErr("Could not resolve", errText(e, 200));
    }
    void refreshInbox();
  };

  const refreshAll = React.useCallback(() => {
    void loadAutonomy();
    void refreshDaemon();
    void refreshInbox();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshDaemon, refreshInbox]);

  // Boot: load everything once, autostart the daemon if the user asked for it.
  React.useEffect(() => {
    refreshAll();
    if (loadPrefs().autostart) void startDaemon(true);
    const inbox = setInterval(() => {
      if (!document.hidden) void refreshInbox();
    }, 20000);
    return () => clearInterval(inbox);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refresh whenever the tab is opened; poll the cheap local status while it is open.
  React.useEffect(() => {
    if (!active) return;
    refreshAll();
    const t = setInterval(() => {
      if (!document.hidden) void refreshDaemon();
    }, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  // A new API token means the previous inbox/daemon context is stale.
  React.useEffect(() => {
    if (apiToken) void refreshInbox();
  }, [apiToken, refreshInbox]);

  const running = !!(local && local.running);
  const logLines =
    local && local.recent
      ? [...local.recent].reverse().map((l: any) => new Date(l.at * 1000).toLocaleTimeString() + "  " + l.msg)
      : [];

  return (
    <Page active={active}>
      <Section
        title="Autonomy level"
        description="How much the calibrated decision layer may do without asking you. Stored in the backend, so the desktop daemon and the cloud fallback follow the same rules."
        action={<Badge variant="outline">{policy}</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={saveAutonomy}>
              <Save /> Save autonomy
            </AsyncButton>
            <AsyncButton variant="outline" onClick={loadAutonomy}>
              <RefreshCw /> Reload
            </AsyncButton>
          </ButtonRow>
        }
      >
        <ToggleGroup
          type="single"
          variant="outline"
          value={level}
          onValueChange={(v) => v && setLevel(v as Level)}
          className="w-full"
          aria-label="Autonomy level"
        >
          {(["observe", "assist", "auto"] as Level[]).map((l) => (
            <ToggleGroupItem key={l} value={l} className="flex-1 capitalize">
              {l}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <Hint>{LEVELS[level]}</Hint>
        {level === "assist" ? (
          <Field
            label={
              <>
                Act alone at confidence ≥ <b className="tabular-nums">{confAuto.toFixed(2)}</b>
              </>
            }
          >
            <Slider
              aria-label="Act alone at confidence"
              min={0.5}
              max={1}
              step={0.01}
              value={[confAuto]}
              onValueChange={([v]) => {
                setConfAuto(v);
                if (confFloor > v) setConfFloor(v);
              }}
            />
          </Field>
        ) : null}
        <Field
          label={
            <>
              Gray-zone floor — below <b className="tabular-nums">{confFloor.toFixed(2)}</b> a PR is escalated to you
            </>
          }
          hint={
            <>
              Safety-flagged PRs (workflows, Dockerfiles, secrets, auth, <code>curl | sh</code>) always escalate, at every
              level.
            </>
          }
        >
          <Slider
            aria-label="Gray-zone floor"
            min={0.3}
            max={0.95}
            step={0.01}
            value={[confFloor]}
            onValueChange={([v]) => setConfFloor(Math.min(v, confAuto))}
          />
        </Field>
      </Section>

      <Section
        title="Escalation inbox"
        description="PRs the decider will not decide alone. “Fast review” posts a zero-CPU diff review; “Run as configured” uses the agent's own execution mode."
        action={
          <Badge variant={inboxErr ? "danger" : escalations.length ? "danger" : "success"}>
            {inboxErr ? "backend unreachable" : `${escalations.length} waiting`}
          </Badge>
        }
        footer={
          <AsyncButton variant="outline" size="sm" onClick={refreshInbox}>
            <RefreshCw /> Refresh
          </AsyncButton>
        }
      >
        {escalations.length === 0 ? (
          <Hint className="py-2">{inboxErr ? "Could not reach the backend." : "Nothing needs you right now."}</Hint>
        ) : (
          <div className="grid gap-3">
            {escalations.map((rec) => {
              const d = rec.decision;
              const failed = rec.state === "failed";
              return (
                <div key={rec.id} className="bg-muted/20 grid gap-2.5 rounded-lg border p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="text-sm">
                      {rec.repo} #{rec.pr_number}
                    </strong>
                    <span className="text-muted-foreground ml-auto text-xs">
                      {(rec.head_sha || "").slice(0, 7)} · {rec.files} files · {rec.churn} lines · {ago(rec.created_at)}
                    </span>
                  </div>
                  {rec.title ? <div className="text-sm">{rec.title}</div> : null}
                  <div className="flex flex-wrap gap-1.5">
                    {d ? (
                      <>
                        <Badge variant={d.decision === "ESCALATE_HUMAN" ? "warning" : "outline"}>{d.decision}</Badge>
                        <Badge variant="outline">{d.reason_code}</Badge>
                        <Badge variant="outline">provider: {d.provider}</Badge>
                        <Badge variant="outline">
                          {rec.decided_by || "?"} · {d.latency_ms || 0} ms
                        </Badge>
                      </>
                    ) : (
                      <Badge variant="danger">{rec.state}</Badge>
                    )}
                  </div>
                  {d ? (
                    <div className="grid gap-1">
                      <Progress value={Math.round(Math.max(0, Math.min(1, d.confidence)) * 100)} className="h-1.5" />
                      <span className="text-muted-foreground text-xs">
                        confidence {Number(d.confidence).toFixed(2)} — {REASONS[d.reason_code] || ""}
                      </span>
                    </div>
                  ) : null}
                  {rec.error ? <p className="text-destructive text-xs break-words">{String(rec.error)}</p> : null}
                  <ButtonRow>
                    <AsyncButton size="sm" onClick={() => resolveItem(rec, "fast_review")}>
                      <Zap /> {failed ? "Retry: fast review" : "Fast review"}
                    </AsyncButton>
                    <AsyncButton size="sm" variant="outline" onClick={() => resolveItem(rec, "sandbox")}>
                      <Rocket /> {failed ? "Retry: as configured" : "Run as configured"}
                    </AsyncButton>
                    {!failed ? (
                      <AsyncButton size="sm" variant="ghost" onClick={() => resolveItem(rec, "skip")}>
                        <SkipForward /> Skip
                      </AsyncButton>
                    ) : null}
                    <Button type="button" size="sm" variant="ghost" onClick={() => openUrl(prUrl(rec))}>
                      <ExternalLink /> Open PR
                    </Button>
                  </ButtonRow>
                </div>
              );
            })}
          </div>
        )}
      </Section>

      <Section
        title="Always-on daemon"
        description="While the app is open (closing the window keeps it in the tray) the daemon decides new PRs locally and keeps the cloud idle. If it stops or the machine sleeps, the cloud fallback takes over automatically — and the same PR is never reviewed twice."
        action={<Badge variant={running ? "success" : "outline"}>{running ? "running" : "stopped"}</Badge>}
        footer={
          <ButtonRow className="w-full">
            {running ? (
              <AsyncButton variant="destructive" onClick={stopDaemon}>
                <Square /> Stop daemon
              </AsyncButton>
            ) : (
              <AsyncButton onClick={onStartClick}>
                <Play /> Start daemon
              </AsyncButton>
            )}
            <AsyncButton variant="outline" onClick={refreshDaemon}>
              <RefreshCw /> Refresh
            </AsyncButton>
          </ButtonRow>
        }
      >
        <FieldGrid>
          <Field label="Poll interval (5–60 s)" htmlFor="daemon-interval">
            <Input
              id="daemon-interval"
              inputMode="numeric"
              value={intervalText}
              onChange={(e) => setIntervalText(e.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Local model sidecar (optional, loopback only)" htmlFor="daemon-sidecar">
            <Input
              id="daemon-sidecar"
              placeholder="http://127.0.0.1:8080"
              value={prefs.sidecar}
              onChange={(e) => setPrefs({ ...prefs, sidecar: e.target.value })}
              autoComplete="off"
            />
          </Field>
        </FieldGrid>
        <Label className="flex items-center gap-3 font-normal">
          <Switch
            checked={prefs.autostart}
            onCheckedChange={(v) => {
              const next = { ...prefs, autostart: v };
              setPrefs(next);
              savePrefs({ ...loadPrefs(), autostart: v });
            }}
          />
          Start automatically when the app opens
        </Label>
        {local ? (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <KV label="Polls" value={String(local.polls)} />
            <KV label="Decided" value={String(local.decided)} />
            <KV label="Settled elsewhere" value={String(local.lost_races)} />
            <KV label="Errors" value={String(local.failures)} />
            <KV label="Last OK" value={secsAgo(local.last_ok_at)} />
            <KV label="Model" value={local.model || "rules"} />
          </div>
        ) : null}
        {local && cloud ? (
          <Hint className={cloud.desktop_online ? "" : "text-warning"}>
            {cloud.desktop_online
              ? `Backend sees this desktop as online — new PRs are handed to it (cloud fallback after ${cloud.defer_grace_s} s).`
              : "Backend does not see the desktop — the cloud decider handles PRs immediately (fallback active)."}
          </Hint>
        ) : null}
        {daemonErr ? <Hint className="text-warning">{daemonErr}</Hint> : null}
        {local && local.last_error ? <Hint className="text-destructive">Last error: {local.last_error}</Hint> : null}
        <Console tall>{logLines.length ? logLines.join("\n") : "No daemon activity yet."}</Console>
      </Section>

      <Section
        title="Recent decisions"
        description="Your resolutions feed back into calibration."
        action={<Badge variant="outline">calibration feedback</Badge>}
      >
        <div className="flex flex-wrap gap-2">
          {Object.entries(stats.feedback || {}).map(([kind, c]: [string, any]) => {
            if (kind === "ESCALATE_HUMAN") {
              const total = c.chose_fast_review + c.chose_sandbox + c.chose_skip;
              return (
                <Badge key={kind} variant="outline">
                  escalations:{" "}
                  {total
                    ? `${c.chose_fast_review} fast review · ${c.chose_sandbox} as configured · ${c.chose_skip} skipped`
                    : "none resolved yet"}
                </Badge>
              );
            }
            const total = c.agree + c.override;
            return (
              <Badge key={kind} variant="outline">
                {kind}: {total ? `${c.agree} agreed / ${c.override} overridden` : "no feedback yet"}
              </Badge>
            );
          })}
        </div>
        {recent.length === 0 ? (
          <Hint>No decisions yet.</Hint>
        ) : (
          <div className="divide-y rounded-md border">
            {recent.slice(0, 25).map((rec) => (
              <div key={rec.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-xs">
                <span className="font-medium">
                  {rec.repo} #{rec.pr_number}
                </span>
                <span className="text-muted-foreground font-mono">{(rec.head_sha || "").slice(0, 7)}</span>
                <Badge variant={stateTone(rec.state)}>{rec.state}</Badge>
                <span className="text-muted-foreground min-w-0 flex-1 truncate">
                  {rec.decision
                    ? `${rec.decision.decision} · ${rec.decision.reason_code} · ${Number(rec.decision.confidence).toFixed(2)}`
                    : "—"}
                </span>
                <span className="text-muted-foreground">{ago(rec.created_at)}</span>
              </div>
            ))}
          </div>
        )}
      </Section>
    </Page>
  );
}
